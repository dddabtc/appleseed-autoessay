"""Curator agent entrypoint for source ranking and local PDF curation."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, StrictStr, ValidationError, root_validator, validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents import research_role_classifier
from autoessay.agents._language import language_directive
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.clients.fulltext_resolver import (
    FulltextResolution,
    FulltextResolutionCandidate,
    FulltextResolutionError,
    resolve_fulltext_pdf_url,
)
from autoessay.clients.pdf_fetcher import OpenAccessUnavailable, fetch_pdf, sha256_bytes
from autoessay.config import Settings, get_settings
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
from autoessay.models import Checkpoint, Project, Run, utcnow
from autoessay.prompts import CURATOR_RANKING_INSTRUCTIONS
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

DEFAULT_SHORTLIST_LIMIT = 24
DEFAULT_MAX_PDF_MB = 30
RELEVANCE_BATCH_SIZE = 8
DIVERSITY_VENUE_CAP = 0.30
DIVERSITY_AUTHOR_CAP = 2
SEARCH_REVIEW_CHECKPOINT_TYPES = {
    "search-review",
    "search_review",
    "source-review",
    "source_review",
    "USER_SEARCH_REVIEW",
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoreResult:
    relevance_scores: dict[str, float]
    warnings: list[dict[str, object]]
    fallback_recency_only: bool
    # PR-J9b: per-source 4-axis breakdown (scope_fit / relevance /
    # impact / frontier_currency). Empty dict when stub on, when
    # 4-axis path fell back to legacy single-axis, or when fully
    # falling back to recency-only. ``_rank_sources`` consults this to
    # apply the 0.85*rerank + 0.15*legacy blend + hard penalty.
    rerank_axes: dict[str, dict[str, float]] = field(default_factory=dict)
    # PR-J9b: per-source LLM rationale (≤200 chars).
    rerank_rationales: dict[str, str] = field(default_factory=dict)
    # PR-J9b: per-source ``retain_decision`` from the rerank LLM. If
    # False, the source gets the deterministic hard-penalty cap in
    # ``_rank_sources`` (codex round-1 A3 + A4).
    rerank_retain: dict[str, bool] = field(default_factory=dict)
    # PR-J9b: True when the 4-axis rerank ran cleanly; False when
    # ``curator_rerank_stub`` is on or when the 4-axis path fell back
    # to legacy single-axis after a transport / schema error. Used by
    # the milestone-tag acceptance check (no rerank_fallback events).
    rerank_active: bool = False


@dataclass(frozen=True)
class DiversityResult:
    selected: list[NormalizedSource]
    runner_ups: list[dict[str, object]]


class CuratorRankedSource(BaseModel):
    """PR-J9b: 4-axis rerank schema. Replaced legacy single-axis
    (relevance / recency / venue_authority / diversity_bonus / rank_score
    asked-but-unused) with explicit scope_fit / impact / frontier_currency
    axes. ``relevance`` retained as one of the 4 axes (semantic match to
    research_kernel). The deterministic ``recency`` / ``venue_authority``
    / ``diversity_bonus`` signals are computed from source metadata in
    ``_rank_sources`` (J9b §3.2 legacy 15% blend), not asked from LLM.

    codex round-1 A2: rerank prompt does NOT receive provenance /
    canonical_bucket / verified_by / source_client (confirmation-bias
    prevention)."""

    source_id: StrictStr
    scope_fit: float
    relevance: float
    impact: float
    frontier_currency: float
    rationale: str = ""
    retain_decision: bool
    risk_flags: list[StrictStr]

    @root_validator(pre=True)
    def _accept_legacy_payload(cls, values: dict[str, object]) -> dict[str, object]:
        """Backward compat: legacy fixtures with relevance_score /
        recency / venue_authority / diversity_bonus / rank_score still
        parse — we drop unused fields and synthesize 4-axis defaults
        when only `relevance` is present (used by stub fixtures + the
        legacy single-axis fallback path). Tests also cover the strict
        4-axis prod payload."""
        values = dict(values)
        if "relevance" not in values and "relevance_score" in values:
            values["relevance"] = values["relevance_score"]
        # Synthesize missing axes from `relevance` (legacy fixtures).
        # Prod 4-axis path always sets all four explicitly; the strict
        # codex-A4 prompt rubric is enforced upstream by the prompt
        # text + risk_flags, not by the schema (we accept legacy mocks
        # so the synth fallback path keeps parsing).
        legacy_relevance = values.get("relevance")
        if isinstance(legacy_relevance, (int, float)):
            for axis in ("scope_fit", "impact", "frontier_currency"):
                values.setdefault(axis, float(legacy_relevance))
        return values

    @validator("source_id")
    def _source_id_must_have_content(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("source_id must be non-empty")
        return cleaned

    @validator("scope_fit", "relevance", "impact", "frontier_currency")
    def _axis_must_be_unit_interval(cls, value: float) -> float:
        if value < 0.0 or value > 1.0:
            raise ValueError("axis score must be between 0 and 1")
        return float(value)

    @validator("rationale", pre=True)
    def _normalize_rationale(cls, value: object) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if len(text) > 200:
            text = text[:200].rstrip()
        return text

    @validator("risk_flags")
    def _risk_flags_must_be_clean(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value)

    class Config:
        extra = "ignore"


class CuratorRanking(BaseModel):
    __root__: list[CuratorRankedSource]

    @validator("__root__")
    def _ranking_must_not_be_empty(
        cls,
        value: list[CuratorRankedSource],
    ) -> list[CuratorRankedSource]:
        if not value:
            raise ValueError("CuratorRanking must contain at least one source")
        return value


class _LegacyRelevanceResponse(BaseModel):
    """PR-I2 retro fix #1: permissive schema for the legacy single-axis
    Tier 2 fallback. Just requires ``scores`` to be parseable JSON; the
    actual extraction is delegated to ``_parse_relevance_response``,
    which already handles the lenient ``[{"source_id", "relevance"}]``
    OR ``{"scores": [{"source_id", "relevance_score"}]}`` shapes the
    legacy LLM prompt asks for. This schema exists solely because
    ``run_llm_step`` requires a non-None output_schema arg."""

    class Config:
        extra = "allow"

    scores: list[dict[str, object]] | None = None


# PR-J9b: 4-axis rerank weight policy. codex round-1 A3 caps legacy
# contribution at 15% (formula: ``final = 0.85*rerank + 0.15*legacy``).
# scope_fit gets the largest single weight because the dominant prod
# failure mode (RCEP 2025 outranking Amsden 1989) was scope mismatch.
RERANK_AXIS_WEIGHTS: dict[str, float] = {
    "scope_fit": 0.35,
    "relevance": 0.25,
    "impact": 0.25,
    "frontier_currency": 0.15,
}
RERANK_BLEND = 0.85
LEGACY_BLEND = 0.15
# Hard penalty: scope_fit < 0.30 OR retain_decision = False forces the
# final rank to no more than HARD_PENALTY_CAP. Codex round-1 A3 +
# A4 — without this, a 2025 RCEP article with scope_fit=0.2 + impact=0.9
# + frontier_currency=0.95 would still float to the top via the soft
# weighted sum. The cap is the only guarantee that scope-mismatched
# sources cannot reach shortlist's first page.
SCOPE_FIT_HARD_PENALTY_THRESHOLD = 0.30
HARD_PENALTY_CAP = 0.30


def run_curator(
    run_id: str,
    db_session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run the curator phase. ``prompt_overrides`` is the resolved
    override map from the rerun dispatcher (Stage 3.A.1). When set,
    ``prompt_overrides["ranking"]`` replaces the static instruction
    block in the system message of every relevance-batch LLM call;
    schema-binding sentences and ``language_directive`` stay outside
    the editable surface.

    ``lock_token`` (Stage 3.E follow-up P0): owner-checked phase-start
    lock release at exit.

    PR-A4.1b (2026-05-02): wraps the runner in
    ``maybe_run_with_versioning`` so vanilla first runs create a
    pv row + run_head + lineage. No-op when /rerun_phase already
    wrapped us."""
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_curator_with_session(
                run_id,
                session,
                hooks or HookRegistry(),
                prompt_overrides=prompt_overrides,
            )

        maybe_run_with_versioning(session, run, "curator", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "curator", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def _run_curator_with_session(
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
    if run.state != "USER_SEARCH_REVIEW":
        raise InvalidTransition(f"Curator requires USER_SEARCH_REVIEW, got {run.state}")
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    transition(run, "CURATOR_RUNNING", session, reason="Curator started")
    append_event(session, run, "phase_started", {"phase": "curator", "run_id": run.id})
    session.commit()
    session.refresh(run)

    domain = load_domain(_domain_path(project.domain_id))
    run_dir = Path(run.run_dir)
    sources_dir = run_dir / "sources"
    fulltext_dir = sources_dir / "fulltext"
    sources_dir.mkdir(parents=True, exist_ok=True)
    fulltext_dir.mkdir(parents=True, exist_ok=True)
    cache_prune = _prune_curator_replacement_cache(
        run_dir=run_dir,
        sources_dir=sources_dir,
        fulltext_dir=fulltext_dir,
    )
    if cache_prune["had_cache"]:
        append_event(session, run, "source_rerun_cache_pruned", cache_prune)
        session.commit()

    skim_candidates = _read_sources_jsonl(run_dir / "discovery" / "skim_candidates.jsonl")
    user_upload_sources = _merge_source_lists(
        _load_user_upload_sources(sources_dir / "shortlist.json"),
        _load_user_upload_sources(sources_dir / "user_upload_sources.json"),
    )
    if not skim_candidates and not user_upload_sources:
        guidance = "No skim candidates or manual uploads are available for curation."
        assert_run_active(run, session)
        _write_report(
            sources_dir / "curation_report.md",
            _report_payload(
                skimmed_in=0,
                approved=0,
                shortlisted=0,
                fetched=0,
                manual_required=0,
                rejected_by_diversity=0,
                policy_rejected=0,
                warnings=[],
            ),
            guidance=guidance,
        )
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="Curator had no sources to rank",
            payload={"guidance": guidance},
        )
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": "curator",
                "failure_class": "failed_fixable",
                "guidance": guidance,
            },
        )
        session.commit()
        return {"run_id": run.id, "state": run.state, "sources": 0, "guidance": guidance}

    approved_ids = _approved_source_ids(session, run.id)
    approved_sources = _filter_approved_sources(skim_candidates, approved_ids)
    approved_sources = _merge_source_lists(approved_sources, user_upload_sources)
    if approved_ids is not None and not approved_sources:
        guidance = (
            "Search review approved no sources that are available for curation. "
            "Approve at least one Scout candidate or upload a PDF."
        )
        assert_run_active(run, session)
        _write_report(
            sources_dir / "curation_report.md",
            _report_payload(
                skimmed_in=len(skim_candidates),
                approved=0,
                shortlisted=0,
                fetched=0,
                manual_required=0,
                rejected_by_diversity=0,
                policy_rejected=0,
                warnings=[],
            ),
            guidance=guidance,
        )
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="Curator had no approved search-review sources",
            payload={"guidance": guidance},
        )
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": "curator",
                "failure_class": "failed_fixable",
                "guidance": guidance,
            },
        )
        session.commit()
        return {"run_id": run.id, "state": run.state, "sources": 0, "guidance": guidance}

    policy = _literature_policy(domain.data)
    policy_sources, policy_rejections = _apply_literature_policy(approved_sources, policy)
    if not policy_sources:
        guidance = "Approved sources were excluded by the domain literature policy."
        assert_run_active(run, session)
        _write_report(
            sources_dir / "curation_report.md",
            _report_payload(
                skimmed_in=len(skim_candidates),
                approved=len(approved_sources),
                shortlisted=0,
                fetched=0,
                manual_required=0,
                rejected_by_diversity=0,
                policy_rejected=len(policy_rejections),
                warnings=[],
            ),
            guidance=guidance,
        )
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="Curator policy left no sources",
            payload={"guidance": guidance},
        )
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": "curator",
                "failure_class": "failed_fixable",
                "guidance": guidance,
            },
        )
        session.commit()
        return {"run_id": run.id, "state": run.state, "sources": 0, "guidance": guidance}

    instructions_override = prompt_overrides.get("ranking") if prompt_overrides else None
    score_result = _score_relevance_batches(
        project.title,
        policy_sources,
        domain.data,
        run=run,
        project=project,
        session=session,
        hooks=hooks,
        instructions_override=instructions_override,
        research_kernel=run.research_kernel_json,
    )
    scored_sources = _rank_sources(
        policy_sources,
        domain.data,
        score_result.relevance_scores,
        fallback_recency_only=score_result.fallback_recency_only,
        rerank_axes=score_result.rerank_axes,
        rerank_rationales=score_result.rerank_rationales,
        rerank_retain=score_result.rerank_retain,
        research_kernel=run.research_kernel_json,
    )
    shortlist_limit = _shortlist_limit(domain.data)
    settings = get_settings()
    verified_sources, verification_rejections = _apply_verification_gate(
        scored_sources,
        settings,
    )
    assert_run_active(run, session)
    _write_jsonl(sources_dir / "verification_gate_rejected.jsonl", verification_rejections)
    verification_gate_payload = {
        "phase": "curator",
        "kept_count": len(verified_sources),
        "rejected_count": len(verification_rejections),
        "experimental_flag": settings.include_unverified_in_citation_pool,
        "rejected_breakdown": _count_by_verification_status(verification_rejections),
        "warning": (
            "experimental flag ON, citation pool includes non-verified sources"
            if settings.include_unverified_in_citation_pool
            else None
        ),
    }
    append_event(session, run, "verification_gate_applied", verification_gate_payload)
    if settings.include_unverified_in_citation_pool:
        logger.warning(
            "Experimental citation-pool flag is ON; curator will include non-verified sources",
            extra={"run_id": run.id, "phase": "curator"},
        )
        append_event(
            session,
            run,
            "verification_gate_warning",
            {
                "phase": "curator",
                "severity": "warning",
                "experimental_flag": True,
                "message": "experimental flag ON, citation pool includes non-verified sources",
                "included_count": len(verified_sources),
            },
        )
    diversity_result = diversity_rerank(verified_sources, shortlist_limit)

    manifest = _load_manifest(sources_dir / "fulltext_manifest.json")
    warnings = score_result.warnings
    manual_requests: list[dict[str, object]] = []
    fetched = 0
    selected: list[NormalizedSource] = []
    total = len(diversity_result.selected)
    for completed, source in enumerate(diversity_result.selected, start=1):
        assert_run_active(run, session)
        resolved_source, did_fetch, manual_request, fetch_warnings = _resolve_source_fulltext(
            source=source,
            run_dir=run_dir,
            fulltext_dir=fulltext_dir,
            manifest=manifest,
            max_size_mb=_max_upload_mb(),
        )
        assert_run_active(run, session)
        selected.append(resolved_source)
        if did_fetch:
            fetched += 1
        if manual_request is not None:
            manual_requests.append(manual_request)
        warnings.extend(fetch_warnings)
        append_event(
            session,
            run,
            "source_progress",
            {
                "phase": "curator",
                "source_id": resolved_source.source_id,
                "status": _curator_status(resolved_source, did_fetch, manual_request),
                "completed": completed,
                "total": total,
            },
        )
        session.commit()

    # PR-C1.a: classify each shortlisted source's research_role and
    # mirror the role into the NormalizedSource entries so
    # shortlist.json (the source-of-truth payload for downstream
    # synthesizer + UI) carries the tier directly.
    role_map = research_role_classifier.classify_sources(
        selected,
        paper_mode=str(run.paper_mode or "case_analysis"),
        research_kernel=dict(run.research_kernel_json or {}),
    )
    for source in selected:
        new_role = role_map.get(
            source.source_id,
            research_role_classifier.DEFAULT_RESEARCH_ROLE,
        )
        if research_role_classifier.is_valid_role(new_role):
            source.research_role = new_role
    assert_run_active(run, session)
    _write_json(sources_dir / "shortlist.json", [_source_payload(source) for source in selected])
    from autoessay.agents.shadow_baseline import maybe_inject_baseline_as_evidence_source

    maybe_inject_baseline_as_evidence_source(run_dir)
    _write_json(sources_dir / "fulltext_manifest.json", manifest)
    _write_json(sources_dir / "runner_up_sources.json", diversity_result.runner_ups)
    _write_jsonl(sources_dir / "curation_warnings.jsonl", warnings)
    if manual_requests:
        _write_jsonl(sources_dir / "manual_upload_requests.jsonl", manual_requests)
    else:
        _remove_if_exists(sources_dir / "manual_upload_requests.jsonl")
    _write_report(
        sources_dir / "curation_report.md",
        _report_payload(
            skimmed_in=len(skim_candidates),
            approved=len(approved_sources),
            shortlisted=len(selected),
            fetched=fetched,
            manual_required=len(manual_requests),
            rejected_by_diversity=len(diversity_result.runner_ups),
            policy_rejected=len(policy_rejections),
            warnings=warnings,
        ),
        guidance=None,
    )

    summary = {
        "phase": "curator",
        "skimmed_in": len(skim_candidates),
        "shortlisted": len(selected),
        "fetched": fetched,
        "manual_required": len(manual_requests),
        "rejected_by_diversity": len(diversity_result.runner_ups),
    }
    assert_run_active(run, session)
    transition(
        run,
        "USER_DEEP_DIVE_REVIEW",
        session,
        reason="Curator completed",
        payload=summary,
    )
    append_event(session, run, "phase_done", summary)
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


