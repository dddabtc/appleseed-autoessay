import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents import curator
from autoessay.agents.curator import _resolve_source_fulltext, run_curator
from autoessay.agents.scout import run_scout
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.clients.fulltext_resolver import FulltextResolution
from autoessay.config import get_settings
from autoessay.models import Checkpoint, Run, RunEvent, utcnow
from autoessay.run_writer import create_run_directory
from autoessay.state_machine import RunCancelled


def test_run_curator_stub_end_to_end_transitions_and_writes_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    get_settings.cache_clear()
    run_id = "run_curator_success"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()

        run_scout(run_id, session)
        summary = run_curator(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    sources_dir = run_dir / "sources"
    shortlist = json.loads((sources_dir / "shortlist.json").read_text(encoding="utf-8"))
    manifest = json.loads((sources_dir / "fulltext_manifest.json").read_text(encoding="utf-8"))
    report = (sources_dir / "curation_report.md").read_text(encoding="utf-8")

    assert run is not None
    assert run.state == "USER_DEEP_DIVE_REVIEW"
    assert summary["state"] == "USER_DEEP_DIVE_REVIEW"
    assert shortlist
    assert any(item["rank_score"] > 0 for item in shortlist)
    assert manifest
    assert "Curation Report" in report
    assert "PDFs fetched:" in report
    assert "phase_started" in [event.event_type for event in events]
    assert events[-1].event_type == "phase_done"
    _assert_diversity_caps(shortlist, limit=24)


def _assert_diversity_caps(shortlist: list[dict[str, object]], limit: int) -> None:
    venue_counts = Counter(str(item.get("venue") or "").casefold() for item in shortlist)
    author_counts: Counter[str] = Counter()
    for item in shortlist:
        authors = item.get("authors")
        if isinstance(authors, list):
            for author in authors:
                author_counts[str(author).casefold()] += 1
    assert max(venue_counts.values(), default=0) <= max(1, int(limit * 0.3))
    assert max(author_counts.values(), default=0) <= 2


def test_run_curator_respects_search_review_checkpoint_selection(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_checkpoint"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_metadata_source("approved_source"), _metadata_source("rejected_source")],
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
        session.add(
            Checkpoint(
                id="checkpoint_search_review",
                run_id=run_id,
                checkpoint_type="search-review",
                status="ACCEPTED",
                decision_payload='{"source_ids": ["approved_source"]}',
                decided_at=utcnow(),
            ),
        )
        session.commit()

        run_curator(run_id, session)

    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    assert [item["source_id"] for item in shortlist] == ["approved_source"]


def test_run_curator_accepts_list_shaped_search_review_checkpoint(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_checkpoint_list"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_metadata_source("approved_source"), _metadata_source("rejected_source")],
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
        session.add(
            Checkpoint(
                id="checkpoint_search_review_list",
                run_id=run_id,
                checkpoint_type="search-review",
                status="ACCEPTED",
                decision_payload='["approved_source"]',
                decided_at=utcnow(),
            ),
        )
        session.commit()

        run_curator(run_id, session)

    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    assert [item["source_id"] for item in shortlist] == ["approved_source"]


def test_run_curator_prunes_non_user_fulltext_cache_before_replacement(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_prunes_cache"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_metadata_source("current_source")],
    )
    sources_dir = run_dir / "sources"
    fulltext_dir = sources_dir / "fulltext"
    uploads_dir = sources_dir / "uploads"
    fulltext_dir.mkdir(parents=True)
    uploads_dir.mkdir(parents=True)
    (fulltext_dir / "old_auto.pdf").write_bytes(b"%PDF old")
    (fulltext_dir / "orphan.pdf").write_bytes(b"%PDF orphan")
    (uploads_dir / "manual_keep.pdf").write_bytes(b"%PDF manual")
    (sources_dir / "fulltext_manifest.json").write_text(
        json.dumps(
            {
                "old_auto": {
                    "pdf_path": "sources/fulltext/old_auto.pdf",
                    "license": "CC-BY",
                },
                "manual_keep": {
                    "pdf_path": "sources/uploads/manual_keep.pdf",
                    "license": "user_upload_local_only",
                },
            },
        ),
        encoding="utf-8",
    )
    (sources_dir / "user_upload_manifest.json").write_text(
        json.dumps(
            {
                "manual_keep": {
                    "pdf_path": "sources/uploads/manual_keep.pdf",
                    "license": "user_upload_local_only",
                },
            },
        ),
        encoding="utf-8",
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

        run_curator(run_id, session)
        event = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.event_type == "source_rerun_cache_pruned")
            .order_by(RunEvent.created_at.desc()),
        )

    manifest = json.loads((sources_dir / "fulltext_manifest.json").read_text(encoding="utf-8"))
    assert "old_auto" not in manifest
    assert "manual_keep" in manifest
    assert not (fulltext_dir / "old_auto.pdf").exists()
    assert not (fulltext_dir / "orphan.pdf").exists()
    assert (uploads_dir / "manual_keep.pdf").exists()
    assert event is not None
    payload = json.loads(event.payload)
    assert payload["contract"] == "replacement"
    assert payload["removed_manifest_source_ids"] == ["old_auto"]
    assert payload["removed_fulltext_file_count"] == 2
    assert payload["retained_user_owned_count"] == 1


def test_run_curator_skips_latest_accepted_checkpoint_without_source_ids(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_checkpoint_missing_key"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_metadata_source("older_approved"), _metadata_source("rejected_source")],
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
        older_created_at = utcnow()
        session.add(
            Checkpoint(
                id="checkpoint_search_review_valid_older",
                run_id=run_id,
                checkpoint_type="search-review",
                status="ACCEPTED",
                decision_payload='{"source_ids": ["older_approved"]}',
                decided_at=utcnow(),
                created_at=older_created_at,
            )
        )
        session.commit()
        session.add(
            Checkpoint(
                id="checkpoint_search_review_missing_key_newer",
                run_id=run_id,
                checkpoint_type="search-review",
                status="ACCEPTED",
                decision_payload='{"note": "accepted but no source_ids key"}',
                decided_at=utcnow(),
                created_at=older_created_at + timedelta(seconds=1),
            )
        )
        session.commit()

        run_curator(run_id, session)

    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    assert [item["source_id"] for item in shortlist] == ["older_approved"]


def test_run_curator_without_search_review_checkpoint_falls_back_to_all_candidates(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_no_checkpoint_fallback"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_metadata_source("candidate_a"), _metadata_source("candidate_b")],
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

        run_curator(run_id, session)

    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    assert {item["source_id"] for item in shortlist} == {"candidate_a", "candidate_b"}


def test_run_curator_treats_empty_search_review_checkpoint_as_empty_selection(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_empty_checkpoint"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_metadata_source("available_source")],
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
        session.add(
            Checkpoint(
                id="checkpoint_search_review_empty",
                run_id=run_id,
                checkpoint_type="search-review",
                status="ACCEPTED",
                decision_payload='{"source_ids": []}',
                decided_at=utcnow(),
            ),
        )
        session.commit()

        summary = run_curator(run_id, session)
        run = session.get(Run, run_id)

    report = (run_dir / "sources" / "curation_report.md").read_text(encoding="utf-8")
    assert run is not None
    assert run.state == "FAILED_FIXABLE"
    assert summary["sources"] == 0
    assert "approved no sources" in report
    assert not (run_dir / "sources" / "shortlist.json").exists()


def test_run_curator_honors_cancel_before_fulltext_progress_write(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_cancel_mid_loop"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_metadata_source("candidate_source")],
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

        def canceling_resolve_source_fulltext(**kwargs):  # type: ignore[no-untyped-def]
            source = kwargs["source"]
            run = session.get(Run, run_id)
            assert run is not None
            run.cancel_requested_at = utcnow()
            session.commit()
            return source, False, None, []

        monkeypatch.setattr(curator, "_resolve_source_fulltext", canceling_resolve_source_fulltext)

        with pytest.raises(RunCancelled):
            run_curator(run_id, session)
        run = session.get(Run, run_id)

    assert run is not None
    assert run.state == "CANCELLED"
    assert not (run_dir / "sources" / "shortlist.json").exists()


def _metadata_source(source_id: str) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024,
        venue=f"Venue {source_id}",
        doi=None,
        url=None,
        pdf_url=None,
        abstract="A source about banking crises.",
        source_client="semantic_scholar",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=0.0,
        risk_flags=[],
        verified_by="crossref",
        verification_status=VerificationStatus.VERIFIED,
        confidence=0.7,
    )


def test_resolve_source_fulltext_uses_resolver_for_open_oa_without_pdf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _metadata_source("openalex:oa")
    source.access_status = AccessStatus.OPEN.value
    source.doi = "10.1000/oa"
    source.url = "https://publisher.test/oa"
    source.pdf_url = None
    manifest: dict[str, dict[str, object]] = {}

    async def fake_resolver(candidates, *, timeout):  # type: ignore[no-untyped-def]
        assert [candidate.kind for candidate in candidates] == ["doi", "landing"]
        assert timeout == 12.0
        return FulltextResolution(
            pdf_url="https://publisher.test/oa.pdf",
            method="html_anchor",
            source_url="https://publisher.test/oa",
            diagnostics=[{"status": "resolved"}],
        )

    async def fake_fetch_pdf(url: str, timeout: float, max_size_mb: int) -> bytes:
        assert url == "https://publisher.test/oa.pdf"
        assert timeout == 30.0
        assert max_size_mb == 30
        return b"%PDF-1.4 resolved"

    monkeypatch.setattr("autoessay.agents.curator.resolve_fulltext_pdf_url", fake_resolver)
    monkeypatch.setattr("autoessay.agents.curator.fetch_pdf", fake_fetch_pdf)

    resolved, did_fetch, manual_request, warnings = _resolve_source_fulltext(
        source=source,
        run_dir=tmp_path,
        fulltext_dir=tmp_path / "sources" / "fulltext",
        manifest=manifest,
        max_size_mb=30,
    )

    assert did_fetch is True
    assert manual_request is None
    assert resolved.pdf_url == "https://publisher.test/oa.pdf"
    assert resolved.access_status == "open"
    assert manifest[source.source_id]["pdf_url"] == "https://publisher.test/oa.pdf"
    assert manifest[source.source_id]["fulltext_resolution"]["method"] == "html_anchor"
    assert warnings[0]["failure_class"] == "fulltext_resolution_resolved"


def test_resolve_source_fulltext_tries_metadata_only_doi_before_skip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _metadata_source("crossref:doi")
    source.doi = "10.1000/crossref"
    source.url = "https://doi.org/10.1000/crossref"
    manifest: dict[str, dict[str, object]] = {}

    async def fake_resolver(candidates, *, timeout):  # type: ignore[no-untyped-def]
        del timeout
        assert candidates[0].url == "https://doi.org/10.1000/crossref"
        return FulltextResolution(
            pdf_url="https://publisher.test/crossref.pdf",
            method="html_meta",
            source_url="https://doi.org/10.1000/crossref",
            diagnostics=[],
        )

    async def fake_fetch_pdf(url: str, timeout: float, max_size_mb: int) -> bytes:
        del timeout, max_size_mb
        assert url == "https://publisher.test/crossref.pdf"
        return b"%PDF-1.4 metadata"

    monkeypatch.setattr("autoessay.agents.curator.resolve_fulltext_pdf_url", fake_resolver)
    monkeypatch.setattr("autoessay.agents.curator.fetch_pdf", fake_fetch_pdf)

    resolved, did_fetch, manual_request, _warnings = _resolve_source_fulltext(
        source=source,
        run_dir=tmp_path,
        fulltext_dir=tmp_path / "sources" / "fulltext",
        manifest=manifest,
        max_size_mb=30,
    )

    assert did_fetch is True
    assert manual_request is None
    assert resolved.access_status == "open"
    assert manifest[source.source_id]["pdf_url"] == "https://publisher.test/crossref.pdf"


def test_resolve_source_fulltext_keeps_metadata_only_when_resolver_misses(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _metadata_source("crossref:miss")
    source.doi = "10.1000/miss"
    manifest: dict[str, dict[str, object]] = {}

    async def fake_resolver(candidates, *, timeout):  # type: ignore[no-untyped-def]
        del candidates, timeout
        from autoessay.clients.fulltext_resolver import FulltextResolutionError

        raise FulltextResolutionError("no PDF link found")

    monkeypatch.setattr("autoessay.agents.curator.resolve_fulltext_pdf_url", fake_resolver)

    resolved, did_fetch, manual_request, warnings = _resolve_source_fulltext(
        source=source,
        run_dir=tmp_path,
        fulltext_dir=tmp_path / "sources" / "fulltext",
        manifest=manifest,
        max_size_mb=30,
    )

    assert resolved == source
    assert did_fetch is False
    assert manual_request is None
    assert manifest == {}
    assert warnings[0]["failure_class"] == "fulltext_resolution_failed"


def _write_sources_jsonl(path: Path, sources: list[NormalizedSource]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for source in sources:
            handle.write(source.json(sort_keys=True) + "\n")
