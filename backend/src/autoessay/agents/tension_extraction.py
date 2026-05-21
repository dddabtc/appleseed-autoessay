"""PR-C3.a + PR-C3.b — tension_extraction phase agent.

C3.a shipped the schema + Settings + state machine wiring + stub-only
``extract_tensions``. C3.b layers on top:

* ``run_tension_extraction(run_id, ...)`` — phase runner entry point
  that mirrors framework_lens.run_framework_lens (lock-release-on-exit
  + maybe_run_with_versioning + state-machine transitions +
  audit-event emission)
* ``_resolve_tension_payload`` — LLM prompt + harness call + Pydantic
  validation + post-LLM ``validate_claim_refs_against_synthesizer``
  gate. Failure path mirrors framework_lens stub fallback (deterministic
  stub artifact + ``tension_extraction_stub_fallback`` event).

State machine: ``USER_FIELD_REVIEW`` → ``TENSION_EXTRACTION_RUNNING``
→ ``USER_TENSION_REVIEW``. Caller (main.py phase-start endpoint) is
responsible for the skip-vs-run decision via
``should_run_tension_extraction``; this runner assumes it has been
told to run.

Settings two-flag layout (codex round-2 amendment 6):

  ``Settings.tension_taxonomy_enabled`` (default False until prod
  flip after real-paper validates):
      operational gate — when False, ``should_run_tension_extraction``
      returns False and the runner is never invoked.

  ``Settings.tension_extraction_stub`` (default False in prod, True
  in CI / pytest / e2e via conftest + run-e2e-server.sh):
      mirrors framework_lens / canonical_mining / curator_rerank.
      When True, the LLM call is short-circuited to a deterministic
      stub artifact.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._language import language_directive
from autoessay.agents._research_kernel_prompt import (
    KERNEL_INJECTION_GUARD,
    research_kernel_for_prompt,
)
from autoessay.agents._tension_taxonomy import (
    SYNTHESIZER_TRACKS,
    TENSION_BOUNDARY_FIELDS_RECOMMENDED,
    TENSION_EXTRACTION_MAX_TENSIONS,
    ClaimRef,
    TensionClass,
    TensionEntry,
    TensionExtractionOutput,
    TensionPole,
    validate_claim_refs_against_synthesizer,
)
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    SchemaViolationError,
    hash_text,
    run_llm_step,
)
from autoessay.models import Project, Run
from autoessay.state_machine import (
    InvalidTransition,
    append_event,
    assert_run_active,
    transition,
)

# Token-budget caps for the LLM prompt.
_MAX_CLAIM_TEXT_CHARS = 240
_MAX_CLAIMS_PER_TRACK_IN_PROMPT = 6


def should_run_tension_extraction(
    *,
    paper_mode: str,
    synthesizer_payload: Mapping[str, Any] | None,
) -> bool:
    """Operational gate (codex round-2 amendment 6).

    Returns True only when:
      * ``Settings.tension_taxonomy_enabled`` is True (default False
        until C3.b), AND
      * synthesizer.json has at least one claim across the 4 tracks
        (codex round-2 amendment 7 — legacy run reader fallback;
        without claims, tension extraction has nothing to ground in).

    paper_mode is reserved for future per-mode policy (e.g.
    ``review_article`` may want broader tension nets); C3.a treats all
    modes uniformly when the operational gate is on.
    """
    if not get_settings().tension_taxonomy_enabled:
        return False
    if synthesizer_payload is None:
        return False
    from autoessay.agents._tension_taxonomy import SYNTHESIZER_TRACKS

    has_claims = any(bool(synthesizer_payload.get(track)) for track in SYNTHESIZER_TRACKS)
    return has_claims


def _stub_extraction_output(
    paper_mode: str,
    synthesizer_payload: Mapping[str, Any],
) -> TensionExtractionOutput:
    """Deterministic stub artifact for CI / e2e / pytest paths. Picks
    real claim_ids from the synthesizer payload so the post-LLM gate
    (`validate_claim_refs_against_synthesizer`) accepts the output;
    falls back to the earliest claim across the 4 tracks when the
    payload is sparse.

    Fixed at 2 tensions so unit tests can assert exact counts. The
    classes chosen (``continuity_vs_rupture`` + ``evidence_vs_theory``)
    are deliberately neutral and apply to most humanities runs.
    """
    refs = _pick_two_claim_refs(synthesizer_payload)
    if refs is None:
        # No claims at all — emit zero tensions. Caller decides whether
        # this is FAILED_FIXABLE or just a clean empty artifact.
        return TensionExtractionOutput(
            schema_version=1,
            extracted_at=_utc_now(),
            paper_mode=paper_mode,
            tensions=[],
        )
    ref_a, ref_b = refs
    tensions = [
        TensionEntry(
            tension_id="t001",
            class_id=TensionClass.CONTINUITY_VS_RUPTURE,
            summary="Stub tension: continuity vs rupture across the corpus's main thread.",
            poles=[
                TensionPole(label="continuity", claim_refs=[ref_a]),
                TensionPole(label="rupture", claim_refs=[ref_b]),
            ],
            boundary_fields={"period_boundaries": "stub:period"},
        ),
        TensionEntry(
            tension_id="t002",
            class_id=TensionClass.EVIDENCE_VS_THEORY,
            summary="Stub tension: evidence vs theory weighting in the corpus.",
            poles=[
                TensionPole(label="evidence", claim_refs=[ref_a]),
                TensionPole(label="theory", claim_refs=[ref_b]),
            ],
            boundary_fields={"data_scope": "stub:scope"},
        ),
    ]
    return TensionExtractionOutput(
        schema_version=1,
        extracted_at=_utc_now(),
        paper_mode=paper_mode,
        tensions=tensions[:TENSION_EXTRACTION_MAX_TENSIONS],
    )


def _pick_two_claim_refs(
    synthesizer_payload: Mapping[str, Any],
) -> tuple[ClaimRef, ClaimRef] | None:
    from autoessay.agents._tension_taxonomy import SYNTHESIZER_TRACKS

    found: list[ClaimRef] = []
    for track in SYNTHESIZER_TRACKS:
        for claim in synthesizer_payload.get(track) or []:
            if not isinstance(claim, dict):
                continue
            sid = claim.get("source_id")
            cid = claim.get("claim_id")
            if isinstance(sid, str) and isinstance(cid, str):
                found.append(ClaimRef(track=track, source_id=sid, claim_id=cid))
                if len(found) == 2:
                    return found[0], found[1]
    if len(found) == 1:
        # Only one claim available — use it for both poles. Caller's
        # validator already accepts this (a tension may legitimately
        # ground both poles in the same claim when the dispute is
        # internal to one source).
        return found[0], found[0]
    return None


def write_tension_extraction_artifact(
    run_dir: Path,
    output: TensionExtractionOutput,
) -> Path:
    """Persist ``synthesis/tension_extraction.json`` for the given run.
    Called by the agent runner after either the stub or real-LLM
    branch produces an output. Returns the artifact path."""
    target = Path(run_dir) / "synthesis" / "tension_extraction.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        output.json(by_alias=False, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def extract_tensions(
    *,
    paper_mode: str,
    synthesizer_payload: Mapping[str, Any],
    run_dir: Path | None = None,
    run: Run | None = None,
    project: Project | None = None,
    session: Session | None = None,
    research_kernel: Mapping[str, Any] | None = None,
) -> tuple[TensionExtractionOutput, list[dict[str, Any]]]:
    """High-level entrypoint used by the API handler / phase runner.
    Returns ``(output, drop_warnings)``.

    C3.a stub-only path stays unchanged when
    ``Settings.tension_extraction_stub=True``. C3.b adds the real-LLM
    path: when stub is False AND ``run`` / ``project`` / ``session``
    are provided, the LLM is asked for tensions; on schema /
    transport / integrity failure the runner falls back to the stub
    artifact + emits a ``tension_extraction_stub_fallback`` audit
    event (mirrors framework_lens C2c behavior).
    """
    if get_settings().tension_extraction_stub:
        output = _stub_extraction_output(paper_mode, synthesizer_payload)
    elif run is None or project is None or session is None:
        raise ValueError("tension_extraction real-LLM path requires run + project + session")
    else:
        output, fallback_reason = _resolve_tension_payload(
            run=run,
            project=project,
            session=session,
            paper_mode=paper_mode,
            synthesizer_payload=synthesizer_payload,
            research_kernel=research_kernel or {},
        )
        if fallback_reason is not None:
            append_event(
                session,
                run,
                "tension_extraction_stub_fallback",
                {
                    "phase": "tension_extraction",
                    "reason_kind": fallback_reason["reason_kind"],
                    "reason_class": fallback_reason["reason_class"],
                    "reason_summary": fallback_reason["reason_summary"],
                },
            )
            session.commit()
    drops = validate_claim_refs_against_synthesizer(output, dict(synthesizer_payload))
    if run_dir is not None:
        write_tension_extraction_artifact(run_dir, output)
    return output, drops


# ----------------------------------------------------------------------
# PR-C3.b LLM enrichment path
# ----------------------------------------------------------------------


def _resolve_tension_payload(
    *,
    run: Run,
    project: Project,
    session: Session,
    paper_mode: str,
    synthesizer_payload: Mapping[str, Any],
    research_kernel: Mapping[str, Any],
) -> tuple[TensionExtractionOutput, dict[str, str] | None]:
    """LLM enrichment with deterministic stub fallback.

    Returns ``(output, fallback_reason_or_None)``. Caller appends a
    ``tension_extraction_stub_fallback`` audit event when the second
    tuple element is non-None. Catch is scoped to the LLM call ONLY;
    artifact write / state transition errors propagate (matches
    framework_lens C2c amendment G semantics)."""
    stub_payload = _stub_extraction_output(paper_mode, synthesizer_payload)
    system_prompt, user_prompt = _tension_extraction_prompt(
        paper_mode=paper_mode,
        project_title=project.title,
        project_language=project.language or "en",
        research_kernel=research_kernel,
        synthesizer_payload=synthesizer_payload,
    )
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="TensionExtraction")
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.2,
        max_tokens=1500,
        response_format={"type": "json_object"},
        request_id="tension_extraction_signals",
        prompt_template_id="tension_extraction.signals.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="tension_extraction",
        step_id="tension_extraction.signals",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "tension_extraction",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "paper_mode": paper_mode,
            "llm_optional": False,
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=HookRegistry(),
                context=context,
                output_schema=TensionExtractionOutput,
                audit=audit,
                max_corrective_retries=2,
                llm_optional=False,
            ),
        )
    except SchemaViolationError as exc:
        return stub_payload, {
            "reason_kind": "schema_violation",
            "reason_class": type(exc).__name__,
            "reason_summary": str(exc)[:200],
        }
    except Exception as exc:  # noqa: BLE001 — transport / provider failure
        return stub_payload, {
            "reason_kind": "transport_error",
            "reason_class": type(exc).__name__,
            "reason_summary": str(exc)[:200],
        }

    parsed = response.parsed
    if not isinstance(parsed, TensionExtractionOutput):
        return stub_payload, {
            "reason_kind": "schema_violation",
            "reason_class": "InvalidParseResult",
            "reason_summary": "LLM response did not parse as TensionExtractionOutput",
        }
    drops = validate_claim_refs_against_synthesizer(parsed, dict(synthesizer_payload))
    if drops:
        # Hallucinated claim_refs; codex round-2 #1 — drop ENTIRE
        # tension when any of its claim_refs miss the synthesizer
        # corpus. If everything's hallucinated, fall back to stub.
        bad_tension_ids = {drop["tension_id"] for drop in drops}
        kept = [t for t in parsed.tensions if t.tension_id not in bad_tension_ids]
        if not kept:
            return stub_payload, {
                "reason_kind": "claim_ref_hallucination",
                "reason_class": "ValidateClaimRefsAgainstSynthesizer",
                "reason_summary": (
                    f"all {len(parsed.tensions)} tensions had hallucinated claim_refs"
                ),
            }
        parsed = TensionExtractionOutput(
            schema_version=parsed.schema_version,
            extracted_at=parsed.extracted_at,
            paper_mode=parsed.paper_mode,
            tensions=kept,
        )
    return parsed, None


def _tension_extraction_prompt(
    *,
    paper_mode: str,
    project_title: str,
    project_language: str,
    research_kernel: Mapping[str, Any],
    synthesizer_payload: Mapping[str, Any],
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the tension-extraction
    LLM call. Codex round-1 #4: boundary_fields filled at extraction
    stage with class-specific recommended keys. Codex round-2 #1:
    claim_refs must point at real (track, source_id, claim_id) triples
    in the synthesizer artifact."""
    kernel_safe = research_kernel_for_prompt(research_kernel)
    claims_summary = _summarize_synthesizer_claims(synthesizer_payload)
    classes_doc = _format_classes_with_boundary_keys()

    system_lines = [
        "You are the Tension Extraction agent for a Chinese-humanities research-essay pipeline.",
        "Your job is to identify the 1-5 most load-bearing IDEATIONAL TENSIONS in the synthesizer's"
        " 4-track claim corpus that this manuscript's argument has to engage with.",
        "",
        "STRICT OUTPUT CONTRACT (codex round-2 amendments 1, 2, 5):",
        "- Output a single JSON object matching the TensionExtractionOutput schema.",
        "- Each tension MUST have exactly TWO poles with distinct labels.",
        "- Each pole's claim_refs MUST be triples (track, source_id, claim_id) that exist in the"
        " synthesizer corpus shown below — do NOT invent claim_ids or source_ids.",
        "- ``class_id`` MUST be one of the 9 enumerated values listed below; pick the closest fit.",
        "- ``boundary_fields`` is a flat dict[str, str] (max 8 keys, max 80 chars per value);"
        " prefer the recommended keys for the chosen class but add domain-specific keys when"
        " the case demands.",
        "- ``summary`` is a single sentence, ≤200 chars, in the project language.",
        "- ``discipline_subtype`` is optional, ≤80 chars, free-form normalized string.",
        "- ``tension_id`` MUST match ``^t\\d{3}$`` (t001, t002, ...) and be unique in the run.",
        "",
        "QUALITY GUIDELINES:",
        "- Pick tensions that the manuscript's discussion / argument sections will need to engage."
        " Avoid surface-level oppositions that have no bearing on the kernel question.",
        "- Each pole should ground in distinct claims; if both poles cite the same claim, the"
        " tension is internal to one source — that's fine but make the labels precise.",
        "- Prefer 3-5 tensions for case_analysis; up to 8 for theory_article; ≤2 only when the"
        " corpus is genuinely thin.",
        "",
        KERNEL_INJECTION_GUARD,
        "",
        language_directive(project_language),
    ]
    system_prompt = "\n".join(system_lines)

    user_lines = [
        f"Project title: {project_title}",
        f"Paper mode: {paper_mode}",
        "",
        "Research kernel (user-authored anchor; do NOT alter the question):",
        json.dumps(kernel_safe, ensure_ascii=False, indent=2),
        "",
        "Synthesizer 4-track claim corpus (these are the only valid claim_refs you may use):",
        claims_summary,
        "",
        "9 tension classes + recommended boundary_fields keys:",
        classes_doc,
        "",
        "Emit the TensionExtractionOutput JSON now.",
    ]
    user_prompt = "\n".join(user_lines)
    return system_prompt, user_prompt