def _prune_curator_replacement_cache(
    *,
    run_dir: Path,
    sources_dir: Path,
    fulltext_dir: Path,
) -> dict[str, object]:
    """Apply the source-rerun replacement contract before curation.

    Curator output is replacement, not incremental. Keep user-owned
    uploads, but clear previously fetched fulltext entries/files so a
    rerun cannot make downstream phases see PDFs from an old shortlist.
    """
    manifest_path = sources_dir / "fulltext_manifest.json"
    user_manifest = _load_manifest(sources_dir / "user_upload_manifest.json")
    manifest = _load_manifest(manifest_path)
    retained_manifest: dict[str, dict[str, object]] = {}
    removed_manifest_source_ids: list[str] = []
    for source_id, entry in manifest.items():
        if _is_user_owned_manifest_entry(source_id, entry, user_manifest):
            retained_manifest[source_id] = entry
        else:
            removed_manifest_source_ids.append(source_id)

    retained_paths: set[Path] = set()
    for entry in retained_manifest.values():
        raw_path = entry.get("pdf_path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        try:
            retained_paths.add(_resolve_run_path(run_dir, raw_path))
        except FileNotFoundError:
            continue

    removed_fulltext_paths: list[str] = []
    for source_id in removed_manifest_source_ids:
        raw_path = manifest.get(source_id, {}).get("pdf_path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        try:
            pdf_path = _resolve_run_path(run_dir, raw_path)
        except FileNotFoundError:
            continue
        if _is_inside(pdf_path, fulltext_dir) and pdf_path.is_file():
            removed_fulltext_paths.append(_relative_path(run_dir, pdf_path))
            pdf_path.unlink()

    if fulltext_dir.exists():
        for path in fulltext_dir.rglob("*"):
            if not path.is_file() or path.resolve() in retained_paths:
                continue
            rel = _relative_path(run_dir, path)
            if rel not in removed_fulltext_paths:
                removed_fulltext_paths.append(rel)
            path.unlink()

    if manifest_path.exists() and manifest != retained_manifest:
        _write_json(manifest_path, retained_manifest)

    return {
        "phase": "curator",
        "contract": "replacement",
        "had_cache": manifest_path.exists() or bool(removed_fulltext_paths),
        "retained_user_owned_count": len(retained_manifest),
        "removed_manifest_count": len(removed_manifest_source_ids),
        "removed_manifest_source_ids": removed_manifest_source_ids[:100],
        "removed_fulltext_file_count": len(removed_fulltext_paths),
        "removed_fulltext_paths": removed_fulltext_paths[:100],
    }


def _is_user_owned_manifest_entry(
    source_id: str,
    entry: Mapping[str, object],
    user_manifest: Mapping[str, Mapping[str, object]],
) -> bool:
    if source_id in user_manifest:
        return True
    if entry.get("license") == "user_upload_local_only":
        return True
    raw_path = entry.get("pdf_path")
    return isinstance(raw_path, str) and raw_path.startswith("sources/uploads/")


def _is_inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def diversity_rerank(sources: Sequence[NormalizedSource], limit: int) -> DiversityResult:
    ordered = sorted(sources, key=lambda item: item.rank_score, reverse=True)
    if limit <= 0:
        return DiversityResult(selected=[], runner_ups=[])

    max_per_venue = max(1, math.floor(limit * DIVERSITY_VENUE_CAP))
    selected: list[NormalizedSource] = []
    venue_counts: Counter[str] = Counter()
    author_counts: Counter[str] = Counter()
    runner_ups: list[dict[str, object]] = []

    for original_rank, source in enumerate(ordered, start=1):
        if len(selected) >= limit:
            continue

        venue_key = _venue_key(source.venue)
        if venue_key and venue_counts[venue_key] >= max_per_venue:
            runner_ups.append(_runner_up_payload(source, original_rank, "venue_cap"))
            continue

        authors = [_author_key(author) for author in source.authors if _author_key(author)]
        if any(author_counts[author] >= DIVERSITY_AUTHOR_CAP for author in authors):
            runner_ups.append(_runner_up_payload(source, original_rank, "author_cap"))
            continue

        selected.append(source)
        if venue_key:
            venue_counts[venue_key] += 1
        for author in authors:
            author_counts[author] += 1

    return DiversityResult(selected=selected, runner_ups=runner_ups)


def _apply_verification_gate(
    sources: list[NormalizedSource],
    settings: Settings,
) -> tuple[list[NormalizedSource], list[dict[str, object]]]:
    if settings.include_unverified_in_citation_pool:
        return list(sources), []

    kept: list[NormalizedSource] = []
    rejected: list[dict[str, object]] = []
    for source in sources:
        status = source.verification_status
        status_value = status.value if hasattr(status, "value") else str(status)
        if status_value == VerificationStatus.VERIFIED.value:
            kept.append(source)
            continue
        rejected.append(
            {
                "source_id": source.source_id,
                "title": source.title,
                "verification_status": status_value,
                "confidence": source.confidence,
                "verified_by": source.verified_by,
                "provenance": source.provenance,
                "risk_flags": list(source.risk_flags),
                "reason": "verification_gate_default_verified_only",
            }
        )
    return kept, rejected


def _count_by_verification_status(
    rejected_records: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in rejected_records:
        status = record.get("verification_status")
        counts[str(status)] += 1
    return dict(sorted(counts.items()))


def store_uploaded_pdf(
    *,
    run: Run,
    requested_source_id: str,
    title: str,
    authors: Sequence[str],
    year: int | None,
    doi: str | None,
    url: str | None,
    pdf_bytes: bytes,
    max_size_mb: int | None = None,
) -> dict[str, object]:
    max_mb = max_size_mb if max_size_mb is not None else _max_upload_mb()
    _validate_pdf_upload(pdf_bytes, max_mb)
    run_dir = Path(run.run_dir)
    sources_dir = run_dir / "sources"
    uploads_dir = sources_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    source_id = _source_id_for_upload(requested_source_id, title)
    pdf_path = uploads_dir / f"{_safe_filename(source_id)}.pdf"
    _write_bytes(pdf_path, pdf_bytes)

    manifest_path = sources_dir / "fulltext_manifest.json"
    manifest = _load_manifest(manifest_path)
    manifest[source_id] = {
        "pdf_path": _relative_path(run_dir, pdf_path),
        "sha256": sha256_bytes(pdf_bytes),
        "size_bytes": len(pdf_bytes),
        "fetched_at": utcnow().isoformat(),
        "license": "user_upload_local_only",
    }
    _write_json(manifest_path, manifest)
    user_manifest_path = sources_dir / "user_upload_manifest.json"
    user_manifest = _load_manifest(user_manifest_path)
    user_manifest[source_id] = manifest[source_id]
    _write_json(user_manifest_path, user_manifest)

    shortlist_path = sources_dir / "shortlist.json"
    shortlist = _read_sources_json(shortlist_path)
    source = _uploaded_source(
        source_id=source_id,
        title=title,
        authors=list(authors),
        year=year,
        doi=doi,
        url=url,
    )
    shortlist = _upsert_source(shortlist, source)
    _write_json(shortlist_path, [_source_payload(item) for item in shortlist])
    user_sources_path = sources_dir / "user_upload_sources.json"
    user_sources = _upsert_source(_load_user_upload_sources(user_sources_path), source)
    _write_json(user_sources_path, [_source_payload(item) for item in user_sources])

    return {
        "source_id": source_id,
        "manifest_entry": manifest[source_id],
        "shortlist_entry": _source_payload(source),
    }


def load_sources_payload(run: Run) -> dict[str, object]:
    run_dir = Path(run.run_dir)
    sources_dir = run_dir / "sources"
    discovery_dir = run_dir / "discovery"
    shortlist = _merge_source_lists(
        _read_sources_json(sources_dir / "shortlist.json"),
        _load_user_upload_sources(sources_dir / "user_upload_sources.json"),
    )
    fulltext_manifest = _load_manifest(sources_dir / "fulltext_manifest.json")
    fulltext_manifest.update(_load_manifest(sources_dir / "user_upload_manifest.json"))
    return {
        "run_id": run.id,
        "shortlist": [_source_payload(source) for source in shortlist],
        "fulltext_manifest": fulltext_manifest,
        "manual_upload_requests": _load_jsonl_objects(
            sources_dir / "manual_upload_requests.jsonl",
        ),
        "curation_report": _read_optional_text(sources_dir / "curation_report.md"),
        "skim_candidates": _load_jsonl_objects(discovery_dir / "skim_candidates.jsonl"),
        "source_quality_counts": _source_quality_counts(discovery_dir, sources_dir),
    }


def _source_quality_counts(discovery_dir: Path, sources_dir: Path) -> dict[str, int]:
    shortlist = _load_json_array(sources_dir / "shortlist.json")
    skim_candidates = _load_jsonl_objects(discovery_dir / "skim_candidates.jsonl")
    return {
        "off_topic_dropped": len(_load_jsonl_objects(discovery_dir / "off_topic_dropped.jsonl")),
        "verification_rejected": len(
            _load_jsonl_objects(sources_dir / "verification_gate_rejected.jsonl")
        ),
        "runner_up": len(_load_json_array(sources_dir / "runner_up_sources.json")),
        "weak_anchor": sum(
            1
            for source in [*skim_candidates, *shortlist]
            if _has_risk_flag(source, "weak_entity_anchor")
        ),
    }


def _has_risk_flag(source: object, flag: str) -> bool:
    if not isinstance(source, Mapping):
        return False
    risk_flags = source.get("risk_flags")
    return isinstance(risk_flags, list) and any(item == flag for item in risk_flags)


def find_local_pdf_path(run: Run, source_id: str) -> Path:
    run_dir = Path(run.run_dir)
    manifest = _load_manifest(run_dir / "sources" / "fulltext_manifest.json")
    manifest.update(_load_manifest(run_dir / "sources" / "user_upload_manifest.json"))
    entry = manifest.get(source_id)
    if not isinstance(entry, dict):
        raise FileNotFoundError(source_id)
    raw_path = entry.get("pdf_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise FileNotFoundError(source_id)
    resolved = _resolve_run_path(run_dir, raw_path)
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(source_id)
    return resolved


def _resolve_source_fulltext(
    *,
    source: NormalizedSource,
    run_dir: Path,
    fulltext_dir: Path,
    manifest: dict[str, dict[str, object]],
    max_size_mb: int,
) -> tuple[NormalizedSource, bool, dict[str, object] | None, list[dict[str, object]]]:
    warnings: list[dict[str, object]] = []
    access_status = _access_value(source)
    if access_status == AccessStatus.BLOCKED.value:
        return _add_risk_flag(source, "access_blocked"), False, None, warnings
    if access_status == AccessStatus.UNAVAILABLE.value:
        return (
            _add_risk_flag(source, "manual_upload_required"),
            False,
            _manual_upload_request(source, "access_unavailable"),
            warnings,
        )
    if access_status not in {AccessStatus.OPEN.value, AccessStatus.METADATA_ONLY.value}:
        return source, False, None, warnings

    resolution: FulltextResolution | None = None
    pdf_url = source.pdf_url
    if not pdf_url:
        resolution = _resolve_fulltext_pdf_candidate(source, warnings=warnings)
        if resolution is not None:
            pdf_url = resolution.pdf_url
            source = source.copy(
                update={
                    "pdf_url": resolution.pdf_url,
                    "access_status": AccessStatus.OPEN.value,
                },
            )
        elif access_status == AccessStatus.METADATA_ONLY.value:
            return source, False, None, warnings
        else:
            return (
                _add_risk_flag(source, "manual_upload_required"),
                False,
                _manual_upload_request(source, "missing_pdf_url"),
                warnings,
            )

    try:
        data = asyncio.run(fetch_pdf(pdf_url, timeout=30.0, max_size_mb=max_size_mb))
    except OpenAccessUnavailable as exc:
        reason = "too_large" if "too large" in str(exc).lower() else "fetch_failed"
        warnings.append(
            {
                "source_id": source.source_id,
                "failure_class": "fixable_deterministic",
                "message": f"PDF fetch failed: {exc}",
            },
        )
        return (
            _add_risk_flag(source, "manual_upload_required"),
            False,
            _manual_upload_request(source, reason),
            warnings,
        )

    pdf_path = fulltext_dir / f"{_safe_filename(source.source_id)}.pdf"
    _write_bytes(pdf_path, data)
    manifest[source.source_id] = {
        "pdf_path": _relative_path(run_dir, pdf_path),
        "sha256": sha256_bytes(data),
        "size_bytes": len(data),
        "fetched_at": utcnow().isoformat(),
        "license": source.license,
        "pdf_url": pdf_url,
    }
    if resolution is not None:
        manifest[source.source_id]["fulltext_resolution"] = {
            "method": resolution.method,
            "source_url": resolution.source_url,
            "diagnostics": resolution.diagnostics,
        }
    return source, True, None, warnings


def _resolve_fulltext_pdf_candidate(
    source: NormalizedSource,
    *,
    warnings: list[dict[str, object]],
) -> FulltextResolution | None:
    candidates = _fulltext_resolution_candidates(source)
    if not candidates:
        return None
    try:
        resolution = asyncio.run(resolve_fulltext_pdf_url(candidates, timeout=12.0))
    except FulltextResolutionError as exc:
        logger.warning(
            "Fulltext resolver did not find a direct PDF URL",
            extra={
                "source_id": source.source_id,
                "candidate_urls": [candidate.url for candidate in candidates],
                "error": str(exc),
            },
        )
        warnings.append(
            {
                "source_id": source.source_id,
                "failure_class": "fulltext_resolution_failed",
                "message": f"Fulltext resolver did not find a direct PDF URL: {exc}",
                "candidate_urls": [candidate.url for candidate in candidates],
            },
        )
        return None
    logger.info(
        "Fulltext resolver found a direct PDF URL",
        extra={
            "source_id": source.source_id,
            "resolution_method": resolution.method,
            "source_url": resolution.source_url,
        },
    )
    warnings.append(
        {
            "source_id": source.source_id,
            "failure_class": "fulltext_resolution_resolved",
            "message": f"Fulltext resolver found a PDF URL via {resolution.method}",
            "candidate_urls": [candidate.url for candidate in candidates],
            "resolved_pdf_url": resolution.pdf_url,
            "resolution_method": resolution.method,
        },
    )
    return resolution


def _fulltext_resolution_candidates(
    source: NormalizedSource,
) -> list[FulltextResolutionCandidate]:
    candidates: list[FulltextResolutionCandidate] = []
    if source.doi:
        doi = source.doi.strip()
        doi_url = doi if doi.startswith(("http://", "https://")) else f"https://doi.org/{doi}"
        candidates.append(
            FulltextResolutionCandidate(
                url=doi_url,
                kind="doi",
            ),
        )
    if source.url:
        candidates.append(FulltextResolutionCandidate(url=source.url, kind="landing"))
    return candidates


def _curator_harness_system_message(
    *, language: str | None, instructions_override: str | None
) -> str:
    """Build the system message for the harness-path curator ranking
    LLM call. ``instructions_override`` replaces the static instruction
    concept (default: :data:`CURATOR_RANKING_INSTRUCTIONS`); the
    schema-binding sentence ("Return one strict JSON array.") and
    :func:`language_directive` always stay outside the editable
    surface (Stage 3.A.1, codex-AGREEd amendment 1)."""
    instructions = instructions_override or CURATOR_RANKING_INSTRUCTIONS
    return " ".join([instructions, "Return one strict JSON array.", language_directive(language)])


def _curator_async_system_message(*, instructions_override: str | None) -> str:
    """Build the system message for the async fallback curator ranking
    LLM call. ``instructions_override`` replaces the static instruction
    concept (default: :data:`CURATOR_RANKING_INSTRUCTIONS`); the
    schema-binding sentence ("Return JSON only.") stays outside the
    editable surface (Stage 3.A.1)."""
    instructions = instructions_override or CURATOR_RANKING_INSTRUCTIONS
    return f"{instructions} Return JSON only."


def _score_relevance_batches(
    topic: str,
    sources: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    *,
    run: Run | None = None,
    project: Project | None = None,
    session: Session | None = None,
    hooks: HookRegistry | None = None,
    instructions_override: str | None = None,
    research_kernel: Mapping[str, object] | None = None,
) -> ScoreResult:
    if get_settings().curator_stub:
        # Full stub: no LLM, heuristic relevance, no 4-axis rerank.
        return ScoreResult(
            relevance_scores={
                source.source_id: _heuristic_relevance(topic, source) for source in sources
            },
            warnings=[],
            fallback_recency_only=False,
        )
    if run is None or project is None or session is None:
        raise ValueError("Curator relevance scoring requires run, project, and session")
    return _score_relevance_batches_via_harness(
        topic=topic,
        sources=sources,
        domain_data=domain_data,
        run=run,
        project=project,
        session=session,
        hooks=hooks or HookRegistry(),
        instructions_override=instructions_override,
        research_kernel=research_kernel,
    )


def _score_relevance_batches_via_harness(
    *,
    topic: str,
    sources: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    instructions_override: str | None = None,
    research_kernel: Mapping[str, object] | None = None,
) -> ScoreResult:
    """PR-J9b + PR-I2 retro fix: 3-tier fallback chain in the curator
    harness path (codex round-1 A2 + retro fix #1 — round-1's
    ``4-axis → legacy single-axis → recency-only`` chain wasn't fully
    wired in J9b's first cut; this version implements all three tiers).

    Tier 1 — 4-axis prompt (prod default, ``curator_rerank_stub=False``):
        ``_curator_ranking_prompt`` asks the LLM for
        scope_fit/relevance/impact/frontier_currency + retain +
        rationale. Success → ScoreResult with rerank_axes populated.

    Tier 2 — Legacy single-axis prompt:
        Reached when (a) ``curator_rerank_stub=True`` (CI / e2e
        determinism path — sends the cheap ``_relevance_prompt``
        instead of the 4-axis one, no token waste), or (b) Tier 1
        failed schema/transport for the batch (codex round-1 A2 chain).
        ``_relevance_prompt`` asks for ``{scores: [{source_id,
        relevance_score}]}``; ``_parse_relevance_response`` permissively
        parses that. Per-batch fallback — Tier 1 successes for other
        batches are kept; only the failed batch is re-asked at Tier 2.

    Tier 3 — recency-only:
        Reached when Tier 2 also fails (either initial transport bomb
        or follow-on after Tier 1 failure). ``fallback_recency_only=
        True`` triggers the ``_rank_sources`` recency-only branch.
        Codex round-1 A7 — preserves the original J6/J8 fallback when
        every LLM tier dies.

    Every fallback emits a ``curator_rerank_fallback`` event with the
    transition reason (codex round-1 blocking amendment — do NOT abuse
    ``state_transition`` for this).
    """
    warnings: list[dict[str, object]] = []
    scores: dict[str, float] = {}
    rerank_axes_acc: dict[str, dict[str, float]] = {}
    rerank_rationales_acc: dict[str, str] = {}
    rerank_retain_acc: dict[str, bool] = {}
    _register_curator_memory_hook(hooks)
    # Per-tier helpers build their own system messages (4-axis vs
    # legacy single-axis use different instruction blocks); the outer
    # wrapper just orchestrates dispatch + per-batch fallback.
    from autoessay.agents._research_kernel_prompt import research_kernel_for_prompt

    kernel_payload = research_kernel_for_prompt(research_kernel)
    rerank_stub_active = get_settings().curator_rerank_stub
    if rerank_stub_active:
        # Stub mode: emit ONE fallback event up front, then run every
        # batch via legacy single-axis (no 4-axis prompt sent).
        _emit_rerank_fallback_event(session, run, reason="rerank_stub_active")

    for batch_index, batch in enumerate(_chunks(list(sources), RELEVANCE_BATCH_SIZE), start=1):
        if rerank_stub_active:
            batch_outcome = _run_legacy_single_axis_batch(
                topic=topic,
                batch=batch,
                domain_data=domain_data,
                run=run,
                project=project,
                session=session,
                hooks=hooks,
                instructions_override=instructions_override,
                batch_index=batch_index,
            )
        else:
            batch_outcome = _run_4axis_batch(
                topic=topic,
                batch=batch,
                domain_data=domain_data,
                run=run,
                project=project,
                session=session,
                hooks=hooks,
                instructions_override=instructions_override,
                research_kernel=kernel_payload,
                batch_index=batch_index,
            )
            if batch_outcome.fell_back:
                # Tier 1 failed for this batch; codex round-1 A2:
                # try Tier 2 (legacy single-axis) before recency.
                _emit_rerank_fallback_event(
                    session,
                    run,
                    reason=batch_outcome.fallback_reason or "schema_violation",
                    batch_index=batch_index,
                )
                batch_outcome = _run_legacy_single_axis_batch(
                    topic=topic,
                    batch=batch,
                    domain_data=domain_data,
                    run=run,
                    project=project,
                    session=session,
                    hooks=hooks,
                    instructions_override=instructions_override,
                    batch_index=batch_index,
                )
        warnings.extend(batch_outcome.warnings)
        if batch_outcome.recency_only:
            # Tier 3 — every LLM tier failed for this batch. Bail the
            # whole run (consistent with J9b's original behavior; we
            # don't half-rank the run).
            return ScoreResult(
                relevance_scores={},
                warnings=warnings,
                fallback_recency_only=True,
            )
        scores.update(batch_outcome.relevance_scores)
        rerank_axes_acc.update(batch_outcome.rerank_axes)
        rerank_rationales_acc.update(batch_outcome.rerank_rationales)
        rerank_retain_acc.update(batch_outcome.rerank_retain)
    rerank_active = (not rerank_stub_active) and bool(rerank_axes_acc)
    return ScoreResult(
        relevance_scores=scores,
        warnings=warnings,
        fallback_recency_only=False,
        rerank_axes=rerank_axes_acc,
        rerank_rationales=rerank_rationales_acc,
        rerank_retain=rerank_retain_acc,
        rerank_active=rerank_active,
    )


@dataclass
class _BatchOutcome:
    """PR-I2 retro fix #1: helper return type for the 3-tier fallback
    chain. ``fell_back`` is True iff the 4-axis tier (Tier 1) raised
    and we should try Tier 2 next; ``recency_only`` is True iff Tier 2
    also raised and the caller must abandon the run."""

    relevance_scores: dict[str, float] = field(default_factory=dict)
    rerank_axes: dict[str, dict[str, float]] = field(default_factory=dict)
    rerank_rationales: dict[str, str] = field(default_factory=dict)
    rerank_retain: dict[str, bool] = field(default_factory=dict)
    warnings: list[dict[str, object]] = field(default_factory=list)
    fell_back: bool = False
    recency_only: bool = False
    fallback_reason: str | None = None


def _run_4axis_batch(
    *,
    topic: str,
    batch: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    instructions_override: str | None,
    research_kernel: Mapping[str, object],
    batch_index: int,
) -> _BatchOutcome:
    """Tier 1 — ask the 4-axis prompt for one batch."""
    prompt = _curator_ranking_prompt(
        topic, batch, domain_data, suffix="", research_kernel=research_kernel
    )
    system_message = _curator_harness_system_message(
        language=project.language, instructions_override=instructions_override
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.0,
        max_tokens=1500,
        response_format=None,
        request_id=f"curator_ranking_batch_{batch_index:03d}",
        prompt_template_id="curator.ranking_batch.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="sources",
        step_id="curator.ranking_batch",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=topic,
        run_metadata={
            "agent_phase": "curator",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "candidate_count": len(batch),
            "batch_index": batch_index,
            "batch_size": len(batch),
            "llm_optional": False,
            "memory_query": (
                f"phase=curator topic={project.title} domain={project.domain_id} "
                f"candidate_count={len(batch)}"
            ),
        },
    )
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Curator")
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=CuratorRanking,
                audit=audit,
                max_corrective_retries=2,
                llm_optional=False,
            ),
        )
    except SchemaViolationError:
        return _BatchOutcome(fell_back=True, fallback_reason="schema_violation")
    except Exception as exc:  # noqa: BLE001 — Tier 1 failure → Tier 2
        return _BatchOutcome(
            fell_back=True,
            fallback_reason=f"transport_error:{type(exc).__name__}",
        )
    warnings: list[dict[str, object]] = []
    if response.attempt > 1:
        warnings.append(
            {
                "source_id": "llm",
                "failure_class": "fixable_prompt",
                "message": "curator relevance JSON did not parse; retried with stricter suffix",
            },
        )
    parsed = _scores_from_curator_ranking(response.parsed)
    return _BatchOutcome(
        relevance_scores=parsed.relevance_scores,
        rerank_axes=parsed.rerank_axes,
        rerank_rationales=parsed.rerank_rationales,
        rerank_retain=parsed.rerank_retain,
        warnings=warnings,
    )


def _run_legacy_single_axis_batch(
    *,
    topic: str,
    batch: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    instructions_override: str | None,
    batch_index: int,
) -> _BatchOutcome:
    """Tier 2 — ask the legacy single-axis ``_relevance_prompt`` for
    one batch. Used directly when ``curator_rerank_stub=True`` (CI
    determinism path; cheaper than 4-axis), and as the Tier 1 fallback
    when 4-axis schema/transport blew up.

    On success, populates ``relevance_scores`` only — no rerank_axes /
    rerank_rationales / rerank_retain (so ``_rank_sources`` applies the
    legacy formula 100%, no blend, no hard penalty cap). On failure,
    returns ``recency_only=True`` so the caller bails to recency-only.
    """
    prompt = _relevance_prompt(topic, batch, domain_data, suffix="")
    system_message = _curator_async_system_message(instructions_override=instructions_override)
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.0,
        max_tokens=1200,
        response_format=None,
        # Same request_id namespace as the 4-axis path — only one tier
        # ever fires per (run, batch_index), so the prompt/response
        # audit file names stay stable across tiers. Legacy variant is
        # disambiguated by the ``run_metadata.tier`` annotation.
        request_id=f"curator_ranking_batch_{batch_index:03d}",
        prompt_template_id="curator.ranking_batch.legacy.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="sources",
        step_id="curator.ranking_batch",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=topic,
        run_metadata={
            "agent_phase": "curator",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "candidate_count": len(batch),
            "batch_index": batch_index,
            "batch_size": len(batch),
            "llm_optional": False,
            "tier": "legacy_single_axis",
        },
    )
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Curator")
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=_LegacyRelevanceResponse,
                audit=audit,
                max_corrective_retries=1,
                llm_optional=False,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — Tier 2 failure → Tier 3
        warnings: list[dict[str, object]] = [
            {
                "source_id": "llm",
                "failure_class": "fixable_prompt",
                "message": (
                    f"curator legacy single-axis fallback failed; "
                    f"fell back to year recency ranking: {exc}"
                ),
            },
        ]
        return _BatchOutcome(warnings=warnings, recency_only=True)
    parsed_text = response.text if hasattr(response, "text") else getattr(response, "content", "")
    relevance_scores = _parse_relevance_response(str(parsed_text)) or {}
    if not relevance_scores:
        warnings = [
            {
                "source_id": "llm",
                "failure_class": "fixable_prompt",
                "message": (
                    "curator legacy single-axis returned unparseable JSON; "
                    "fell back to year recency ranking"
                ),
            },
        ]
        return _BatchOutcome(warnings=warnings, recency_only=True)
    return _BatchOutcome(relevance_scores=relevance_scores)


def _emit_rerank_fallback_event(
    session: Session,
    run: Run,
    *,
    reason: str,
    batch_index: int | None = None,
) -> None:
    """PR-J9b: dedicated ``curator_rerank_fallback`` event type — codex
    round-1 blocking amendment forbid abusing ``state_transition`` for
    this. The milestone-tag acceptance check (and any future Curator
    soak audit) reads this event to confirm a clean 4-axis prod run.
    Reason values: ``rerank_stub_active`` / ``schema_violation`` /
    ``transport_error:<class>``."""
    payload: dict[str, object] = {
        "phase": "sources",
        "reason": reason,
        "fallback_to": "legacy_single_axis_or_recency",
    }
    if batch_index is not None:
        payload["batch_index"] = batch_index
    append_event(session, run, "curator_rerank_fallback", payload)


def _relevance_prompt(
    topic: str,
    batch: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    *,
    suffix: str,
) -> str:
    payload = {
        "topic": topic,
        "domain_id": domain_data.get("id"),
        "required_shape": {"scores": [{"source_id": "string", "relevance_score": "0..1"}]},
        "records": [
            {
                "source_id": source.source_id,
                "title": source.title,
                "authors": source.authors[:8],
                "year": source.year,
                "venue": source.venue,
                "abstract": _truncate(source.abstract, 1200),
                "access_status": _access_value(source),
            }
            for source in batch
        ],
    }
    return (
        "Score each record's relevance to the topic from 0 to 1. "
        "Return JSON with a scores array and no explanatory prose.\n"
        f"{json.dumps(payload, sort_keys=True)}"
        f"{suffix}"
    )


def _curator_ranking_prompt(
    topic: str,
    batch: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    *,
    suffix: str,
    research_kernel: Mapping[str, object] | None = None,
) -> str:
    """PR-J9b: 4-axis rerank prompt. Replaces the J8 single-axis
    ranking prompt — codex round-1 placed the standalone LLM rerank in
    the Curator phase (Scout's signals get overwritten here anyway,
    see J9 round-1 amendment 1). Asks LLM to score each source on:

    - scope_fit (35%): research subject + period match against the
      kernel's tentative_question / observed_puzzle / scope. NOT a
      publication-year filter (codex round-1 A4: a 2010 review of
      1945-1990 Korea is in-scope; a 2025 RCEP article is not).
    - relevance (25%): semantic content match to the kernel's
      research question.
    - impact (25%): citation prestige + canon status, inferred from
      title / authors / venue. Codex round-1 A2 — we deliberately do
      NOT pass provenance / canonical_bucket / verified_by /
      source_client to prevent confirmation bias toward LLM-mined
      canon.
    - frontier_currency (15%): is the work part of a recent novel
      direction (new material / new method / new debate)? Old canon
      should score 0 here and rely on impact.

    Hard penalty (deterministic, applied in ``_rank_sources``): if
    scope_fit < 0.30 OR retain_decision = False, the final rank gets
    capped to ``HARD_PENALTY_CAP``. The prompt also asks the LLM to
    set retain_decision = False for clear scope mismatches; both
    guards are belt-and-braces because LLM judgment alone (codex
    round-1 A3) can't be trusted to consistently downrank
    scope-mismatched recency."""
    kernel_payload = dict(research_kernel) if research_kernel else {}
    payload = {
        "topic": topic,
        # PR-J7/J8/J9b: research_kernel anchors all four axes. Empty
        # dict on missing kernel (degrade to topic-only anchoring;
        # same contract as J6 / J7).
        "research_kernel": kernel_payload,
        "domain_id": domain_data.get("id"),
        "axis_weights": dict(RERANK_AXIS_WEIGHTS),
        "required_shape": [
            {
                "source_id": "string",
                "scope_fit": "0..1",
                "relevance": "0..1",
                "impact": "0..1",
                "frontier_currency": "0..1",
                "rationale": "string ≤200 chars",
                "retain_decision": "boolean",
                "risk_flags": ["string"],
            },
        ],
        "records": [
            {
                "source_id": source.source_id,
                "title": source.title,
                "authors": source.authors[:8],
                "year": source.year,
                "venue": source.venue,
                "abstract": _truncate(source.abstract, 1200),
                "access_status": _access_value(source),
            }
            for source in batch
        ],
    }
    return (
        "You are the Curator's source rerank LLM. For each record, "
        "score four axes against the topic AND research_kernel "
        "(tentative_question + observed_puzzle + scope):\n"
        "- scope_fit: does the source's actual subject matter and time "
        "period fall inside the kernel's scope? Treat publication year "
        "as IRRELEVANT for scope (a 2010 review of 1945-1990 Korea is "
        "in-scope; a 2025 RCEP paper is OUT-OF-SCOPE for a 1945-1990 "
        "Korean miracle kernel). If scope_fit is below 0.30 OR the "
        "source is clearly off-topic, set retain_decision = false.\n"
        "- relevance: semantic content match to the research question "
        "and observed_puzzle (independent of scope/period).\n"
        "- impact: inferred citation prestige + canonical status from "
        "title, author, and venue. A foundational monograph should "
        "score high here even if old.\n"
        "- frontier_currency: does the source represent a recent novel "
        "direction (new material, method, or debate)? Old canon should "
        "score near 0 here; that is fine — impact will carry it.\n"
        "Return a strict JSON array — one object per input record — "
        "with keys: source_id, scope_fit, relevance, impact, "
        "frontier_currency, rationale (≤200 chars explaining the "
        "scope_fit verdict), retain_decision, risk_flags. The Python "
        "curator will combine your axes with deterministic recency / "
        "venue / diversity signals (legacy 15% blend) and apply a "
        "hard penalty when scope_fit < 0.30 OR retain_decision is "
        "false. Do not include markdown or explanatory prose outside "
        "the JSON.\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        f"{suffix}"
    )


@dataclass(frozen=True)
class _RerankParse:
    """PR-J9b: parsed 4-axis output of one curator ranking batch."""

    relevance_scores: dict[str, float]
    rerank_axes: dict[str, dict[str, float]]
    rerank_rationales: dict[str, str]
    rerank_retain: dict[str, bool]


def _scores_from_curator_ranking(parsed: object) -> _RerankParse:
    """PR-J9b: extract 4-axis breakdown + retain_decision from the LLM
    response. Returns a ``_RerankParse`` with empty dicts when the
    parse is degenerate (caller treats that as a fallback signal)."""
    ranking: list[CuratorRankedSource]
    if isinstance(parsed, CuratorRanking):
        ranking = parsed.__root__
    elif isinstance(parsed, list):
        ranking = [item for item in parsed if isinstance(item, CuratorRankedSource)]
    else:
        return _RerankParse({}, {}, {}, {})
    relevance_scores: dict[str, float] = {}
    rerank_axes: dict[str, dict[str, float]] = {}
    rerank_rationales: dict[str, str] = {}
    rerank_retain: dict[str, bool] = {}
    for item in ranking:
        sid = item.source_id
        relevance_scores[sid] = _clamp(float(item.relevance), 0.0, 1.0)
        rerank_axes[sid] = {
            "scope_fit": _clamp(float(item.scope_fit), 0.0, 1.0),
            "relevance": _clamp(float(item.relevance), 0.0, 1.0),
            "impact": _clamp(float(item.impact), 0.0, 1.0),
            "frontier_currency": _clamp(float(item.frontier_currency), 0.0, 1.0),
        }
        rerank_rationales[sid] = item.rationale
        rerank_retain[sid] = bool(item.retain_decision)
    return _RerankParse(
        relevance_scores=relevance_scores,
        rerank_axes=rerank_axes,
        rerank_rationales=rerank_rationales,
        rerank_retain=rerank_retain,
    )


def _register_curator_memory_hook(hooks: HookRegistry) -> None:
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


def _parse_relevance_response(value: str) -> dict[str, float] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    records: object
    if isinstance(decoded, list):
        records = decoded
    elif isinstance(decoded, dict):
        records = decoded.get("scores")
    else:
        return None
    if not isinstance(records, list):
        return None
    scores: dict[str, float] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        raw_score = item.get("relevance_score", item.get("relevance"))
        if isinstance(source_id, str) and isinstance(raw_score, (int, float)):
            scores[source_id] = _clamp(float(raw_score), 0.0, 1.0)
    return scores if scores else None


def _rank_sources(
    sources: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    relevance_scores: Mapping[str, float],
    *,
    fallback_recency_only: bool,
    rerank_axes: Mapping[str, Mapping[str, float]] | None = None,
    rerank_rationales: Mapping[str, str] | None = None,
    rerank_retain: Mapping[str, bool] | None = None,
    research_kernel: Mapping[str, object] | None = None,
) -> list[NormalizedSource]:
    """PR-J9b: blends 4-axis LLM rerank (when available) with the
    legacy ``relevance + recency + venue + diversity`` formula. Final
    rank = ``RERANK_BLEND * 4axis_weighted + LEGACY_BLEND * legacy``
    (codex round-1 A3: legacy stays at 15% so RCEP scope-mismatch
    cannot reach top via recency). When scope_fit < 0.30 OR
    retain_decision = False, ``rank_score`` is capped to
    ``HARD_PENALTY_CAP`` deterministically (codex round-1 A3 + A4) —
    this is the only mechanism that guarantees scope-mismatched
    sources cannot float to the shortlist's first page on a noisy
    LLM batch.

    Persists the 4-axis breakdown + rationale + retain flag onto each
    NormalizedSource so shortlist.json carries per-source audit (codex
    round-1 A4).
    """
    current_year = utcnow().year
    recency_window = _recency_window(domain_data)
    source_client_counts = Counter(source.source_client for source in sources)
    rerank_axes = rerank_axes or {}
    rerank_rationales = rerank_rationales or {}
    rerank_retain = rerank_retain or {}
    ranked: list[NormalizedSource] = []
    for source in sources:
        recency_score = _recency_score(source.year, current_year, recency_window)
        update_payload: dict[str, object] = {}
        if fallback_recency_only:
            rank_score = recency_score
        else:
            relevance_score = relevance_scores.get(
                source.source_id,
                _heuristic_relevance("", source),
            )
            venue_score = _venue_authority_score(source.venue, domain_data)
            diversity_score = 1.0 / math.sqrt(max(1, source_client_counts[source.source_client]))
            legacy_score = (
                (0.65 * relevance_score)
                + (0.20 * recency_score)
                + (0.10 * venue_score)
                + (0.05 * diversity_score)
            )
            axes = rerank_axes.get(source.source_id)
            if axes is not None and all(k in axes for k in RERANK_AXIS_WEIGHTS):
                rerank_weighted = sum(
                    RERANK_AXIS_WEIGHTS[k] * float(axes[k]) for k in RERANK_AXIS_WEIGHTS
                )
                rank_score = RERANK_BLEND * rerank_weighted + LEGACY_BLEND * legacy_score
                # Hard penalty cap (codex round-1 A3 + A4): scope
                # mismatch or explicit drop-decision cannot ride
                # recency / venue / impact to the top.
                scope_fit = float(axes.get("scope_fit", 0.0))
                retain = rerank_retain.get(source.source_id, True)
                if scope_fit < SCOPE_FIT_HARD_PENALTY_THRESHOLD or not retain:
                    rank_score = min(rank_score, HARD_PENALTY_CAP)
                # Persist axes + rationale onto the NormalizedSource so
                # shortlist.json shows the per-source audit.
                update_payload["rerank_axes"] = {k: float(axes[k]) for k in RERANK_AXIS_WEIGHTS}
                rationale = rerank_rationales.get(source.source_id)
                if rationale:
                    update_payload["rerank_rationale"] = rationale
            else:
                rank_score = legacy_score
        rank_score = _apply_kernel_access_adjustment(
            source,
            rank_score,
            research_kernel=research_kernel,
        )
        update_payload["rank_score"] = round(rank_score, 6)
        ranked.append(source.copy(update=update_payload))
    return sorted(ranked, key=lambda item: item.rank_score, reverse=True)


def _apply_kernel_access_adjustment(
    source: NormalizedSource,
    rank_score: float,
    *,
    research_kernel: Mapping[str, object] | None,
) -> float:
    """Small deterministic guardrail for rerank blind spots.

    The LLM reranker is intentionally not told whether a source has
    fulltext access, and it can undervalue title/abstract matches that
    use a different wording than the project title. When the user kernel
    clearly names a concrete material channel, keep accessible matching
    sources inside the synthesizer deep-dive window. This only reorders
    verified sources; it does not bypass verification or cite metadata as
    substantive evidence.
    """
    if not research_kernel:
        return rank_score
    kernel_terms = _kernel_alignment_terms(research_kernel)
    if not kernel_terms:
        return rank_score
    haystack = _source_alignment_text(source)
    hits = {term for term in kernel_terms if term in haystack}
    adjusted = rank_score
    has_text_access = bool(source.abstract or source.pdf_url)
    if has_text_access and len(hits) >= 2:
        adjusted = max(adjusted + 0.10, 0.68)
    elif len(hits) >= 3:
        adjusted = max(adjusted + 0.06, 0.62)
    if (
        _kernel_centers_dollar_gold(research_kernel)
        and not _kernel_mentions_sterling(research_kernel)
        and any(term in haystack for term in ("sterling", "pound sterling", "英镑"))
    ):
        adjusted = min(adjusted, 0.64)
    return _clamp(adjusted, 0.0, 1.0)


def _kernel_alignment_terms(research_kernel: Mapping[str, object]) -> set[str]:
    text = _kernel_text(research_kernel)
    terms = _term_set(text)
    bridged: set[str] = set(terms)
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


def _source_alignment_text(source: NormalizedSource) -> str:
    return " ".join(
        item
        for item in [
            source.title,
            source.abstract or "",
            source.venue or "",
            source.doi or "",
            source.url or "",
        ]
        if item
    ).casefold()


def _kernel_centers_dollar_gold(research_kernel: Mapping[str, object]) -> bool:
    text = _kernel_text(research_kernel)
    return ("美元" in text or "dollar" in text) and ("黄金" in text or "gold" in text)


def _kernel_mentions_sterling(research_kernel: Mapping[str, object]) -> bool:
    text = _kernel_text(research_kernel)
    return any(term in text for term in ("英镑", "sterling", "pound"))


def _recency_score(year: int | None, current_year: int, recency_window: int) -> float:
    if year is None:
        return 0.35
    age = max(0, current_year - year)
    return _clamp(1.0 - (age / max(1, recency_window)), 0.0, 1.0)


def _venue_authority_score(venue: str | None, domain_data: Mapping[str, Any]) -> float:
    weights = _venue_weights(domain_data)
    if not venue or not weights:
        return 0.5
    normalized = _normalize_key(venue)
    if normalized in weights:
        return weights[normalized]
    for venue_key, weight in weights.items():
        if venue_key and venue_key in normalized:
            return weight
    return 0.5


def _venue_weights(domain_data: Mapping[str, Any]) -> dict[str, float]:
    literature = domain_data.get("literature", {})
    venue_weights: object = {}
    if isinstance(literature, dict):
        venue_weights = literature.get("venue_weights", {})
    domain_section = domain_data.get("domain", {})
    if not venue_weights and isinstance(domain_section, dict):
        nested_literature = domain_section.get("literature", {})
        if isinstance(nested_literature, dict):
            venue_weights = nested_literature.get("venue_weights", {})
    if not isinstance(venue_weights, dict):
        return {}
    weights: dict[str, float] = {}
    for key, value in venue_weights.items():
        if isinstance(key, str) and isinstance(value, (int, float)):
            weights[_normalize_key(key)] = _clamp(float(value), 0.0, 1.0)
    return weights


def _apply_literature_policy(
    sources: Sequence[NormalizedSource],
    policy: Mapping[str, object],
) -> tuple[list[NormalizedSource], list[dict[str, object]]]:
    kept: list[NormalizedSource] = []
    rejected: list[dict[str, object]] = []
    for source in sources:
        if _is_wikipedia_canonical_seed(source):
            rejected.append(_policy_rejection(source, "canonical_seed"))
            continue
        source_kind = _source_kind(source)
        if source_kind == "working_paper" and not bool(policy["include_working_papers"]):
            rejected.append(_policy_rejection(source, source_kind))
            continue
        if source_kind == "book" and not bool(policy["include_books"]):
            rejected.append(_policy_rejection(source, source_kind))
            continue
        if source_kind == "preprint" and not bool(policy["include_preprints"]):
            rejected.append(_policy_rejection(source, source_kind))
            continue
        kept.append(source)
    return kept, rejected


def _literature_policy(domain_data: Mapping[str, Any]) -> dict[str, object]:
    policy: object = domain_data.get("literature_policy", {})
    literature = domain_data.get("literature", {})
    if isinstance(literature, dict):
        policy = literature.get("policy", policy)
    domain_section = domain_data.get("domain", {})
    if isinstance(domain_section, dict):
        nested_literature = domain_section.get("literature", {})
        if isinstance(nested_literature, dict):
            policy = nested_literature.get("policy", policy)
    if not isinstance(policy, dict):
        policy = {}
    return {
        "include_working_papers": _bool_policy(policy, "include_working_papers", True),
        "include_books": _bool_policy(policy, "include_books", True),
        "include_preprints": _bool_policy(policy, "include_preprints", True),
        "paywalled_policy": str(policy.get("paywalled_policy", "metadata_only")),
    }


def _bool_policy(policy: Mapping[str, object], key: str, default: bool) -> bool:
    value = policy.get(key)
    return value if isinstance(value, bool) else default


def _source_kind(source: NormalizedSource) -> str:
    haystack = " ".join(
        item for item in [source.source_client, source.venue or "", source.title] if item
    ).casefold()
    if "working paper" in haystack or "ssrn" in haystack:
        return "working_paper"
    if "preprint" in haystack or "arxiv" in haystack:
        return "preprint"
    if "book" in haystack:
        return "book"
    return "article"


def _is_wikipedia_canonical_seed(source: NormalizedSource) -> bool:
    return (
        source.source_id.startswith("wikipedia_zh:") or source.provenance == "wiki_canonical_seed"
    )


def _policy_rejection(source: NormalizedSource, source_kind: str) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "title": source.title,
        "reason": (
            "canonical_seed_not_citable"
            if source_kind == "canonical_seed"
            else f"domain_policy_excludes_{source_kind}"
        ),
    }


