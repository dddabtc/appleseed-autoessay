from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_project

from autoessay.agents import scout
from autoessay.agents.scout import _collect_sources
from autoessay.clients.common import NormalizedSource
from autoessay.models import Run


class _R10FixtureClient:
    automated = True

    def __init__(self, sources: list[NormalizedSource]) -> None:
        self._sources = sources

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        del query, year_window, limit
        return list(self._sources)

    async def aclose(self) -> None:
        return None


@pytest.mark.real_r10_fixture
def test_scout_classifies_kept_r10_sources_after_topic_filter(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "frontend/tmp/qa-artifacts/run_d9295f9ad25146008fe5870cadcbe2d6"
        / "phase-outputs/02-curator.json"
    )
    if not fixture_path.exists():
        pytest.skip("real R10 fixture is not present in this checkout")

    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    candidates = [
        NormalizedSource.parse_obj(item) for item in payload["artifact"]["skim_candidates"]
    ]

    monkeypatch.setattr(
        scout,
        "get_lit_client",
        lambda source_id, source_config=None, domain_config=None: _R10FixtureClient(candidates),
    )
    run_dir = tmp_path / "run"
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)

    with app_session() as session:
        project = seed_project(session)
        run = Run(
            id="run_scout_classify_after_filter",
            project_id=project.id,
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="DOMAIN_LOADED",
            baseline_hash="test",
        )
        session.add(run)
        session.commit()

        result = asyncio.run(
            _collect_sources(
                run=run,
                session=session,
                discovery_dir=discovery_dir,
                domain_data={
                    "id": payload["run"]["domain_id"],
                    "search": {
                        "exclusion_terms": [],
                        "sources": [{"id": "r10_fixture", "enabled": True}],
                    },
                },
                topic="布雷顿森林金本位承诺的实际约束力失效节点",
                research_kernel=payload["run"]["research_kernel"],
                # PR-344 added the ``proposal`` kw-only arg to scout._collect_sources
                # but this test pre-dates it. Pass None to keep the test exercising
                # the original kernel-only code path (matching how scout falls back
                # when no proposal has been authored yet).
                proposal=None,
                queries=["r10 fixture"],
                year_window=None,
            )
        )

    kept = _result_sources(result)

    assert 5 <= len(kept) <= 25
    assert all(source.dict()["verification_status"] for source in kept)
    assert all(isinstance(source.dict()["verification_status"], str) for source in kept)
    assert all(0.0 <= source.confidence <= 1.0 for source in kept)
    assert any(
        source.dict()["verification_status"] == "verified" and source.confidence == 0.85
        for source in kept
    )
    assert any(
        source.dict()["verification_status"] == "disputed" and source.confidence == 0.05
        for source in kept
    )
    assert all(
        source.dict()["verification_status"] == "disputed" and source.confidence == 0.05
        for source in kept
        if "cnki_stub" in source.risk_flags
    )


def _result_sources(result: dict[str, Any]) -> list[NormalizedSource]:
    sources = result["sources"]
    assert isinstance(sources, list)
    assert all(isinstance(source, NormalizedSource) for source in sources)
    return sources
