"""Ideator agent for source-bound novelty angle cards."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, StrictStr, ValidationError, validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._language import language_directive
from autoessay.agents.detailed_outline import (
    build_detailed_outlines,
    outlines_to_dict,
    render_outlines_markdown,
)
from autoessay.agents.phase_context import phase_context_prompt_block
from autoessay.agents.proposal import load_proposal_payload
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.domain_loader import load_domain
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    SchemaViolationError,
    hash_text,
    run_llm_step,
)
from autoessay.memory import MemoryClient, make_memory_pre_llm_hook
from autoessay.models import Project, Run
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

MIN_ANGLE_CARDS = 4
MAX_ANGLE_CARDS = 6
SOURCE_NOTES_CHAR_LIMIT = 12000
CLAIMS_LIMIT = 80


class AngleCardOutput(BaseModel):
    angle_id: StrictStr
    working_title: StrictStr
    thesis_one_sentence: StrictStr
    key_claim_ids: list[StrictStr]
    why_novel: StrictStr
    evidence_so_far: StrictStr
    missing_evidence: StrictStr
    journal_fit_note: StrictStr
    risks: list[StrictStr]
    # PR-C2.b Tier 4 (2026-05-03): optional structured cross-references
    # to upstream framework_lens phase output and methodology choice.
    # Both default to empty so legacy LLM output without these keys
    # remains valid.
    framework_lens: list[StrictStr] = []
    methodological_choice: StrictStr = ""

    @validator(
        "angle_id",
        "working_title",
        "thesis_one_sentence",
        "why_novel",
        "evidence_so_far",
        "missing_evidence",
        "journal_fit_note",
    )
    def _text_must_have_content(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("field must be non-empty")
        return cleaned

    @validator("methodological_choice")
    def _methodological_choice_normalized(cls, value: str) -> str:
        # Optional field — empty is fine; otherwise collapse internal
        # whitespace like the other text fields.
        return " ".join(value.split())

    @validator("key_claim_ids", "risks", "framework_lens")
    def _string_lists_must_be_clean(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value)

    class Config:
        extra = "ignore"


class IdeatorOutput(BaseModel):
    angle_cards: list[AngleCardOutput]

    @validator("angle_cards")
    def _angle_cards_must_match_count(cls, value: list[AngleCardOutput]) -> list[AngleCardOutput]:
        if not (MIN_ANGLE_CARDS <= len(value) <= MAX_ANGLE_CARDS):
            raise ValueError(
                f"angle_cards must contain {MIN_ANGLE_CARDS}-{MAX_ANGLE_CARDS} cards",
            )
        return value

    class Config:
        extra = "ignore"


AngleCardModel = AngleCardOutput
AngleCardsPayload = IdeatorOutput


def run_ideator(
    run_id: str,
    db_session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run the ideator.

    ``prompt_overrides`` is the resolved override map from the rerun
    endpoint (codex-AGREEd #2 stage 2.B). Stage 2.B uses
    ``prompt_overrides["main"]`` as the static instruction block.

    ``lock_token`` (Stage 3.E follow-up P0): owner-checked phase-start
    lock release at exit.

    PR-A4.1b (2026-05-02): wraps in ``maybe_run_with_versioning``.
    """
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_ideator_with_session(
                run_id,
                session,
                hooks,
                prompt_overrides=prompt_overrides,
            )

        maybe_run_with_versioning(session, run, "ideator", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "ideator", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def load_novelty_payload(run: Run) -> dict[str, object]:
    novelty_dir = Path(run.run_dir) / "novelty"
    selected_thesis = _load_json_mapping(novelty_dir / "selected_thesis.json")
    detailed_outlines = _load_json_mapping(novelty_dir / "detailed_outlines.json")
    raw_outlines = detailed_outlines.get("outlines")
    outlines_list = raw_outlines if isinstance(raw_outlines, list) else []
    return {
        "run_id": run.id,
        "angle_cards": _load_json_array(novelty_dir / "angle_cards.json"),
        "ideator_report": _read_optional_text(novelty_dir / "ideator_report.md"),
        "selected_thesis": selected_thesis if selected_thesis else None,
        "detailed_outlines": outlines_list,
        "detailed_outlines_md": _read_optional_text(novelty_dir / "detailed_outlines.md"),
    }


def select_thesis_for_run(
    run: Run,
    selected_angle_id: str,
    edits: Mapping[str, object] | None = None,
) -> dict[str, object]:
    cards = _load_angle_cards(Path(run.run_dir) / "novelty" / "angle_cards.json")
    by_id = {str(card.get("angle_id") or ""): card for card in cards}
    selected = by_id.get(selected_angle_id)
    if selected is None:
        raise ValueError(f"angle card not found: {selected_angle_id}")
    selected_thesis = _apply_edits(selected, edits or {})
    selected_thesis["selected_angle_id"] = selected_angle_id
    _write_json(Path(run.run_dir) / "novelty" / "selected_thesis.json", selected_thesis)
    return selected_thesis


def _run_ideator_with_session(
    run_id: str,
    session: Session,
    hooks: HookRegistry | None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    assert_run_active(run, session)
    # PR-C2.b: ideator accepts either USER_FIELD_REVIEW (lens-skipped
    # common path) or USER_LENS_REVIEW (post-lens path) as input.
    from autoessay.phase_rerun import IDEATOR_VALID_INPUT_STATES

    if run.state not in IDEATOR_VALID_INPUT_STATES:
        raise InvalidTransition(
            f"Ideator requires one of {sorted(IDEATOR_VALID_INPUT_STATES)}, got {run.state}",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    transition(run, "IDEATOR_RUNNING", session, reason="Ideator started")
    append_event(session, run, "phase_started", {"phase": "ideator", "run_id": run.id})
    session.commit()
    session.refresh(run)

    run_dir = Path(run.run_dir)
    synthesis_dir = run_dir / "synthesis"
    novelty_dir = run_dir / "novelty"
    novelty_dir.mkdir(parents=True, exist_ok=True)
    claims = _load_jsonl_objects(synthesis_dir / "claims.jsonl")
    source_notes = _load_source_notes(synthesis_dir / "source_notes")
    domain = load_domain(_domain_path(project.domain_id))
    proposal = _proposal_context(run)

    settings = get_settings()
    instructions_override = prompt_overrides.get("main") if prompt_overrides else None
    angle_cards: list[dict[str, object]] | None
    if settings.ideator_stub:
        angle_cards = _stub_angle_cards(
            project.title,
            project.target_journal,
            claims,
            source_notes,
            proposal,
        )
    else:
        try:
            angle_cards = _ideator_via_harness(
                run=run,
                project=project,
                session=session,
                domain_data=domain.data,
                claims=claims,
                source_notes=source_notes,
                proposal=proposal,
                hooks=hooks or HookRegistry(),
                request_id="ideator_angle_cards",
                instructions_override=instructions_override,
            )
        except SchemaViolationError:
            angle_cards = None
        except Exception:  # noqa: BLE001 - phase records fixable failure.
            angle_cards = None
    if angle_cards is None:
        guidance = "Ideator could not parse strict angle-card JSON after one retry."
        payload: dict[str, object] = {"angle_cards": []}
        _write_json(novelty_dir / "angle_cards_v001.json", payload)
        _write_json(novelty_dir / "angle_cards.json", payload)
        _write_report(
            novelty_dir / "ideator_report.md",
            claims=claims,
            source_notes=source_notes,
            angle_cards=[],
            guidance=guidance,
        )
        return _fail_fixable(run, session, guidance)

    # PR-C2.b Tier 4 (referential integrity): if a framework_lens
    # artifact exists, filter angle.framework_lens to only valid
    # lens names. LLMs occasionally invent lens names; this is the
    # last-line guard. Empty valid set (legacy / no lens run) is a
    # no-op — see _filter_angle_lens_references.
    from autoessay.framework_lens import lens_names_from_payload, read_framework_lens

    lens_artifact = read_framework_lens(run_dir)
    valid_lens_names = (
        lens_names_from_payload(lens_artifact) if lens_artifact is not None else set()
    )
    angle_cards, dropped_lens_refs = _filter_angle_lens_references(
        angle_cards,
        valid_lens_names,
    )
    angle_cards = _order_angle_cards_for_kernel(
        angle_cards,
        getattr(run, "research_kernel_json", None),
    )
    if dropped_lens_refs:
        append_event(
            session,
            run,
            "ideator_lens_refs_dropped",
            {
                "phase": "ideator",
                "dropped": dropped_lens_refs,
                "valid_count": len(valid_lens_names),
            },
        )

    payload = {"angle_cards": angle_cards}
    _write_json(novelty_dir / "angle_cards_v001.json", payload)
    _write_json(novelty_dir / "angle_cards.json", payload)
    _write_report(
        novelty_dir / "ideator_report.md",
        claims=claims,
        source_notes=source_notes,
        angle_cards=angle_cards,
        guidance=None,
    )

    project_language = getattr(project, "language", None) or "en"
    detailed_outlines = build_detailed_outlines(
        run=run,
        session=session,
        angle_cards=angle_cards,
        claims=claims,
        source_notes=source_notes,
        project_title=project.title,
        project_language=project_language,
    )
    if detailed_outlines:
        _write_json(
            novelty_dir / "detailed_outlines.json",
            outlines_to_dict(detailed_outlines),
        )
        _write_text(
            novelty_dir / "detailed_outlines.md",
            render_outlines_markdown(detailed_outlines, project_language),
        )

    summary = {
        "phase": "ideator",
        "angle_cards": len(angle_cards),
        "claims_read": len(claims),
        "source_notes": len(source_notes),
        "detailed_outlines": len(detailed_outlines),
    }
    transition(run, "USER_NOVELTY_REVIEW", session, reason="Ideator completed", payload=summary)
    append_event(session, run, "phase_done", summary)
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


def _fail_fixable(run: Run, session: Session, guidance: str) -> dict[str, object]:
    transition(
        run,
        "FAILED_FIXABLE",
        session,
        reason="Ideator needs prompt-fixable output",
        payload={"guidance": guidance},
    )
    append_event(
        session,
        run,
        "phase_failed",
        {
            "phase": "ideator",
            "failure_class": "failed_fixable",
            "guidance": guidance,
        },
    )
    session.commit()
    return {"run_id": run.id, "state": run.state, "guidance": guidance}


def _ideator_via_harness(
    *,
    run: Run,
    project: Project,
    session: Session,
    domain_data: Mapping[str, Any],
    claims: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, object],
    proposal: Mapping[str, object] | None,
    hooks: HookRegistry,
    request_id: str,
    discussion_history: Sequence[Mapping[str, object]] = (),
    current_angle_cards: Sequence[Mapping[str, object]] = (),
    instructions_override: str | None = None,
) -> list[dict[str, object]] | None:
    from autoessay.agents._research_kernel_prompt import (
        KERNEL_INJECTION_GUARD,
        research_kernel_for_prompt,
    )

    research_kernel = research_kernel_for_prompt(
        getattr(run, "research_kernel_json", None),
    )
    accumulated_context = phase_context_prompt_block(run.run_dir, "ideator")
    prompt = _angle_prompt(
        project_title=project.title,
        target_journal=project.target_journal,
        domain_data=domain_data,
        claims=claims,
        source_notes=source_notes,
        proposal=proposal,
        discussion_history=discussion_history,
        current_angle_cards=current_angle_cards,
        suffix="",
        instructions_override=instructions_override,
        research_kernel=research_kernel,
        accumulated_context=accumulated_context,
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Ideator. Produce source-bound novelty angle cards "
                    "anchored on the user's project_title and "
                    "research_kernel.tentative_question. proposal_research_question "
                    "is supporting context; do not let it dominate when it "
                    "conflicts with the kernel. "
                    + KERNEL_INJECTION_GUARD
                    + " Return one strict JSON object. Do not claim publishability. "
                    "Do not invent evidence. " + language_directive(project.language)
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.2,
        max_tokens=2600,
        response_format={"type": "json_object"},
        request_id=request_id,
        prompt_template_id="ideator.angle_cards.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="novelty",
        step_id="ideator.angle_cards",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "target_journal": project.target_journal,
            "claims_read": len(claims),
            "source_notes": len(source_notes),
            "angle_count": MIN_ANGLE_CARDS,
            "memory_query": (
                f"phase=ideator topic={project.title} angle_count={MIN_ANGLE_CARDS} "
                f"domain={project.domain_id}"
            ),
        },
    )
    _register_ideator_memory_hook(hooks)
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Ideator")
    response = asyncio.run(
        run_llm_step(
            request=request,
            hooks=hooks,
            context=context,
            output_schema=IdeatorOutput,
            audit=audit,
            max_corrective_retries=2,
            llm_optional=False,
        ),
    )
    parsed = response.parsed
    if isinstance(parsed, IdeatorOutput):
        return _angle_cards_from_output(parsed, claims)
    if isinstance(parsed, Mapping):
        return _parse_angle_cards_response(json.dumps(dict(parsed)), claims)
    return None


def _register_ideator_memory_hook(hooks: HookRegistry) -> None:
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


def _angle_prompt(
    *,
    project_title: str,
    target_journal: str | None,
    domain_data: Mapping[str, Any],
    claims: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, object],
    proposal: Mapping[str, object] | None,
    suffix: str,
    discussion_history: Sequence[Mapping[str, object]] = (),
    current_angle_cards: Sequence[Mapping[str, object]] = (),
    instructions_override: str | None = None,
    research_kernel: Mapping[str, object] | None = None,
    accumulated_context: str = "",
) -> str:
    """Build the ideator's angle-cards LLM prompt.

    ``instructions_override`` replaces the static instruction block
    (codex-AGREEd #2 stage 2.B). Dynamic context (the project title,
    claims, source notes, proposal, discussion history, schema spec)
    is always appended verbatim — overriding it would break schema
    parsing or starve the LLM of input data.
    """
    from autoessay.prompts import IDEATOR_MAIN_INSTRUCTIONS

    instructions = instructions_override or IDEATOR_MAIN_INSTRUCTIONS
    required_schema = {
        "angle_cards": [
            {
                "angle_id": "angle_001",
                "working_title": "string",
                "thesis_one_sentence": "one sentence",
                "key_claim_ids": ["claim_id"],
                "why_novel": "string",
                "evidence_so_far": "string",
                "missing_evidence": "string",
                "journal_fit_note": "string",
                "risks": ["string"],
            },
        ],
    }
    prompt_payload = {
        "project_title": project_title,
        # PR-J7: research_kernel is the user-authored anchor; it
        # outranks proposal (which is LLM-generated and may have
        # drifted) and domain templates.
        "research_kernel": dict(research_kernel) if research_kernel else {},
        "target_journal": target_journal,
        "domain": _domain_summary(domain_data),
        "proposal": _proposal_summary(proposal),
        "claims": _compact_claims(claims),
        "source_notes": _truncate(
            json.dumps(source_notes, sort_keys=True),
            SOURCE_NOTES_CHAR_LIMIT,
        ),
    }
    if accumulated_context:
        prompt_payload["global_context_pack_non_citable"] = accumulated_context
    discussion_block = ""
    regeneration_instruction = ""
    if discussion_history:
        discussion_block = (
            "Previous discussion: "
            f"{json.dumps(_compact_discussion(discussion_history), sort_keys=True)}.\n\n"
        )
        prompt_payload["current_angle_cards"] = [
            dict(card) for card in current_angle_cards if isinstance(card, Mapping)
        ]
        regeneration_instruction = (
            "Regenerate the angle cards in response to the latest user feedback. "
            "Keep useful prior ideas only when they still answer the discussion. "
        )
    return (
        discussion_block + regeneration_instruction + f"{instructions} "
        f"Produce {MIN_ANGLE_CARDS}-{MAX_ANGLE_CARDS} cards. "
        f"Input: {json.dumps(prompt_payload, sort_keys=True)}. "
        f"Return strict JSON matching this schema: {json.dumps(required_schema, sort_keys=True)}"
        f"{suffix}"
    )