def _approved_source_ids(session: Session, run_id: str) -> set[str] | None:
    checkpoints = list(
        session.scalars(
            select(Checkpoint)
            .where(Checkpoint.run_id == run_id)
            .order_by(Checkpoint.created_at.desc()),
        ),
    )
    for checkpoint in checkpoints:
        if checkpoint.checkpoint_type not in SEARCH_REVIEW_CHECKPOINT_TYPES:
            continue
        if checkpoint.status != "ACCEPTED":
            continue
        source_ids = _source_ids_from_json(checkpoint.decision_payload)
        if source_ids is not None:
            return source_ids
    return None


def _source_ids_from_json(value: str) -> set[str] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return _source_ids_from_payload(decoded)


def _source_ids_from_payload(payload: object) -> set[str] | None:
    if isinstance(payload, list):
        return _string_set(payload)
    if not isinstance(payload, Mapping):
        return None
    for key in ("source_ids", "approved_source_ids", "approved_sources", "approved"):
        value = payload.get(key)
        if isinstance(value, list):
            return _string_set(value)
    return None


def _string_set(items: Sequence[object]) -> set[str]:
    return {item for item in items if isinstance(item, str) and item}


def _filter_approved_sources(
    sources: Sequence[NormalizedSource],
    approved_ids: set[str] | None,
) -> list[NormalizedSource]:
    if approved_ids is None:
        return list(sources)
    return [source for source in sources if source.source_id in approved_ids]


