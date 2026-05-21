"""PR-C2.a + PR-C2c: framework-lens phase agent.

Reads ``sources/shortlist.json`` (theoretical_lens-tagged sources)
plus ``synthesis/synthesizer.json::theoretical_lens_track`` and
emits ``synthesis/framework_lens.json``. The lens artifact records a
schema-v2 ``synthesizer_input_ref`` instead of mutating the upstream
``synthesizer.json`` artifact.

State machine: USER_FIELD_REVIEW -> FRAMEWORK_LENS_RUNNING ->
USER_LENS_REVIEW. Caller (main.py phase-start endpoint) is
responsible for the skip-vs-run decision via
``framework_lens.should_run_framework_lens``; this agent assumes
it has been told to run.

PR-C2c: by default the LLM enrichment path runs; the deterministic
stub path is gated by ``Settings.framework_lens_stub`` (env name
``AUTOESSAY_FRAMEWORK_LENS_STUB``) and used by CI / e2e / pytest.
LLM transport / schema-violation / integrity-violation failures
fall back to the stub deterministically and emit a
``framework_lens_stub_fallback`` event so observers can tell a
fallback run apart from a real LLM run (the
``milestone-lens-llm`` tag gate refuses fallback runs).

The ``theory_article + 0 theoretical_lens source`` case is handled
BEFORE the LLM call via the existing FAILED_FIXABLE branch; it does
NOT participate in the fallback path (per HANDOFF §11.7.1
amendment 1).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._framework_lens_schema import (
    FrameworkLensSignalsOutput,
    framework_lens_integrity,
)
from autoessay.agents._language import language_directive
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.framework_lens import (
    LensSignal,
    build_synthesizer_input_ref,
    compose_framework_lens,
    is_stub_enabled,
    write_framework_lens,
)
from autoessay.harness import (
    AuditVerdict,
    AuditWriter,
    HookContext,
    HookRegistry,
    HookResult,
    LLMCallRequest,
    LLMCallResponse,
    SchemaViolationError,
    hash_text,
    run_llm_step,
)
from autoessay.memory import MemoryClient, make_memory_pre_llm_hook
from autoessay.models import Project, Run
from autoessay.state_machine import (
    InvalidTransition,
    append_event,
    assert_run_active,
    transition,
)

# Truncation budgets: keep the prompt small. The LLM is asked to name
# theoretical lenses, not summarize entire abstracts.
_MAX_LENS_SOURCES_IN_PROMPT = 8
_MAX_ABSTRACT_CHARS = 600
_MAX_LENS_CLAIMS_IN_PROMPT = 12
_MAX_CLAIM_TEXT_CHARS = 280


def run_framework_lens(
    run_id: str,
    db_session: Session | None = None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Phase-runner entry point. Mirrors the wrapping pattern of
    other agents (lock-release-on-exit + maybe_run_with_versioning).
    """
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    del prompt_overrides  # not yet exposed to user editing

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_framework_lens_with_session(run_id, session)

        maybe_run_with_versioning(session, run, "framework_lens", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "framework_lens", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def _run_framework_lens_with_session(
    run_id: str,
    session: Session,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    if run.state != "USER_FIELD_REVIEW":
        raise InvalidTransition(f"FrameworkLens requires USER_FIELD_REVIEW, got {run.state}")
    # Round-1 audit #25: lens runner had no cancellation guard.
    # Without this, a user could click "cancel run" while the lens
    # phase was queued/in-flight and the agent would still write
    # framework_lens.json even though the run was marked cancelled.
    assert_run_active(run, session)

    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run {run_id}: {run.project_id}")

    transition(
        run,
        "FRAMEWORK_LENS_RUNNING",
        session,
        reason="FrameworkLens started",
    )
    append_event(
        session,
        run,
        "phase_started",
        {"phase": "framework_lens", "run_id": run.id},
    )
    session.commit()
    session.refresh(run)

    run_dir = Path(run.run_dir)
    shortlist = _load_json_array(run_dir / "sources" / "shortlist.json")
    dual_track = _load_json_object(run_dir / "synthesis" / "synthesizer.json")
    paper_mode = str(run.paper_mode or "case_analysis")

    # PR-C2.b codex amendment 1: theory_article cannot silently
    # produce an empty lens artifact. If we got here AND there are
    # zero lens inputs (no theoretical_lens_track + no
    # theoretical_lens shortlist sources), transition to
    # FAILED_FIXABLE with localized guidance so the user can go
    # tag a source via the Sources tab and rerun.
    from autoessay.framework_lens import (
        framework_lens_skip_failure_guidance,
        has_theoretical_lens_inputs,
    )

    eligible_lens_input_present = has_theoretical_lens_inputs(
        dual_track=dual_track,
        shortlist=shortlist,
    )

    if paper_mode == "theory_article" and not eligible_lens_input_present:
        guidance = framework_lens_skip_failure_guidance(paper_mode)
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="FrameworkLens needs theoretical_lens sources",
            payload={"guidance": guidance, "phase": "framework_lens"},
        )
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": "framework_lens",
                "failure_class": "fixable_input",
                "guidance": guidance,
            },
        )
        session.commit()
        return {
            "run_id": run.id,
            "state": run.state,
            "phase": "framework_lens",
            "guidance": guidance,
        }

    from autoessay.phase_version import get_run_head

    synthesizer_pv_id = get_run_head(
        session,
        run.id,
        "synthesizer",
        branch_id=run.active_branch_id,
    )
    synthesizer_input_ref = build_synthesizer_input_ref(
        run_dir,
        synthesizer_pv_id=synthesizer_pv_id,
    )

    # Stub payload is always built so we have a deterministic fallback
    # ready (and so CI-stub mode just uses this directly).
    stub_payload = compose_framework_lens(
        shortlist=shortlist,
        dual_track=dual_track,
        paper_mode=paper_mode,
        synthesizer_input_ref=synthesizer_input_ref,
        stub=True,  # force the stub branch regardless of env
    )

    if is_stub_enabled():
        payload = stub_payload
    else:
        payload, fallback_reason = _resolve_framework_lens_payload(
            run=run,
            project=project,
            session=session,
            stub_payload=stub_payload,
            shortlist=shortlist,
            dual_track=dual_track,
            paper_mode=paper_mode,
            synthesizer_input_ref=synthesizer_input_ref,
            eligible_lens_input_present=eligible_lens_input_present,
        )
        if fallback_reason is not None:
            append_event(
                session,
                run,
                "framework_lens_stub_fallback",
                {
                    "phase": "framework_lens",
                    "reason_kind": fallback_reason["reason_kind"],
                    "reason_class": fallback_reason["reason_class"],
                    "reason_summary": fallback_reason["reason_summary"],
                },
            )
            session.commit()

    write_framework_lens(run_dir, payload)

    signals_field = payload.get("signals")
    signal_count = len(signals_field) if isinstance(signals_field, list) else 0
    summary = {
        "phase": "framework_lens",
        "signals": signal_count,
    }
    transition(
        run,
        "USER_LENS_REVIEW",
        session,
        reason="FrameworkLens completed",
        payload=summary,
    )
    append_event(session, run, "phase_done", summary)
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