def _parse_angle_cards_response(
    value: str,
    claims: Sequence[Mapping[str, object]],
) -> list[dict[str, object]] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    payload = {"angle_cards": decoded} if isinstance(decoded, list) else decoded
    if not isinstance(payload, dict):
        return None
    try:
        parsed = IdeatorOutput.parse_obj(payload)
    except ValidationError:
        return None
    return _angle_cards_from_output(parsed, claims)


def _angle_cards_from_output(
    parsed: IdeatorOutput,
    claims: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    known_claim_ids = {
        str(claim.get("claim_id"))
        for claim in claims
        if isinstance(claim.get("claim_id"), str) and claim.get("claim_id")
    }
    return [_angle_card_payload(card, known_claim_ids) for card in parsed.angle_cards]


def _angle_card_payload(card: AngleCardOutput, known_claim_ids: set[str]) -> dict[str, object]:
    claim_ids = [
        item for item in card.key_claim_ids if not known_claim_ids or item in known_claim_ids
    ]
    return {
        "angle_id": card.angle_id,
        "working_title": card.working_title,
        "thesis_one_sentence": card.thesis_one_sentence,
        "key_claim_ids": claim_ids,
        "why_novel": card.why_novel,
        "evidence_so_far": card.evidence_so_far,
        "missing_evidence": card.missing_evidence,
        "journal_fit_note": card.journal_fit_note,
        "risks": list(card.risks),
        # PR-C2.b Tier 4: pass through the new structured fields.
        # Referential integrity (filtering against valid lens names)
        # happens later in _filter_angle_lens_references — this
        # payload mapping is just a verbatim copy.
        "framework_lens": list(card.framework_lens),
        "methodological_choice": card.methodological_choice,
    }


def _filter_angle_lens_references(
    angle_cards: list[dict[str, object]],
    valid_lens_names: set[str],
) -> tuple[list[dict[str, object]], list[str]]:
    """Filter every angle's ``framework_lens`` field down to names
    that exist in the upstream framework_lens.json artifact.

    PR-C2.b Tier 4 referential integrity: prevent angles from
    referencing lens names that don't exist (LLM hallucinations or
    stale outputs from a re-run with new lens inputs). Returns the
    filtered cards + the list of dropped references for event
    logging.

    When ``valid_lens_names`` is empty (legacy run / lens phase not
    yet executed) this is a no-op — we don't strip references
    because there's no source of truth to validate against.
    """
    if not valid_lens_names:
        return angle_cards, []
    dropped: list[str] = []
    out: list[dict[str, object]] = []
    for card in angle_cards:
        lens_field = card.get("framework_lens", [])
        if not isinstance(lens_field, list):
            out.append(card)
            continue
        kept: list[str] = []
        for name in lens_field:
            if isinstance(name, str) and name in valid_lens_names:
                kept.append(name)
            elif isinstance(name, str):
                dropped.append(name)
        new_card = dict(card)
        new_card["framework_lens"] = kept
        out.append(new_card)
    return out, dropped


def _order_angle_cards_for_kernel(
    angle_cards: list[dict[str, object]],
    research_kernel: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    if not angle_cards or not research_kernel:
        return angle_cards
    kernel_terms = _kernel_alignment_terms(research_kernel)
    if not kernel_terms:
        return angle_cards
    kernel_text = _kernel_text(research_kernel)
    centers_dollar_gold = ("美元" in kernel_text or "dollar" in kernel_text) and (
        "黄金" in kernel_text or "gold" in kernel_text
    )
    mentions_sterling = any(term in kernel_text for term in ("英镑", "sterling", "pound"))

    def score(index_card: tuple[int, dict[str, object]]) -> tuple[float, int]:
        index, card = index_card
        text = _angle_alignment_text(card)
        hits = {term for term in kernel_terms if term in text}
        value = float(len(hits))
        if centers_dollar_gold:
            for phrase in (
                "london gold pool",
                "gold pool",
                "金池",
                "federal reserve",
                "美联储",
                "fomc",
                "imf",
                "美元",
                "dollar",
                "gold",
                "黄金",
                "convertibility",
                "可兑换",
            ):
                if phrase in text:
                    value += 1.0
            if not mentions_sterling and any(
                phrase in text for phrase in ("sterling", "pound sterling", "英镑")
            ):
                value -= 3.0
        return value, -index

    ordered = sorted(enumerate(angle_cards), key=score, reverse=True)
    return [dict(card) for _, card in ordered]


def _angle_alignment_text(card: Mapping[str, object]) -> str:
    fields = [
        "working_title",
        "thesis_one_sentence",
        "why_novel",
        "evidence_so_far",
        "missing_evidence",
        "journal_fit_note",
        "methodological_choice",
    ]
    parts = [str(card.get(field) or "") for field in fields]
    risks = card.get("risks")
    if isinstance(risks, list):
        parts.extend(str(item) for item in risks if isinstance(item, str))
    return " ".join(parts).casefold()


def _kernel_alignment_terms(research_kernel: Mapping[str, object]) -> set[str]:
    text = _kernel_text(research_kernel)
    terms = {term for term in re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", text)}
    bridge = {
        "美元": ("dollar", "dollars"),
        "黄金": ("gold",),
        "可兑换": ("convertibility", "convertible"),
        "兑换": ("convertibility", "convertible"),
        "美联储": ("federal reserve", "fed", "fomc"),
        "黄金池": ("london gold pool", "gold pool"),
        "布雷顿森林": ("bretton woods",),
        "会议纪要": ("minutes", "transcript"),
        "备忘录": ("memorandum", "memo"),
        "阳明": ("yangming", "wang yangming"),
        "江南": ("jiangnan",),
        "刊本": ("edition", "print", "publishing"),
        "序跋": ("preface", "colophon"),
    }
    bridged = set(terms)
    for needle, additions in bridge.items():
        if needle in text:
            bridged.update(additions)
    return {term.casefold() for term in bridged if len(term) >= 3}


def _kernel_text(research_kernel: Mapping[str, object]) -> str:
    parts: list[str] = []
    for value in research_kernel.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(item) for item in value if isinstance(item, str))
    return " ".join(parts).casefold()


def regenerate_angle_cards_for_discussion(
    run: Run,
    session: Session,
    discussion_history: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], int]:
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run.id}")
    run_dir = Path(run.run_dir)
    synthesis_dir = run_dir / "synthesis"
    novelty_dir = run_dir / "novelty"
    novelty_dir.mkdir(parents=True, exist_ok=True)
    current_cards = _load_angle_cards(novelty_dir / "angle_cards.json")
    if not current_cards:
        raise ValueError("current angle cards not found")
    claims = _load_jsonl_objects(synthesis_dir / "claims.jsonl")
    source_notes = _load_source_notes(synthesis_dir / "source_notes")
    domain = load_domain(_domain_path(project.domain_id))
    proposal = _proposal_context(run)
    generation_token = _next_angle_cards_generation_token(novelty_dir)
    settings = get_settings()
    angle_cards: list[dict[str, object]] | None
    if settings.ideator_stub:
        angle_cards = _stub_angle_cards(
            project.title,
            project.target_journal,
            claims,
            source_notes,
            proposal,
            discussion_history=discussion_history,
        )
    else:
        try:
            angle_cards = _ideator_via_harness(
                run=run,
                project=project,
                session=session,
                domain_data=domain.data,
                claims=claims,
                source_notes=source_notes,
                proposal=proposal,
                hooks=HookRegistry(),
                request_id=f"ideator_angle_cards_regenerate_v{generation_token:03d}",
                discussion_history=discussion_history,
                current_angle_cards=current_cards,
            )
        except SchemaViolationError as exc:
            raise ValueError("Ideator could not parse regenerated angle-card JSON") from exc
        except Exception as exc:  # noqa: BLE001 - preserve legacy parse-failure surface.
            raise ValueError("Ideator could not parse regenerated angle-card JSON") from exc
    if angle_cards is None:
        raise ValueError("Ideator could not parse regenerated angle-card JSON")

    # Codex round-4 #3 (2026-05-03): mirror the lens-reference filter
    # that runs in _run_ideator_with_session. Without this, a novelty
    # discussion regenerate could reintroduce hallucinated lens names
    # that the ideator's first-run filter had previously stripped.
    from autoessay.framework_lens import lens_names_from_payload, read_framework_lens

    lens_artifact = read_framework_lens(run_dir)
    valid_lens_names = (
        lens_names_from_payload(lens_artifact) if lens_artifact is not None else set()
    )
    angle_cards, dropped_lens_refs = _filter_angle_lens_references(
        angle_cards,
        valid_lens_names,
    )
    if dropped_lens_refs:
        append_event(
            session,
            run,
            "ideator_lens_refs_dropped",
            {
                "phase": "ideator",
                "dropped": dropped_lens_refs,
                "valid_count": len(valid_lens_names),
                "trigger": "novelty_discussion_regenerate",
            },
        )

    version_path = novelty_dir / f"angle_cards_v{generation_token:03d}.json"
    payload = {"angle_cards": angle_cards}
    _write_json(version_path, payload)
    _write_json(novelty_dir / "angle_cards.json", payload)
    _write_report(
        novelty_dir / "ideator_report.md",
        claims=claims,
        source_notes=source_notes,
        angle_cards=angle_cards,
        guidance=None,
    )
    return angle_cards, generation_token


