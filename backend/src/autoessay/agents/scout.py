"""Scout agent entrypoint for literature discovery."""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, root_validator, validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._language import language_directive
from autoessay.agents._source_verification import classify_source
from autoessay.agents.proposal import load_proposal_payload
from autoessay.clients.common import AccessStatus, ClientSearchError, NormalizedSource
from autoessay.clients.registry import get_lit_client, scout_stub_enabled
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.dedup import DedupStats, deduplicate_sources
from autoessay.domain_loader import load_domain
from autoessay.harness import (
    AuditVerdict,
    AuditWriter,
    HookContext,
    HookRegistry,
    HookResult,
    LLMCallRequest,
    LLMCallResponse,
    hash_text,
)
from autoessay.harness.runner import run_llm_step
from autoessay.memory import MemoryClient, make_memory_pre_llm_hook
from autoessay.models import Project, Run
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

if TYPE_CHECKING:
    from autoessay.agents._topic_fitness import TopicFitnessResult

SCOUT_QUERY_MIN = 4
SCOUT_QUERY_MAX = 8
SOURCE_QUERY_MAX = 8
SOURCE_RESULT_LIMIT = 20
CHINESE_HUMANITIES_VENUES: tuple[str, ...] = (
    "历史研究",
    "中国社会科学",
    "文学评论",
    "哲学研究",
    "经济研究",
)
QUERY_PACK_FIELDS: tuple[str, ...] = (
    "zh_native",
    "en_translated",
    "venue_boosted_zh",
    "exact_title_kernel",
)


class ScoutQuerySet(BaseModel):
    queries: list[str]
    rationale: str

    @validator("queries")
    def _queries_must_have_content(cls, value: list[str]) -> list[str]:
        cleaned = _normalize_queries(value)
        if not cleaned:
            raise ValueError("queries must contain at least one non-empty string")
        return cleaned

    @validator("rationale")
    def _rationale_must_have_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("rationale must be non-empty")
        return cleaned