def _merge_source_lists(
    first: Sequence[NormalizedSource],
    second: Sequence[NormalizedSource],
) -> list[NormalizedSource]:
    merged: dict[str, NormalizedSource] = {}
    for source in [*first, *second]:
        merged[source.source_id] = source
    return list(merged.values())


def _load_user_upload_sources(path: Path) -> list[NormalizedSource]:
    return [source for source in _read_sources_json(path) if source.source_client == "user_upload"]


def _read_sources_jsonl(path: Path) -> list[NormalizedSource]:
    if not path.exists():
        return []
    sources: list[NormalizedSource] = []
    for record in _load_jsonl_objects(path):
        try:
            sources.append(NormalizedSource.parse_obj(record))
        except ValidationError:
            continue
    return sources


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


def _manual_upload_request(source: NormalizedSource, reason: str) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "title": source.title,
        "doi": source.doi,
        "url": source.url or source.pdf_url,
        "suggested_location": f"sources/uploads/{_safe_filename(source.source_id)}.pdf",
        "reason": reason,
    }


def _curator_status(
    source: NormalizedSource,
    did_fetch: bool,
    manual_request: dict[str, object] | None,
) -> str:
    if did_fetch:
        return "fetched"
    if manual_request is not None:
        return "manual_upload_required"
    if _access_value(source) == AccessStatus.BLOCKED.value:
        return "blocked"
    return "metadata_only"