def _stub_angle_cards(
    project_title: str,
    target_journal: str | None,
    claims: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, object],
    proposal: Mapping[str, object] | None,
    *,
    discussion_history: Sequence[Mapping[str, object]] = (),
) -> list[dict[str, object]]:
    claim_ids = [
        str(claim.get("claim_id"))
        for claim in claims
        if isinstance(claim.get("claim_id"), str) and claim.get("claim_id")
    ]
    source_count = len(source_notes)
    journal_note = target_journal or "the configured history journal"
    cards: list[dict[str, object]] = []
    focus_terms = [
        "institutional timing",
        "credit-market transmission",
        "archival method",
        "policy memory",
    ]
    latest_feedback = _latest_user_feedback(discussion_history)
    if latest_feedback:
        focus_terms[0] = _feedback_focus(latest_feedback)
    proposal_question = ""
    if proposal is not None and isinstance(proposal.get("research_question"), str):
        proposal_question = str(proposal["research_question"])
    for index, focus in enumerate(focus_terms, start=1):
        thesis_tail = (
            f"the argument tied to the current source pack and refining {proposal_question}."
            if proposal_question
            else "the argument tied to the current source pack."
        )
        cards.append(
            {
                "angle_id": f"angle_{index:03d}",
                "working_title": f"{project_title}: {focus.title()}",
                "thesis_one_sentence": (
                    f"{project_title} can be framed through {focus} while keeping {thesis_tail}"
                ),
                "key_claim_ids": claim_ids[:3],
                "why_novel": (
                    "Stub novelty is a test-only synthesis of field-map gaps and source-bound "
                    "claims."
                ),
                "evidence_so_far": f"{len(claim_ids)} claims across {source_count} source notes.",
                "missing_evidence": "Primary-source or page-specific evidence still needs review.",
                "journal_fit_note": (
                    f"Potential fit for {journal_note} if evidence density improves."
                ),
                "risks": [
                    "Evidence base may be too thin for a causal claim.",
                    "Novelty has not been externally validated.",
                ],
            },
        )
    return cards


