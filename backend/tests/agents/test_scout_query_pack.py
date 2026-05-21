import json
from pathlib import Path

from autoessay.agents.scout import (
    CHINESE_HUMANITIES_VENUES,
    QueryPack,
    _chinese_index_coverage_warnings,
    _expand_queries,
    _fallback_query_pack,
    _official_archive_sources_for_kernel,
    _queries_for_source,
    _query_hard_constraints,
    _query_object_prompt,
)
from autoessay.config import get_settings
from autoessay.harness import HookRegistry

DOMAIN_DATA = {
    "id": "financial_history",
    "search": {
        "sources": [
            {
                "id": "openalex",
                "enabled": True,
                "query_templates": ["{topic} 金融史"],
            }
        ],
        "default_query_terms": ["financial history", "economic history"],
        "exclusion_terms": ["technical analysis"],
    },
}
RESEARCH_KERNEL = {
    "tentative_question": "明清江南棉布业的市场结构如何变化",
    "observed_puzzle": "既有研究对区域市场整合解释不一",
}
AG_TRADE_PROPOSAL = {
    "research_question": "中国从南亚进口农产品贸易边际如何受贸易便利化影响？",
    "preliminary_approach": "Use a gravity model with HS-6 panel data from UN COMTRADE.",
    "scope": "2002-2021 bilateral agricultural imports.",
    "preliminary_keywords": [
        "gravity model",
        "UN COMTRADE",
        "HS-6",
        "2002-2021",
        "bilateral agricultural imports",
    ],
}


def test_query_object_prompt_requests_four_query_pack_fields() -> None:
    prompt = _query_object_prompt("明清江南棉布业市场结构", DOMAIN_DATA, None, RESEARCH_KERNEL)

    assert "zh_native" in prompt
    assert "en_translated" in prompt
    assert "venue_boosted_zh" in prompt
    assert "exact_title_kernel" in prompt
    assert "queries" in prompt

    payload = json.loads(prompt[prompt.rfind("\n") + 1 :])
    assert payload["required_count"] == [4, 8]
    assert set(payload["output_schema"]) >= {
        "zh_native",
        "en_translated",
        "venue_boosted_zh",
        "exact_title_kernel",
        "queries",
        "rationale",
    }


def test_query_object_prompt_exposes_proposal_hard_constraints() -> None:
    prompt = _query_object_prompt(
        "中国从南亚进口农产品贸易边际研究",
        DOMAIN_DATA,
        AG_TRADE_PROPOSAL,
        {"scope": "South Asia agricultural imports"},
    )

    payload = json.loads(prompt[prompt.rfind("\n") + 1 :])
    hard_constraints = payload["hard_constraints"]
    assert "gravity model" in hard_constraints
    assert "UN COMTRADE" in hard_constraints
    assert "HS-6" in hard_constraints
    assert "2002-2021" in hard_constraints


def test_fallback_query_pack_prioritizes_proposal_hard_constraints() -> None:
    pack = _fallback_query_pack(
        "中国从南亚进口农产品贸易边际研究",
        DOMAIN_DATA,
        AG_TRADE_PROPOSAL,
        {"scope": "South Asia agricultural imports"},
    )
    joined = "\n".join(pack.queries[:3])

    assert "gravity model" in joined
    assert "UN COMTRADE" in joined
    assert "HS-6" in joined
    assert "2002-2021" in joined


def test_query_hard_constraints_normalizes_high_signal_terms() -> None:
    constraints = _query_hard_constraints(
        AG_TRADE_PROPOSAL,
        {"method_preference": "fixed effects with HS 6 customs data"},
    )

    assert constraints[:4] == ["gravity model", "UN COMTRADE", "HS-6", "2002-2021"]
    assert "HS-6" in constraints


def test_chinese_index_warning_when_zh_domain_lacks_cnki_or_wanfang() -> None:
    warnings = _chinese_index_coverage_warnings(
        topic="中国从南亚进口农产品贸易边际研究",
        language="zh",
        domain_data={
            "id": "general_academic",
            "search": {"sources": [{"id": "openalex", "enabled": True}]},
        },
    )

    assert warnings
    warning = warnings[0]
    assert warning["failure_class"] == "coverage_warning"
    assert warning["missing_indexes"] == ["cnki", "wanfang"]


def test_chinese_index_warning_suppressed_when_cnki_enabled() -> None:
    warnings = _chinese_index_coverage_warnings(
        topic="中国从南亚进口农产品贸易边际研究",
        language="zh",
        domain_data={
            "id": "financial_history",
            "search": {"sources": [{"id": "cnki", "enabled": True}]},
        },
    )

    assert warnings == []


def test_query_pack_merges_four_categories_and_keeps_reasonable_translation() -> None:
    pack = QueryPack(
        zh_native=["明清江南棉布业 市场结构"],
        en_translated=["Ming Qing Jiangnan cotton textile market structure"],
        venue_boosted_zh=["明清江南棉布业 市场结构 《历史研究》"],
        exact_title_kernel=['"明清江南棉布业市场结构" "市场结构"'],
        rationale="mocked LLM translation",
    )

    assert pack.queries == [
        "明清江南棉布业 市场结构",
        "Ming Qing Jiangnan cotton textile market structure",
        "明清江南棉布业 市场结构 《历史研究》",
        '"明清江南棉布业市场结构" "市场结构"',
    ]
    assert "Jiangnan" in pack.en_translated[0]
    assert "market structure" in pack.en_translated[0]


