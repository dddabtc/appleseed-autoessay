from __future__ import annotations

from autoessay.agents._topic_fitness import (
    filter_off_topic_candidates,
    source_pool_quality_event_needed,
)
from autoessay.clients.common import NormalizedSource


def _source(
    source_id: str,
    title: str,
    *,
    abstract: str | None = None,
    provenance: str = "search",
    risk_flags: list[str] | None = None,
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=[],
        year=None,
        venue=None,
        doi=None,
        url=None,
        pdf_url=None,
        abstract=abstract,
        source_client="crossref",
        access_status="metadata_only",
        license=None,
        risk_flags=risk_flags or [],
        provenance=provenance,
    )


def test_missing_concept_bucket_uses_single_bucket_anchor_instead_of_opening() -> None:
    keep = _source("s1", "Bretton Woods source")
    drop = _source("s2", "Quantum computing survey")

    result = filter_off_topic_candidates(
        [keep, drop],
        title="布雷顿森林",
        research_kernel=None,
    )

    assert result.kept == [keep]
    assert len(result.dropped) == 1
    assert result.dropped[0]["source_id"] == "s2"
    assert result.audit["gate_mode"] == "single_bucket_anchor"
    assert "missing_concept_bucket" in result.audit["warnings"]


def test_missing_kernel_concepts_fall_back_to_proposal_keywords() -> None:
    keep = _source(
        "gravity",
        "China South Asia agricultural import margins and gravity model evidence",
        abstract="HS-6 bilateral agricultural trade and UN COMTRADE data.",
    )
    drop = _source("asr", "Chinese speech recognition transformer model")

    result = filter_off_topic_candidates(
        [keep, drop],
        title="中国从南亚进口农产品的贸易边际",
        research_kernel={"kernel_schema_version": 1},
        proposal={
            "research_question": (
                "How do gravity model variables shape China's South Asia "
                "agricultural import margins?"
            ),
            "preliminary_keywords": ["gravity model", "UN COMTRADE", "HS-6"],
        },
        min_pool=1,
    )

    assert [source.source_id for source in result.kept] == ["gravity"]
    assert "weak_entity_anchor" in result.kept[0].risk_flags
    assert result.dropped[0]["source_id"] == "asr"
    assert result.audit["gate_mode"] == "entity_and_concept"
    assert "concept_bucket_from_proposal" in result.audit["warnings"]


def test_and_gate_keeps_entity_and_concept_match() -> None:
    candidate = _source(
        "steil",
        "The Battle of Bretton Woods",
        abstract="A history of the gold standard and postwar monetary order.",
    )

    result = filter_off_topic_candidates(
        [candidate],
        title="布雷顿森林",
        research_kernel={"tentative_question": "金本位承诺如何约束战后货币秩序？"},
    )

    assert result.kept == [candidate]
    assert result.dropped == []


def test_llm_canon_with_entity_match_does_not_bypass_concept_gate() -> None:
    candidate = _source(
        "canonical",
        "Apollo program official chronology",
        provenance="llm_canon",
    )

    result = filter_off_topic_candidates(
        [candidate],
        title="Apollo program logistics",
        research_kernel={"tentative_question": "procurement contracts shaped budget politics"},
        min_pool=1,
    )

    assert result.kept == []
    assert result.dropped[0]["source_id"] == "canonical"


def test_and_gate_drops_no_overlap() -> None:
    candidate = _source("forest", "森林管护与生态林业建设")

    result = filter_off_topic_candidates(
        [candidate],
        title="布雷顿森林",
        research_kernel={"tentative_question": "金本位承诺如何约束战后货币秩序？"},
    )

    assert result.kept == []
    assert result.dropped[0]["reason"] == "no_overlap"


def test_homophone_ban_drops_even_with_surface_entity_match() -> None:
    candidate = _source("brayton", "高参数超临界CO₂布雷顿循环热力系统优化与关键部件匹配特性研究")

    result = filter_off_topic_candidates(
        [candidate],
        title="布雷顿森林",
        research_kernel={"tentative_question": "金本位承诺如何约束战后货币秩序？"},
        domain_data={"id": "financial_history", "search": {"exclusion_terms": []}},
    )

    assert result.kept == []
    assert result.dropped[0]["reason"] == "homophone_ban"


def test_rescue_keeps_strong_concept_match_when_pool_is_small() -> None:
    candidate = _source(
        "concept_only",
        "Gold dollar convertibility crisis",
        abstract="IMF memoranda on gold pool settlement and dollar convertibility.",
    )

    result = filter_off_topic_candidates(
        [candidate],
        title="布雷顿森林",
        research_kernel={
            "tentative_question": "金本位承诺的实际约束力",
            "scope": "美元—黄金兑换通道，以 IMF 备忘录与黄金池记录为主。",
        },
        min_pool=2,
    )

    assert len(result.kept) == 1
    assert result.kept[0].source_id == "concept_only"
    assert "weak_entity_anchor" in result.kept[0].risk_flags
    assert result.audit["rescued_count"] == 1


def test_min_pool_warning_does_not_force_keep_when_all_drop() -> None:
    candidates = [
        _source("s1", "Quantum computing survey"),
        _source("s2", "Marine ecology methods"),
        _source("s3", "Ceramic materials handbook"),
    ]

    result = filter_off_topic_candidates(
        candidates,
        title="布雷顿森林",
        research_kernel={"tentative_question": "金本位承诺如何约束战后货币秩序？"},
        min_pool=5,
    )

    assert result.kept == []
    assert len(result.dropped) == 3
    assert result.audit["min_pool_triggered"] is True
    assert "min_pool_triggered" in result.audit["warnings"]


def test_high_drop_rate_sets_quality_warning_signal() -> None:
    keep = _source(
        "keep",
        "Bretton Woods gold standard",
        abstract="Gold convertibility and dollar constraints.",
    )
    drop = _source("drop", "Quantum computing survey")

    result = filter_off_topic_candidates(
        [keep, drop],
        title="布雷顿森林",
        research_kernel={"tentative_question": "金本位承诺如何约束战后货币秩序？"},
        min_pool=1,
    )

    assert result.audit["drop_rate"] == 0.5
    assert "high_drop_rate" in result.audit["warnings"]
    assert source_pool_quality_event_needed(result.audit) is True


def test_dropped_audit_records_have_required_jsonl_fields() -> None:
    result = filter_off_topic_candidates(
        [_source("drop", "Quantum computing survey")],
        title="布雷顿森林",
        research_kernel={"tentative_question": "金本位承诺如何约束战后货币秩序？"},
    )

    assert result.dropped
    record = result.dropped[0]
    assert {"source_id", "title", "reason", "entity_match", "concept_match"} <= set(record)
    assert record["source_id"] == "drop"
    assert record["title"] == "Quantum computing survey"
    assert record["reason"] == "no_overlap"
    assert isinstance(record["entity_match"], list)
    assert isinstance(record["concept_match"], list)