def _add_risk_flag(source: NormalizedSource, flag: str) -> NormalizedSource:
    risk_flags = list(source.risk_flags)
    if flag not in risk_flags:
        risk_flags.append(flag)
    return source.copy(update={"risk_flags": risk_flags})


def _uploaded_source(
    *,
    source_id: str,
    title: str,
    authors: list[str],
    year: int | None,
    doi: str | None,
    url: str | None,
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=authors,
        year=year,
        venue=None,
        doi=doi,
        url=url,
        pdf_url=None,
        abstract=None,
        source_client="user_upload",
        access_status=AccessStatus.OPEN,
        license="user_upload_local_only",
        rank_score=1.0,
        risk_flags=[],
        verified_by="manual_upload",
        verification_status=VerificationStatus.VERIFIED,
        confidence=0.8,
    )


def _upsert_source(
    shortlist: Sequence[NormalizedSource],
    source: NormalizedSource,
) -> list[NormalizedSource]:
    updated: list[NormalizedSource] = []
    found = False
    for item in shortlist:
        if item.source_id == source.source_id:
            updated.append(source)
            found = True
        else:
            updated.append(item)
    if not found:
        updated.append(source)
    return updated


def _validate_pdf_upload(pdf_bytes: bytes, max_size_mb: int) -> None:
    max_bytes = max_size_mb * 1024 * 1024
    if len(pdf_bytes) > max_bytes:
        raise ValueError(f"PDF exceeds {max_size_mb} MB limit")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("uploaded file is not a PDF")


