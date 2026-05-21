from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoessay.agents._topic_fitness import filter_off_topic_candidates
from autoessay.clients.common import NormalizedSource


@pytest.mark.real_r10_fixture
def test_real_r10_fixture_keeps_bretton_woods_and_drops_brayton_cycle() -> None:
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

    result = filter_off_topic_candidates(
        candidates,
        title="布雷顿森林金本位承诺的实际约束力失效节点",
        research_kernel=payload["run"]["research_kernel"],
        domain_data={"id": payload["run"]["domain_id"], "search": {"exclusion_terms": []}},
        min_pool=5,
    )

    kept_titles = {source.title for source in result.kept}

    assert 5 <= len(result.kept) <= 25
    assert "The Battle of Bretton Woods" in kept_titles
    assert "高参数超临界CO₂布雷顿循环热力系统优化与关键部件匹配特性研究" not in kept_titles