def test_query_pack_json_persisted_in_stub_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    discovery_dir = tmp_path / "discovery"
    discovery_dir.mkdir()

    queries = _expand_queries(
        "明清江南棉布业市场结构",
        DOMAIN_DATA,
        discovery_dir,
        warnings=[],
        proposal=None,
        run=None,  # type: ignore[arg-type]
        project=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        hooks=HookRegistry(),
        research_kernel=RESEARCH_KERNEL,
    )

    query_pack = json.loads((discovery_dir / "query_pack.json").read_text(encoding="utf-8"))
    legacy_queries = json.loads((discovery_dir / "queries.json").read_text(encoding="utf-8"))

    assert legacy_queries == queries
    assert query_pack["queries"] == queries
    assert query_pack["zh_native"]
    assert query_pack["en_translated"]
    assert query_pack["exact_title_kernel"]
    assert any(
        venue in item
        for venue in CHINESE_HUMANITIES_VENUES
        for item in query_pack["venue_boosted_zh"]
    )
    get_settings.cache_clear()


def test_queries_for_source_adds_concise_bretton_openalex_supplements() -> None:
    out = _queries_for_source(
        "[PWTEST] 布雷顿森林金本位承诺失效节点研究",
        ["非常长的布雷顿森林金本位承诺失效节点精确检索式"] * 8,
        {"id": "openalex", "query_templates": ["{topic} monetary history"]},
        research_kernel={
            "tentative_question": "布雷顿森林金本位承诺的实际约束力如何失效？",
            "scope": "1960-1971 年美元—黄金兑换通道与 London Gold Pool",
        },
    )

    assert out[0] == "Bretton Woods dollar gold convertibility 1968 London Gold Pool"
    assert any("gold exchange standard" in query for query in out)
    assert len(out) <= 8


def test_queries_for_source_keeps_jiangnan_publishing_supplements_topic_scoped() -> None:
    out = _queries_for_source(
        "明末清初江南阳明心学传播路径研究",
        ["明末清初江南阳明心学 讲会 刊本序跋 传播路径"],
        {"id": "semantic_scholar", "query_templates": []},
        research_kernel={
            "observed_puzzle": "既有思想史研究多把这一阶段视为一次性扩散。",
            "tentative_question": "江南阳明心学传播应被理解为一次性扩散还是多线过程？",
            "scope": (
                "限定 1573-1644 年江南地区，材料以讲会语录、刊本序跋、府县学官档与同时期文集为主。"
            ),
        },
    )

    assert "Wang Yangming Jiangnan late Ming learning societies publishing" in out
    assert not any("late Qing Jiangnan editions dating" in query for query in out)
    assert not any("Jiangnan publishing late Qing" in query for query in out)


def test_queries_for_source_keeps_late_qing_jiangnan_supplements() -> None:
    out = _queries_for_source(
        "晚清江南刊本断代依据重建研究",
        ["晚清江南刊本 断代 版本学"],
        {"id": "openalex", "query_templates": []},
        research_kernel={
            "tentative_question": "晚清江南刊本断代依据如何重建？",
            "scope": "限定 19世纪后期江南刊本，材料以序跋、牌记、刻工题记与重刊记录为主。",
        },
    )

    assert "late Qing Jiangnan editions dating prefaces colophons" in out
    assert "Jiangnan publishing late Qing print culture edition dating" in out


def test_queries_for_source_does_not_add_english_supplements_to_cnki() -> None:
    out = _queries_for_source(
        "布雷顿森林金本位承诺失效节点研究",
        ["布雷顿森林 失效节点"],
        {"id": "cnki", "query_templates": ["{topic} 金融史"]},
        research_kernel={"scope": "London Gold Pool"},
    )

    assert not any("Bretton Woods" in query for query in out)


def test_official_archive_sources_added_for_bretton_gold_kernel() -> None:
    sources = _official_archive_sources_for_kernel(
        {
            "observed_puzzle": "布雷顿森林体系的金本位安排在制度文本上保留。",
            "tentative_question": "美元—黄金承诺的可兑换约束何时失效？",
            "scope": "1960-1971 年美元 gold convertibility 与 London Gold Pool。",
        },
    )

    ids = {source.source_id for source in sources}
    assert "official:imf:annual-report-1968" in ids
    assert "official:fraser:fed-annual-report-1968" in ids
    assert "official:fraser:bog-minutes-1968-03-20" in ids
    assert all(source.verified_by == "official_archive" for source in sources)
    assert all(source.verification_status == "unverified" for source in sources)


def test_official_archive_sources_not_added_for_unrelated_kernel() -> None:
    assert (
        _official_archive_sources_for_kernel(
            {
                "scope": "19 世纪后期江南刊本序跋与刻工题记。",
                "tentative_question": "断代依据如何重建？",
            },
        )
        == []
    )
