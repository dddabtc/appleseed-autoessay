import json
import os
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.drafter import DEFAULT_SECTION_TITLES, run_drafter
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.models import Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory

pytestmark = pytest.mark.skipif(
    os.getenv("AUTOESSAY_LIVE_DRAFTER") != "1",
    reason="live Drafter invariant test is opt-in via AUTOESSAY_LIVE_DRAFTER=1",
)


@pytest.mark.live
def test_live_drafter_legacy_and_harness_paths_satisfy_invariants(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "0")
    legacy_run_dir = _seed_drafter_run(app_session, tmp_path, run_id="run_live_drafter_legacy")
    harness_run_dir = _seed_drafter_run(app_session, tmp_path, run_id="run_live_drafter_harness")

    with app_session() as session:
        get_settings.cache_clear()
        legacy_summary = run_drafter("run_live_drafter_legacy", session)
        get_settings.cache_clear()
        harness_summary = run_drafter("run_live_drafter_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_live_drafter_harness"),
            ),
        )

    legacy_claims = _read_jsonl(legacy_run_dir / "drafts" / "v001" / "claim_map.jsonl")
    harness_claims = _read_jsonl(harness_run_dir / "drafts" / "v001" / "claim_map.jsonl")
    harness_manuscript = (harness_run_dir / "drafts" / "v001" / "manuscript.md").read_text(
        encoding="utf-8",
    )
    shortlist_ids = _shortlist_ids(harness_run_dir)

    assert legacy_summary["state"] == harness_summary["state"] == "DRAFTER_RUNNING"
    assert legacy_claims
    assert harness_claims
    assert "## Introduction" in harness_manuscript
    assert "## Conclusion" in harness_manuscript
    for claim in harness_claims:
        source_ids = claim["source_ids"]
        assert isinstance(source_ids, list)
        assert all(
            source_id in shortlist_ids or source_id == "[UNCITED]" for source_id in source_ids
        )
    assert any(call.status == "accepted" for call in provider_calls)
    assert len(DEFAULT_SECTION_TITLES) <= len(provider_calls) <= len(DEFAULT_SECTION_TITLES) * 2


def _seed_drafter_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_NOVELTY_REVIEW",
        domain_id="financial_history",
    )
    _write_drafter_inputs(run_dir)
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
                state="USER_NOVELTY_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _write_drafter_inputs(run_dir: Path) -> None:
    sources_dir = run_dir / "sources"
    notes_dir = run_dir / "synthesis" / "source_notes"
    novelty_dir = run_dir / "novelty"
    sources_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    novelty_dir.mkdir(parents=True, exist_ok=True)
    sources = [_source("source_001"), _source("source_002")]
    (sources_dir / "shortlist.json").write_text(
        json.dumps([source.dict() for source in sources], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    for source in sources:
        (notes_dir / f"{source.source_id}.json").write_text(
            json.dumps(
                {
                    "source_id": source.source_id,
                    "thesis": f"{source.source_id} links banking stress to institutions.",
                    "evidence": "The source supplies historical banking-crisis evidence.",
                    "method": "Financial-history source reading.",
                    "limits": "The source is bounded and should not be overgeneralized.",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    (novelty_dir / "selected_thesis.json").write_text(
        json.dumps(
            {
                "angle_id": "angle_001",
                "working_title": "Banking crisis angle",
                "thesis_one_sentence": (
                    "Banking crisis responses reveal how institutional capacity shaped "
                    "financial stability."
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _source(source_id: str) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024,
        venue=f"Journal {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=None,
        abstract=(
            "This source discusses banking crises, institutional responses, archival "
            "evidence, and financial-history methods."
        ),
        source_client="semantic_scholar",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _shortlist_ids(run_dir: Path) -> set[str]:
    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    return {str(item["source_id"]) for item in shortlist if isinstance(item, dict)}
