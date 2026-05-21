"""PR-260 — per-section length floor in DRAFTER_MAIN_INSTRUCTIONS.

Real-paper run #11 produced a structurally complete CNKI-shape
manuscript (摘要 / 关键词 / 一-八 sections / 参考文献) at ~10K
chars vs the gpt-5.5 baseline's ~25K. Each body section averaged
~1100 chars (~600 zh chars), too thin for substantive academic
argument. The new directive pushes the LLM to write ≥3
paragraphs per section, ≥1200 zh chars, with a per-paragraph
structure (中心句 / 理由 / 小结). The upstream
``max_tokens=4500`` (PR-257b) already leaves room for it to land.

This test locks the directive into the prompt + checks that legacy
caller paths still see it (so a future refactor that drops it
would fail loudly).
"""

from __future__ import annotations

from autoessay.prompts import DRAFTER_MAIN_INSTRUCTIONS


def test_drafter_main_instructions_includes_length_floor() -> None:
    assert "篇幅下限规则" in DRAFTER_MAIN_INSTRUCTIONS
    assert "1200 中文字符" in DRAFTER_MAIN_INSTRUCTIONS
    assert "3 个完整段落" in DRAFTER_MAIN_INSTRUCTIONS


def test_drafter_main_instructions_keeps_existing_rules() -> None:
    """Length floor must not displace the existing rules — sanity
    check that the citation enforcement + forbidden-patterns blocks
    are still present after the edit."""
    assert "引用强制规则" in DRAFTER_MAIN_INSTRUCTIONS
    assert "绝对规则" in DRAFTER_MAIN_INSTRUCTIONS
    assert "中心问题" in DRAFTER_MAIN_INSTRUCTIONS


def test_drafter_main_instructions_describes_paragraph_shape() -> None:
    """Per-paragraph structural directive (中心句 / 理由 / 小结)
    must be in the prompt, otherwise the floor risks producing
    longer-but-still-thin paragraphs."""
    assert "中心句" in DRAFTER_MAIN_INSTRUCTIONS
    # The floor sentence explains how to handle a thin source
    # pool — never pad with un-cited filler.
    assert "宁可" in DRAFTER_MAIN_INSTRUCTIONS