def _next_angle_cards_generation_token(novelty_dir: Path) -> int:
    existing_tokens: list[int] = []
    for path in novelty_dir.glob("angle_cards_v*.json"):
        match = re.search(r"_v(\d{3})\.json$", path.name)
        if match:
            existing_tokens.append(int(match.group(1)))
    if not existing_tokens and (novelty_dir / "angle_cards.json").exists():
        _write_text(
            novelty_dir / "angle_cards_v001.json",
            (novelty_dir / "angle_cards.json").read_text(encoding="utf-8"),
        )
        existing_tokens.append(1)
    return (max(existing_tokens) + 1) if existing_tokens else 1


def _compact_discussion(
    discussion_history: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    for message in discussion_history:
        role = message.get("role")
        content = message.get("content")
        token = message.get("generation_token")
        if not isinstance(role, str) or not isinstance(content, str):
            continue
        compact.append(
            {
                "role": role,
                "content": _truncate(content, 2000),
                "generation_token": token if isinstance(token, int) else None,
            },
        )
    return compact


def _latest_user_feedback(discussion_history: Sequence[Mapping[str, object]]) -> str:
    for message in reversed(discussion_history):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return str(message["content"])
    return ""


def _feedback_focus(feedback: str) -> str:
    words = [
        word for word in re.findall(r"\b[a-z][a-z'-]*\b", feedback.casefold()) if len(word) > 3
    ]
    if not words:
        return "user feedback"
    return " ".join(words[:4])


def _clean_string_list(values: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = " ".join(str(value).split())
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            cleaned.append(item)
    return cleaned


def _write_report(
    path: Path,
    *,
    claims: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, object],
    angle_cards: Sequence[Mapping[str, object]],
    guidance: str | None,
) -> None:
    lines = [
        "# Ideator Report",
        "",
        f"- Claims read: {len(claims)}",
        f"- Source notes read: {len(source_notes)}",
        f"- Angle cards: {len(angle_cards)}",
        "",
        "## Angle Cards",
        "",
    ]
    if angle_cards:
        for card in angle_cards:
            lines.extend(
                [
                    f"### {card.get('angle_id')}: {card.get('working_title')}",
                    "",
                    f"- Thesis: {card.get('thesis_one_sentence')}",
                    f"- Key claims: {', '.join(_string_list(card.get('key_claim_ids'))) or 'none'}",
                    f"- Missing evidence: {card.get('missing_evidence')}",
                    "",
                ],
            )
    else:
        lines.append("- none")
    if guidance:
        lines.extend(["", "## Guidance", "", guidance])
    _write_text(path, "\n".join(lines) + "\n")


def _apply_edits(
    card: Mapping[str, object],
    edits: Mapping[str, object],
) -> dict[str, object]:
    selected = dict(card)
    for key, value in edits.items():
        if key in {"key_claim_ids", "risks"}:
            if isinstance(value, list):
                selected[key] = [str(item) for item in value if isinstance(item, str)]
        elif (
            key
            in {
                "working_title",
                "thesis_one_sentence",
                "why_novel",
                "evidence_so_far",
                "missing_evidence",
                "journal_fit_note",
            }
            and isinstance(value, str)
            and value.strip()
        ):
            selected[key] = value.strip()
    return selected


def _compact_claims(claims: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    for claim in claims[:CLAIMS_LIMIT]:
        compact.append(
            {
                "claim_id": claim.get("claim_id"),
                "source_id": claim.get("source_id"),
                "claim_type": claim.get("claim_type"),
                "text": claim.get("text"),
            },
        )
    return compact


def _domain_summary(domain_data: Mapping[str, Any]) -> dict[str, object]:
    return {
        "id": domain_data.get("id"),
        "display_name": domain_data.get("display_name"),
        "journals": domain_data.get("journals", {}),
        "citation": domain_data.get("citation", {}),
        "evidence": domain_data.get("evidence", {}),
    }


def _proposal_context(run: Run) -> dict[str, object] | None:
    try:
        payload = load_proposal_payload(run)
    except FileNotFoundError:
        return None
    proposal_json = payload.get("proposal_json")
    return dict(proposal_json) if isinstance(proposal_json, dict) else None


def _proposal_summary(proposal: Mapping[str, object] | None) -> dict[str, object]:
    if proposal is None:
        return {}
    return {
        "research_question": proposal.get("research_question"),
        "significance": proposal.get("significance"),
        "scope": proposal.get("scope"),
        "preliminary_keywords": proposal.get("preliminary_keywords"),
    }


def _load_angle_cards(path: Path) -> list[dict[str, object]]:
    payload = _load_json_mapping(path)
    cards = payload.get("angle_cards")
    if not isinstance(cards, list):
        return []
    return [dict(card) for card in cards if isinstance(card, dict)]


def _load_source_notes(path: Path) -> dict[str, object]:
    notes: dict[str, object] = {}
    if not path.exists():
        return notes
    for note_path in sorted(path.glob("*.json")):
        note = _load_json_mapping(note_path)
        source_id = note.get("source_id")
        if isinstance(source_id, str) and source_id:
            notes[source_id] = note
    return notes


def _load_jsonl_objects(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                records.append(decoded)
    return records


def _load_json_array(path: Path) -> list[object]:
    payload = _load_json_mapping(path)
    cards = payload.get("angle_cards")
    if isinstance(cards, list):
        return cards
    return []


def _load_json_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: value for key, value in decoded.items() if isinstance(key, str)}


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _domain_path(domain_id: str) -> Path:
    settings = get_settings()
    path = settings.domain_dir / f"{domain_id}.yaml"
    if path.exists():
        return path
    return Path(__file__).resolve().parents[4] / "domains" / f"{domain_id}.yaml"


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]
