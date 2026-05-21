import json
import os
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.curator import CuratorRanking, run_curator
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.config import get_settings
from autoessay.models import Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory

pytestmark = pytest.mark.skipif(
    os.getenv("AUTOESSAY_LIVE_CURATOR") != "1",
    reason="live Curator invariant test is opt-in via AUTOESSAY_LIVE_CURATOR=1",
)


@pytest.mark.live
def test_live_curator_legacy_and_harness_paths_satisfy_invariants(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "0")
    legacy_run_dir = _seed_curator_run(
        app_session,
        tmp_path,
        run_id="run_live_curator_legacy",
    )
    harness_run_dir = _seed_curator_run(
        app_session,
        tmp_path,
        run_id="run_live_curator_harness",
    )

    with app_session() as session:
        get_settings.cache_clear()
        legacy_summary = run_curator("run_live_curator_legacy", session)
        get_settings.cache_clear()
        harness_summary = run_curator("run_live_curator_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_live_curator_harness"),
            ),
        )

    legacy_shortlist = _read_json(legacy_run_dir / "sources" / "shortlist.json")
    harness_shortlist = _read_json(harness_run_dir / "sources" / "shortlist.json")
    response_text = (
        harness_run_dir / "sources" / "responses" / "curator_ranking_batch_001.txt"
    ).read_text(encoding="utf-8")

    assert legacy_summary["state"] == harness_summary["state"] == "USER_DEEP_DIVE_REVIEW"
    assert isinstance(legacy_shortlist, list)
    assert isinstance(harness_shortlist, list)
    assert len(legacy_shortlist) > 0
    assert len(harness_shortlist) > 0
    assert all(isinstance(item, dict) and item.get("source_id") for item in harness_shortlist)
    assert any(call.status == "accepted" for call in provider_calls)
    assert 1 <= len(provider_calls) <= 2
    CuratorRanking.parse_raw(response_text)


def _seed_curator_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True, exist_ok=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_source("source_001", 0), _source("source_002", 1)],
    )
    with app_session() as session:
        project = session.get(Project, "proj_test")
        if project is None:
            project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(project)
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
    return run_dir


def _source(source_id: str, index: int) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024 - index,
        venue=f"Journal {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=None,
        abstract="A source about banking crises and lender-of-last-resort practice.",
        source_client="semantic_scholar",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=0.0,
        risk_flags=[],
        verified_by="crossref",
        verification_status=VerificationStatus.VERIFIED,
        confidence=0.7,
    )


def _write_sources_jsonl(path: Path, sources: list[NormalizedSource]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for source in sources:
            handle.write(source.json(sort_keys=True) + "\n")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))