class QueryPack(BaseModel):
    zh_native: list[str] = Field(default_factory=list)
    en_translated: list[str] = Field(default_factory=list)
    venue_boosted_zh: list[str] = Field(default_factory=list)
    exact_title_kernel: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    rationale: str

    @validator(
        "zh_native",
        "en_translated",
        "venue_boosted_zh",
        "exact_title_kernel",
        "queries",
        pre=True,
    )
    def _query_lists_must_be_strings(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return _normalize_queries(item for item in value if isinstance(item, str))

    @root_validator
    def _populate_compat_queries(cls, values: dict[str, object]) -> dict[str, object]:
        categorized: list[str] = []
        for field in QUERY_PACK_FIELDS:
            raw = values.get(field)
            if isinstance(raw, list):
                categorized.extend(item for item in raw if isinstance(item, str))
        queries = _normalize_queries(categorized)
        if not queries:
            raw_queries = values.get("queries")
            if isinstance(raw_queries, list):
                queries = _normalize_queries(item for item in raw_queries if isinstance(item, str))
        if not queries:
            raise ValueError("query pack must contain at least one usable query")
        values["queries"] = queries
        return values

    @validator("rationale")
    def _rationale_must_have_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("rationale must be non-empty")
        return cleaned


def run_scout(
    run_id: str,
    db_session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run scout. Stage 3.E follow-up P0: ``lock_token`` triggers
    owner-checked phase-start lock release at exit. PR-A4.1b
    (2026-05-02): wraps the agent in ``maybe_run_with_versioning``
    so vanilla first runs also create a ``phase_versions`` row +
    ``run_heads`` + ``phase_version_inputs`` lineage. The
    ``maybe_*`` variant is a no-op when /rerun_phase already
    wrapped us."""
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_scout_with_session(
                run_id,
                session,
                hooks or HookRegistry(),
            )

        maybe_run_with_versioning(session, run, "scout", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "scout", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def _run_scout_with_session(
    run_id: str,
    session: Session,
    hooks: HookRegistry,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    assert_run_active(run, session)
    if run.state not in {"DOMAIN_LOADED", "USER_PROPOSAL_REVIEW", "SCOUT_RUNNING"}:
        raise InvalidTransition(
            "Scout requires DOMAIN_LOADED, USER_PROPOSAL_REVIEW, or SCOUT_RUNNING, "
            f"got {run.state}",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    if run.state != "SCOUT_RUNNING":
        transition(run, "SCOUT_RUNNING", session, reason="Scout started")
    append_event(session, run, "phase_started", {"phase": "scout", "run_id": run.id})
    session.commit()
    session.refresh(run)

    domain = load_domain(_domain_path(project.domain_id))
    run_dir = Path(run.run_dir)
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True, exist_ok=True)

    # PR-263d (W4 wiring, codex round-3 verdict): generate the
    # shadow-baseline anchor BEST-EFFORT at scout start. This is
    # NOT a dependency of scout — it's a sidecar artifact for
    # downstream drafter (PR-263c consumer) and future polish loop
    # (PR-261). Failure is logged as an event but never blocks
    # scout's main job. Stub-mode default ON in dev/CI; flip OFF
    # in real-paper acceptance walks.
    _run_shadow_baseline_best_effort(
        run=run,
        project=project,
        session=session,
        run_dir=run_dir,
    )
    assert_run_active(run, session)

    warnings: list[dict[str, object]] = []
    proposal = _proposal_context(run)
    research_kernel = (
        dict(run.research_kernel_json) if isinstance(run.research_kernel_json, Mapping) else {}
    )
    index_warnings = _chinese_index_coverage_warnings(
        topic=project.title,
        language=project.language,
        domain_data=domain.data,
    )
    warnings.extend(index_warnings)
    for warning in index_warnings:
        assert_run_active(run, session)
        append_event(session, run, "source_index_coverage_warning", warning)
    if index_warnings:
        session.commit()
    queries = _expand_queries(
        project.title,
        domain.data,
        discovery_dir,
        warnings,
        proposal,
        run=run,
        project=project,
        session=session,
        hooks=hooks,
        research_kernel=research_kernel,
    )

    search_result = asyncio.run(
        _collect_sources(
            run=run,
            session=session,
            discovery_dir=discovery_dir,
            domain_data=domain.data,
            topic=project.title,
            research_kernel=research_kernel,
            proposal=proposal,
            queries=queries,
            year_window=_year_window(domain.data),
        ),
    )
    assert_run_active(run, session)
    warnings.extend(search_result["warnings"])

    if not search_result["automated_success"]:
        assert_run_active(run, session)
        _write_warnings(discovery_dir, warnings)
        transition(
            run,
            "FAILED_VENDOR",
            session,
            reason="All automated Scout sources failed",
            payload={"warnings_path": "discovery/warnings.jsonl"},
        )
        append_event(
            session,
            run,
            "phase_failed",
            {"phase": "scout", "failure_class": "failed_vendor"},
        )
        session.commit()
        return {
            "run_id": run.id,
            "state": run.state,
            "sources": 0,
            "warnings": len(warnings),
        }

    raw_sources = search_result["sources"]

    # PR-J9 v1: LLM canonical / frontier mining.
    # ``mine_and_verify_canonical_sources`` does 2 LLM calls (canon
    # + frontier) + Crossref roundtrip verification, and returns
    # NormalizedSource[] with provenance="llm_canon" + canonical_bucket
    # + canonical_rationale. Stub mode (Settings.canonical_mining_stub)
    # short-circuits to []. Mining is enrichment, not gating: any
    # mining failure surfaces as a warning + the scout continues with
    # the vendor-only set. Codex round-1 amendment 3.5: serialize for
    # v1 (no gather() with shared SQLAlchemy session).
    from autoessay.agents._canonical_mining import (
        merge_canonical_with_search,
        mine_and_verify_canonical_sources,
    )

    canonical_sources, mining_warnings = asyncio.run(
        mine_and_verify_canonical_sources(
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            title=project.title,
            research_kernel=research_kernel,
            domain_id=project.domain_id,
        )
    )
    warnings.extend(mining_warnings)
    if canonical_sources:
        raw_sources = merge_canonical_with_search(canonical_sources, raw_sources)
    shadow_sources, shadow_enrichment_warnings = asyncio.run(
        _enrich_shadow_baseline_sources_best_effort(
            run=run,
            session=session,
            run_dir=run_dir,
        )
    )
    warnings.extend(shadow_enrichment_warnings)
    if shadow_sources:
        raw_sources = merge_canonical_with_search(shadow_sources, raw_sources)
    official_sources = _official_archive_sources_for_kernel(research_kernel)
    if official_sources:
        raw_sources = merge_canonical_with_search(official_sources, raw_sources)
        assert_run_active(run, session)
        append_event(
            session,
            run,
            "official_archive_sources_added",
            {
                "phase": "scout",
                "count": len(official_sources),
                "source_ids": [source.source_id for source in official_sources],
            },
        )
        session.commit()
    for source in raw_sources:
        status, confidence = classify_source(source)
        source.verification_status = status
        source.confidence = confidence

    from autoessay.agents._topic_fitness import source_pool_quality_event_needed

    weighted_sources = _apply_source_weights(raw_sources, domain.data)
    deduped_sources, dedup_stats = deduplicate_sources(weighted_sources)
    final_topic_fitness = _final_source_pool_topic_fitness(
        deduped_sources,
        title=project.title,
        research_kernel=research_kernel,
        proposal=proposal,
        domain_data=domain.data,
    )
    final_off_topic_dropped = final_topic_fitness.dropped
    assert_run_active(run, session)
    _write_records_jsonl(discovery_dir / "off_topic_dropped.jsonl", final_off_topic_dropped)
    warnings.append(_topic_fitness_warning(final_topic_fitness.audit))
    if source_pool_quality_event_needed(final_topic_fitness.audit):
        assert_run_active(run, session)
        append_event(session, run, "source_pool_quality_warning", final_topic_fitness.audit)
        session.commit()
    deduped_sources = final_topic_fitness.kept

    assert_run_active(run, session)
    _write_json(discovery_dir / "queries.json", queries)
    _write_jsonl(discovery_dir / "skim_candidates.jsonl", deduped_sources)
    _write_report(discovery_dir / "scout_report.md", weighted_sources, deduped_sources, dedup_stats)
    _write_warnings(discovery_dir, warnings)

    assert_run_active(run, session)
    transition(
        run,
        "USER_SEARCH_REVIEW",
        session,
        reason="Scout completed",
        payload={
            "candidate_count": len(deduped_sources),
            "dedup_losses": dedup_stats.total - dedup_stats.kept,
            "off_topic_dropped": len(final_off_topic_dropped),
            "weak_anchor": _weak_anchor_count(deduped_sources),
        },
    )
    append_event(
        session,
        run,
        "phase_done",
        {
            "phase": "scout",
            "candidate_count": len(deduped_sources),
            "dedup_losses": dedup_stats.total - dedup_stats.kept,
            "off_topic_dropped": len(final_off_topic_dropped),
            "weak_anchor": _weak_anchor_count(deduped_sources),
        },
    )
    session.commit()
    return {
        "run_id": run.id,
        "state": run.state,
        "sources": len(deduped_sources),
        "raw_sources": len(weighted_sources),
        "warnings": len(warnings),
        "queries": len(queries),
        "off_topic_dropped": len(final_off_topic_dropped),
        "weak_anchor": _weak_anchor_count(deduped_sources),
    }


def _official_archive_sources_for_kernel(
    research_kernel: Mapping[str, object] | None,
) -> list[NormalizedSource]:
    """Deterministic primary-source injection for narrow official archives.

    The regular literature clients surface journal/book metadata well,
    but the Bretton Woods kernel explicitly asks for official archive
    evidence. Crossref/OpenAlex will not reliably return IMF annual
    reports, FRASER Board minutes, or archive catalogue records, yet
    those are public official records with stable URLs. This helper
    adds a small verified allowlist only when the kernel is clearly the
    Bretton Woods dollar-gold / London Gold Pool topic.
    """
    if not _is_bretton_gold_kernel(research_kernel):
        return []
    return [
        _official_source(
            source_id="official:imf:annual-report-1968",
            title="International Monetary Fund Annual Report 1968",
            authors=["International Monetary Fund"],
            year=1968,
            venue="IMF Annual Report",
            url="https://www.imf.org/external/pubs/ft/ar/archive/pdf/ar1968.pdf",
            pdf_url="https://www.imf.org/external/pubs/ft/ar/archive/pdf/ar1968.pdf",
            abstract=(
                "Official IMF annual report describing the 1967-1968 gold crisis, "
                "the closing of the London gold market on March 15, 1968, the "
                "March 16-17 Washington meeting of gold-pool central bank "
                "governors, the end of gold-pool arrangements, and the continuing "
                "official $35 transactions among monetary authorities."
            ),
            rank_score=1.45,
        ),
        _official_source(
            source_id="official:fraser:fed-annual-report-1968",
            title="Annual Report of the Board of Governors of the Federal Reserve System 1968",
            authors=["Board of Governors of the Federal Reserve System"],
            year=1968,
            venue="Federal Reserve Annual Report",
            url="https://fraser.stlouisfed.org/files/docs/publications/arfr/1960s/arfr_1968.pdf",
            pdf_url="https://fraser.stlouisfed.org/files/docs/publications/arfr/1960s/arfr_1968.pdf",
            abstract=(
                "Official Federal Reserve annual report covering 1968 gold "
                "developments: first-quarter U.S. gold sales in support of the "
                "gold pool, speculative pressure after sterling devaluation, the "
                "March 17 communique by active gold-pool central banks, and the "
                "move to no longer sell gold except to monetary authorities."
            ),
            rank_score=1.43,
        ),
        _official_source(
            source_id="official:fraser:bog-minutes-1968-03-20",
            title=(
                "Minutes of the Board of Governors of the Federal Reserve System, "
                "Meeting Minutes, March 20, 1968"
            ),
            authors=["Board of Governors of the Federal Reserve System"],
            year=1968,
            venue="FRASER / Federal Reserve Board Minutes",
            url=(
                "https://fraser.stlouisfed.org/title/minutes-board-governors-"
                "federal-reserve-system-821/meeting-minutes-march-20-1968-683087"
            ),
            pdf_url=(
                "https://fraser.stlouisfed.org/files/docs/historical/frsbog/"
                "minutes/19680320_Minutes.pdf"
            ),
            abstract=(
                "Official Federal Reserve Board meeting minutes from March 20, "
                "1968, immediately after the March 17 gold-pool communique, "
                "providing primary board-level context for the official response "
                "to the gold market crisis."
            ),
            rank_score=1.40,
        ),
        _official_source(
            source_id="official:fraser:bog-minutes-1968-03-28",
            title=(
                "Minutes of the Board of Governors of the Federal Reserve System, "
                "Meeting Minutes, March 28, 1968"
            ),
            authors=["Board of Governors of the Federal Reserve System"],
            year=1968,
            venue="FRASER / Federal Reserve Board Minutes",
            url=(
                "https://fraser.stlouisfed.org/title/minutes-board-governors-"
                "federal-reserve-system-821/meeting-minutes-march-28-1968-683092"
            ),
            pdf_url=(
                "https://fraser.stlouisfed.org/files/docs/historical/frsbog/"
                "minutes/19680328_Minutes.pdf"
            ),
            abstract=(
                "Official Federal Reserve Board meeting minutes from March 28, "
                "1968, part of the post-gold-pool crisis decision record and a "
                "primary source for the official-policy sequence after the March "
                "1968 London Gold Pool break."
            ),
            rank_score=1.38,
        ),
        _official_source(
            source_id="official:imf:archives-gold-study-hirsch-1966-1968",
            title="IMF Archives Catalog: Gold Study (Hirsch), December 9, 1966-June 12, 1968",
            authors=["International Monetary Fund Archives"],
            year=1968,
            venue="IMF Archives Catalog",
            url="https://archivescatalog.imf.org/Details/archive/110007533",
            pdf_url=None,
            abstract=(
                "IMF Archives public catalogue record for the Research Department "
                "file 'Gold Study (Hirsch)', dated December 9, 1966-June 12, "
                "1968, subject Gold, reviewed and open for public access. This "
                "is an official archive finding aid for locating IMF gold-study "
                "material in the critical 1966-1968 window."
            ),
            rank_score=1.32,
            access_status=AccessStatus.METADATA_ONLY,
        ),
    ]


def _is_bretton_gold_kernel(research_kernel: Mapping[str, object] | None) -> bool:
    if not isinstance(research_kernel, Mapping):
        return False
    text = " ".join(
        str(research_kernel.get(key) or "")
        for key in ("observed_puzzle", "tentative_question", "scope")
    ).lower()
    has_bretton = "布雷顿" in text or "bretton" in text
    has_gold = "黄金" in text or "gold" in text
    has_convertibility = "可兑换" in text or "convertibility" in text or "gold pool" in text
    return has_bretton and has_gold and has_convertibility


def _official_source(
    *,
    source_id: str,
    title: str,
    authors: list[str],
    year: int,
    venue: str,
    url: str,
    pdf_url: str | None,
    abstract: str,
    rank_score: float,
    access_status: AccessStatus = AccessStatus.OPEN,
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=None,
        url=url,
        pdf_url=pdf_url,
        abstract=abstract,
        source_client="official_archive",
        access_status=access_status,
        license=None,
        rank_score=rank_score,
        risk_flags=[] if access_status == AccessStatus.OPEN else ["metadata_only_no_full_text"],
        provenance="search",
        canonical_bucket="frontier",
        canonical_rationale="deterministic official archive source for research kernel",
        verified_by="official_archive",
    )


async def _collect_sources(
    *,
    run: Run,
    session: Session,
    discovery_dir: Path,
    domain_data: Mapping[str, Any],
    topic: str,
    research_kernel: Mapping[str, object] | None,
    proposal: Mapping[str, object] | None,
    queries: list[str],
    year_window: int | None,
) -> dict[str, Any]:
    from autoessay.agents._topic_fitness import (
        filter_off_topic_candidates,
        source_pool_quality_event_needed,
    )

    tasks: list[asyncio.Task[dict[str, Any]]] = []
    source_configs = _enabled_source_configs(domain_data)
    total = 0
    clients = []
    try:
        for source_config in source_configs:
            source_id = str(source_config["id"])
            client = get_lit_client(source_id, source_config, domain_data)
            clients.append(client)
            source_queries = _queries_for_source(
                topic,
                queries,
                source_config,
                research_kernel=research_kernel,
            )
            for query in source_queries:
                total += 1
                tasks.append(
                    asyncio.create_task(
                        _search_one(
                            client=client,
                            source_id=source_id,
                            query=query,
                            year_window=year_window,
                        ),
                    ),
                )

        sources: list[NormalizedSource] = []
        warnings: list[dict[str, object]] = []
        automated_success: set[str] = set()
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            result = await task
            assert_run_active(run, session)
            source_id = str(result["source_id"])
            query = str(result["query"])
            result_sources = result["sources"]
            error = result["error"]
            if isinstance(result_sources, list):
                sources.extend(result_sources)
            if error is None:
                client = result["client"]
                if getattr(client, "automated", True):
                    automated_success.add(source_id)
            else:
                warnings.append(
                    {
                        "source_id": source_id,
                        "query": query,
                        "failure_class": "fixable_deterministic",
                        "message": str(error),
                    },
                )
            append_event(
                session,
                run,
                "source_progress",
                {
                    "phase": "scout",
                    "source_id": source_id,
                    "query": query,
                    "count": len(result_sources) if isinstance(result_sources, list) else 0,
                    "status": "ok" if error is None else "warning",
                    "error": str(error) if error is not None else None,
                    "completed": completed,
                    "total": total,
                },
            )
            session.commit()
        topic_fitness = filter_off_topic_candidates(
            sources,
            title=topic,
            research_kernel=research_kernel,
            proposal=proposal,
            domain_data=domain_data,
        )
        topic_fitness = _ensure_topic_fitness_dedup_floor(topic_fitness, sources)
        assert_run_active(run, session)
        _write_records_jsonl(
            discovery_dir / "initial_off_topic_dropped.jsonl",
            topic_fitness.dropped,
        )
        warnings.append(_topic_fitness_warning(topic_fitness.audit))
        if source_pool_quality_event_needed(topic_fitness.audit):
            assert_run_active(run, session)
            append_event(session, run, "source_pool_quality_warning", topic_fitness.audit)
            session.commit()
        for source in topic_fitness.kept:
            status, confidence = classify_source(source)
            source.verification_status = status
            source.confidence = confidence
        return {
            "sources": topic_fitness.kept,
            "warnings": warnings,
            "automated_success": bool(automated_success),
        }
    finally:
        for client in clients:
            await client.aclose()


def _ensure_topic_fitness_dedup_floor(
    topic_fitness: TopicFitnessResult,
    candidates: Sequence[NormalizedSource],
) -> TopicFitnessResult:
    from autoessay.agents._topic_fitness import TopicFitnessResult

    min_sources = get_settings().synthesizer_min_processed_sources
    if min_sources <= 0:
        return topic_fitness

    filtered_deduped_count = _deduped_source_count(topic_fitness.kept)
    if filtered_deduped_count >= min_sources:
        return topic_fitness

    raw_candidates = list(candidates)
    raw_deduped_count = _deduped_source_count(raw_candidates)
    if raw_deduped_count < min_sources:
        return topic_fitness

    audit = dict(topic_fitness.audit)
    raw_warnings = audit.get("warnings")
    warning_codes = (
        [item for item in raw_warnings if isinstance(item, str)]
        if isinstance(raw_warnings, list)
        else []
    )
    if "dedup_min_pool_bypass" not in warning_codes:
        warning_codes.append("dedup_min_pool_bypass")
    audit.update(
        {
            "gate_mode": "dedup_min_pool_bypass",
            "bypass_reason": "filtered_deduped_source_floor",
            "filtered_kept_count": len(topic_fitness.kept),
            "filtered_dropped_count": len(topic_fitness.dropped),
            "filtered_deduped_count": filtered_deduped_count,
            "raw_deduped_count": raw_deduped_count,
            "kept_count": len(raw_candidates),
            "dropped_count": 0,
            "drop_rate": 0.0,
            "min_pool_triggered": False,
            "top_drop_reasons": [],
            "warnings": warning_codes,
        },
    )
    return TopicFitnessResult(
        kept=raw_candidates,
        dropped=[],
        drop_reasons={},
        audit=audit,
    )


def _deduped_source_count(sources: Sequence[NormalizedSource]) -> int:
    deduped, _stats = deduplicate_sources(list(sources))
    return len(deduped)


def _final_source_pool_topic_fitness(
    sources: Sequence[NormalizedSource],
    *,
    title: str,
    research_kernel: Mapping[str, object] | None,
    proposal: Mapping[str, object] | None,
    domain_data: Mapping[str, Any],
) -> TopicFitnessResult:
    from autoessay.agents._topic_fitness import filter_off_topic_candidates

    return filter_off_topic_candidates(
        sources,
        title=title,
        research_kernel=research_kernel,
        proposal=proposal,
        domain_data=domain_data,
    )


def _weak_anchor_count(sources: Sequence[NormalizedSource]) -> int:
    return sum(1 for source in sources if "weak_entity_anchor" in source.risk_flags)


async def _search_one(
    *,
    client: Any,
    source_id: str,
    query: str,
    year_window: int | None,
) -> dict[str, Any]:
    try:
        sources = await client.search(query, year_window, SOURCE_RESULT_LIMIT)
        return {
            "client": client,
            "source_id": source_id,
            "query": query,
            "sources": sources,
            "error": None,
        }
    except ClientSearchError as exc:
        return {
            "client": client,
            "source_id": source_id,
            "query": query,
            "sources": [],
            "error": exc,
        }
    except Exception as exc:  # noqa: BLE001 - Scout records vendor failures and continues.
        return {
            "client": client,
            "source_id": source_id,
            "query": query,
            "sources": [],
            "error": exc,
        }


def _expand_queries(
    topic: str,
    domain_data: Mapping[str, Any],
    discovery_dir: Path,
    warnings: list[dict[str, object]],
    proposal: Mapping[str, object] | None,
    *,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    research_kernel: Mapping[str, object] | None = None,
) -> list[str]:
    """PR-J6: ``research_kernel`` (the user-authored kernel from PR-C0
    intake) is now threaded through the prompt + the deterministic
    fallback so scout queries are anchored on the user's
    ``tentative_question`` + ``observed_puzzle`` instead of being
    dominated by ``domain_data.default_query_terms``."""
    if scout_stub_enabled():
        query_pack = _fallback_query_pack(topic, domain_data, proposal, research_kernel)
        _write_query_artifacts(discovery_dir, query_pack)
        return list(query_pack.queries)
    return _expand_queries_via_harness(
        topic=topic,
        domain_data=domain_data,
        discovery_dir=discovery_dir,
        warnings=warnings,
        proposal=proposal,
        run=run,
        project=project,
        session=session,
        hooks=hooks,
        research_kernel=research_kernel,
    )


def _expand_queries_via_harness(
    *,
    topic: str,
    domain_data: Mapping[str, Any],
    discovery_dir: Path,
    warnings: list[dict[str, object]],
    proposal: Mapping[str, object] | None,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    research_kernel: Mapping[str, object] | None = None,
) -> list[str]:
    prompt = _query_object_prompt(topic, domain_data, proposal, research_kernel)
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "Propose a structured scout query pack strongly anchored on the user's "
                    "title and research_kernel. Return one strict JSON object with the four "
                    "query categories and the backward-compatible queries field. Do not "
                    "infer consensus. " + language_directive(project.language)
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.2,
        max_tokens=500,
        response_format={"type": "json_object"},
        request_id="scout_query_expansion",
        prompt_template_id="scout.query_expansion.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="discovery",
        step_id="scout.query_expansion",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=topic,
        run_metadata={
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "llm_optional": True,
            "memory_query": f"phase=scout topic={project.title} domain={project.domain_id}",
        },
    )
    # PR-J6 codex round-1 amendment: register the title-overlap hook on
    # a fresh ``HookRegistry`` per call. The caller's ``hooks`` object
    # may be reused across runs (see ``run_scout(hooks=hooks)``); a
    # run-specific hook attached to it would leak the previous run's
    # title/kernel into the next, causing the next run to be rejected
    # for queries it would otherwise accept.
    call_hooks = HookRegistry()
    _copy_pre_llm_hooks(hooks, call_hooks)
    _copy_post_llm_hooks(hooks, call_hooks)
    _register_scout_memory_hook(call_hooks)
    _register_scout_title_overlap_hook(
        call_hooks,
        title=topic,
        research_kernel=research_kernel,
        ratio=get_settings().scout_title_anchor_ratio,
    )
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Scout")

    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=call_hooks,
                context=context,
                output_schema=QueryPack,
                audit=audit,
                max_corrective_retries=2,
                llm_optional=True,
                fallback=lambda: _fallback_query_pack(
                    topic,
                    domain_data,
                    proposal,
                    research_kernel,
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - Scout can continue with domain templates.
        warnings.append(
            {
                "source_id": "llm",
                "failure_class": "fixable_prompt",
                "message": f"query expansion transport failed; used domain templates: {exc}",
            },
        )
        query_pack = _fallback_query_pack(topic, domain_data, proposal, research_kernel)
        _write_query_artifacts(discovery_dir, query_pack)
        return list(query_pack.queries)

    parsed = response.parsed
    if isinstance(parsed, QueryPack):
        query_pack = parsed
        queries = _normalize_queries(query_pack.queries)
    elif isinstance(parsed, ScoutQuerySet):
        query_pack = _query_pack_from_queries(
            parsed.queries,
            rationale=parsed.rationale,
            topic=topic,
            domain_data=domain_data,
            proposal=proposal,
            research_kernel=research_kernel,
        )
        queries = _normalize_queries(query_pack.queries)
    elif isinstance(parsed, Mapping):
        try:
            query_pack = QueryPack.parse_obj(parsed)
        except Exception:  # noqa: BLE001 - fallback below records a deterministic warning.
            raw_queries = parsed.get("queries", [])
            query_pack = _query_pack_from_queries(
                raw_queries if isinstance(raw_queries, list) else [],
                rationale=str(parsed.get("rationale") or "LLM query response"),
                topic=topic,
                domain_data=domain_data,
                proposal=proposal,
                research_kernel=research_kernel,
            )
        queries = _normalize_queries(query_pack.queries)
    else:
        query_pack = _fallback_query_pack(topic, domain_data, proposal, research_kernel)
        queries = []
    if not queries:
        warnings.append(
            {
                "source_id": "llm",
                "failure_class": "fixable_prompt",
                "message": "query expansion returned no usable queries; used domain templates",
            },
        )
        query_pack = _fallback_query_pack(topic, domain_data, proposal, research_kernel)
        queries = _normalize_queries(query_pack.queries)
    _write_query_artifacts(discovery_dir, query_pack)
    return queries


def _copy_pre_llm_hooks(src: HookRegistry, dst: HookRegistry) -> None:
    """Defensive copy of pre_llm hook entries so the per-call registry
    inherits caller-registered hooks (e.g. session-level memory pre_llm)
    without mutating the caller's registry."""
    for name, fn in getattr(src, "_pre_llm", []):
        dst.register_pre_llm(name, fn)


def _copy_post_llm_hooks(src: HookRegistry, dst: HookRegistry) -> None:
    for name, fn in getattr(src, "_post_llm", []):
        dst.register_post_llm(name, fn)


def _register_scout_memory_hook(hooks: HookRegistry) -> None:
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


# ----------------------------------------------------------------------
# PR-J6 — title/kernel overlap gate
# ----------------------------------------------------------------------


_KEYWORD_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "as",
        "if",
        "than",
        "then",
        "so",
        "this",
        "that",
        "these",
        "those",
        "how",
        "why",
        "what",
        "when",
        "where",
        "which",
        "do",
        "did",
        "does",
        "shall",
        "will",
        "would",
        "should",
        "could",
        "can",
        "may",
        "might",
        "have",
        "has",
        "had",
    },
)


