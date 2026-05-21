"""Tests for the literature usage table builder."""

from __future__ import annotations

from autoessay.agents.literature_usage import (
    build_literature_usage_table,
    relation_label,
)
from autoessay.clients.common import AccessStatus, NormalizedSource


def _src(source_id: str, **overrides) -> NormalizedSource:  # type: ignore[no-untyped-def]
    base = {
        "source_id": source_id,
        "title": f"Title {source_id}",
        "authors": ["Doe, J.", "Roe, R."],
        "year": 2025,
        "venue": "Journal X",
        "doi": "10.1/x",
        "url": None,
        "pdf_url": None,
        "abstract": None,
        "source_client": "crossref",
        "access_status": AccessStatus.METADATA_ONLY,
        "license": None,
        "rank_score": 1.0,
        "risk_flags": [],
    }
    base.update(overrides)
    return NormalizedSource.parse_obj(base)


def test_empty_input_returns_empty_string() -> None:
    assert build_literature_usage_table(cited_sources=[], claim_map=[]) == ""


def test_basic_zh_table_has_chinese_headers_and_relation() -> None:
    out = build_literature_usage_table(
        cited_sources=[_src("crossref:abc")],
        claim_map=[
            {
                "section_id": "introduction",
                "paragraph_id": "introduction-p001",
                "claim_text": "claim",
                "source_ids": ["crossref:abc"],
            },
        ],
        project_language="zh",
    )
    assert "# 文献使用表" in out
    assert "| source_id | 作者 | 年份 | 题名 | 文献类型 | 核心观点 | 使用位置 | 与本文关系 |" in out
    # introduction-p001 is the location.
    assert "introduction-p001" in out
    # default relation in zh is 背景
    assert "背景" in out


def test_used_in_aggregates_paragraphs() -> None:
    out = build_literature_usage_table(
        cited_sources=[_src("crossref:abc")],
        claim_map=[
            {
                "section_id": "intro",
                "paragraph_id": "intro-p001",
                "claim_text": "x",
                "source_ids": ["crossref:abc"],
            },
            {
                "section_id": "intro",
                "paragraph_id": "intro-p002",
                "claim_text": "y",
                "source_ids": ["crossref:abc"],
            },
            # duplicate (same paragraph) should not be doubled
            {
                "section_id": "intro",
                "paragraph_id": "intro-p001",
                "claim_text": "z",
                "source_ids": ["crossref:abc"],
            },
        ],
    )
    assert "intro-p001, intro-p002" in out


def test_uncited_marker_in_source_ids_is_ignored() -> None:
    out = build_literature_usage_table(
        cited_sources=[_src("crossref:abc")],
        claim_map=[
            {
                "paragraph_id": "intro-p001",
                "source_ids": ["crossref:abc", "[UNCITED]"],
            },
        ],
    )
    # only crossref:abc used_in entries; no spurious "[UNCITED]" rows.
    assert "[UNCITED]" not in out
    assert "intro-p001" in out


def test_classify_type_detects_book_publisher() -> None:
    out = build_literature_usage_table(
        cited_sources=[_src("book1", venue="Cambridge University Press")],
        claim_map=[{"paragraph_id": "intro-p001", "source_ids": ["book1"]}],
    )
    assert "book" in out


def test_relation_map_overrides_default() -> None:
    out = build_literature_usage_table(
        cited_sources=[_src("a"), _src("b")],
        claim_map=[
            {"paragraph_id": "p1", "source_ids": ["a"]},
            {"paragraph_id": "p2", "source_ids": ["b"]},
        ],
        project_language="zh",
        relation_map={"a": "support", "b": "challenge"},
    )
    assert "支持" in out
    assert "反驳" in out


def test_relation_label_helper() -> None:
    assert relation_label("support", "zh") == "支持"
    assert relation_label("method", "en") == "method reference"
    assert relation_label("unknown_key", "zh") == "背景"


def test_uses_synthesizer_thesis_as_core_point() -> None:
    out = build_literature_usage_table(
        cited_sources=[_src("a", abstract="abstract sentence")],
        claim_map=[{"paragraph_id": "p1", "source_ids": ["a"]}],
        source_notes={"a": {"thesis": "synthesized thesis sentence"}},
        project_language="zh",
    )
    assert "synthesized thesis sentence" in out
    assert "abstract sentence" not in out