# ----------------------------------------------------------------------
# PR-C2c LLM enrichment path
# ----------------------------------------------------------------------


def _resolve_framework_lens_payload(
    *,
    run: Run,
    project: Project,
    session: Session,
    stub_payload: dict[str, object],
    shortlist: Sequence[Mapping[str, object]],
    dual_track: Mapping[str, object] | None,
    paper_mode: str,
    synthesizer_input_ref: Mapping[str, object],
    eligible_lens_input_present: bool,
) -> tuple[dict[str, object], dict[str, str] | None]:
    """LLM enrichment with deterministic stub fallback.

    Returns (payload_to_write, fallback_reason_or_None). The caller
    appends a ``framework_lens_stub_fallback`` audit event when the
    second tuple element is non-None.

    Codex round-1 amendment G: catch ``SchemaViolationError`` separately
    from generic ``Exception`` (transport / provider failures) and tag
    each fallback with a ``reason_kind`` so observers and tests can tell
    schema/integrity rejections apart from network/provider issues.
    Catch is scoped to the LLM call ONLY; artifact write / state
    transition / payload assembly errors propagate.
    """
    # Build prompt context.
    theoretical_lens_sources = [
        s
        for s in shortlist
        if isinstance(s, Mapping) and s.get("research_role") == "theoretical_lens"
    ][:_MAX_LENS_SOURCES_IN_PROMPT]
    eligible_source_ids = tuple(
        str(s["source_id"])
        for s in theoretical_lens_sources
        if isinstance(s.get("source_id"), str) and s["source_id"]
    )
    theoretical_lens_claims = _theoretical_lens_claims(dual_track)
    inventory = _evidence_inventory(dual_track, shortlist)

    hooks = HookRegistry()
    _register_framework_lens_memory_hook(hooks)
    _register_framework_lens_integrity_hook(
        hooks,
        eligible_source_ids=eligible_source_ids,
        paper_mode=paper_mode,
        eligible_lens_input_present=eligible_lens_input_present,
    )

    # PR-C3.b codex round-2 amendment 3: lens prompt consumes compact
    # tensions when present (artifact written by tension_extraction
    # phase). Absent artifact → empty list, lens prompt unchanged from
    # pre-C3 behavior. Caller has no obligation to run tension first.
    tensions_compact = _load_compact_tensions(run.run_dir)

    system_prompt, user_prompt = _framework_lens_prompt(
        paper_mode=paper_mode,
        project_title=project.title,
        project_language=project.language or "en",
        research_kernel=run.research_kernel_json or {},
        theoretical_lens_sources=theoretical_lens_sources,
        theoretical_lens_claims=theoretical_lens_claims,
        inventory=inventory,
        tensions_compact=tensions_compact,
    )
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="FrameworkLens")
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.2,
        max_tokens=900,
        response_format={"type": "json_object"},
        request_id="framework_lens_signals",
        prompt_template_id="framework_lens.signals.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="framework_lens",
        step_id="framework_lens.signals",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "framework_lens",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "paper_mode": paper_mode,
            "eligible_lens_source_count": len(eligible_source_ids),
            "theoretical_lens_claim_count": len(theoretical_lens_claims),
            "memory_query": (f"phase=framework_lens topic={project.title} paper_mode={paper_mode}"),
        },
    )

    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=FrameworkLensSignalsOutput,
                audit=audit,
                max_corrective_retries=2,
                llm_optional=False,
            ),
        )
    except SchemaViolationError as exc:
        return stub_payload, {
            "reason_kind": "schema_or_integrity",
            "reason_class": type(exc).__name__,
            "reason_summary": str(exc)[:500],
        }
    except Exception as exc:  # noqa: BLE001 — provider/transport fallback
        return stub_payload, {
            "reason_kind": "transport_or_provider",
            "reason_class": type(exc).__name__,
            "reason_summary": str(exc)[:500],
        }

    parsed = response.parsed
    if not isinstance(parsed, FrameworkLensSignalsOutput):
        return stub_payload, {
            "reason_kind": "schema_or_integrity",
            "reason_class": "ParsedTypeMismatch",
            "reason_summary": (
                f"LLM response did not parse as FrameworkLensSignalsOutput "
                f"(got {type(parsed).__name__})"
            ),
        }

    llm_signals = [_lens_signal_from_output(sig) for sig in parsed.signals]
    payload = dict(stub_payload)
    payload["signals"] = [
        {
            "lens_name": s.lens_name,
            "key_concepts": list(s.key_concepts),
            "source_id": s.source_id,
            "applicability_to_kernel": s.applicability_to_kernel,
        }
        for s in llm_signals
    ]
    return payload, None