def _looks_like_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _extract_keywords(text: str) -> set[str]:
    """Tokenize ``text`` for the title-overlap gate. Reuses jieba for
    Chinese (codex round-1 amendment 2.3 — already pinned in
    ``backend/pyproject.toml``); falls back to CJK char-bigrams when
    jieba is unavailable. Latin / mixed text uses a regex over
    word-boundary tokens.

    Drops single-character tokens and a small Western stopword list
    so common function words don't trivially anchor a query.

    Returns lower-cased set so caller's ``a & b`` overlap is symmetric.
    """
    if not isinstance(text, str) or not text.strip():
        return set()
    tokens: list[str] = []
    if _looks_like_chinese(text):
        try:
            import jieba

            tokens.extend(
                token.strip() for token in jieba.cut(text, cut_all=False, HMM=True) if token.strip()
            )
        except Exception:  # noqa: BLE001 — jieba can warn-then-fail at first import
            cjk_only = "".join(ch for ch in text if "一" <= ch <= "鿿")
            tokens.extend(cjk_only[i : i + 2] for i in range(len(cjk_only) - 1))
    # Always also extract Latin-style tokens (mixed-script titles like
    # "Bourdieu in 江南" produce one Latin keyword + several CJK ones).
    tokens.extend(re.findall(r"[A-Za-z][A-Za-z'-]{1,}", text))

    out: set[str] = set()
    for raw in tokens:
        cleaned = raw.strip().casefold()
        if not cleaned:
            continue
        if len(cleaned) < 2:
            continue
        if cleaned in _KEYWORD_STOPWORDS:
            continue
        out.add(cleaned)
    return out


