"""Tests for manuscript front-matter + references composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from autoessay.agents import manuscript_compose as compose_module
from autoessay.agents.manuscript_compose import (
    FrontMatter,
    _ordered_cited_sources,
    _refined_title_default,
    _render_front_block,
    _render_references_block,
    compose_manuscript,
    strip_existing_paper_matter,
)
from autoessay.clients.common import AccessStatus, NormalizedSource


@dataclass
class _DummyRun:
    id: str = "run_test"
    run_dir: str = "/tmp/manuscript-compose-test-run"


_DUMMY_RUN = _DummyRun()
_DUMMY_SESSION: Any = object()


def _source(source_id: str, title: str, year: int = 2024) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=["甲", "乙"],
        year=year,
        venue="财经研究",
        doi="10.1/xyz",
        url=None,
        pdf_url=None,
        abstract=None,
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )


def test_refined_title_default_prefers_thesis_working_title() -> None:
    assert (
        _refined_title_default(
            "user-typed topic",
            {"working_title": "更精炼的标题"},
        )
        == "更精炼的标题"
    )
    assert _refined_title_default("user topic", None) == "user topic"
    assert _refined_title_default("user", {"working_title": ""}) == "user"


def test_render_front_block_zh_includes_authors_abstract_keywords() -> None:
    block = _render_front_block(
        front=FrontMatter(
            title="国央企预算管理：成本控制与绩效改善",
            abstract="本文以XX为切入点，分析XX机制，发现XX。",
            keywords=["预算管理", "成本控制", "国央企", "绩效"],
        ),
        authors=["张三"],
        project_language="zh",
    )
    assert block.startswith("# 国央企预算管理：")
    assert "**作者**：张三" in block
    assert "**摘要**：" in block
    assert "**关键词**：预算管理；成本控制；国央企；绩效" in block


def test_render_front_block_en_uses_english_labels() -> None:
    block = _render_front_block(
        front=FrontMatter(
            title="Title",
            abstract="A short abstract.",
            keywords=["alpha", "beta"],
        ),
        authors=["Doe"],
        project_language="en",
    )
    assert "**Authors**: Doe" in block
    assert "**Abstract**: A short abstract." in block
    assert "**Keywords**: alpha; beta" in block


def test_render_front_block_skips_empty_blocks() -> None:
    block = _render_front_block(
        front=FrontMatter(title="Just a title", abstract="", keywords=[]),
        authors=[],
        project_language="en",
    )
    assert block == "# Just a title"


def test_render_front_block_skips_placeholder_author_names() -> None:
    block = _render_front_block(
        front=FrontMatter(title="Title", abstract="", keywords=[]),
        authors=["Single User", "Admin"],
        project_language="zh",
    )
    assert block == "# Title"


def test_ordered_cited_sources_uses_appearance_order_in_body() -> None:
    sources = [
        _source("crossref:bbb", "B paper"),
        _source("crossref:aaa", "A paper"),
        _source("crossref:ccc", "C paper"),
    ]
    body = "First we cite crossref:aaa then crossref:ccc and finally crossref:bbb."
    ordered = _ordered_cited_sources(sources, body)
    assert [s.source_id for s in ordered] == [
        "crossref:aaa",
        "crossref:ccc",
        "crossref:bbb",
    ]


def test_ordered_cited_sources_falls_back_to_alpha_for_unmentioned() -> None:
    sources = [_source("crossref:zzz", "Z"), _source("crossref:aaa", "A")]
    ordered = _ordered_cited_sources(sources, "no source ids in this body")
    assert [s.source_id for s in ordered] == ["crossref:aaa", "crossref:zzz"]


def test_ordered_cited_sources_preserves_input_order_for_numeric_markers() -> None:
    sources = [_source("crossref:zzz", "Z"), _source("crossref:aaa", "A")]
    ordered = _ordered_cited_sources(sources, "正文已经使用数字引文[1][2]。")
    assert [s.source_id for s in ordered] == ["crossref:zzz", "crossref:aaa"]


def test_render_references_block_numbered_zh() -> None:
    block = _render_references_block(
        cited_sources=[_source("s1", "标题甲", year=2025)],
        body_markdown="正文 cites s1.",
        project_language="zh",
    )
    assert "## 参考文献" in block
    assert "[1] 甲; 乙. 标题甲. 财经研究, 2025. DOI: 10.1/xyz." in block


def test_render_references_block_empty_when_no_cited_sources() -> None:
    assert (
        _render_references_block(
            cited_sources=[],
            body_markdown="some body",
            project_language="zh",
        )
        == ""
    )


def test_compose_manuscript_uses_stub_when_front_matter_stub_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FRONT_MATTER_STUB", "1")
    from autoessay.config import get_settings

    get_settings.cache_clear()
    body = "<a id='introduction'></a>\n## 引言\n\n本节论述要点 (s1)。\n"
    out = compose_manuscript(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        body_markdown=body,
        project_title="原始用户题目",
        project_language="zh",
        authors=["管理员"],
        cited_sources=[_source("s1", "样本", year=2025)],
        selected_thesis={"working_title": "更精炼的标题"},
    )
    # Front block uses thesis working title, not user topic.
    assert "# 更精炼的标题" in out
    assert "**作者**：管理员" in out
    # Body present
    assert "## 引言" in out
    # References at end
    assert "## 参考文献" in out
    assert "[1] 甲; 乙. 样本. 财经研究, 2025." in out
    # Standard horizontal rule separators
    assert "\n---\n" in out


def test_strip_existing_zh_paper_matter_keeps_body_only() -> None:
    body = """## 摘要