def _register_framework_lens_memory_hook(hooks: HookRegistry) -> None:
    """Mirror synthesizer's memory_read pre_llm hook so lens enrichment
    pulls cross-run guidance the same way other agents do."""
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


def _register_framework_lens_integrity_hook(
    hooks: HookRegistry,
    *,
    eligible_source_ids: Sequence[str],
    paper_mode: str,
    eligible_lens_input_present: bool,
) -> None:
    """Post-LLM hook that runs ``framework_lens_integrity`` against the
    parsed payload. Errors get wrapped into ``HookResult(verdict=
    REJECTED_SCHEMA_VIOLATION, annotations={'errors': [...],
    'message': ...})`` so the harness's corrective-suffix retry loop
    has actionable feedback (per codex round-1 amendment for J6
    parallel — annotations MUST contain ``message`` or ``errors`` keys
    or the corrective suffix won't tell the LLM what to fix).

    A fresh ``HookRegistry`` is created per LLM call in
    ``_resolve_framework_lens_payload``, so this run-specific hook
    cannot leak into a sibling run (no shared registry).
    """
    eligible_tuple = tuple(eligible_source_ids)

    def _hook(ctx: HookContext, response: LLMCallResponse) -> HookResult | None:
        del ctx
        parsed = response.parsed
        if not isinstance(parsed, FrameworkLensSignalsOutput):
            # Earlier validation already failed; let the schema path
            # handle it.
            return None
        errors = framework_lens_integrity(
            parsed,
            eligible_source_ids=eligible_tuple,
            paper_mode=paper_mode,
            eligible_lens_input_present=eligible_lens_input_present,
        )
        if not errors:
            return None
        return HookResult(
            annotations={
                "errors": errors,
                "error_count": len(errors),
                "message": "; ".join(errors[:3]),
            },
            verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
        )

    hooks.register_post_llm("framework_lens_integrity", _hook)