def _register_scout_title_overlap_hook(
    hooks: HookRegistry,
    *,
    title: str,
    research_kernel: Mapping[str, object] | None,
    ratio: float,
) -> None:
    """Post-LLM hook that rejects scout query-expansion responses where
    fewer than ``ratio`` of the returned queries pass the entity +
    concept anchor check.

    PR-J8 (codex round-1 amendment 8): the original PR-J6 hook accepted
    a query as ``anchored`` if it contained ≥1 keyword from title OR
    kernel. In Chinese this is too lax — a query like "韩国 出口" only
    needs the entity word "韩国" to pass, then OpenAlex returns a 2025
    RCEP paper whose title also contains "对韩出口". The query passed
    the gate but the result is off-topic.

    Tightened contract: each query must contain ≥1 ENTITY keyword
    (from title) AND ≥1 CONCEPT keyword (from
    ``research_kernel.tentative_question`` / ``observed_puzzle`` /
    ``theory_preference`` / ``method_preference`` / ``scope``). Title
    alone is the entity bucket; concept covers what the user wants
    to ASK about that entity. Both must appear in each accepted query.

    Falls back to the OR-anchor (PR-J6) behavior when only one of
    title or kernel produces keywords — degrade open is better than
    rejecting every response when one bucket is empty.

    Codex round-1 amendments folded:
      * 2.3 — jieba for CJK with bigram fallback.
      * 2.3 — annotations include both ``message`` and ``errors`` keys
        so ``run_llm_step``'s corrective-suffix retry has actionable
        feedback (the harness only extracts those keys for the retry
        message; bare ``anchored_count`` would be invisible to the LLM).
      * 2.3 — the registry passed in is the per-call copy made by
        ``_expand_queries_via_harness``; this hook is NOT registered on
        the caller's shared ``HookRegistry``.
      * 6 — gate is a structural guardrail, not a semantic relevance
        proof. Real-paper validation still required for substance.
      * J8.8 — entity + concept semantic anchor (this rewrite).
    """
    title_keywords = _extract_keywords(title)
    raw_concept_keywords = _gather_kernel_concept_keywords(research_kernel)
    # Subtract title overlap from the concept bucket — otherwise an
    # entity-only query like "韩国 RCEP 机电" trivially "anchors on
    # concept" because the kernel's tentative_question also contains
    # the entity word "韩国". The AND-gate would then degenerate back
    # to the J6 OR-gate. Concept must be DISJOINT from entity for the
    # gate to do useful work.
    concept_keywords = raw_concept_keywords - title_keywords
    if not title_keywords and not concept_keywords:
        return  # nothing to anchor on — degrade open

    # If only one bucket has content (after subtraction), fall back to
    # the OR-anchor that PR-J6 used (degrade open). Both buckets
    # non-empty → tighten to AND.
    use_and_gate = bool(title_keywords) and bool(concept_keywords)

    def _hook(ctx: HookContext, response: LLMCallResponse) -> HookResult | None:
        del ctx
        parsed = response.parsed
        queries: list[str] = []
        if isinstance(parsed, QueryPack | ScoutQuerySet):
            queries = list(parsed.queries)
        elif isinstance(parsed, Mapping):
            raw = parsed.get("queries", [])
            if isinstance(raw, list):
                queries = [str(q) for q in raw if isinstance(q, str)]
        if not queries:
            return None  # let downstream handle empty queries
        anchored = 0
        per_query: list[dict[str, object]] = []
        for q in queries:
            q_keywords = _extract_keywords(q)
            entity_match = title_keywords & q_keywords
            concept_match = concept_keywords & q_keywords
            if use_and_gate:
                is_anchored = bool(entity_match) and bool(concept_match)
            else:
                is_anchored = bool(entity_match) or bool(concept_match)
            if is_anchored:
                anchored += 1
            per_query.append(
                {
                    "query": q,
                    "anchored": is_anchored,
                    "entity_match": sorted(entity_match)[:5],
                    "concept_match": sorted(concept_match)[:5],
                },
            )
        if anchored * 1.0 / len(queries) >= ratio:
            return None
        title_hint = sorted(title_keywords)[:8]
        concept_hint = sorted(concept_keywords)[:8]
        if use_and_gate:
            message = (
                f"Only {anchored} of {len(queries)} queries pass the entity + "
                f"concept anchor check (need ≥{ratio:.0%}). Each query MUST "
                "contain at least one keyword from the title (the entity — "
                f"e.g. {title_hint}) AND at least one keyword from the "
                "research_kernel concept set (the question / mechanism — "
                f"e.g. {concept_hint}). Single-keyword anchoring (entity "
                "only OR concept only) is no longer accepted because it "
                "lets off-topic results through (e.g. an entity-only "
                "Chinese query like '韩国 出口' returns 2025 trade papers "
                "for a 1945-1990 Korean miracle research kernel)."
            )
            errors = [
                "queries: each must contain ≥1 title keyword AND ≥1 "
                "research_kernel concept keyword (entity + concept anchor)",
            ]
        else:
            message = (
                f"Only {anchored} of {len(queries)} queries share a keyword "
                f"with the title or research_kernel.tentative_question "
                f"(need ≥{ratio:.0%}). Rewrite the queries to anchor each on "
                f"a concrete title term ({title_hint}) or a kernel concept "
                f"({concept_hint})."
            )
            errors = [
                "queries: at least half must contain ≥1 keyword from the "
                "title or research_kernel concept set",
            ]
        return HookResult(
            annotations={
                "message": message,
                "errors": errors,
                "anchored_count": anchored,
                "total_count": len(queries),
                "ratio_required": ratio,
                "gate_mode": "entity_and_concept" if use_and_gate else "or_fallback",
                "title_keywords": title_hint,
                "concept_keywords": concept_hint,
                "per_query": per_query,
            },
            verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
        )

    hooks.register_post_llm("scout_title_overlap", _hook)