def _source_id_for_upload(requested_source_id: str, title: str) -> str:
    if requested_source_id != "new" and requested_source_id.strip():
        return requested_source_id.strip()
    slug = _safe_filename(title)[:32] or "source"
    return f"user_{slug}_{uuid4().hex[:10]}"


def _report_payload(
    *,
    skimmed_in: int,
    approved: int,
    shortlisted: int,
    fetched: int,
    manual_required: int,
    rejected_by_diversity: int,
    policy_rejected: int,
    warnings: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "skimmed_in": skimmed_in,
        "approved": approved,
        "shortlisted": shortlisted,
        "fetched": fetched,
        "manual_required": manual_required,
        "rejected_by_diversity": rejected_by_diversity,
        "policy_rejected": policy_rejected,
        "warnings": len(warnings),
    }


def _write_report(path: Path, payload: Mapping[str, object], guidance: str | None) -> None:
    lines = [
        "# Curation Report",
        "",
        f"- Skimmed in: {payload['skimmed_in']}",
        f"- Approved for curation: {payload['approved']}",
        f"- Shortlisted: {payload['shortlisted']}",
        f"- PDFs fetched: {payload['fetched']}",
        f"- Manual upload required: {payload['manual_required']}",
        f"- Rejected by diversity rule: {payload['rejected_by_diversity']}",
        f"- Rejected by literature policy: {payload['policy_rejected']}",
        f"- Warnings: {payload['warnings']}",
    ]
    if guidance:
        lines.extend(["", "## Guidance", "", guidance])
    _write_text(path, "\n".join(lines) + "\n")


