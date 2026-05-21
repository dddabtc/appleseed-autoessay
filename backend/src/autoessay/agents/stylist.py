"""Stylist agent for v1 prose-only revision and stop-slop scoring."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, StrictStr, ValidationError, validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._humanizer import humanizer_directive
from autoessay.agents._language import language_directive
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.harness import (
    AuditVerdict,
    AuditWriter,
    HookContext,
    HookRegistry,
    HookResult,
    LLMCallRequest,
    SchemaViolationError,
    hash_text,
    run_llm_step,
)
from autoessay.memory import MemoryClient, make_memory_pre_llm_hook
from autoessay.models import Project, Run
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition
from autoessay.stop_slop import StopSlopRules, load_stop_slop_rules, score_text
from autoessay.style_profile import (
    StyleProfile,
    build_style_profile,
    style_profile_summary,
)

SCORE_THRESHOLD = 35

# 2026-05-12 round-0 v2 canary follow-up — empirical scaffolding survives
# stylist re-prose. Section and re-polish prompts both append this so a
# manuscript containing LaTeX equations / markdown tables / 【待填】
# placeholders does not get prose-flattened into confabulated assertions.
_STYLIST_EMPIRICAL_PRESERVATION_GUARD = (
    "empirical_preservation_guard: Preserve all LaTeX equations "
    "($$...$$, $...$, \\begin{equation}...\\end{equation}), markdown or "
    "ASCII tables, and placeholders such as 【待填】, 【TBD】, 【待补】, "
    "[FILL] verbatim. Do not turn them into prose; do not delete tables; "
    "do not fill placeholders with numbers, citations, or claims. "
    "Placeholders are editorial scaffolding, not citations or source_ids. "
    "If empirical-result prose asserts completed findings without a "
    "supporting table, citation, or placeholder, downgrade it to a "
    "design / expectation statement or add 【待填】."
)


@dataclass(frozen=True)
class ManuscriptSection:
    section_id: str
    title: str
    prose: str


@dataclass(frozen=True)
class SectionRevision:
    prose: str
    edit_summary: list[str]
    preserved_claim_ids: list[str]


@dataclass
class StylistHarnessOutcome:
    revision: SectionRevision | None
    safety_guidance: str | None = None
    ngram_overlaps: list[str] = field(default_factory=list)


@dataclass
class NGramGuardState:
    overlaps: list[str] = field(default_factory=list)


@dataclass
class StopSlopScoreState:
    initial_score: dict[str, object] | None = None
    section_scores: dict[str, dict[str, object]] = field(default_factory=dict)
    final_score: dict[str, object] | None = None


class RawStylistResponse(BaseModel):
    revised_prose: str
    edit_summary: list[str] = Field(default_factory=list)
    preserved_claim_ids: list[str] = Field(default_factory=list)

    class Config:
        extra = "ignore"


class StylistSection(BaseModel):
    revised_prose: StrictStr
    edit_summary: list[StrictStr] = Field(default_factory=list)
    preserved_claim_ids: list[StrictStr] = Field(default_factory=list)

    @validator("revised_prose")
    def _revised_prose_must_have_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("revised_prose must be non-empty markdown")
        return value

    class Config:
        extra = "ignore"


def run_stylist(
    run_id: str,
    db_session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run the stylist.

    ``prompt_overrides["main"]`` replaces the universal instruction
    block (revise-prose-only + STRICT RULE) on every per-section call
    in this run. ``prompt_overrides["repolish"]`` (Stage 3.A.3)
    replaces the full-manuscript second-pass instruction. The two
    surfaces are independent.

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
            result["value"] = _run_stylist_with_session(
                run_id,
                session,
                hooks or HookRegistry(),
                prompt_overrides=prompt_overrides,
            )

        maybe_run_with_versioning(session, run, "stylist", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "stylist", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def load_style_payload(run: Run) -> dict[str, object]:
    draft_dir = _latest_draft_dir(Path(run.run_dir))
    if draft_dir is None:
        raise FileNotFoundError("draft not found")
    style_dir = draft_dir / "style"
    if not style_dir.exists():
        raise FileNotFoundError("style artifacts not found")
    violations_path = style_dir / "n_gram_violations.json"
    return {
        "run_id": run.id,
        "version": draft_dir.name,
        "paper_styled": _read_optional_text(style_dir / "paper_styled.md"),
        "style_delta": _read_optional_text(style_dir / "style_delta.md"),
        "stop_slop_score": _load_json_mapping(style_dir / "stop_slop_score.json"),
        "n_gram_violations": (
            _load_json_array(violations_path) if violations_path.exists() else None
        ),
    }


def load_style_score_payload(run: Run) -> dict[str, object]:
    draft_dir = _latest_draft_dir(Path(run.run_dir))
    if draft_dir is None:
        raise FileNotFoundError("draft not found")
    score_path = draft_dir / "style" / "stop_slop_score.json"
    if not score_path.exists():
        raise FileNotFoundError("style score not found")
    return _load_json_mapping(score_path)


def _run_stylist_with_session(
    run_id: str,
    session: Session,
    hooks: HookRegistry,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    assert_run_active(run, session)
    if run.state not in {"DRAFTER_RUNNING", "USER_REVISION_REVIEW"}:
        raise InvalidTransition(
            f"Stylist requires DRAFTER_RUNNING or USER_REVISION_REVIEW, got {run.state}",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    run_dir = Path(run.run_dir)
    draft_dir = _latest_draft_dir(run_dir)
    if draft_dir is None:
        return _fail_fixable(run, session, "Stylist needs a completed drafter artifact.")

    manuscript_path = draft_dir / "manuscript.md"
    claim_map_path = draft_dir / "claim_map.jsonl"
    citations_path = draft_dir / "citations.bib"
    manuscript = _read_optional_text(manuscript_path)
    if not manuscript.strip() or not claim_map_path.exists() or not citations_path.exists():
        return _fail_fixable(
            run,
            session,
            "Latest draft is missing manuscript or citation artifacts.",
        )

    transition(run, "STYLIST_RUNNING", session, reason="Stylist started")
    append_event(
        session,
        run,
        "phase_started",
        {"phase": "stylist", "run_id": run.id, "draft_version": draft_dir.name},
    )
    session.commit()
    session.refresh(run)

    settings = get_settings()
    rules = load_stop_slop_rules()
    claim_map = _load_jsonl_objects(claim_map_path)
    citations_bib = _read_optional_text(citations_path)
    sections = _parse_sections(manuscript)
    if not sections:
        return _fail_fixable(run, session, "Stylist could not find manuscript sections.")

    allow_prior_text = settings.allow_prior_text
    profile = build_style_profile(session, project, allow_prior_text=allow_prior_text)
    use_harness = not settings.stylist_stub
    audit = None
    score_state = StopSlopScoreState()
    initial_score = {} if use_harness else score_text(manuscript, rules.phrases, rules.structures)
    if use_harness:
        audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Stylist")
    instructions_override = prompt_overrides.get("main") if prompt_overrides else None
    repolish_override = prompt_overrides.get("repolish") if prompt_overrides else None

    revised_sections: list[ManuscriptSection] = []
    section_summaries: list[dict[str, object]] = []
    warnings: list[str] = []
    ngram_violations: list[dict[str, object]] = []
    bib_keys = _bib_keys(citations_bib)
    total_sections = len(sections)

    for completed, section in enumerate(sections, start=1):
        section_claim_ids = _claim_ids_for_section(claim_map, section.section_id)
        section_findings = score_text(section.prose, rules.phrases, rules.structures).get(
            "findings",
            [],
        )
        status = "revised"
        accepted_prose = section.prose
        edit_summary: list[str] = []
        if use_harness and audit is not None:
            outcome = _stylist_via_harness(
                section=section,
                claim_ids=section_claim_ids,
                style_profile=profile,
                section_findings=section_findings,
                original_manuscript=manuscript,
                bib_keys=bib_keys,
                rules=rules,
                score_state=score_state,
                run=run,
                project=project,
                base_hooks=hooks,
                audit=audit,
                section_index=completed,
                section_count=total_sections,
                instructions_override=instructions_override,
            )
            if outcome.safety_guidance is not None:
                return _fail_fixable(run, session, outcome.safety_guidance)
            if outcome.revision is None:
                status = "kept_original"
                message = f"WARNING: {section.title} kept original prose after JSON parse failure."
                warnings.append(message)
                edit_summary = [message]
            elif outcome.ngram_overlaps:
                status = "kept_original_ngram_guard"
                edit_summary = [
                    "WARNING: revised prose overlapped a prior-paper local example; original kept.",
                ]
                ngram_violations.append(
                    {
                        "section_id": section.section_id,
                        "section_title": section.title,
                        "overlaps": outcome.ngram_overlaps,
                    },
                )
            else:
                accepted_prose = outcome.revision.prose
                edit_summary = outcome.revision.edit_summary
        else:
            revision = _revise_section(
                section=section,
                claim_ids=section_claim_ids,
                style_profile=profile,
                section_findings=section_findings,
                instructions_override=instructions_override,
            )
            if revision is None:
                status = "kept_original"
                message = f"WARNING: {section.title} kept original prose after JSON parse failure."
                warnings.append(message)
                edit_summary = [message]
            else:
                missing_claims = _missing_claim_ids(section_claim_ids, revision.preserved_claim_ids)
                if missing_claims:
                    guidance = (
                        f"Stylist dropped claim_id(s) in {section.title}: "
                        f"{', '.join(missing_claims)}"
                    )
                    return _fail_fixable(run, session, guidance)
                missing_keys = _missing_citation_keys(section.prose, revision.prose, bib_keys)
                if missing_keys:
                    guidance = (
                        f"Stylist dropped BibTeX key(s) in {section.title}: "
                        f"{', '.join(missing_keys)}"
                    )
                    return _fail_fixable(run, session, guidance)
                overlaps = _five_gram_overlaps(revision.prose, profile.short_local_examples)
                if overlaps:
                    status = "kept_original_ngram_guard"
                    edit_summary = [
                        (
                            "WARNING: revised prose overlapped a prior-paper local example; "
                            "original kept."
                        ),
                    ]
                    ngram_violations.append(
                        {
                            "section_id": section.section_id,
                            "section_title": section.title,
                            "overlaps": overlaps,
                        },
                    )
                else:
                    accepted_prose = revision.prose
                    edit_summary = revision.edit_summary

        revised_sections.append(
            ManuscriptSection(
                section_id=section.section_id,
                title=section.title,
                prose=accepted_prose,
            ),
        )
        section_summaries.append(
            {
                "section_id": section.section_id,
                "section_title": section.title,
                "status": status,
                "edit_summary": edit_summary,
            },
        )
        append_event(
            session,
            run,
            "section_progress",
            {
                "phase": "stylist",
                "draft_version": draft_dir.name,
                "section_id": section.section_id,
                "section_title": section.title,
                "status": status,
                "completed": completed,
                "total": total_sections,
            },
        )
        session.commit()

    styled_manuscript = _compose_sections(revised_sections)
    final_score = score_text(styled_manuscript, rules.phrases, rules.structures)
    score_state.final_score = final_score
    repolish_attempted = False
    final_total = final_score.get("total", 0)
    if isinstance(final_total, int) and final_total < SCORE_THRESHOLD:
        repolish_attempted = True
        lowest_dimension = _lowest_dimension(final_score)
        all_claim_ids = _all_claim_ids(claim_map)
        if use_harness and audit is not None:
            repolish_outcome = _repolish_via_harness(
                manuscript=styled_manuscript,
                original_manuscript=manuscript,
                claim_ids=all_claim_ids,
                style_profile=profile,
                lowest_dimension=lowest_dimension,
                bib_keys=bib_keys,
                rules=rules,
                score_state=score_state,
                run=run,
                project=project,
                base_hooks=hooks,
                audit=audit,
                instructions_override=repolish_override,
            )
            if repolish_outcome.safety_guidance is not None:
                return _fail_fixable(run, session, repolish_outcome.safety_guidance)
            if repolish_outcome.revision is None:
                warnings.append("WARNING: re-polish JSON did not parse; section revisions kept.")
            elif repolish_outcome.ngram_overlaps:
                warnings.append(
                    "WARNING: re-polish overlapped prior-paper example; kept previous prose.",
                )
                ngram_violations.append(
                    {
                        "section_id": "full_manuscript",
                        "section_title": "Full manuscript re-polish",
                        "overlaps": repolish_outcome.ngram_overlaps,
                    },
                )
            else:
                styled_manuscript = repolish_outcome.revision.prose
                section_summaries.append(
                    {
                        "section_id": "full_manuscript",
                        "section_title": "Full manuscript re-polish",
                        "status": "repolished",
                        "edit_summary": repolish_outcome.revision.edit_summary,
                    },
                )
                final_score = score_state.final_score or score_text(
                    styled_manuscript,
                    rules.phrases,
                    rules.structures,
                )
        else:
            repolished = _repolish_manuscript(
                manuscript=styled_manuscript,
                claim_ids=all_claim_ids,
                style_profile=profile,
                lowest_dimension=lowest_dimension,
                instructions_override=repolish_override,
            )
            if repolished is None:
                warnings.append("WARNING: re-polish JSON did not parse; section revisions kept.")
            else:
                missing_claims = _missing_claim_ids(all_claim_ids, repolished.preserved_claim_ids)
                if missing_claims:
                    guidance = f"Stylist re-polish dropped claim_id(s): {', '.join(missing_claims)}"
                    return _fail_fixable(run, session, guidance)
                missing_keys = _missing_citation_keys(manuscript, repolished.prose, bib_keys)
                if missing_keys:
                    guidance = f"Stylist re-polish dropped BibTeX key(s): {', '.join(missing_keys)}"
                    return _fail_fixable(run, session, guidance)
                overlaps = _five_gram_overlaps(repolished.prose, profile.short_local_examples)
                if overlaps:
                    warnings.append(
                        "WARNING: re-polish overlapped prior-paper example; kept previous prose.",
                    )
                    ngram_violations.append(
                        {
                            "section_id": "full_manuscript",
                            "section_title": "Full manuscript re-polish",
                            "overlaps": overlaps,
                        },
                    )
                else:
                    styled_manuscript = repolished.prose
                    section_summaries.append(
                        {
                            "section_id": "full_manuscript",
                            "section_title": "Full manuscript re-polish",
                            "status": "repolished",
                            "edit_summary": repolished.edit_summary,
                        },
                    )
                    final_score = score_text(styled_manuscript, rules.phrases, rules.structures)
        score_state.final_score = final_score

    style_dir = draft_dir / "style"
    style_dir.mkdir(parents=True, exist_ok=True)
    if use_harness:
        initial_score = score_state.initial_score or score_text(
            manuscript,
            rules.phrases,
            rules.structures,
        )
    score_payload = {
        "initial": initial_score,
        "final": final_score,
        "threshold": SCORE_THRESHOLD,
        "repolish_attempted": repolish_attempted,
        "dimension_deltas": _dimension_deltas(initial_score, final_score),
    }
    _write_text(style_dir / "paper_styled.md", styled_manuscript)
    _write_text(
        style_dir / "style_delta.md",
        _style_delta_markdown(
            draft_version=draft_dir.name,
            section_summaries=section_summaries,
            warnings=warnings,
            initial_score=initial_score,
            final_score=final_score,
            profile=profile,
            allow_prior_text=allow_prior_text,
        ),
    )
    _write_json(style_dir / "stop_slop_score.json", score_payload)
    if ngram_violations:
        _write_json(style_dir / "n_gram_violations.json", ngram_violations)
    else:
        _remove_if_exists(style_dir / "n_gram_violations.json")

    summary = {
        "phase": "stylist",
        "draft_version": draft_dir.name,
        "initial_total": initial_score.get("total"),
        "final_total": final_score.get("total"),
        "dimension_deltas": _dimension_deltas(initial_score, final_score),
        "n_gram_violations": len(ngram_violations),
        "next_stage": "user_revision_review",
    }
    transition(run, "USER_REVISION_REVIEW", session, reason="Stylist completed", payload=summary)
    append_event(session, run, "phase_done", summary)
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


def _fail_fixable(run: Run, session: Session, guidance: str) -> dict[str, object]:
    if run.state != "FAILED_FIXABLE":
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="Stylist needs user-fixable input",
            payload={"guidance": guidance},
        )
    append_event(
        session,
        run,
        "phase_failed",
        {
            "phase": "stylist",
            "failure_class": "failed_fixable",
            "guidance": guidance,
        },
    )
    session.commit()
    return {"run_id": run.id, "state": run.state, "guidance": guidance}


def _revise_section(
    *,
    section: ManuscriptSection,
    claim_ids: Sequence[str],
    style_profile: StyleProfile,
    section_findings: object,
    instructions_override: str | None = None,
) -> SectionRevision | None:
    if get_settings().stylist_stub:
        return SectionRevision(
            prose=section.prose,
            edit_summary=["Stub stylist preserved prose."],
            preserved_claim_ids=list(claim_ids),
        )
    return None


def _repolish_manuscript(
    *,
    manuscript: str,
    claim_ids: Sequence[str],
    style_profile: StyleProfile,
    lowest_dimension: str,
    instructions_override: str | None = None,
) -> SectionRevision | None:
    if get_settings().stylist_stub:
        return SectionRevision(
            prose=manuscript,
            edit_summary=[f"Stub re-polish preserved prose for {lowest_dimension}."],
            preserved_claim_ids=list(claim_ids),
        )
    return None


def _stylist_via_harness(
    *,
    section: ManuscriptSection,
    claim_ids: Sequence[str],
    style_profile: StyleProfile,
    section_findings: object,
    original_manuscript: str,
    bib_keys: set[str],
    rules: StopSlopRules,
    score_state: StopSlopScoreState,
    run: Run,
    project: Project,
    base_hooks: HookRegistry,
    audit: AuditWriter,
    section_index: int,
    section_count: int,
    instructions_override: str | None = None,
) -> StylistHarnessOutcome:
    prompt = _section_prompt(
        section=section,
        claim_ids=claim_ids,
        style_profile=style_profile,
        section_findings=section_findings,
        suffix="",
        instructions_override=instructions_override,
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Stylist. Revise academic prose only. Preserve claims, "
                    "citations, order, and evidence. "
                    + language_directive(project.language)
                    + "\n\n"
                    + humanizer_directive(project.language)
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.15,
        max_tokens=2800,
        response_format={"type": "json_object"},
        request_id=f"stylist_section_{_safe_request_id(section.section_id)}",
        prompt_template_id="stylist.section.v1",
    )
    guard_state = NGramGuardState()
    hooks = _make_stylist_hooks(
        base_hooks=base_hooks,
        original_manuscript=original_manuscript,
        original_prose=section.prose,
        expected_claim_ids=claim_ids,
        bib_keys=bib_keys,
        style_profile=style_profile,
        rules=rules,
        score_state=score_state,
        guard_state=guard_state,
    )
    context = HookContext(
        run_id=run.id,
        phase="stylist",
        step_id="stylist.section",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "stylist",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "section_id": section.section_id,
            "section_title": section.title,
            "section_index": section_index,
            "section_count": section_count,
            "score_scope": "section",
            "memory_query": (
                f"phase=stylist section_id={section.section_id} topic={project.title}"
            ),
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=StylistSection,
                audit=audit,
                max_corrective_retries=2,
                fallback=lambda: StylistSection(
                    revised_prose=section.prose,
                    edit_summary=[
                        (
                            "WARNING: revised prose overlapped a prior-paper local example; "
                            "original kept."
                        ),
                    ],
                    preserved_claim_ids=list(claim_ids),
                ),
            ),
        )
    except SchemaViolationError as exc:
        return StylistHarnessOutcome(
            revision=None,
            safety_guidance=_stylist_safety_guidance(exc),
        )
    except Exception:  # noqa: BLE001 - caller applies section-level fallback policy.
        return StylistHarnessOutcome(revision=None)
    return StylistHarnessOutcome(
        revision=_revision_from_output(response.parsed),
        ngram_overlaps=guard_state.overlaps,
    )


def _repolish_via_harness(
    *,
    manuscript: str,
    original_manuscript: str,
    claim_ids: Sequence[str],
    style_profile: StyleProfile,
    lowest_dimension: str,
    bib_keys: set[str],
    rules: StopSlopRules,
    score_state: StopSlopScoreState,
    run: Run,
    project: Project,
    base_hooks: HookRegistry,
    audit: AuditWriter,
    instructions_override: str | None = None,
) -> StylistHarnessOutcome:
    prompt = _repolish_prompt(
        manuscript=manuscript,
        claim_ids=claim_ids,
        style_profile=style_profile,
        lowest_dimension=lowest_dimension,
        instructions_override=instructions_override,
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Stylist. Perform one prose-only re-polish. Preserve claims, "
                    "citations, order, and evidence."
                    + "\n\n"
                    + language_directive(project.language)
                    + "\n\n"
                    + humanizer_directive(project.language)
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.15,
        max_tokens=3600,
        response_format={"type": "json_object"},
        request_id="stylist_repolish_full_manuscript",
        prompt_template_id="stylist.repolish.v1",
    )
    guard_state = NGramGuardState()
    hooks = _make_stylist_hooks(
        base_hooks=base_hooks,
        original_manuscript=original_manuscript,
        original_prose=original_manuscript,
        expected_claim_ids=claim_ids,
        bib_keys=bib_keys,
        style_profile=style_profile,
        rules=rules,
        score_state=score_state,
        guard_state=guard_state,
    )
    context = HookContext(
        run_id=run.id,
        phase="stylist",
        step_id="stylist.repolish",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "stylist",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "section_id": "full_manuscript",
            "section_title": "Full manuscript re-polish",
            "score_scope": "full_manuscript",
            "memory_query": f"phase=stylist repolish=true topic={project.title}",
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=StylistSection,
                audit=audit,
                max_corrective_retries=0,
                fallback=lambda: StylistSection(
                    revised_prose=manuscript,
                    edit_summary=[
                        "WARNING: re-polish overlapped prior-paper example; previous prose kept.",
                    ],
                    preserved_claim_ids=list(claim_ids),
                ),
            ),
        )
    except SchemaViolationError as exc:
        return StylistHarnessOutcome(
            revision=None,
            safety_guidance=_stylist_safety_guidance(exc),
        )
    except Exception:  # noqa: BLE001 - caller applies full-manuscript fallback policy.
        return StylistHarnessOutcome(revision=None)
    return StylistHarnessOutcome(
        revision=_revision_from_output(response.parsed),
        ngram_overlaps=guard_state.overlaps,
    )


def _make_stylist_hooks(
    *,
    base_hooks: HookRegistry,
    original_manuscript: str,
    original_prose: str,
    expected_claim_ids: Sequence[str],
    bib_keys: set[str],
    style_profile: StyleProfile,
    rules: StopSlopRules,
    score_state: StopSlopScoreState,
    guard_state: NGramGuardState,
) -> HookRegistry:
    hooks = _copy_hook_registry(base_hooks)
    _register_stylist_memory_hook(hooks)
    hooks.register_pre_llm(
        "stop_slop_initial_score",
        _make_stop_slop_initial_score_hook(
            original_manuscript=original_manuscript,
            rules=rules,
            score_state=score_state,
        ),
    )
    hooks.register_post_llm(
        "ngram_guard",
        _make_ngram_guard_hook(style_profile.short_local_examples, guard_state),
    )
    hooks.register_post_llm(
        "claim_preservation",
        _make_claim_preservation_hook(expected_claim_ids),
    )
    hooks.register_post_llm(
        "citation_preservation",
        _make_citation_preservation_hook(original_prose, bib_keys),
    )
    hooks.register_post_llm(
        "stop_slop_revision_score",
        _make_stop_slop_revision_score_hook(rules=rules, score_state=score_state),
    )
    return hooks


def _copy_hook_registry(base_hooks: HookRegistry) -> HookRegistry:
    copied = HookRegistry()
    copied._pre_llm = list(base_hooks._pre_llm)
    copied._post_llm = list(base_hooks._post_llm)
    copied._pre_tool = list(base_hooks._pre_tool)
    copied._post_tool = list(base_hooks._post_tool)
    return copied


def _make_stop_slop_initial_score_hook(
    *,
    original_manuscript: str,
    rules: StopSlopRules,
    score_state: StopSlopScoreState,
) -> Callable[[HookContext], HookContext]:
    def pre_llm(ctx: HookContext) -> HookContext:
        if score_state.initial_score is None:
            score_state.initial_score = score_text(
                original_manuscript,
                rules.phrases,
                rules.structures,
            )
        return ctx

    return pre_llm


def _make_stop_slop_revision_score_hook(
    *,
    rules: StopSlopRules,
    score_state: StopSlopScoreState,
) -> Callable[[HookContext, Any], HookResult]:
    def post_llm(ctx: HookContext, response: Any) -> HookResult:
        revision = _revision_from_output(response.parsed)
        if revision is None:
            return HookResult()
        score = score_text(revision.prose, rules.phrases, rules.structures)
        if ctx.run_metadata.get("score_scope") == "full_manuscript":
            score_state.final_score = score
        else:
            section_id = str(ctx.run_metadata.get("section_id", "section"))
            score_state.section_scores[section_id] = score
        return HookResult(annotations={"total": score.get("total")})

    return post_llm


def _make_claim_preservation_hook(
    expected_claim_ids: Sequence[str],
) -> Callable[[HookContext, Any], HookResult]:
    def post_llm(ctx: HookContext, response: Any) -> HookResult:
        revision = _revision_from_output(response.parsed)
        if revision is None:
            return HookResult()
        missing = _missing_claim_ids(expected_claim_ids, revision.preserved_claim_ids)
        if not missing:
            return HookResult(annotations={"checked_claim_ids": list(expected_claim_ids)})
        title = str(ctx.run_metadata.get("section_title", "section"))
        return HookResult(
            annotations={
                "errors": [f"Stylist dropped claim_id(s) in {title}: {', '.join(missing)}"],
                "missing_claim_ids": missing,
            },
            verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
        )

    return post_llm


def _make_citation_preservation_hook(
    original_prose: str,
    bib_keys: set[str],
) -> Callable[[HookContext, Any], HookResult]:
    def post_llm(ctx: HookContext, response: Any) -> HookResult:
        revision = _revision_from_output(response.parsed)
        if revision is None:
            return HookResult()
        missing = _missing_citation_keys(original_prose, revision.prose, bib_keys)
        if not missing:
            return HookResult(annotations={"checked_bib_keys": sorted(bib_keys)})
        title = str(ctx.run_metadata.get("section_title", "section"))
        return HookResult(
            annotations={
                "errors": [f"Stylist dropped BibTeX key(s) in {title}: {', '.join(missing)}"],
                "missing_bib_keys": missing,
            },
            verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
        )

    return post_llm


def _make_ngram_guard_hook(
    examples: Sequence[str],
    guard_state: NGramGuardState,
) -> Callable[[HookContext, Any], HookResult]:
    def post_llm(_ctx: HookContext, response: Any) -> HookResult:
        revision = _revision_from_output(response.parsed)
        if revision is None:
            return HookResult()
        overlaps = _five_gram_overlaps(revision.prose, examples)
        if not overlaps:
            return HookResult(annotations={"overlap_count": 0})
        guard_state.overlaps = overlaps
        return HookResult(
            annotations={
                "message": "revised prose overlapped style_profile.short_local_examples",
                "overlaps": overlaps,
            },
            verdict=AuditVerdict.REJECTED_FALLBACK_USED,
        )

    return post_llm


def _register_stylist_memory_hook(hooks: HookRegistry) -> None:
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


def _stylist_safety_guidance(error: SchemaViolationError) -> str | None:
    for response in error.attempts:
        for message in response.validation_result.errors:
            if message.startswith("claim_preservation: "):
                return message.removeprefix("claim_preservation: ")
            if message.startswith("citation_preservation: "):
                return message.removeprefix("citation_preservation: ")
    return None


def _revision_from_output(parsed: object) -> SectionRevision | None:
    if isinstance(parsed, StylistSection):
        return SectionRevision(
            prose=parsed.revised_prose.strip(),
            edit_summary=[str(item) for item in parsed.edit_summary],
            preserved_claim_ids=[str(item) for item in parsed.preserved_claim_ids],
        )
    if not isinstance(parsed, Mapping):
        return None
    try:
        raw = RawStylistResponse.parse_obj(parsed)
    except ValidationError:
        return None
    return SectionRevision(
        prose=raw.revised_prose.strip(),
        edit_summary=[str(item) for item in raw.edit_summary],
        preserved_claim_ids=[str(item) for item in raw.preserved_claim_ids],
    )


def _section_prompt(
    *,
    section: ManuscriptSection,
    claim_ids: Sequence[str],
    style_profile: StyleProfile,
    section_findings: object,
    suffix: str,
    instructions_override: str | None = None,
) -> str:
    """Build the stylist's per-section LLM prompt.

    ``instructions_override`` replaces the universal "Revise prose
    only … rejected output." instruction block (the static portion
    captured by :data:`STYLIST_MAIN_INSTRUCTIONS`). Codex round-1 #2
    stage 2.B-extension: a trailing space is preserved before the
    schema block whether or not the override sets one, so the
    rendered prompt format is identical to pre-2.B output.
    """
    from autoessay.prompts import STYLIST_MAIN_INSTRUCTIONS

    instructions = instructions_override or STYLIST_MAIN_INSTRUCTIONS
    required_schema = {
        "revised_prose": "markdown prose for this section only",
        "edit_summary": ["list of prose-only changes"],
        "preserved_claim_ids": list(claim_ids),
    }
    return (
        "You are Stylist. "
        f"Section name: {section.title}. "
        f"Claim IDs that must be preserved: {json.dumps(list(claim_ids), sort_keys=True)}. "
        f"Draft section: {section.prose}. "
        f"Style profile: {json.dumps(style_profile_summary(style_profile), sort_keys=True)}. "
        f"Stop-slop findings: {json.dumps(section_findings, sort_keys=True)}. "
        + instructions
        + " "
        + _STYLIST_EMPIRICAL_PRESERVATION_GUARD
        + " "
        + f"Return strict JSON matching this schema: {json.dumps(required_schema, sort_keys=True)}"
        + suffix
    )


def _repolish_prompt(
    *,
    manuscript: str,
    claim_ids: Sequence[str],
    style_profile: StyleProfile,
    lowest_dimension: str,
    instructions_override: str | None = None,
) -> str:
    """Build the stylist's full-manuscript re-polish LLM prompt.

    ``instructions_override`` (Stage 3.A.3) replaces the static
    instruction concept (default :data:`STYLIST_REPOLISH_INSTRUCTIONS`).
    Dynamic context (the lowest stop-slop dimension's actual value,
    claim IDs, style profile, manuscript text, schema spec) stays
    appended after the override so the LLM still receives the data
    it needs and the schema parser does not break.

    Trailing-space discipline: the override block ends with an
    unconditional space before the appended dynamic block.
    """
    from autoessay.prompts import STYLIST_REPOLISH_INSTRUCTIONS

    instructions = instructions_override or STYLIST_REPOLISH_INSTRUCTIONS
    required_schema = {
        "revised_prose": "full revised markdown manuscript",
        "edit_summary": [f"changes that raise {lowest_dimension}"],
        "preserved_claim_ids": list(claim_ids),
    }
    return (
        instructions
        + " "
        + _STYLIST_EMPIRICAL_PRESERVATION_GUARD
        + " "
        + f"Lowest stop-slop dimension: {lowest_dimension}. "
        + f"Claim IDs that must be preserved: {json.dumps(list(claim_ids), sort_keys=True)}. "
        + f"Style profile: {json.dumps(style_profile_summary(style_profile), sort_keys=True)}. "
        + f"Manuscript: {manuscript}. "
        + f"Return strict JSON matching this schema: {json.dumps(required_schema, sort_keys=True)}"
    )


def _parse_sections(manuscript: str) -> list[ManuscriptSection]:
    sections: list[ManuscriptSection] = []
    pending_anchor: str | None = None
    current_id = ""
    current_title = ""
    current_lines: list[str] = []
    for line in manuscript.splitlines():
        anchor_match = re.fullmatch(r'<a id="([^"]+)"></a>', line.strip())
        if anchor_match:
            pending_anchor = anchor_match.group(1)
            continue
        if line.startswith("## "):
            if current_title:
                sections.append(
                    ManuscriptSection(
                        section_id=current_id,
                        title=current_title,
                        prose="\n".join(current_lines).strip(),
                    ),
                )
            current_title = line.removeprefix("## ").strip()
            current_id = pending_anchor or _slugify(current_title)
            pending_anchor = None
            current_lines = []
            continue
        if current_title:
            current_lines.append(line)
    if current_title:
        sections.append(
            ManuscriptSection(
                section_id=current_id,
                title=current_title,
                prose="\n".join(current_lines).strip(),
            ),
        )
    return sections


def _compose_sections(sections: Sequence[ManuscriptSection]) -> str:
    chunks: list[str] = []
    for section in sections:
        chunks.append(f"## {section.title}")
        chunks.append("")
        chunks.append(section.prose.strip())
        chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


def _claim_ids_for_section(
    claim_map: Sequence[Mapping[str, object]],
    section_id: str,
) -> list[str]:
    claim_ids: list[str] = []
    for record in claim_map:
        if str(record.get("section_id", "")) != section_id:
            continue
        claim_id = _claim_id(record)
        if claim_id:
            claim_ids.append(claim_id)
    return claim_ids


def _all_claim_ids(claim_map: Sequence[Mapping[str, object]]) -> list[str]:
    return [claim_id for record in claim_map if (claim_id := _claim_id(record))]


def _claim_id(record: Mapping[str, object]) -> str:
    raw = record.get("claim_id") or record.get("paragraph_id")
    return str(raw) if isinstance(raw, str) and raw else ""


def _missing_claim_ids(
    expected_claim_ids: Sequence[str],
    preserved_claim_ids: Sequence[str],
) -> list[str]:
    preserved = set(preserved_claim_ids)
    return [claim_id for claim_id in expected_claim_ids if claim_id not in preserved]


def _bib_keys(citations_bib: str) -> set[str]:
    return set(re.findall(r"@\w+\{([^,\s]+),", citations_bib))


def _missing_citation_keys(original: str, revised: str, bib_keys: set[str]) -> list[str]:
    original_keys = _referenced_bib_keys(original, bib_keys)
    revised_keys = _referenced_bib_keys(revised, bib_keys)
    return sorted(original_keys - revised_keys)


def _referenced_bib_keys(text: str, bib_keys: set[str]) -> set[str]:
    referenced: set[str] = set()
    for key in bib_keys:
        if re.search(rf"(?<![A-Za-z0-9_:-]){re.escape(key)}(?![A-Za-z0-9_:-])", text):
            referenced.add(key)
    return referenced


def _five_gram_overlaps(revised: str, examples: Sequence[str]) -> list[str]:
    if not examples:
        return []
    example_grams: set[str] = set()
    for example in examples:
        example_grams.update(_n_grams(_tokens(example), 5))
    overlaps = sorted(_n_grams(_tokens(revised), 5) & example_grams)
    return overlaps[:20]


def _n_grams(tokens: Sequence[str], size: int) -> set[str]:
    if len(tokens) < size:
        return set()
    return {" ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1)}


def _tokens(text: str) -> list[str]:
    return re.findall(r"\b[a-z0-9][a-z0-9'-]*\b", text.casefold())


def _style_delta_markdown(
    *,
    draft_version: str,
    section_summaries: Sequence[Mapping[str, object]],
    warnings: Sequence[str],
    initial_score: Mapping[str, object],
    final_score: Mapping[str, object],
    profile: StyleProfile,
    allow_prior_text: bool,
) -> str:
    lines = [
        "# Style Delta",
        "",
        f"- Draft version: {draft_version}",
        f"- Initial stop-slop total: {initial_score.get('total')}",
        f"- Final stop-slop total: {final_score.get('total')}",
        f"- Prior-paper short examples used: {bool(profile.short_local_examples)}",
        f"- Prior text allowed: {allow_prior_text}",
        "",
        "## Sections",
        "",
    ]
    for summary in section_summaries:
        title = summary.get("section_title", "section")
        status = summary.get("status", "unknown")
        lines.append(f"### {title}")
        lines.append("")
        lines.append(f"- Status: {status}")
        edit_summary = summary.get("edit_summary")
        if isinstance(edit_summary, list) and edit_summary:
            for item in edit_summary:
                lines.append(f"- {item}")
        else:
            lines.append("- No changes recorded.")
        lines.append("")
    if warnings:
        lines.extend(["## Warnings", ""])
        for warning in warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines).rstrip() + "\n"


def _dimension_deltas(
    initial_score: Mapping[str, object],
    final_score: Mapping[str, object],
) -> dict[str, int]:
    initial = initial_score.get("dimensions")
    final = final_score.get("dimensions")
    if not isinstance(initial, dict) or not isinstance(final, dict):
        return {}
    deltas: dict[str, int] = {}
    for key, final_value in final.items():
        initial_value = initial.get(key)
        if isinstance(key, str) and isinstance(initial_value, int) and isinstance(final_value, int):
            deltas[key] = final_value - initial_value
    return deltas


def _lowest_dimension(score: Mapping[str, object]) -> str:
    dimensions = score.get("dimensions")
    if not isinstance(dimensions, dict):
        return "directness"
    numeric = {
        key: value
        for key, value in dimensions.items()
        if isinstance(key, str) and isinstance(value, int)
    }
    if not numeric:
        return "directness"
    return min(numeric, key=lambda key: numeric[key])


def stylist_artifacts_ready(run: Run) -> tuple[bool, str | None]:
    """``(True, None)`` iff drafter has produced the artifacts stylist
    needs; ``(False, reason)`` otherwise.

    Used by ``start_stylist`` to reject up-front during the
    ``DRAFTER_RUNNING`` window (which begins immediately on angle-select
    but lasts 5-10min until drafter actually writes the manuscript) so
    the click does not produce an orphan FAIL_FIXABLE state. Mirrors the
    in-agent precondition at stylist.py:185-196 exactly.
    """
    run_dir = Path(run.run_dir)
    draft_dir = _latest_draft_dir(run_dir)
    if draft_dir is None:
        return False, "Drafter has not produced a draft directory yet."
    manuscript = draft_dir / "manuscript.md"
    if not manuscript.exists() or not manuscript.read_text(encoding="utf-8").strip():
        return False, "Drafter has not finished writing the manuscript."
    if not (draft_dir / "claim_map.jsonl").exists():
        return False, "Drafter has not produced a claim map."
    if not (draft_dir / "citations.bib").exists():
        return False, "Drafter has not produced a citations file."
    return True, None


def _latest_draft_dir(run_dir: Path) -> Path | None:
    drafts_dir = run_dir / "drafts"
    if not drafts_dir.exists():
        return None
    candidates = [
        child
        for child in drafts_dir.iterdir()
        if child.is_dir() and re.fullmatch(r"v\d{3}", child.name)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.name)[-1]


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


def _load_json_array(path: Path) -> list[object]:
    if not path.exists():
        return []
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


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


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _strip_json_fence(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "section"


def _safe_request_id(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_")
    return safe or "section"