def _gather_kernel_concept_keywords(
    research_kernel: Mapping[str, object] | None,
) -> set[str]:
    """PR-J8: pool concept keywords from every kernel field that
    expresses what the user wants to ASK about (vs the entity itself,
    which lives in the title). ``tentative_question`` carries the
    central concept; ``observed_puzzle`` / ``theory_preference`` /
    ``method_preference`` / ``scope`` add supporting concept terms
    (theory anchors, methodological terms, era markers).

    Excludes ``scope`` proper-noun era markers from the concept bucket
    if they're already in the title, but this is hard to do
    programmatically — accept the slight overlap; the AND-gate still
    works because we just need ≥1 from each side.
    """
    if not isinstance(research_kernel, Mapping):
        return set()
    out: set[str] = set()
    for key in (
        "tentative_question",
        "observed_puzzle",
        "theory_preference",
        "method_preference",
        "scope",
    ):
        value = research_kernel.get(key)
        if isinstance(value, str) and value.strip():
            out.update(_extract_keywords(value))
    return out


def _parse_query_json(value: str) -> list[str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and item.strip()]


def _normalize_queries(queries: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for query in queries:
        cleaned = " ".join(query.split())
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            normalized.append(cleaned)
        if len(normalized) >= SCOUT_QUERY_MAX:
            break
    return normalized[:SCOUT_QUERY_MAX]


def _fallback_query_pack(
    topic: str,
    domain_data: Mapping[str, Any],
    proposal: Mapping[str, object] | None = None,
    research_kernel: Mapping[str, object] | None = None,
) -> QueryPack:
    base_queries = _fallback_queries(topic, domain_data, proposal, research_kernel)
    hard_constraints = _query_hard_constraints(proposal, research_kernel)
    constrained_queries = _hard_constraint_queries(topic, hard_constraints)
    base_queries = _normalize_queries([*constrained_queries, *base_queries])
    zh_native = _normalize_queries(query for query in base_queries if _looks_like_chinese(query))[
        :2
    ]
    en_translated = _normalize_queries(
        query for query in base_queries if not _looks_like_chinese(query)
    )[:2]
    if not en_translated:
        en_translated = _fallback_english_queries(topic, domain_data)[:2]
    return QueryPack(
        zh_native=zh_native,
        en_translated=en_translated,
        venue_boosted_zh=_venue_boost_queries(topic, research_kernel),
        exact_title_kernel=_exact_title_kernel_queries(topic, research_kernel),
        rationale="deterministic query-pack fallback",
    )


def _query_pack_from_queries(
    queries: Iterable[object],
    *,
    rationale: str,
    topic: str,
    domain_data: Mapping[str, Any],
    proposal: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None,
) -> QueryPack:
    normalized = _normalize_queries(query for query in queries if isinstance(query, str))
    if not normalized:
        return _fallback_query_pack(topic, domain_data, proposal, research_kernel)
    return QueryPack(queries=normalized, rationale=rationale or "legacy query response")


def _fallback_english_queries(topic: str, domain_data: Mapping[str, Any]) -> list[str]:
    search = domain_data.get("search", {})
    default_terms = search.get("default_query_terms", []) if isinstance(search, dict) else []
    candidates = [
        f"{topic} {term}"
        for term in default_terms
        if isinstance(term, str) and term.strip() and not _looks_like_chinese(term)
    ]
    if not candidates:
        candidates = [f"{topic} financial history", f"{topic} economic history"]
    return _normalize_queries(candidates)


def _query_hard_constraints(
    proposal: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None,
) -> list[str]:
    """Extract high-signal query constraints from proposal/kernel text.

    These are method/data/period/granularity anchors such as
    ``gravity model``, ``UN COMTRADE``, ``HS-6`` and ``2002-2021``.
    They should travel with the title into Scout queries; otherwise
    Crossref/OpenAlex broad bibliographic matching drifts toward
    generic agriculture / supply-chain / model papers.
    """
    candidates: list[str] = []
    if isinstance(proposal, Mapping):
        for keyword in _proposal_keywords(proposal):
            candidates.append(keyword)
        for key in ("research_question", "preliminary_approach", "scope"):
            value = proposal.get(key)
            if isinstance(value, str):
                candidates.extend(_hard_constraint_phrases(value))
    if isinstance(research_kernel, Mapping):
        for key in (
            "tentative_question",
            "observed_puzzle",
            "theory_preference",
            "method_preference",
            "scope",
        ):
            value = research_kernel.get(key)
            if isinstance(value, str):
                candidates.extend(_hard_constraint_phrases(value))
    return _normalize_queries(_high_signal_constraint(term) for term in candidates)


def _hard_constraint_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    # Data sets / institutions / classification systems:
    # UN COMTRADE, HS-6, OECD, IMF, RCEP, DID, IV, etc.
    phrases.extend(
        match.group(0).strip()
        for match in re.finditer(r"\b[A-Z][A-Z0-9&/-]*(?:\s+[A-Z][A-Z0-9&/-]*){0,3}\b", text)
    )
    # Time windows and policy periods.
    phrases.extend(
        match.group(0).replace(" ", "")
        for match in re.finditer(r"\b(?:18|19|20)\d{2}\s*[-–—]\s*(?:18|19|20)\d{2}\b", text)
    )
    # English method/data phrases commonly lost when the surrounding
    # title is Chinese.
    for match in re.finditer(
        r"\b(?:gravity model|difference[- ]in[- ]differences|event study|panel data|"
        r"fixed effects?|instrumental variables?|UN COMTRADE|COMTRADE|HS[- ]?\d+)\b",
        text,
        flags=re.IGNORECASE,
    ):
        phrases.append(match.group(0).strip())
    return phrases


def _high_signal_constraint(term: str) -> str:
    cleaned = " ".join(term.strip().split())
    if not cleaned:
        return ""
    # Keep common acronyms uppercase and normalize HS code spacing.
    cleaned = re.sub(r"\bHS\s*-?\s*(\d+)\b", r"HS-\1", cleaned, flags=re.IGNORECASE)
    upper_like = cleaned.upper()
    if cleaned == upper_like or any(ch.isdigit() for ch in cleaned):
        return upper_like
    return cleaned


def _hard_constraint_queries(topic: str, constraints: Sequence[str]) -> list[str]:
    if not constraints:
        return []
    compact = _normalize_queries(constraints)[:5]
    joined = " ".join(compact[:4])
    candidates = [f"{topic} {joined}".strip()]
    if len(compact) >= 2:
        candidates.append(f"{compact[0]} {compact[1]}")
    if len(compact) >= 3:
        candidates.append(" ".join(compact[:3]))
    return _normalize_queries(candidates)


def _venue_boost_queries(
    topic: str,
    research_kernel: Mapping[str, object] | None,
) -> list[str]:
    seed = _first_query_seed(topic, research_kernel)
    return _normalize_queries(f"{seed} 《{venue}》" for venue in CHINESE_HUMANITIES_VENUES[:2])


def _exact_title_kernel_queries(
    topic: str,
    research_kernel: Mapping[str, object] | None,
) -> list[str]:
    title = topic.strip()
    kernel_seeds = _kernel_query_seeds(research_kernel)
    candidates: list[str] = []
    if title and kernel_seeds:
        candidates.append(f'"{title}" "{kernel_seeds[0]}"')
    if title:
        candidates.append(f'"{title}"')
    for seed in kernel_seeds[1:2]:
        candidates.append(f'"{seed}"')
    return _normalize_queries(candidates)


def _first_query_seed(
    topic: str,
    research_kernel: Mapping[str, object] | None,
) -> str:
    for seed in _kernel_query_seeds(research_kernel):
        return f"{topic} {seed}".strip()
    return topic.strip()


def _kernel_query_seeds(research_kernel: Mapping[str, object] | None) -> list[str]:
    if not isinstance(research_kernel, Mapping):
        return []
    seeds: list[str] = []
    for key in ("tentative_question", "observed_puzzle", "scope"):
        value = research_kernel.get(key)
        if isinstance(value, str) and value.strip():
            seeds.append(value.strip()[:80])
    return _normalize_queries(seeds)


def _write_query_artifacts(discovery_dir: Path, query_pack: QueryPack) -> None:
    _write_json(discovery_dir / "query_pack.json", _query_pack_payload(query_pack))
    _write_json(discovery_dir / "queries.json", list(query_pack.queries))


def _query_pack_payload(query_pack: QueryPack) -> dict[str, object]:
    return {
        "zh_native": list(query_pack.zh_native),
        "en_translated": list(query_pack.en_translated),
        "venue_boosted_zh": list(query_pack.venue_boosted_zh),
        "exact_title_kernel": list(query_pack.exact_title_kernel),
        "queries": list(query_pack.queries),
        "rationale": query_pack.rationale,
    }


def _fallback_queries(
    topic: str,
    domain_data: Mapping[str, Any],
    proposal: Mapping[str, object] | None = None,
    research_kernel: Mapping[str, object] | None = None,
) -> list[str]:
    """PR-J6: kernel-first fallback. The deterministic fallback used to
    walk ``proposal_question → proposal_keywords → domain_templates``,
    which left ``runs.research_kernel_json::tentative_question`` (the
    user-authored field) entirely out of the candidate set. Now the
    kernel comes first; domain templates are last.

    Codex round-1 amendment 2.4: cap query length AFTER composing
    candidates (handled by ``_normalize_queries``); don't truncate
    individual kernel fields here beyond the long-string clamp in
    ``_kernel_query_payload``.
    """
    candidates: list[str] = []
    if isinstance(research_kernel, Mapping):
        tentative = research_kernel.get("tentative_question")
        if isinstance(tentative, str) and tentative.strip():
            candidates.append(tentative.strip())
            candidates.append(f"{topic} {tentative.strip()}")
        puzzle = research_kernel.get("observed_puzzle")
        if isinstance(puzzle, str) and puzzle.strip():
            # observed_puzzle can be 200+ chars; clamp to a reasonable
            # search-query length but keep the leading fragment.
            candidates.append(puzzle.strip()[:120])
    proposal_question = _proposal_research_question(proposal)
    if proposal_question:
        candidates.append(proposal_question)
    proposal_keywords = _proposal_keywords(proposal)
    for keyword in proposal_keywords:
        candidates.append(f"{topic} {keyword}")
    candidates.append(topic)
    for source_config in _enabled_source_configs(domain_data):
        candidates.extend(_render_templates(topic, source_config))
    if len(candidates) < SCOUT_QUERY_MIN:
        search = domain_data.get("search", {})
        default_terms = search.get("default_query_terms", []) if isinstance(search, dict) else []
        for term in default_terms:
            if isinstance(term, str):
                candidates.append(f"{topic} {term}")
    if len(candidates) < SCOUT_QUERY_MIN:
        candidates.extend([f"{topic} recent literature", f"{topic} frontier debate"])
    return _normalize_queries(candidates)[:SCOUT_QUERY_MAX]


def _kernel_query_payload(
    research_kernel: Mapping[str, object] | None,
) -> dict[str, object]:
    """PR-J7: thin wrapper around the shared
    ``agents._research_kernel_prompt.research_kernel_for_prompt`` so
    scout, drafter, ideator, critic, synthesizer all see identical
    field projection + length capping. Backward compatible — same
    return shape as the original PR-J6 implementation."""
    from autoessay.agents._research_kernel_prompt import research_kernel_for_prompt

    return research_kernel_for_prompt(research_kernel)


def _query_prompt(
    topic: str,
    domain_data: Mapping[str, Any],
    proposal: Mapping[str, object] | None,
    *,
    suffix: str,
) -> str:
    search = domain_data.get("search", {})
    default_terms = search.get("default_query_terms", []) if isinstance(search, dict) else []
    exclusion_terms = search.get("exclusion_terms", []) if isinstance(search, dict) else []
    payload = {
        "topic": topic,
        "domain_id": domain_data.get("id"),
        "default_query_terms": default_terms,
        "exclusion_terms": exclusion_terms,
        "proposal_research_question": _proposal_research_question(proposal),
        "proposal_preliminary_keywords": _proposal_keywords(proposal),
        "required_count": [SCOUT_QUERY_MIN, SCOUT_QUERY_MAX],
    }
    return (
        "Create 4 to 8 search queries for the supplied financial-history domain payload. "
        "Use the proposal context to focus the search when present, but do not treat it "
        "as a final novelty claim. Return strict JSON list[str]. Avoid excluded terms "
        "and do not assert findings.\n"
        f"{json.dumps(payload, sort_keys=True)}"
        f"{suffix}"
    )


def _query_object_prompt(
    topic: str,
    domain_data: Mapping[str, Any],
    proposal: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None = None,
) -> str:
    """PR-J6: anchor the prompt on title + ``research_kernel`` (the
    user-authored kernel from PR-C0 intake) as co-primary; demote
    ``domain_data.default_query_terms`` to AUXILIARY status so they
    don't hijack niche topics.

    Codex round-1 amendment 2.1: title gives lexical topic; kernel
    gives focus + disambiguation. Both outrank proposal; domain terms
    are auxiliary only. Schema is a structured ``QueryPack`` object,
    with ``queries`` retained for backward compatibility.
    """
    search = domain_data.get("search", {})
    default_terms = search.get("default_query_terms", []) if isinstance(search, dict) else []
    exclusion_terms = search.get("exclusion_terms", []) if isinstance(search, dict) else []
    payload = {
        # User-authored anchors (highest priority).
        "title": topic,
        "research_kernel": _kernel_query_payload(research_kernel),
        "hard_constraints": _query_hard_constraints(proposal, research_kernel),
        # LLM-generated context (medium priority).
        "proposal_research_question": _proposal_research_question(proposal),
        "proposal_preliminary_keywords": _proposal_keywords(proposal),
        # Domain auxiliary (lowest priority).
        "domain_id": domain_data.get("id"),
        "domain_default_query_terms_for_inspiration_only": default_terms,
        "exclusion_terms": exclusion_terms,
        "venue_boost_keywords": [f"《{venue}》" for venue in CHINESE_HUMANITIES_VENUES],
        "required_count": [SCOUT_QUERY_MIN, SCOUT_QUERY_MAX],
        "output_schema": {
            "zh_native": (
                "list[str], 1-2 Chinese/native-language queries preserving title/kernel wording"
            ),
            "en_translated": "list[str], 1-2 English translations of the title/kernel search angle",
            "venue_boosted_zh": "list[str], 1-2 Chinese queries including one venue_boost_keyword",
            "exact_title_kernel": (
                "list[str], 1-2 exact phrase queries using title and/or kernel text"
            ),
            "queries": "list[str]",
            "rationale": "short string explaining why these query angles were chosen",
        },
    }
    return (
        "Generate search queries for the user's specific paper. Anchor the queries on:\n"
        "  1. **title** (highest priority — the user explicitly wrote this)\n"
        "  2. **research_kernel.tentative_question** + **observed_puzzle** "
        "(user-authored research focus)\n"
        "  3. **hard_constraints** (method, dataset, classification level, period; "
        "must be preserved verbatim when present)\n"
        "  4. proposal_research_question (LLM-generated; may have drifted from title — "
        "treat as supporting context, not authority)\n"
        "  5. domain_default_query_terms (inspiration ONLY; do NOT let domain templates "
        "dominate when the user's title is in a niche subtopic)\n\n"
        "Output requirements (CHECK BEFORE RETURNING):\n"
        "- Produce FOUR query categories: zh_native, en_translated, venue_boosted_zh, "
        "exact_title_kernel\n"
        "- Each category must contain 1-2 strings when possible; total merged queries "
        "must be 4-8\n"
        "- Also include backward-compatible queries: merge the four category lists in "
        "that order, deduplicated\n"
        "- zh_native must preserve the Chinese/native wording from title or "
        "research_kernel when present\n"
        "- en_translated must translate the same title/kernel search angle into English; "
        "do not broaden it into generic domain terms\n"
        "- venue_boosted_zh must include at least one of the supplied Chinese venue "
        "keywords when the title/kernel is Chinese or China-facing\n"
        "- exact_title_kernel should use quoted exact phrases for title and/or compact "
        "kernel phrases\n"
        "- If hard_constraints is non-empty, at least HALF of merged queries must include "
        "one or more of those constraints verbatim (examples: gravity model, UN COMTRADE, "
        "HS-6, 2002-2021). Do not replace them with broad synonyms.\n"
        "- At least HALF of merged queries MUST contain ≥1 keyword from title or "
        "research_kernel.tentative_question (verbatim or close synonym)\n"
        "- Do NOT return queries that are pure restatements of domain templates "
        "without title/kernel grounding\n\n"
        "Return strict JSON object with keys zh_native, en_translated, venue_boosted_zh, "
        "exact_title_kernel, queries, and rationale. Avoid excluded terms and do not "
        "assert findings.\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def _run_shadow_baseline_best_effort(
    *,
    run: Run,
    project: Project,
    session: Session,
    run_dir: Path,
) -> None:
    """PR-263d (W4 wiring): generate the shadow-baseline anchor at
    scout start, BEST-EFFORT only.

    Codex round-3 verdict: shadow-baseline is a sidecar artifact for
    downstream drafter (PR-263c consumer) + future polish loop
    (PR-261). Failure is non-fatal — it emits a
    ``shadow_baseline_failed`` event and lets scout continue. The
    drafter consumer (``shadow_knowledge_directive_for_run``)
    already handles the missing-artifact case by returning an
    empty directive.

    1× retry on parse failure / LLM error (codex Q2: don't bake in
    aggressive retries; shadow is enhancement, not load-bearing).
    """
    from autoessay.agents.shadow_baseline import (
        load_shadow_baseline,
        persist_shadow_baseline,
        run_shadow_baseline,
    )
    from autoessay.harness import AuditWriter

    # Skip if already on disk (idempotent on phase reruns / scout
    # retries — don't burn another LLM call when the artifact is
    # already there).
    if load_shadow_baseline(run_dir) is not None:
        return

    raw_kernel = run.research_kernel_json if isinstance(run.research_kernel_json, Mapping) else {}
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="ShadowBaseline")

    last_err: Exception | None = None
    for attempt in range(2):  # initial + 1 retry
        try:
            output = run_shadow_baseline(
                run_id=run.id,
                project_title=project.title,
                user_id=project.user_id,
                research_kernel=raw_kernel,
                audit=audit,
            )
            if output is not None:
                persist_shadow_baseline(run_dir, output)
                append_event(
                    session,
                    run,
                    "shadow_baseline_done",
                    {
                        "phase": "scout",
                        "attempt": attempt + 1,
                        "manuscript_chars": len(output.manuscript_markdown),
                        "argument_map_entries": len(output.argument_map),
                        "reference_candidates": len(output.reference_candidates),
                    },
                )
                session.commit()
                return
            # Output was None — parse failure inside run_shadow_baseline.
            last_err = RuntimeError("shadow_baseline returned None")
        except Exception as exc:  # noqa: BLE001 - logged + degrades
            last_err = exc

    append_event(
        session,
        run,
        "shadow_baseline_failed",
        {
            "phase": "scout",
            "attempts": 2,
            "error_class": type(last_err).__name__ if last_err else "unknown",
            "error_message": str(last_err)[:200] if last_err else "",
        },
    )
    session.commit()