def _runner_up_payload(
    source: NormalizedSource,
    original_rank: int,
    reason: str,
) -> dict[str, object]:
    payload = _source_payload(source)
    payload["original_rank"] = original_rank
    payload["diversity_reject_reason"] = reason
    return payload


def _source_payload(source: NormalizedSource) -> dict[str, object]:
    return dict(source.dict())


def _chunks(items: list[NormalizedSource], size: int) -> list[list[NormalizedSource]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _heuristic_relevance(topic: str, source: NormalizedSource) -> float:
    topic_terms = _term_set(topic)
    record_terms = _term_set(f"{source.title} {source.abstract or ''}")
    if not topic_terms:
        return 0.55
    overlap = len(topic_terms & record_terms)
    return _clamp(0.35 + (0.12 * overlap), 0.35, 0.95)


def _term_set(value: str) -> set[str]:
    return {term for term in re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", value.casefold())}


def _shortlist_limit(domain_data: Mapping[str, Any]) -> int:
    search = domain_data.get("search", {})
    if isinstance(search, dict):
        telescope = search.get("telescope", {})
        if isinstance(telescope, dict):
            limit = telescope.get("shortlist_limit")
            if isinstance(limit, int) and limit > 0:
                return limit
    return DEFAULT_SHORTLIST_LIMIT


def _recency_window(domain_data: Mapping[str, Any]) -> int:
    ranking = domain_data.get("ranking", {})
    if isinstance(ranking, dict):
        window = ranking.get("recency_window_years")
        if isinstance(window, int) and window > 0:
            return window
    return 10


def _max_upload_mb() -> int:
    settings = get_settings()
    return settings.max_upload_mb if settings.max_upload_mb > 0 else DEFAULT_MAX_PDF_MB


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return cleaned or "source"


def _access_value(source: NormalizedSource) -> str:
    return str(source.access_status)


def _venue_key(venue: str | None) -> str:
    return _normalize_key(venue or "")


def _author_key(author: str) -> str:
    return _normalize_key(author)


def _normalize_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _clean_string_list(items: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = " ".join(str(item).split())
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            cleaned.append(value)
    return cleaned


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _truncate(value: str | None, max_chars: int) -> str | None:
    if value is None or len(value) <= max_chars:
        return value
    return value[:max_chars]


def _json_dict(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


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


def _write_json(path: Path, payload: object) -> None:
    _write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
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


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def _remove_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def _relative_path(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


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