_MAX_TENSIONS_IN_LENS_PROMPT = 5
_MAX_TENSION_SUMMARY_IN_LENS_PROMPT = 120


def _load_compact_tensions(run_dir: str) -> list[Mapping[str, object]]:
    """PR-C3.b: read ``synthesis/tension_extraction.json`` (if present)
    and return a compact ≤5-tension list for the lens prompt. Each
    tension is reduced to ``{tension_id, class_id, summary,
    boundary_fields_keys}`` — ≤120 chars per summary, no claim_refs
    (the lens only needs the boundary signal, not the corpus
    pointers). Codex round-2 amendment 3: pre-cap so prompt size
    stays bounded."""
    from pathlib import Path

    from autoessay.agents.tension_extraction import load_tension_extraction

    output = load_tension_extraction(Path(run_dir))
    if output is None:
        return []
    compact: list[Mapping[str, object]] = []
    for tension in output.tensions[:_MAX_TENSIONS_IN_LENS_PROMPT]:
        summary = tension.summary[:_MAX_TENSION_SUMMARY_IN_LENS_PROMPT]
        compact.append(
            {
                "tension_id": tension.tension_id,
                "class_id": tension.class_id.value,
                "summary": summary,
                "boundary_fields_keys": list(tension.boundary_fields.keys()),
            },
        )
    return compact