async def _enrich_shadow_baseline_sources_best_effort(
    *,
    run: Run,
    session: Session,
    run_dir: Path,
) -> tuple[list[NormalizedSource], list[dict[str, object]]]:
    """Verify shadow-baseline reference_candidates and return citable sources.

    This is scout-side enrichment, not a gate: failures are surfaced as
    warnings/events and scout continues with its existing source pool. The
    returned sources still go through scout classification and curator's
    verified-only gate before they can become citation material.
    """
    from autoessay.agents.shadow_baseline import load_shadow_baseline
    from autoessay.agents.source_enrichment import enrich_with_shadow_baseline
    from autoessay.clients.crossref import CrossrefClient
    from autoessay.clients.openalex import OpenAlexClient
    from autoessay.clients.openlibrary import OpenLibraryClient

    output = load_shadow_baseline(run_dir)
    if output is None:
        return [], []

    crossref_client = CrossrefClient()
    openalex_client = OpenAlexClient(filters=None)
    openlibrary_client = OpenLibraryClient()
    try:
        verified, drop_warnings = await enrich_with_shadow_baseline(
            output,
            crossref_client=crossref_client,
            openalex_client=openalex_client,
            openlibrary_client=openlibrary_client,
        )
    except Exception as exc:  # noqa: BLE001 - enrichment must not block scout
        append_event(
            session,
            run,
            "shadow_baseline_enrichment_failed",
            {
                "phase": "scout",
                "error_class": type(exc).__name__,
                "error_message": str(exc)[:200],
                "reference_candidates": len(output.reference_candidates),
            },
        )
        session.commit()
        return [], [
            {
                "source_id": "shadow_baseline_enrichment",
                "failure_class": "fixable_deterministic",
                "message": f"shadow baseline source enrichment failed: {exc}",
            }
        ]
    finally:
        await crossref_client.aclose()
        await openalex_client.aclose()
        await openlibrary_client.aclose()

    append_event(
        session,
        run,
        "shadow_baseline_enrichment_done",
        {
            "phase": "scout",
            "reference_candidates": len(output.reference_candidates),
            "verified_count": len(verified),
            "dropped_count": len(drop_warnings),
        },
    )
    session.commit()

    warnings: list[dict[str, object]] = []
    for drop in drop_warnings:
        candidate = drop.get("candidate")
        candidate_title = candidate.get("title") if isinstance(candidate, dict) else None
        title = drop.get("work_title") or drop.get("title") or candidate_title
        reason = drop.get("reason") or drop.get("crossref_reason") or "not_verified"
        warnings.append(
            {
                "source_id": f"shadow_baseline_ref:{str(title or 'unknown')[:80]}",
                "failure_class": "fixable_deterministic",
                "message": f"shadow baseline reference candidate not verified: {reason}",
            }
        )
    return verified, warnings


