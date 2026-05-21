"""Synthesizer stub agent for top-K source notes and flat claims."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, StrictStr, ValidationError, validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._language import language_directive
from autoessay.agents.material_diagnostic import (
    diagnostic_to_dict,
    render_material_diagnostic_markdown,
    run_material_diagnostic,
)
from autoessay.agents.proposal import load_proposal_payload
from autoessay.clients import pdf_text
from autoessay.clients.common import NormalizedSource, VerificationStatus
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
from autoessay.models import Checkpoint, Project, Run
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

# PR-G-Sources Stage 1 (codex round-2 amendment Q1): the original
# 6-source deep-dive cap was a synthesizer-side bottleneck — drafter
# read shortlist (~24 entries) but only ~6 had ``source_notes``,
# so claim_map could only cite from those 6 even when shortlist
# offered more eligible sources. Bumping default to 14 lets the
# pipeline cite up to ``cited_sources_diversity_floor`` (12 by
# default) without changing retrieval or curator behavior. Per-
# domain ``search.telescope.deep_dive_limit`` overrides this; the
# Settings env override (AUTOESSAY_SYNTHESIZER_DEEP_DIVE_LIMIT)
# overrides both.
DEFAULT_DEEP_DIVE_LIMIT = 14
LLM_TEXT_CHAR_LIMIT = 12000
CLAIM_TYPES = ("consensus", "debate", "finding", "method", "limit")
CLAIM_TYPE_SET = set(CLAIM_TYPES)
DEEP_DIVE_CHECKPOINT_TYPES = {
    "deep-dive-review",
    "deep_dive_review",
    "deep-dive",
    "deep_dive",
    "source-deep-dive",
    "source_deep_dive",
    "USER_DEEP_DIVE_REVIEW",
}


class SynthesizerClaim(BaseModel):
    claim_id: StrictStr
    text: StrictStr
    claim_type: StrictStr
    n_sources_supporting: int | None
    page_anchor: StrictStr | None

    @validator("claim_id", "text")
    def _required_text_must_have_content(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("field must be non-empty")
        return cleaned

    @validator("claim_type")
    def _claim_type_must_match_taxonomy(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if cleaned not in CLAIM_TYPE_SET:
            raise ValueError("claim_type must match the Synthesizer taxonomy")
        return cleaned

    class Config:
        extra = "ignore"


class SynthesizerSourceNote(BaseModel):
    source_id: StrictStr
    thesis: StrictStr
    method: StrictStr
    evidence: StrictStr
    limits: StrictStr
    claims: list[SynthesizerClaim]

    @validator("source_id", "thesis", "method", "evidence", "limits")
    def _text_must_have_content(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("field must be non-empty")
        return cleaned

    class Config:
        extra = "ignore"


def run_synthesizer(
    run_id: str,
    db_session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run the synthesizer.

    ``prompt_overrides`` is the resolved override map from the rerun
    endpoint (codex-AGREEd #2 stage 2.B). Stage 2.B uses
    ``prompt_overrides["main"]`` as the static instruction block;
    other keys are reserved for future surfaces.

    ``lock_token`` (Stage 3.E follow-up P0): owner-checked phase-start
    lock release at exit.

    PR-A4.1b (2026-05-02): wraps the runner in
    ``maybe_run_with_versioning`` so vanilla first runs create a
    pv row + run_head + lineage.
    """
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_synthesizer_with_session(
                run_id,
                session,
                hooks or HookRegistry(),
                prompt_overrides=prompt_overrides,
            )

        maybe_run_with_versioning(session, run, "synthesizer", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "synthesizer", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def load_synthesis_payload(run: Run) -> dict[str, object]:
    synthesis_dir = Path(run.run_dir) / "synthesis"
    diagnostic = _load_json_mapping(synthesis_dir / "material_diagnostic.json")
    # PR-C1.b: surface the dual-track artifact when it exists. None
    # for legacy runs that completed synthesizer before C1.a.
    dual_track_path = synthesis_dir / "synthesizer.json"
    dual_track = None
    if dual_track_path.exists():
        loaded = _load_json_mapping(dual_track_path)
        if loaded:
            dual_track = loaded
    return {
        "run_id": run.id,
        "claims": _load_jsonl_objects(synthesis_dir / "claims.jsonl"),
        "source_notes": _load_source_notes(synthesis_dir / "source_notes"),
        "synthesizer_report": _read_optional_text(synthesis_dir / "synthesizer_report.md"),
        "material_diagnostic": diagnostic if diagnostic else None,
        "material_diagnostic_md": _read_optional_text(
            synthesis_dir / "material_diagnostic.md",
        ),
        "dual_track": dual_track,
    }


def _run_synthesizer_with_session(
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
    if run.state != "USER_DEEP_DIVE_REVIEW":
        raise InvalidTransition(f"Synthesizer requires USER_DEEP_DIVE_REVIEW, got {run.state}")
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    transition(run, "SYNTHESIZER_RUNNING", session, reason="Synthesizer started")
    append_event(session, run, "phase_started", {"phase": "synthesizer", "run_id": run.id})
    session.commit()
    session.refresh(run)

    domain = load_domain(_domain_path(project.domain_id))
    run_dir = Path(run.run_dir)
    synthesis_dir = run_dir / "synthesis"
    source_notes_dir = synthesis_dir / "source_notes"
    source_notes_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    shortlist = _read_sources_json(run_dir / "sources" / "shortlist.json")
    manifest = _load_manifest(run_dir / "sources" / "fulltext_manifest.json")
    proposal = _proposal_context(run)
    selected_sources = _select_sources(
        session,
        run.id,
        shortlist,
        limit=_deep_dive_limit(domain.data),
    )
    if not selected_sources:
        guidance = (
            "No sources were selected for Synthesizer. Approve at least one deep-dive source."
        )
        _write_jsonl(synthesis_dir / "claims.jsonl", [])
        _write_report(
            synthesis_dir / "synthesizer_report.md",
            selected_count=0,
            processed_count=0,
            claims=[],
            poor_extraction_warnings=[],
            warnings=[],
            guidance=guidance,
        )
        return _fail_fixable(
            run,
            session,
            guidance,
            selected_count=0,
            processed_count=0,
            per_source_warnings=[],
        )

    warnings: list[dict[str, object]] = []
    poor_extraction_warnings: list[dict[str, object]] = []
    claims: list[dict[str, object]] = []
    processed_count = 0
    extraction_failures = 0
    skipped_before_llm_count = 0
    total = len(selected_sources)
    use_harness = not settings.synthesizer_stub
    if use_harness:
        _register_synthesizer_memory_hook(hooks)

    for completed, source in enumerate(selected_sources, start=1):
        source_text, extraction_warning = _source_text(
            run_dir,
            source,
            manifest,
            skip_on_poor_extraction=use_harness,
        )
        if extraction_warning is not None:
            extraction_failures += 1
            poor_extraction_warnings.append(extraction_warning)
            warnings.append(extraction_warning)
        if source_text is None:
            skipped_before_llm_count += 1
            if extraction_warning is None:
                warning: dict[str, object] = {
                    "source_id": source.source_id,
                    "failure_class": "fixable_deterministic",
                    "message": "No PDF text or abstract was available for synthesis.",
                }
                warnings.append(warning)
            status = "skipped"
            source_claims: list[dict[str, object]] = []
        else:
            summary = _summarize_source(
                source=source,
                source_text=source_text,
                domain_data=domain.data,
                project_title=project.title,
                proposal=proposal,
                run=run,
                project=project,
                session=session,
                hooks=hooks,
                source_index=completed,
                source_count=total,
                skipped_before_llm_count=skipped_before_llm_count,
                instructions_override=(prompt_overrides.get("main") if prompt_overrides else None),
            )
            if summary is None:
                warnings.append(
                    {
                        "source_id": source.source_id,
                        "failure_class": "fixable_prompt",
                        "message": "Synthesizer summary JSON did not parse after retry.",
                    },
                )
                status = "skipped"
                source_claims = []
            else:
                processed_count += 1
                _write_json(source_notes_dir / f"{_safe_filename(source.source_id)}.json", summary)
                source_claims = _claim_records(summary)
                claims.extend(source_claims)
                status = "summarized"

        append_event(
            session,
            run,
            "source_progress",
            {
                "phase": "synthesizer",
                "source_id": source.source_id,
                "status": status,
                "claims": len(source_claims),
                "completed": completed,
                "total": total,
            },
        )
        session.commit()

    _write_jsonl(synthesis_dir / "claims.jsonl", claims)

    # PR-C1.a dual-track output. Partition claims by their source's
    # research_role and write the new ``synthesis/synthesizer.json``
    # artifact alongside the legacy ``claims.jsonl``. Primary-track
    # claims (research_role=primary_source) also land in
    # ``synthesis/evidence_ledger.jsonl`` for the audit trail. The
    # legacy artifacts (claims.jsonl, source_notes/*) are still
    # written verbatim so existing downstream code (drafter,
    # /api/runs/{id}/synthesis) keeps working unchanged.
    _write_dual_track_synthesizer(
        run_dir=run_dir,
        shortlist=shortlist,
        claims=claims,
    )

    # Hard threshold: a configurable minimum (default 3) of sources must
    # actually be processed by the LLM with real text. Below that, the
    # Drafter has nothing to cite and any output is necessarily
    # boilerplate or hallucinated. Override via
    # AUTOESSAY_SYNTHESIZER_MIN_PROCESSED_SOURCES (tests set this to 0).
    min_required = settings.synthesizer_min_processed_sources
    if min_required > 0 and processed_count < min_required:
        guidance = (
            f"Only {processed_count} of {total} selected sources produced "
            f"real synthesizer notes (minimum required: {min_required}). "
            "The Drafter would have to write claims without supporting "
            "material. Upload more PDFs, broaden the shortlist at "
            "USER_DEEP_DIVE_REVIEW, or refine the topic."
        )
        _write_report(
            synthesis_dir / "synthesizer_report.md",
            selected_count=total,
            processed_count=processed_count,
            claims=claims,
            poor_extraction_warnings=poor_extraction_warnings,
            warnings=warnings,
            guidance=guidance,
        )
        return _fail_fixable(
            run,
            session,
            guidance,
            selected_count=total,
            processed_count=processed_count,
            per_source_warnings=warnings,
        )
    if extraction_failures > (total / 2):
        guidance = (
            "More than half of selected deep-dive PDFs had poor text extraction. "
            "Upload better PDFs or select sources with readable full text."
        )
        _write_report(
            synthesis_dir / "synthesizer_report.md",
            selected_count=total,
            processed_count=processed_count,
            claims=claims,
            poor_extraction_warnings=poor_extraction_warnings,
            warnings=warnings,
            guidance=guidance,
        )
        return _fail_fixable(
            run,
            session,
            guidance,
            selected_count=total,
            processed_count=processed_count,
            per_source_warnings=warnings,
        )

    _write_report(
        synthesis_dir / "synthesizer_report.md",
        selected_count=total,
        processed_count=processed_count,
        claims=claims,
        poor_extraction_warnings=poor_extraction_warnings,
        warnings=warnings,
        guidance=None,
    )

    project_language = getattr(project, "language", None) or "en"
    source_notes = _load_source_notes(source_notes_dir)
    diagnostic = run_material_diagnostic(
        run=run,
        session=session,
        project_title=project.title,
        project_language=project_language,
        source_notes={sid: note for sid, note in source_notes.items() if isinstance(note, Mapping)},
        claims=claims,
        proposal=proposal,
    )
    _write_text(
        synthesis_dir / "material_diagnostic.md",
        render_material_diagnostic_markdown(diagnostic, project_language),
    )
    _write_json(synthesis_dir / "material_diagnostic.json", diagnostic_to_dict(diagnostic))

    summary = {
        "phase": "synthesizer",
        "sources_selected": total,
        "sources_processed": processed_count,
        "claims": len(claims),
        "warnings": len(warnings),
        "material_diagnostic": {
            "sufficient": diagnostic.sufficient,
            "recommended_action": diagnostic.recommended_action,
        },
    }
    transition(run, "USER_FIELD_REVIEW", session, reason="Synthesizer completed", payload=summary)
    append_event(session, run, "phase_done", summary)
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


def _fail_fixable(
    run: Run,
    session: Session,
    guidance: str,
    *,
    selected_count: int,
    processed_count: int,
    per_source_warnings: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """PR-J5: surface per-source skip reasons in the FAILED_FIXABLE
    payload so the FailureResolutionBanner can show users WHICH sources
    failed and WHY (no PDF + no abstract / PoorExtraction / LLM parse
    fail), not just a generic "upload more PDFs / broaden / refine"
    suggestion. The warnings list already carries `failure_class`
    classification (``fixable_deterministic`` = pre-LLM no-text /
    ``fixable_prompt`` = LLM parse fail / PoorExtraction-prefixed
    message); J5 just exposes it on the wire.

    The breakdown is bounded to ``_PER_SOURCE_WARNING_LIMIT`` items
    (default 24 — matches the curator shortlist cap; UI doesn't need
    more). Each surfaced warning carries `source_id` + `failure_class`
    + `message[:280]` (truncated so a single bad warning doesn't
    bloat the SSE payload)."""
    surfaced: list[dict[str, object]] = []
    for warning in per_source_warnings[:_PER_SOURCE_WARNING_LIMIT]:
        if not isinstance(warning, Mapping):
            continue
        surfaced.append(
            {
                "source_id": str(warning.get("source_id", "")),
                "failure_class": str(warning.get("failure_class", "")),
                "message": str(warning.get("message", ""))[:280],
            }
        )
    payload: dict[str, object] = {
        "guidance": guidance,
        "selected_count": selected_count,
        "processed_count": processed_count,
        "per_source_warnings": surfaced,
        "per_source_warning_total": len(per_source_warnings),
    }
    transition(
        run,
        "FAILED_FIXABLE",
        session,
        reason="Synthesizer needs user-fixable input",
        payload=payload,
    )
    append_event(
        session,
        run,
        "phase_failed",
        {
            "phase": "synthesizer",
            "failure_class": "failed_fixable",
            **payload,
        },
    )
    session.commit()
    return {
        "run_id": run.id,
        "state": run.state,
        "sources_selected": selected_count,
        "sources_processed": processed_count,
        "guidance": guidance,
        "per_source_warnings": surfaced,
        "per_source_warning_total": len(per_source_warnings),
    }


_PER_SOURCE_WARNING_LIMIT = 24


def _source_text(
    run_dir: Path,
    source: NormalizedSource,
    manifest: Mapping[str, Mapping[str, object]],
    *,
    skip_on_poor_extraction: bool = False,
) -> tuple[str | None, dict[str, object] | None]:
    entry = manifest.get(source.source_id)
    if entry is not None:
        raw_path = entry.get("pdf_path")
        if isinstance(raw_path, str) and raw_path:
            try:
                pdf_bytes = _resolve_run_path(run_dir, raw_path).read_bytes()
                return pdf_text.extract_text(pdf_bytes, source_id=source.source_id), None
            except (OSError, pdf_text.PoorExtraction) as exc:
                warning: dict[str, object] = {
                    "source_id": source.source_id,
                    "failure_class": "fixable_deterministic",
                    "message": f"PoorExtraction: {exc}",
                }
                if skip_on_poor_extraction:
                    return None, warning
                if source.abstract:
                    return source.abstract, warning
                return None, warning

    if source.abstract:
        return source.abstract, None
    metadata_text = _verified_metadata_source_text(source)
    if metadata_text is not None:
        return metadata_text, None
    return None, None


def _verified_metadata_source_text(source: NormalizedSource) -> str | None:
    """Conservative fallback for verified books with no PDF/abstract.

    Humanities kernels often rely on canonical monographs that external
    metadata services verify but do not expose as OA text. Treat them as
    bibliographic-positioning material only: the prompt receives title,
    authors, venue, DOI, verifier, and canonical rationale, plus an
    explicit instruction not to infer substantive findings.
    """
    status = source.verification_status
    status_value = status.value if hasattr(status, "value") else str(status)
    if status_value != VerificationStatus.VERIFIED.value:
        return None
    if source.provenance != "llm_canon" and not source.verified_by:
        return None
    rationale = (source.canonical_rationale or "").strip()
    if not rationale and not source.doi:
        return None

    parts = [
        "VERIFIED BIBLIOGRAPHIC METADATA ONLY.",
        "No PDF text or abstract is available for this source.",
        "Use this record only for literature positioning, chronology, scope, "
        "and citation authenticity. Do not infer substantive arguments or "
        "empirical findings beyond the metadata and rationale below.",
        f"Title: {source.title}",
        f"Authors: {', '.join(source.authors) if source.authors else 'unknown'}",
        f"Year: {source.year if source.year is not None else 'unknown'}",
        f"Venue/Publisher: {source.venue or 'unknown'}",
        f"DOI/URL: {source.doi or source.url or 'unknown'}",
        f"Verified by: {source.verified_by or source.source_client}",
    ]
    if source.canonical_bucket:
        parts.append(f"Canonical bucket: {source.canonical_bucket}")
    if rationale:
        parts.append(f"Canonical rationale: {rationale}")
    return "\n".join(parts)


def _summarize_source(
    *,
    source: NormalizedSource,
    source_text: str,
    domain_data: Mapping[str, Any],
    project_title: str,
    proposal: Mapping[str, object] | None,
    run: Run | None = None,
    project: Project | None = None,
    session: Session | None = None,
    hooks: HookRegistry | None = None,
    source_index: int = 1,
    source_count: int = 1,
    skipped_before_llm_count: int = 0,
    instructions_override: str | None = None,
) -> dict[str, object] | None:
    if get_settings().synthesizer_stub:
        return _stub_summary(source, source_text)
    if run is None or project is None or session is None:
        raise ValueError("Synthesizer summary requires run, project, and session")
    try:
        return _synthesizer_via_harness(
            source=source,
            source_text=source_text,
            domain_data=domain_data,
            project_title=project_title,
            proposal=proposal,
            run=run,
            project=project,
            session=session,
            hooks=hooks or HookRegistry(),
            source_index=source_index,
            source_count=source_count,
            skipped_before_llm_count=skipped_before_llm_count,
            instructions_override=instructions_override,
        )
    except SchemaViolationError:
        return None
    except Exception:  # noqa: BLE001 - caller records a source-level warning.
        return None


def _synthesizer_via_harness(
    *,
    source: NormalizedSource,
    source_text: str,
    domain_data: Mapping[str, Any],
    project_title: str,
    proposal: Mapping[str, object] | None,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    source_index: int,
    source_count: int,
    skipped_before_llm_count: int,
    instructions_override: str | None = None,
) -> dict[str, object] | None:
    from autoessay.agents._research_kernel_prompt import (
        KERNEL_INJECTION_GUARD,
        research_kernel_for_prompt,
    )
    from autoessay.agents._shadow_knowledge_injection import (
        shadow_knowledge_directive_for_run,
    )

    research_kernel = research_kernel_for_prompt(
        getattr(run, "research_kernel_json", None),
    )
    # PR-263e: synthesizer mirrors PR-263c drafter wiring — load the
    # shadow_baseline artifact (if present) and pass its compact
    # argument_map + reference_candidates as a "mention but don't
    # cite" directive. Empty string when no artifact exists.
    shadow_knowledge_directive = shadow_knowledge_directive_for_run(run.run_dir)
    prompt = _summary_prompt(
        source=source,
        source_text=source_text,
        domain_data=domain_data,
        project_title=project_title,
        proposal=proposal,
        suffix="",
        instructions_override=instructions_override,
        research_kernel=research_kernel,
        shadow_knowledge_directive=shadow_knowledge_directive,
    )
    request_id = f"synthesizer_source_note_{_safe_filename(source.source_id)}"
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Synthesizer. Extract source-bound summaries and claims "
                    "relevant to the user's research_kernel. Prioritize claims that "
                    "connect to the kernel's tentative_question or observed_puzzle "
                    "when the source text supports them. "
                    + KERNEL_INJECTION_GUARD
                    + " Return one strict JSON object. Do not infer claims "
                    "unsupported by the supplied source text. "
                    + language_directive(project.language)
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.0,
        max_tokens=1400,
        response_format={"type": "json_object"},
        request_id=request_id,
        prompt_template_id="synthesizer.source_note.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="synthesis",
        step_id="synthesizer.source_note",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project_title,
        run_metadata={
            "agent_phase": "synthesizer",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "source_id": source.source_id,
            "source_index": source_index,
            "source_count": source_count,
            "skip_count": skipped_before_llm_count,
            "memory_query": f"phase=synthesizer topic={project_title} source_count={source_count}",
        },
    )
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Synthesizer")
    response = asyncio.run(
        run_llm_step(
            request=request,
            hooks=hooks,
            context=context,
            output_schema=SynthesizerSourceNote,
            audit=audit,
            max_corrective_retries=2,
            llm_optional=False,
        ),
    )
    return _source_note_from_output(response.parsed, source.source_id)


def _source_note_from_output(parsed: object, expected_source_id: str) -> dict[str, object] | None:
    if isinstance(parsed, SynthesizerSourceNote):
        return _normalize_summary(parsed.dict(), expected_source_id)
    if isinstance(parsed, Mapping):
        return _normalize_summary(parsed, expected_source_id)
    return None


def _register_synthesizer_memory_hook(hooks: HookRegistry) -> None:
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


def _summary_prompt(
    *,
    source: NormalizedSource,
    source_text: str,
    domain_data: Mapping[str, Any],
    project_title: str,
    proposal: Mapping[str, object] | None,
    suffix: str,
    instructions_override: str | None = None,
    research_kernel: Mapping[str, object] | None = None,
    shadow_knowledge_directive: str = "",
    accumulated_context: str = "",
) -> str:
    """Build the synthesizer's per-source LLM prompt.

    When ``instructions_override`` is set the user-supplied static
    instruction block replaces the baked-in default. The dynamic
    context (sources, domain, proposal, question, schema spec) is
    always appended verbatim — overriding it would break schema
    parsing or starve the LLM of the source text.

    ``shadow_knowledge_directive`` is the compact argument_map +
    reference_candidates block from PR-263c (PR-263e: now consumed
    by synthesizer too). Empty string ⇒ no shadow_baseline artifact
    on disk; appended verbatim with the same "mention but don't
    cite" policy line. Synthesizer treats this as background
    context for shaping its source notes — it doesn't inject the
    directive into ``claims`` or ``cited_sources``; that mapping
    happens later in drafter.
    """
    from autoessay.prompts import SYNTHESIZER_MAIN_INSTRUCTIONS

    instructions = instructions_override or SYNTHESIZER_MAIN_INSTRUCTIONS
    source_notes = {
        "source_id": source.source_id,
        "title": source.title,
        "authors": source.authors,
        "year": source.year,
        "venue": source.venue,
        "abstract": source.abstract,
        "text": _truncate(source_text, LLM_TEXT_CHAR_LIMIT),
    }
    domain_summary = _domain_summary(domain_data)
    proposal_summary = _proposal_summary(proposal)
    required_schema = {
        "source_id": source.source_id,
        "thesis": "1-2 sentence statement of the paper's main argument",
        "method": "string",
        "evidence": "string",
        "limits": "string",
        "claims": [
            {
                "claim_id": "uuid",
                "text": "string",
                "claim_type": "consensus|debate|finding|method|limit",
                "n_sources_supporting": "int|null",
                "page_anchor": "p.NN|null",
            },
        ],
    }
    # PR-J7: research_kernel is the user-authored anchor; outranks
    # proposal (LLM-generated) and domain templates. Empty dict on
    # missing kernel (degrade to title-only anchoring).
    kernel_payload = dict(research_kernel) if research_kernel else {}
    user_anchor = json.dumps(
        {"project_title": project_title, "research_kernel": kernel_payload},
        ensure_ascii=False,
        sort_keys=True,
    )
    return (
        f"{instructions} "
        f"User anchor: {user_anchor}. "
        f"{accumulated_context}"
        f"Sources: {json.dumps(source_notes, sort_keys=True)}. "
        f"Domain: {json.dumps(domain_summary, sort_keys=True)}. "
        f"Proposal: {json.dumps(proposal_summary, sort_keys=True)}. "
        f"Question: {project_title}. "
        "The output must match this required schema exactly: "
        f"{json.dumps(required_schema, sort_keys=True)}"
        f"{shadow_knowledge_directive}"
        f"{suffix}"
    )


def _stub_summary(source: NormalizedSource, source_text: str) -> dict[str, object]:
    text_basis = "PDF text" if source_text.startswith("STUB-EXTRACTED-TEXT") else "abstract"
    page_anchor = "p.1" if text_basis == "PDF text" else None
    return {
        "source_id": source.source_id,
        "title": source.title,
        "thesis": f"{source.title} links financial-history evidence to the run topic.",
        "method": "Stub Synthesizer notes a qualitative source-reading method.",
        "evidence": f"Stub evidence is based on {text_basis} for {source.source_id}.",
        "limits": "Stub output is not a full field map and should not be treated as consensus.",
        "claims": [
            {
                "claim_id": str(uuid4()),
                "text": f"{source.title} reports a source-bound finding relevant to the topic.",
                "claim_type": "finding",
                "n_sources_supporting": 1,
                "page_anchor": page_anchor,
            },
            {
                "claim_id": str(uuid4()),
                "text": f"{source.source_id} identifies a methodological limit for interpretation.",
                "claim_type": "limit",
                "n_sources_supporting": 1,
                "page_anchor": page_anchor,
            },
        ],
    }


def _parse_summary_response(value: str, expected_source_id: str) -> dict[str, object] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return _normalize_summary(decoded, expected_source_id)


def _normalize_summary(
    payload: Mapping[str, object],
    expected_source_id: str,
) -> dict[str, object] | None:
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        return None

    claims: list[dict[str, object]] = []
    for raw_claim in raw_claims:
        if not isinstance(raw_claim, dict):
            continue
        text = _string_or_none(raw_claim.get("text"))
        if text is None:
            continue
        claim_type = _string_or_none(raw_claim.get("claim_type")) or "finding"
        if claim_type not in CLAIM_TYPE_SET:
            claim_type = "finding"
        claim_id = _string_or_none(raw_claim.get("claim_id")) or str(uuid4())
        n_sources_supporting = raw_claim.get("n_sources_supporting")
        page_anchor = _page_anchor(raw_claim.get("page_anchor"))
        claims.append(
            {
                "claim_id": claim_id,
                "text": text,
                "claim_type": claim_type,
                "n_sources_supporting": (
                    n_sources_supporting if isinstance(n_sources_supporting, int) else None
                ),
                "page_anchor": page_anchor,
            },
        )

    return {
        "source_id": expected_source_id,
        "thesis": _string_or_none(payload.get("thesis")) or "",
        "method": _string_or_none(payload.get("method")) or "",
        "evidence": _string_or_none(payload.get("evidence")) or "",
        "limits": _string_or_none(payload.get("limits")) or "",
        "claims": claims,
    }


def _coerce_float(value: object, default: float) -> float:
    """Best-effort coercion for ledger confidence values that may
    arrive as int / str / None from upstream JSON."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _research_role_of(
    source: NormalizedSource | object,
) -> str:
    """Reads ``research_role`` off a NormalizedSource (PR-C1.a
    field) or any object exposing the attribute. Defaults to
    ``secondary_argument`` when the attribute is missing —
    matches the PR-C1.a default for backfilled rows.
    """
    role = getattr(source, "research_role", None)
    if isinstance(role, str) and role:
        return role
    return "secondary_argument"


def _write_dual_track_synthesizer(
    *,
    run_dir: Path,
    shortlist: Sequence[NormalizedSource],
    claims: Sequence[Mapping[str, object]],
) -> None:
    """PR-C1.a: emit ``synthesis/synthesizer.json`` partitioning
    claims by source ``research_role`` plus append primary-track
    claims to ``synthesis/evidence_ledger.jsonl``.

    ``synthesizer.json`` schema (kernel_schema_version = 1):

      {
        "schema_version": 1,
        "primary_track": [{"source_id", "claim_id", "text",
                           "claim_type", "n_sources_supporting",
                           "page_anchor"}, ...],
        "secondary_track": [<same shape>],
        "theoretical_lens_track": [<same shape>],
        "methodological_track": [<same shape>],
        "tension_summary_ref": null,         // PR-C3 will fill
      }

    Empty tracks are kept as empty arrays so the frontend renderer
    can rely on the keys' presence.
    """
    from autoessay.evidence_ledger import (
        append_rows,
        claim_row,
        ensure_synthesis_dir,
    )

    role_by_source: dict[str, str] = {s.source_id: _research_role_of(s) for s in shortlist}

    primary_track: list[dict[str, object]] = []
    secondary_track: list[dict[str, object]] = []
    theoretical_track: list[dict[str, object]] = []
    methodological_track: list[dict[str, object]] = []

    for claim in claims:
        source_id = str(claim.get("source_id") or "")
        role = role_by_source.get(source_id, "secondary_argument")
        record = dict(claim)
        if role == "primary_source":
            primary_track.append(record)
        elif role == "theoretical_lens":
            theoretical_track.append(record)
        elif role == "methodological_reference":
            methodological_track.append(record)
        else:
            secondary_track.append(record)

    ensure_synthesis_dir(run_dir)
    payload: dict[str, object] = {
        "schema_version": 1,
        "primary_track": primary_track,
        "secondary_track": secondary_track,
        "theoretical_lens_track": theoretical_track,
        "methodological_track": methodological_track,
        "tension_summary_ref": None,
    }
    _write_json(run_dir / "synthesis" / "synthesizer.json", payload)

    # Mirror primary-track claims into the evidence ledger. Each
    # row is idempotent by claim_id (sha256 of source_id, text,
    # citation_target). Re-running the synthesizer with the same
    # extracted claims is a no-op against the ledger.
    if primary_track:
        ledger_rows = [
            claim_row(
                source_id=str(c.get("source_id") or ""),
                claim_text=str(c.get("text") or ""),
                citation_target=str(c.get("citation_target") or c.get("source_id") or ""),
                confidence=_coerce_float(c.get("confidence"), 0.5),
                extra={
                    "claim_type": str(c.get("claim_type") or "finding"),
                    "synthesizer_claim_id": str(c.get("claim_id") or ""),
                },
            )
            for c in primary_track
        ]
        append_rows(run_dir, ledger_rows)


def _claim_records(summary: Mapping[str, object]) -> list[dict[str, object]]:
    source_id = str(summary["source_id"])
    raw_claims = summary.get("claims")
    if not isinstance(raw_claims, list):
        return []
    records: list[dict[str, object]] = []
    for raw_claim in raw_claims:
        if not isinstance(raw_claim, dict):
            continue
        records.append(
            {
                "source_id": source_id,
                "claim_id": str(raw_claim.get("claim_id") or uuid4()),
                "text": str(raw_claim.get("text") or ""),
                "claim_type": str(raw_claim.get("claim_type") or "finding"),
                "n_sources_supporting": raw_claim.get("n_sources_supporting"),
                "page_anchor": _page_anchor(raw_claim.get("page_anchor")),
            },
        )
    return records


def _select_sources(
    session: Session,
    run_id: str,
    shortlist: Sequence[NormalizedSource],
    *,
    limit: int,
) -> list[NormalizedSource]:
    approved_ids = _approved_source_ids(session, run_id)
    if approved_ids is not None:
        by_id = {source.source_id: source for source in shortlist}
        return [by_id[source_id] for source_id in approved_ids if source_id in by_id]
    return sorted(shortlist, key=lambda source: source.rank_score, reverse=True)[:limit]


def _approved_source_ids(session: Session, run_id: str) -> list[str] | None:
    checkpoints = list(
        session.scalars(
            select(Checkpoint)
            .where(Checkpoint.run_id == run_id)
            .order_by(Checkpoint.created_at.desc()),
        ),
    )
    for checkpoint in checkpoints:
        if checkpoint.checkpoint_type not in DEEP_DIVE_CHECKPOINT_TYPES:
            continue
        if checkpoint.status != "ACCEPTED":
            continue
        source_ids = _source_ids_from_json(checkpoint.decision_payload)
        if source_ids is not None:
            return source_ids
    return None


def _source_ids_from_json(value: str) -> list[str] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return _source_ids_from_payload(decoded)


def _source_ids_from_payload(payload: object) -> list[str] | None:
    if isinstance(payload, list):
        return _string_list(payload)
    if not isinstance(payload, Mapping):
        return None
    for key in (
        "source_ids",
        "approved_source_ids",
        "approved_sources",
        "approved",
        "deep_dive_source_ids",
        "selected_source_ids",
        "selection",
    ):
        raw_ids = payload.get(key)
        if isinstance(raw_ids, list):
            return _string_list(raw_ids)
    return None


def _string_list(items: Sequence[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _deep_dive_limit(domain_data: Mapping[str, Any]) -> int:
    """Resolution chain (highest priority first):

    1. ``AUTOESSAY_SYNTHESIZER_DEEP_DIVE_LIMIT`` env override
       (``Settings.synthesizer_deep_dive_limit``); set when an
       operator wants to override every domain at once.
    2. Per-domain ``search.telescope.deep_dive_limit`` from
       ``domains/*.yaml``.
    3. ``DEFAULT_DEEP_DIVE_LIMIT`` (14, raised from 6 in
       PR-G-Sources Stage 1).
    """
    settings = get_settings()
    env_override = settings.synthesizer_deep_dive_limit
    if env_override is not None and env_override > 0:
        return env_override
    search = domain_data.get("search", {})
    if isinstance(search, dict):
        telescope = search.get("telescope", {})
        if isinstance(telescope, dict):
            limit = telescope.get("deep_dive_limit")
            if isinstance(limit, int) and limit > 0:
                return limit
    return DEFAULT_DEEP_DIVE_LIMIT


def _domain_summary(domain_data: Mapping[str, Any]) -> dict[str, object]:
    return {
        "id": domain_data.get("id"),
        "display_name": domain_data.get("display_name"),
        "terms": domain_data.get("terms", {}),
        "evidence": domain_data.get("evidence", {}),
        "journals": domain_data.get("journals", {}),
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
        "preliminary_keywords": proposal.get("preliminary_keywords"),
        "scope": proposal.get("scope"),
    }


def _write_report(
    path: Path,
    *,
    selected_count: int,
    processed_count: int,
    claims: Sequence[Mapping[str, object]],
    poor_extraction_warnings: Sequence[Mapping[str, object]],
    warnings: Sequence[Mapping[str, object]],
    guidance: str | None,
) -> None:
    counts = Counter(str(claim.get("claim_type") or "finding") for claim in claims)
    lines = [
        "# Synthesizer Report",
        "",
        f"- Sources selected: {selected_count}",
        f"- Sources processed: {processed_count}",
        f"- Claims total: {len(claims)}",
        f"- Warnings: {len(warnings)}",
        "",
        "## Claims by type",
        "",
    ]
    for claim_type in CLAIM_TYPES:
        lines.append(f"- {claim_type}: {counts.get(claim_type, 0)}")

    lines.extend(["", "## PoorExtraction warnings", ""])
    if poor_extraction_warnings:
        for warning in poor_extraction_warnings:
            lines.append(f"- {warning.get('source_id')}: {warning.get('message')}")
    else:
        lines.append("- none")

    if guidance:
        lines.extend(["", "## Guidance", "", guidance])
    _write_text(path, "\n".join(lines) + "\n")


def _read_sources_json(path: Path) -> list[NormalizedSource]:
    records = _load_json_array(path)
    sources: list[NormalizedSource] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            sources.append(NormalizedSource.parse_obj(record))
        except ValidationError:
            continue
    return sources


def _load_manifest(path: Path) -> dict[str, dict[str, object]]:
    manifest: dict[str, dict[str, object]] = {}
    for key, value in _load_json_mapping(path).items():
        if isinstance(value, dict):
            manifest[key] = dict(value)
    return manifest


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
    if not path.exists():
        return []
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


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


def _write_jsonl(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n",
            )
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _resolve_run_path(run_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = run_dir / path
    resolved = path.resolve()
    run_root = run_dir.resolve()
    if not resolved.is_relative_to(run_root):
        raise FileNotFoundError(raw_path)
    return resolved


def _domain_path(domain_id: str) -> Path:
    settings = get_settings()
    path = settings.domain_dir / f"{domain_id}.yaml"
    if path.exists():
        return path
    return Path(__file__).resolve().parents[4] / "domains" / f"{domain_id}.yaml"


def _safe_filename(value: str) -> str:
    cleaned = value.replace("/", "-").replace("\\", "-").strip()
    return cleaned or "source"


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars]


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _page_anchor(value: object) -> str | None:
    text = _string_or_none(value)
    if text is None or text.lower() == "null":
        return None
    return text