def _framework_lens_prompt(
    *,
    paper_mode: str,
    project_title: str,
    project_language: str,
    research_kernel: Mapping[str, object],
    theoretical_lens_sources: Sequence[Mapping[str, object]],
    theoretical_lens_claims: Sequence[Mapping[str, object]],
    inventory: Mapping[str, int],
    tensions_compact: Sequence[Mapping[str, object]] = (),
) -> tuple[str, str]:
    """Build the (system, user) prompt pair for framework_lens
    enrichment. Codex round-1 amendment F: pass the
    ``theoretical_lens_track`` claim summaries from the synthesizer
    artifact so the LLM sees what claims have already been extracted,
    not just shortlist metadata. Source list is truncated to
    ``_MAX_LENS_SOURCES_IN_PROMPT`` and the eligible_source_ids the
    integrity hook validates against MUST equal the truncated set
    actually shown in the prompt."""
    is_theory_article = paper_mode == "theory_article"
    target_count_hint = (
        "Aim for 2 to 3 lenses (or more, up to 8) when the sources actually "
        "support that many. Do not invent lenses to meet a count."
        if is_theory_article
        else "Return 0 to 3 lenses, only when a source genuinely supplies a "
        "theoretical framework that helps explain the kernel."
    )
    role_emphasis = (
        "This is a THEORY-CENTRED paper. The lens IS the theoretical core; "
        "every signal must articulate how the framework is mobilized by the "
        "research question."
        if is_theory_article
        else "Lenses are auxiliary here. Return signals only when a source "
        "actually contributes a theoretical framework with explanatory leverage."
    )

    sources_summary = [
        {
            "source_id": str(s.get("source_id") or ""),
            "title": str(s.get("title") or ""),
            "venue": str(s.get("venue") or ""),
            "research_role": str(s.get("research_role") or ""),
            "abstract": _truncate(str(s.get("abstract") or ""), _MAX_ABSTRACT_CHARS),
        }
        for s in theoretical_lens_sources
    ]
    claims_summary = [
        {
            "source_id": str(c.get("source_id") or ""),
            "claim_id": str(c.get("claim_id") or ""),
            "text": _truncate(str(c.get("text") or ""), _MAX_CLAIM_TEXT_CHARS),
        }
        for c in theoretical_lens_claims[:_MAX_LENS_CLAIMS_IN_PROMPT]
    ]
    kernel_payload = _research_kernel_for_prompt(research_kernel)
    user_payload = {
        "paper_mode": paper_mode,
        "project_title": project_title,
        "research_kernel": kernel_payload,
        "theoretical_lens_sources": sources_summary,
        "theoretical_lens_claims_from_synthesizer": claims_summary,
        # PR-C3.b codex round-2 amendment 3: compact tensions (≤5)
        # so the lens prompt can mention how the framework engages
        # them. Empty list when tension_extraction phase didn't run /
        # was skipped.
        "open_tensions_from_tension_extraction": list(tensions_compact),
        "evidence_inventory": dict(inventory),
        "target_count_hint": target_count_hint,
        "role_emphasis": role_emphasis,
        "schema": {
            "signals": [
                {
                    "lens_name": (
                        "specific framework name (e.g. 'Bourdieu: habitus', "
                        "'Polanyi: embeddedness'); placeholders like 'Lens 1', "
                        "'Default Lens', 'Generic Theory' are forbidden"
                    ),
                    "key_concepts": ["one or more concept strings"],
                    "source_id": (
                        "MUST be one of theoretical_lens_sources[].source_id "
                        "above (the truncated set; not a free pick)"
                    ),
                    "applicability_to_kernel": (
                        "30-300 chars: how this lens is mobilized by the "
                        "research_kernel.tentative_question"
                    ),
                }
            ]
        },
    }
    tensions_directive = (
        "When ``open_tensions_from_tension_extraction`` is non-empty, treat "
        "those tensions as the manuscript's load-bearing arguments. If a "
        "lens you're suggesting has implications for one of those tensions' "
        "boundary fields, mention how the framework engages the tension in "
        "``applicability_to_kernel`` (do NOT cite tension_id or class_id by "
        "name in body text; this is scaffolding metadata). Empty tensions "
        "list = no requirement; behave as before."
        if tensions_compact
        else ""
    )
    system_prompt = (
        "You are FrameworkLens. Identify the theoretical lenses (frameworks) "
        "embedded in the supplied theoretical_lens-tagged sources, and "
        "articulate how each applies to this paper's research kernel. Return "
        'one strict JSON object {"signals": [...]} matching the schema. '
        "Each signal's source_id MUST be one of the supplied "
        "theoretical_lens_sources. Do not invent sources or generic "
        "framework names. Be specific about what the framework does for the "
        f"kernel. {role_emphasis} {tensions_directive} "
        f"{language_directive(project_language)}"
    )
    user_prompt = (
        "Generate the framework_lens.signals payload from the inputs below. "
        f"Constraints: {target_count_hint} Each signal must reference a "
        "source_id from theoretical_lens_sources and explain its "
        "applicability concretely (no AI-stock phrasing).\n\n"
        f"{json.dumps(user_payload, ensure_ascii=False, sort_keys=True)}"
    )
    return system_prompt, user_prompt