def _proposal_context(run: Run) -> dict[str, object] | None:
    try:
        payload = load_proposal_payload(run)
    except FileNotFoundError:
        return None
    proposal_json = payload.get("proposal_json")
    return dict(proposal_json) if isinstance(proposal_json, dict) else None


def _proposal_research_question(proposal: Mapping[str, object] | None) -> str:
    if proposal is None:
        return ""
    value = proposal.get("research_question")
    return value if isinstance(value, str) else ""


def _proposal_keywords(proposal: Mapping[str, object] | None) -> list[str]:
    if proposal is None:
        return []
    keywords = proposal.get("preliminary_keywords")
    if not isinstance(keywords, list):
        return []
    return [keyword for keyword in keywords if isinstance(keyword, str) and keyword.strip()]


def _chinese_index_coverage_warnings(
    *,
    topic: str,
    language: str | None,
    domain_data: Mapping[str, Any],
) -> list[dict[str, object]]:
    source_ids = [str(source.get("id") or "") for source in _enabled_source_configs(domain_data)]
    has_chinese_index = any(source_id in {"cnki", "wanfang"} for source_id in source_ids)
    if has_chinese_index:
        return []
    if language != "zh" and not _looks_like_chinese(topic):
        return []
    return [
        {
            "source_id": "source_index_coverage",
            "query": topic,
            "failure_class": "coverage_warning",
            "message": (
                "Chinese project/title but this domain has no CNKI/Wanfang source enabled; "
                "Scout will use the configured international metadata indexes only."
            ),
            "configured_sources": [source_id for source_id in source_ids if source_id],
            "missing_indexes": ["cnki", "wanfang"],
        }
    ]