def _summarize_synthesizer_claims(synthesizer_payload: Mapping[str, Any]) -> str:
    """Compact representation of the 4 tracks for the LLM prompt.
    Each track gets up to ``_MAX_CLAIMS_PER_TRACK_IN_PROMPT`` claims;
    each claim's text is truncated to ``_MAX_CLAIM_TEXT_CHARS``."""
    chunks: list[str] = []
    for track in SYNTHESIZER_TRACKS:
        items = synthesizer_payload.get(track) or []
        if not items:
            chunks.append(f"## {track} (empty)")
            continue
        shown = min(len(items), _MAX_CLAIMS_PER_TRACK_IN_PROMPT)
        chunks.append(f"## {track} ({shown} of {len(items)} claims)")
        for claim in items[:_MAX_CLAIMS_PER_TRACK_IN_PROMPT]:
            if not isinstance(claim, dict):
                continue
            sid = claim.get("source_id", "?")
            cid = claim.get("claim_id", "?")
            text = str(claim.get("text", ""))[:_MAX_CLAIM_TEXT_CHARS]
            chunks.append(f"- ({track}, {sid}, {cid}): {text}")
    return "\n".join(chunks)


def _format_classes_with_boundary_keys() -> str:
    """Render the 9-class enum with recommended boundary_fields keys
    so the LLM can pick the right class + keys per tension."""
    lines: list[str] = []
    for cls in TensionClass:
        keys = TENSION_BOUNDARY_FIELDS_RECOMMENDED[cls]
        lines.append(f"- ``{cls.value}`` — recommended boundary keys: {list(keys)}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Phase runner (mirrors framework_lens.run_framework_lens shape)
# ----------------------------------------------------------------------


def run_tension_extraction(
    run_id: str,
    db_session: Session | None = None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Phase-runner entry point. Mirrors the wrapping pattern of
    framework_lens (lock-release-on-exit + maybe_run_with_versioning).
    Caller is responsible for the skip-vs-run decision via
    ``should_run_tension_extraction``; this runner assumes it has been
    told to run."""
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    del prompt_overrides  # not yet exposed to user editing

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_tension_extraction_with_session(run_id, session)

        maybe_run_with_versioning(session, run, "tension_extraction", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "tension_extraction", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def _run_tension_extraction_with_session(
    run_id: str,
    session: Session,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    valid_inputs = {"USER_FIELD_REVIEW", "USER_TENSION_REVIEW"}
    if run.state not in valid_inputs:
        raise InvalidTransition(
            f"TensionExtraction requires one of {sorted(valid_inputs)}, got {run.state}"
        )
    assert_run_active(run, session)

    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run {run_id}: {run.project_id}")

    transition(
        run,
        "TENSION_EXTRACTION_RUNNING",
        session,
        reason="TensionExtraction started",
    )
    append_event(
        session,
        run,
        "phase_started",
        {"phase": "tension_extraction", "run_id": run.id},
    )
    session.commit()

    run_dir = Path(run.run_dir)
    synthesizer_path = run_dir / "synthesis" / "synthesizer.json"
    if not synthesizer_path.exists():
        # Without a synthesizer artifact tension extraction has nothing
        # to ground in. This is a FAILED_FIXABLE — the user must (re-)run
        # synthesizer first.
        guidance = (
            "Tension extraction requires synthesis/synthesizer.json. "
            "Re-run the Synthesizer phase first."
        )
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="TensionExtraction missing synthesizer artifact",
            payload={"phase": "tension_extraction", "guidance": guidance},
        )
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": "tension_extraction",
                "failure_class": "failed_fixable",
                "guidance": guidance,
            },
        )
        session.commit()
        return {"run_id": run.id, "state": run.state, "guidance": guidance}

    try:
        synthesizer_payload = json.loads(
            synthesizer_path.read_text(encoding="utf-8"),
        )
    except json.JSONDecodeError as exc:
        guidance = f"Cannot read synthesis/synthesizer.json: {exc}"
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="TensionExtraction synthesizer artifact unreadable",
            payload={"phase": "tension_extraction", "guidance": guidance},
        )
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": "tension_extraction",
                "failure_class": "failed_fixable",
                "guidance": guidance,
            },
        )
        session.commit()
        return {"run_id": run.id, "state": run.state, "guidance": guidance}

    output, drops = extract_tensions(
        paper_mode=run.paper_mode or "case_analysis",
        synthesizer_payload=synthesizer_payload,
        run_dir=run_dir,
        run=run,
        project=project,
        session=session,
        research_kernel=run.research_kernel_json or {},
    )

    summary = {
        "phase": "tension_extraction",
        "tensions": len(output.tensions),
        "drops": len(drops),
    }
    transition(
        run,
        "USER_TENSION_REVIEW",
        session,
        reason="TensionExtraction completed",
        payload=summary,
    )
    append_event(session, run, "phase_done", summary)
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_tension_extraction(run_dir: Path) -> TensionExtractionOutput | None:
    """Reader for downstream consumers (lens prompt in C3.b, drafter
    scaffold in C3.b, frontend TensionSubview in C3.b). Returns None
    when the artifact is absent (legacy run / phase skipped)."""
    path = Path(run_dir) / "synthesis" / "tension_extraction.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    try:
        return TensionExtractionOutput.parse_obj(data)
    except Exception:  # noqa: BLE001 — malformed artifact treated same as absent
        return None


__all__ = [
    "extract_tensions",
    "load_tension_extraction",
    "run_tension_extraction",
    "should_run_tension_extraction",
    "write_tension_extraction_artifact",
]
