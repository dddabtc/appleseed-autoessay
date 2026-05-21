import os
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.scout import ScoutQuerySet, run_scout
from autoessay.config import get_settings
from autoessay.models import ProviderCall, Run
from autoessay.run_writer import create_run_directory

pytestmark = pytest.mark.skipif(
    os.getenv("AUTOESSAY_LIVE_SCOUT") != "1",
    reason="live Scout invariant test is opt-in via AUTOESSAY_LIVE_SCOUT=1",
)


@pytest.mark.live
def test_live_scout_legacy_and_harness_paths_satisfy_invariants(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "0")
    legacy_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_live_scout_legacy",
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )
    harness_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_live_scout_harness",
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add_all(
            [
                Run(
                    id="run_live_scout_legacy",
                    project_id=project.id,
                    domain_version="0.1.0",
                    run_dir=str(legacy_run_dir),
                    state="DOMAIN_LOADED",
                    baseline_hash="test",
                ),
                Run(
                    id="run_live_scout_harness",
                    project_id=project.id,
                    domain_version="0.1.0",
                    run_dir=str(harness_run_dir),
                    state="DOMAIN_LOADED",
                    baseline_hash="test",
                ),
            ],
        )
        session.commit()
        get_settings.cache_clear()
        legacy_summary = run_scout("run_live_scout_legacy", session)
        get_settings.cache_clear()
        harness_summary = run_scout("run_live_scout_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_live_scout_harness"),
            ),
        )

    legacy_candidates = _read_jsonl(legacy_run_dir / "discovery" / "skim_candidates.jsonl")
    harness_candidates = _read_jsonl(harness_run_dir / "discovery" / "skim_candidates.jsonl")
    harness_queries = _read_json(harness_run_dir / "discovery" / "queries.json")

    assert legacy_summary["state"] == harness_summary["state"] == "USER_SEARCH_REVIEW"
    assert len(legacy_candidates) > 0
    assert len(harness_candidates) > 0
    assert _doi_count(harness_candidates) == len(_doi_set(harness_candidates))
    assert all(item.get("source_id") for item in harness_candidates)
    ScoutQuerySet(queries=harness_queries, rationale="artifact schema invariant")
    assert len(provider_calls) >= 1
    assert len(provider_calls) <= 2


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _read_json(path: Path) -> object:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _doi_set(candidates: list[dict[str, object]]) -> set[str]:
    return {str(item["doi"]) for item in candidates if item.get("doi")}


def _doi_count(candidates: list[dict[str, object]]) -> int:
    return sum(1 for item in candidates if item.get("doi"))