def _research_kernel_for_prompt(
    research_kernel: Mapping[str, object],
) -> dict[str, object]:
    """Project the kernel onto the fields scout / framework_lens care
    about. Truncates long strings to keep the prompt small. Kernel is
    an opaque blob (PR-C0 design), so unknown keys are ignored.
    """
    if not isinstance(research_kernel, Mapping):
        return {}
    out: dict[str, object] = {}
    for key in (
        "tentative_question",
        "observed_puzzle",
        "scope",
        "method_preference",
        "theory_preference",
    ):
        value = research_kernel.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = _truncate(value.strip(), 500)
    paper_mode = research_kernel.get("paper_mode")
    if isinstance(paper_mode, str) and paper_mode.strip():
        out["paper_mode_hint"] = paper_mode.strip()
    return out


def _theoretical_lens_claims(
    dual_track: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    """Pull claims from ``synthesis/synthesizer.json::theoretical_lens_track``
    if present. The synthesizer dual-track output puts theoretical_lens
    claims under the ``theoretical_lens_track`` key (see
    backend/src/autoessay/agents/synthesizer.py::_write_dual_track_synthesizer).
    """
    if not isinstance(dual_track, Mapping):
        return []
    track = dual_track.get("theoretical_lens_track")
    if not isinstance(track, list):
        return []
    out: list[dict[str, object]] = []
    for entry in track:
        if isinstance(entry, Mapping):
            out.append(dict(entry))
    return out


def _evidence_inventory(
    dual_track: Mapping[str, object] | None,
    shortlist: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Counts the LLM should know about so it can calibrate density:
    how many lens-eligible sources, primary sources, secondary
    arguments are available."""
    primary = 0
    secondary = 0
    lens = 0
    for entry in shortlist:
        if not isinstance(entry, Mapping):
            continue
        role = entry.get("research_role")
        if role == "primary_source":
            primary += 1
        elif role == "theoretical_lens":
            lens += 1
        elif role in (None, "", "secondary_argument"):
            secondary += 1
    track_claim_count = 0
    if isinstance(dual_track, Mapping):
        track = dual_track.get("theoretical_lens_track")
        if isinstance(track, list):
            track_claim_count = len(track)
    return {
        "primary_source_count": primary,
        "secondary_argument_count": secondary,
        "theoretical_lens_source_count": lens,
        "theoretical_lens_claim_count": track_claim_count,
    }


def _lens_signal_from_output(signal: Any) -> LensSignal:
    """Convert a parsed ``LensSignalOutput`` to the legacy ``LensSignal``
    dataclass so the rest of compose_framework_lens / write_framework_lens
    keeps its existing shape."""
    return LensSignal(
        lens_name=signal.lens_name,
        key_concepts=tuple(signal.key_concepts),
        source_id=signal.source_id,
        applicability_to_kernel=signal.applicability_to_kernel,
    )


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _load_json_array(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, dict)]


def _load_json_object(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None
