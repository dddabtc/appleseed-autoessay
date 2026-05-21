"""Slice F - shadow_baseline activation guards."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from autoessay.agents._shadow_knowledge_injection import build_shadow_knowledge_directive
from autoessay.agents.drafter import (
    SectionPlan,
    _cited_source_ids,
    _drafted_section_from_output,
    _flatten_claim_map,
    _metadata_payload,
    _section_prompt,
)
from autoessay.agents.shadow_baseline import (
    ArgumentMapEntry,
    ReferenceCandidate,
    SectionPlanEntry,
    ShadowBaselineOutput,
    _looks_like_stub_artifact,
    _stub_output,
    load_shadow_baseline,
    persist_shadow_baseline,
    run_shadow_baseline,
    shadow_baseline_paths,
)
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.harness import AuditWriter


def test_shadow_baseline_stub_prod_default_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOESSAY_SHADOW_BASELINE_STUB", raising=False)
    get_settings.cache_clear()

    assert get_settings().shadow_baseline_stub is False


def test_shadow_baseline_real_mode_calls_llm_and_persists_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_real_shadow_baseline(monkeypatch)
    calls: list[object] = []

    async def fake_run_llm_step(**kwargs: object) -> object:
        calls.append(kwargs["request"])
        return SimpleNamespace(parsed=_real_shadow_output())

    monkeypatch.setattr(
        "autoessay.agents.shadow_baseline.run_llm_step",
        fake_run_llm_step,
    )

    out = run_shadow_baseline(
        run_id="run_real_shadow",
        project_title="江南刊本断代",
        user_id="user_test",
        research_kernel={"tentative_question": "断代依据如何重建？"},
        audit=cast(AuditWriter, None),
        run_dir=tmp_path,
    )
    assert out is not None
    persist_shadow_baseline(tmp_path, out)

    json_path, md_path = shadow_baseline_paths(tmp_path)
    assert len(calls) == 1
    assert json_path.exists()
    assert md_path.exists()
    assert "真实 baseline 摘要" in md_path.read_text(encoding="utf-8")


def test_real_mode_cleans_prior_stub_artifacts_before_llm_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persist_shadow_baseline(tmp_path, _stub_output())
    json_path, md_path = shadow_baseline_paths(tmp_path)
    assert _looks_like_stub_artifact(json_path.read_text(encoding="utf-8"))
    assert _looks_like_stub_artifact(md_path.read_text(encoding="utf-8"))

    _force_real_shadow_baseline(monkeypatch)
    existed_during_llm: list[tuple[bool, bool]] = []

    async def fake_run_llm_step(**_kwargs: object) -> object:
        existed_during_llm.append((json_path.exists(), md_path.exists()))
        return SimpleNamespace(parsed=_real_shadow_output())

    monkeypatch.setattr(
        "autoessay.agents.shadow_baseline.run_llm_step",
        fake_run_llm_step,
    )

    out = run_shadow_baseline(
        run_id="run_shadow_cleanup",
        project_title="江南刊本断代",
        user_id="user_test",
        research_kernel={},
        audit=cast(AuditWriter, None),
        run_dir=tmp_path,
    )
    assert out is not None
    persist_shadow_baseline(tmp_path, out)

    assert existed_during_llm == [(False, False)]
    assert not _looks_like_stub_artifact(json_path.read_text(encoding="utf-8"))
    assert not _looks_like_stub_artifact(md_path.read_text(encoding="utf-8"))


def test_real_mode_loader_does_not_treat_prior_stub_as_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persist_shadow_baseline(tmp_path, _stub_output())
    json_path, md_path = shadow_baseline_paths(tmp_path)
    assert json_path.exists()
    assert md_path.exists()

    _force_real_shadow_baseline(monkeypatch)

    assert load_shadow_baseline(tmp_path) is None
    assert not json_path.exists()
    assert not md_path.exists()


def test_looks_like_stub_artifact_formats() -> None:
    stub_json = json.dumps(_stub_output().dict(), ensure_ascii=False, sort_keys=True)
    real_json = json.dumps(_real_shadow_output().dict(), ensure_ascii=False, sort_keys=True)

    assert _looks_like_stub_artifact('{"stub": true, "manuscript_markdown": "x"}')
    assert _looks_like_stub_artifact('{"artifact_id": "baseline_v0_stub"}')
    assert _looks_like_stub_artifact('{"manuscript_markdown": "x", "reference_candidates": []}')
    assert _looks_like_stub_artifact(stub_json)
    assert _looks_like_stub_artifact(_stub_output().manuscript_markdown)
    assert not _looks_like_stub_artifact(real_json)
    assert not _looks_like_stub_artifact("## 摘要\n\n普通背景材料。\n")


def test_drafter_shadow_knowledge_directive_is_background_only() -> None:
    directive = build_shadow_knowledge_directive(_shadow_output_with_candidates())
    prompt = _section_prompt(
        section=_section(),
        selected_thesis={"thesis_one_sentence": "断代依据需要重建。"},
        source_notes={},
        shortlist=[_source("X1"), _source("X2")],
        domain_data={},
        target_journal=None,
        suffix="",
        project_title="江南刊本断代",
        research_kernel={"tentative_question": "断代依据如何重建？"},
        shadow_knowledge_directive=directive,
    )

    assert directive in prompt
    assert "background context document" in prompt
    assert "仅用作论证结构 / 经典背景知识 / 参考写作框架" in prompt
    assert "不要在正文中引用其中提到的具体文献、年份、作者、统计数字" in prompt
    assert "claim_map.source_ids" in prompt
    assert "Approved sources" in prompt


def test_shadow_candidates_do_not_enter_shortlist_or_cited_sources(tmp_path: Path) -> None:
    shortlist = [_source("X1"), _source("X2")]
    shadow_only_ids = {"shadow:Y1", "shadow:Y2"}
    _write_shortlist(tmp_path, shortlist)
    persist_shadow_baseline(tmp_path, _shadow_output_with_candidates())

    drafted = _drafted_section_from_output(
        {
            "section_id": "introduction",
            "section_title": "一、引言",
            "prose": "正文只允许使用 shortlist 证据。",
            "claim_map": [
                {
                    "paragraph_id": "introduction-p001",
                    "claim_text": "该段落尝试混入 shadow-only source_id。",
                    "source_ids": ["X1", "shadow:Y1"],
                },
            ],
        },
        _section(),
        shortlist,
    )
    assert drafted is not None
    claim_records = _flatten_claim_map([drafted], "v001")
    cited_ids = _cited_source_ids(claim_records)
    cited_sources = [source for source in shortlist if source.source_id in cited_ids]
    metadata = _metadata_payload("v001", [drafted], claim_records, cited_sources)

    shortlist_ids = {
        str(item["source_id"])
        for item in json.loads(
            (tmp_path / "sources" / "shortlist.json").read_text(encoding="utf-8")
        )
    }
    assert shortlist_ids == {"X1", "X2"}
    assert shortlist_ids.isdisjoint(shadow_only_ids)
    assert set(cast(list[str], metadata["cited_sources"])) == {"X1"}
    assert set(cast(list[str], metadata["cited_sources"])).isdisjoint(shadow_only_ids)


def test_drafter_claim_map_source_ids_are_subset_of_shortlist() -> None:
    shortlist = [_source("X1"), _source("X2")]
    shadow_only_ids = {"shadow:Y1", "shadow:Y2"}

    drafted = _drafted_section_from_output(
        {
            "section_id": "introduction",
            "section_title": "一、引言",
            "prose": "正文只允许使用 shortlist 证据。",
            "claim_map": [
                {
                    "paragraph_id": "introduction-p001",
                    "claim_text": "有效来源与 shadow-only 来源混写。",
                    "source_ids": ["shadow:Y1", "X2", "shadow:Y2"],
                },
            ],
        },
        _section(),
        shortlist,
    )

    assert drafted is not None
    shortlist_ids = {source.source_id for source in shortlist}
    for claim in drafted.claim_map:
        source_ids = claim["source_ids"]
        assert isinstance(source_ids, list)
        real_ids = {str(source_id) for source_id in source_ids if source_id != "[UNCITED]"}
        assert real_ids <= shortlist_ids
        assert real_ids.isdisjoint(shadow_only_ids)


def _force_real_shadow_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_SHADOW_BASELINE_STUB", "0")
    get_settings.cache_clear()


def _real_shadow_output() -> ShadowBaselineOutput:
    return ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\n真实 baseline 摘要。\n\n## 一、引言\n\n真实正文。\n",
        argument_map=[
            ArgumentMapEntry(
                section_id="introduction",
                central_claim="真实论证主线",
                key_evidence=["序跋", "刻工题记"],
            ),
        ],
        reference_candidates=[
            ReferenceCandidate(
                author="Real Author",
                year="2024",
                title="Real Verified-Looking Work",
                venue="Real Journal",
                type="article",
                doi_or_isbn="10.1000/real",
                why_relevant="real candidate",
            ),
        ],
        section_plan=[
            SectionPlanEntry(
                section_id="introduction",
                title="一、引言",
                target_words=1200,
                key_argument="提出问题",
            ),
        ],
    )


def _shadow_output_with_candidates() -> ShadowBaselineOutput:
    return ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\nshadow 背景材料。\n",
        argument_map=[
            ArgumentMapEntry(
                section_id="introduction",
                central_claim="序跋与刻工题记应被并读。",
                key_evidence=["未验证的统计数字", "未验证的年份"],
            ),
        ],
        reference_candidates=[
            ReferenceCandidate(
                author="Shadow Author One",
                year="1938",
                title="Shadow-only Y1",
                venue="Shadow Press",
                type="book",
                doi_or_isbn="shadow:Y1",
                why_relevant="background only",
            ),
            ReferenceCandidate(
                author="Shadow Author Two",
                year="1948",
                title="Shadow-only Y2",
                venue="Shadow Journal",
                type="article",
                doi_or_isbn="shadow:Y2",
                why_relevant="background only",
            ),
        ],
        section_plan=[
            SectionPlanEntry(
                section_id="introduction",
                title="一、引言",
                target_words=1200,
                key_argument="提出问题",
            ),
        ],
    )


def _section() -> SectionPlan:
    return SectionPlan(section_id="introduction", title="一、引言", target_words=1200)


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
        abstract="Abstract evidence for the test topic.",
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )


def _write_shortlist(run_dir: Path, shortlist: list[NormalizedSource]) -> None:
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / "shortlist.json").write_text(
        json.dumps([source.dict() for source in shortlist], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