旧摘要。

## 关键词

旧关键词。

## 一、引言

正文[1]。

## 参考文献

[1] 旧文献。
"""
    stripped = strip_existing_paper_matter(body, "zh")
    assert stripped.startswith("## 一、引言")
    assert "旧摘要" not in stripped
    assert "旧关键词" not in stripped
    assert "## 参考文献" not in stripped


def test_compose_manuscript_is_idempotent_for_existing_zh_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FRONT_MATTER_STUB", "1")
    from autoessay.config import get_settings

    get_settings.cache_clear()
    wrapped = """## 摘要

旧摘要。

## 关键词

旧关键词。

## 一、引言

正文[1]。

## 参考文献

[1] 旧文献。
"""
    out = compose_manuscript(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        body_markdown=wrapped,
        project_title="题目",
        project_language="zh",
        authors=[],
        cited_sources=[_source("s1", "样本", year=2025)],
        selected_thesis=None,
    )
    assert out.count("## 参考文献") == 1
    assert out.count("## 摘要") == 0
    assert out.count("**摘要**") == 0
    assert "旧文献" not in out


def test_compose_manuscript_falls_back_when_llm_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the front-matter LLM call raises, the exporter must still
    produce a manuscript — just without abstract/keywords.

    PR-D2.1: monkeypatches ``_generate_front_matter_via_llm`` (the hub
    entry point) instead of ``LLMClient`` directly, since the hub now
    routes through ``harness.run_llm_step``.
    """
    monkeypatch.setenv("AUTOESSAY_FRONT_MATTER_STUB", "0")
    from autoessay.config import get_settings

    get_settings.cache_clear()

    async def boom(**_kwargs: object) -> None:
        raise RuntimeError("upstream unreachable")

    monkeypatch.setattr(compose_module, "_generate_front_matter_via_llm", boom)
    out = compose_manuscript(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        body_markdown="## 引言\n\n本节内容。",
        project_title="题目甲",
        project_language="zh",
        authors=["作者"],
        cited_sources=[],
        selected_thesis=None,
    )
    assert "# 题目甲" in out
    assert "**作者**：作者" in out
    # No abstract, no keywords, no references
    assert "**摘要**" not in out
    assert "**关键词**" not in out
    assert "## 参考文献" not in out
