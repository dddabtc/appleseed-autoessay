"""PR-G-Regressions-2 test: Chinese abstract no longer admits
English thesis_one_sentence.

Round-1+2 real-paper acceptance scored pipeline合规性 1.5 / 7.0
vs baseline 8.5 — the largest single contributor was the
Chinese 摘要 block carrying an English thesis sentence at its
head (because ideator ran with project.language='en' default
even though kernel was Chinese). PR-G-Regressions-2 gates the
selected_thesis fields by language so they only enter the zh
abstract when actually written in Chinese.
"""

from __future__ import annotations

from autoessay.agents.drafter import (
    DraftedSection,
    _format_zh_abstract,
    _is_predominantly_chinese,
)


def _intro_section(prose: str) -> DraftedSection:
    return DraftedSection(
        section_id="introduction",
        title="一、引言",
        prose=prose,
        claim_map=[],
        failed=False,
        warnings=[],
        word_count=100,
        target_words=1500,
    )


# ----- _is_predominantly_chinese ---------------------------------


def test_pure_chinese_is_predominantly_chinese() -> None:
    assert _is_predominantly_chinese("本文讨论晚清江南刊本的断代依据") is True


def test_pure_english_is_not_predominantly_chinese() -> None:
    assert (
        _is_predominantly_chinese(
            "The article argues that the dating of late-19th-century imprints"
        )
        is False
    )


def test_mostly_english_with_few_chinese_chars_is_not_predominantly_chinese() -> None:
    """A real-paper round-1 thesis_one_sentence: mostly English
    with a stray Chinese gloss in parens — should NOT count as
    predominantly Chinese."""
    text = "The article argues for re-establishing dating from internal evidence (摘要 sample)"
    assert _is_predominantly_chinese(text) is False


def test_empty_or_whitespace_only_is_not_predominantly_chinese() -> None:
    assert _is_predominantly_chinese("") is False
    assert _is_predominantly_chinese("   \n\t   ") is False


# ----- _format_zh_abstract gate ----------------------------------


def test_english_thesis_skipped_intro_used() -> None:
    """The reproducer of round-1+2 bug: selected_thesis is English,
    intro prose is Chinese. Result should be Chinese-only."""
    selected_thesis = {
        "thesis_one_sentence": (
            "The article argues that the dating of late-19th-century "
            "Jiangnan imprints should be re-established from internal evidence."
        ),
        "working_title": "Reconstructing Dating from Internal Evidence",
    }
    intro = _intro_section(
        "本文先指出，晚清江南刊本的断代与文体归属之所以反复出现分歧，"
        "是因为研究常把后出目录、馆藏著录和对象内部材料放在同一层级。"
    )
    abstract = _format_zh_abstract(selected_thesis, [intro])
    # The English sentence is gone
    assert "The article argues" not in abstract
    assert "Reconstructing" not in abstract
    # The Chinese intro is still present
    assert "晚清江南刊本" in abstract


def test_chinese_thesis_kept() -> None:
    """When ideator did emit a Chinese thesis_one_sentence, it
    should still anchor the 摘要 block."""
    selected_thesis = {
        "thesis_one_sentence": "本文以序跋与刻工题记为核心证据重建晚清江南刊本的断代依据。",
    }
    intro = _intro_section("第二段论述具体方法步骤。")
    abstract = _format_zh_abstract(selected_thesis, [intro])
    assert "本文以序跋与刻工题记" in abstract


def test_no_selected_thesis_uses_intro() -> None:
    """No selected_thesis (legacy run) → intro prose alone."""
    intro = _intro_section("本研究讨论晚清江南刊本的断代依据。")
    abstract = _format_zh_abstract(None, [intro])
    assert "本研究" in abstract


def test_empty_inputs_return_empty() -> None:
    """Empty thesis + no intro → empty string (skip wrapper)."""
    assert _format_zh_abstract(None, []) == ""