def _enabled_source_configs(domain_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    search = domain_data.get("search", {})
    sources = search.get("sources", []) if isinstance(search, dict) else []
    if not isinstance(sources, list):
        sources = []
    enabled = [
        dict(source)
        for source in sources
        if (
            isinstance(source, dict)
            and source.get("enabled") is True
            and isinstance(source.get("id"), str)
        )
    ]
    if isinstance(search, dict):
        cnki = search.get("cnki")
        if (
            isinstance(cnki, dict)
            and cnki.get("enabled") is True
            and not any(source.get("id") == "cnki" for source in enabled)
        ):
            enabled.append(
                {
                    "id": "cnki",
                    "enabled": True,
                    "weight": cnki.get("weight", 0.85),
                    "query_templates": cnki.get(
                        "query_templates",
                        ["{topic} 金融史", "{topic} 经济史"],
                    ),
                },
            )
    return enabled


def _queries_for_source(
    topic: str,
    queries: list[str],
    source_config: Mapping[str, Any],
    *,
    research_kernel: Mapping[str, object] | None = None,
) -> list[str]:
    source_id = str(source_config.get("id") or "")
    candidates = (
        _source_specific_query_supplements(topic, research_kernel, source_id)
        + queries
        + _render_templates(topic, source_config)
    )
    normalized = _normalize_queries(candidates)
    return normalized[:SOURCE_QUERY_MAX]


def _source_specific_query_supplements(
    topic: str,
    research_kernel: Mapping[str, object] | None,
    source_id: str,
) -> list[str]:
    if source_id not in {"openalex", "semantic_scholar", "crossref"}:
        return []
    haystack_parts = [topic]
    if isinstance(research_kernel, Mapping):
        for key in ("tentative_question", "observed_puzzle", "scope"):
            value = research_kernel.get(key)
            if isinstance(value, str):
                haystack_parts.append(value)
    haystack = " ".join(haystack_parts).casefold()
    candidates: list[str] = []
    if "布雷顿" in haystack or "bretton" in haystack:
        candidates.extend(
            [
                "Bretton Woods dollar gold convertibility 1968 London Gold Pool",
                "Bretton Woods gold exchange standard collapse 1968 1971",
                "gold-dollar convertibility Bretton Woods 1971",
            ]
        )
    if _is_late_qing_jiangnan_edition_dating_context(haystack):
        candidates.extend(
            [
                "late Qing Jiangnan editions dating prefaces colophons",
                "Jiangnan publishing late Qing print culture edition dating",
            ]
        )
    if "阳明" in haystack or "yangming" in haystack:
        candidates.extend(
            [
                "Wang Yangming Jiangnan late Ming learning societies publishing",
                "Yangming learning late Ming Jiangnan academies lectures",
            ]
        )
    return _normalize_queries(candidates)


def _is_late_qing_jiangnan_edition_dating_context(haystack: str) -> bool:
    has_jiangnan = "江南" in haystack or "jiangnan" in haystack
    has_edition_context = any(
        token in haystack
        for token in (
            "刊本",
            "版本",
            "edition",
            "editions",
            "imprint",
            "imprints",
            "publishing",
        )
    )
    has_dating_context = any(
        token in haystack
        for token in (
            "断代",
            "定年",
            "dating",
            "chronology",
            "chronological",
        )
    )
    has_late_qing_context = any(
        token in haystack
        for token in (
            "晚清",
            "late qing",
            "19世纪",
            "十九世纪",
            "19th",
            "nineteenth",
            "咸丰",
            "同治",
            "光绪",
            "宣统",
        )
    )
    return has_jiangnan and has_edition_context and has_dating_context and has_late_qing_context


def _render_templates(topic: str, source_config: Mapping[str, Any]) -> list[str]:
    templates = source_config.get("query_templates", [])
    if not isinstance(templates, list):
        return []
    rendered = []
    for template in templates:
        if isinstance(template, str):
            rendered.append(template.replace("{topic}", topic))
    return rendered


def _year_window(domain_data: Mapping[str, Any]) -> int | None:
    ranking = domain_data.get("ranking", {})
    if not isinstance(ranking, dict):
        return None
    window = ranking.get("recency_window_years")
    return window if isinstance(window, int) and window > 0 else None


def _apply_source_weights(
    sources: list[NormalizedSource],
    domain_data: Mapping[str, Any],
) -> list[NormalizedSource]:
    weights = {
        str(source["id"]): float(source.get("weight", 1.0))
        for source in _enabled_source_configs(domain_data)
        if isinstance(source.get("weight"), (int, float))
    }
    ranked: list[NormalizedSource] = []
    for index, source in enumerate(sources):
        base = source.rank_score if source.rank_score > 0 else max(0.0, 1.0 - (index * 0.001))
        rank_score = base * weights.get(source.source_client, 1.0)
        ranked.append(source.copy(update={"rank_score": rank_score}))
    return ranked


def _write_report(
    path: Path,
    raw_sources: list[NormalizedSource],
    deduped_sources: list[NormalizedSource],
    dedup_stats: DedupStats,
) -> None:
    counts = Counter(source.source_client for source in raw_sources)
    lines = [
        "# Scout Report",
        "",
        "## Counts Per Source",
        "",
    ]
    if counts:
        for source_id, count in sorted(counts.items()):
            lines.append(f"- {source_id}: {count}")
    else:
        lines.append("- none: 0")
    lines.extend(
        [
            "",
            "## Deduplication",
            "",
            f"- Raw candidates: {dedup_stats.total}",
            f"- Kept candidates: {dedup_stats.kept}",
            f"- DOI duplicates removed: {dedup_stats.doi_duplicates}",
            f"- Fuzzy-title duplicates removed: {dedup_stats.fuzzy_duplicates}",
            "",
            "## Top 10",
            "",
        ],
    )
    for index, source in enumerate(
        sorted(deduped_sources, key=lambda item: item.rank_score, reverse=True)[:10],
        start=1,
    ):
        year = source.year if source.year is not None else "n.d."
        lines.append(
            f"{index}. {source.title} ({year}) - {source.source_client} - {source.rank_score:.3f}",
        )
    _write_text(path, "\n".join(lines) + "\n")


def _write_warnings(discovery_dir: Path, warnings: list[dict[str, object]]) -> None:
    path = discovery_dir / "warnings.jsonl"
    if not warnings:
        _write_text(path, "")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for warning in warnings:
            handle.write(
                json.dumps(warning, sort_keys=True, ensure_ascii=False) + "\n",
            )
    temporary.replace(path)


def _topic_fitness_warning(audit: Mapping[str, object]) -> dict[str, object]:
    kept = audit.get("kept_count", 0)
    candidate_count = audit.get("candidate_count", 0)
    dropped = audit.get("dropped_count", 0)
    message = (
        f"Topic fitness filter kept {kept} of {candidate_count} candidates and dropped {dropped}."
    )
    if audit.get("bypass_reason") == "filtered_deduped_source_floor":
        filtered = audit.get("filtered_deduped_count", "?")
        raw = audit.get("raw_deduped_count", "?")
        message += (
            " Bypassed candidate drops because the filtered deduped pool "
            f"fell below the downstream minimum ({filtered} filtered vs {raw} raw)."
        )
    return {
        "source_id": "topic_fitness_filter",
        "query": "",
        "failure_class": "source_pool_quality",
        "message": message,
    }


def _write_json(path: Path, payload: object) -> None:
    _write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _write_records_jsonl(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _write_jsonl(path: Path, sources: list[NormalizedSource]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for source in sources:
            handle.write(source.json(sort_keys=True, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _domain_path(domain_id: str) -> Path:
    settings = get_settings()
    path = settings.domain_dir / f"{domain_id}.yaml"
    if path.exists():
        return path
    return Path(__file__).resolve().parents[4] / "domains" / f"{domain_id}.yaml"
