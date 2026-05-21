"""PR-398 regression: math-mode (gpt-5.5 holistic stage B) drafter
prose often starts with ``### 一、引言`` (h3) instead of the expected
``## 一、引言`` (h2). The h2-only ``_SECTION_HEADING_RE`` didn't
recognize it, the section-assembly fallback prepended ``## 一、引言``,
and the manuscript ended up with two consecutive identical headings —
visible to the user in the docx export 2026-05-14.

Codex A' amendment: only normalize h3+ first lines whose text EQUALS
``section.title``. Legitimate subsections like ``### （一）书目著录路径``
must still trigger the prepend-parent-heading fallback (regression risk
the naive ``#{1,6}`` regex would have introduced).
"""

from __future__ import annotations

from autoessay.agents.drafter import DraftedSection, _manuscript_markdown


def _section(title: str, prose: str, *, section_id: str = "introduction") -> DraftedSection:
    return DraftedSection(
        section_id=section_id,
        title=title,
        prose=prose,
        claim_map=[],
        failed=False,
        warnings=[],
        word_count=len(prose),
        target_words=len(prose),
    )


def test_h3_heading_matching_title_is_normalized_to_h2() -> None:
    """The bug repro: ``### 一、引言`` first line + section.title
    ``一、引言`` → must NOT produce a duplicate."""
    section = _section(
        "一、引言",
        "### 一、引言\n\n晚清江南刊本断代之所以值得重建...",
    )
    out = _manuscript_markdown([section])
    # Single h2 heading, no leftover h3 duplicate.
    assert out.count("## 一、引言") == 1
    assert "### 一、引言" not in out
    # Body still present.
    assert "晚清江南刊本断代之所以值得重建" in out


def test_h2_heading_normalization_unchanged() -> None:
    """Existing h2 path: heading text gets replaced with section.title."""
    section = _section("三、研究方法", "## Method\n\nbody text")
    out = _manuscript_markdown([section])
    assert "## 三、研究方法" in out
    assert "## Method" not in out
    assert "body text" in out


def test_legitimate_h3_subsection_not_normalized() -> None:
    """Codex amendment regression guard: ``### （一）书目著录路径``
    is a real subsection, not a duplicate of the parent. Must NOT be
    rewritten to ``## 二、文献综述`` — that would eat the subsection.
    """
    section = _section(
        "二、文献综述",
        "### （一）书目著录路径\n\n关于著录路径的讨论...",
    )
    out = _manuscript_markdown([section])
    # Parent heading prepended (else branch).
    assert "## 二、文献综述" in out
    # Subsection heading preserved verbatim.
    assert "### （一）书目著录路径" in out


def test_no_leading_heading_prepends_section_title() -> None:
    """No heading at all → prepend ``## section.title``."""
    section = _section("四、案例分析", "本节通过晚清江南刊本断代案例…")
    out = _manuscript_markdown([section])
    assert out.startswith("## 四、案例分析")
    assert "本节通过晚清江南刊本断代案例" in out


def test_multiple_sections_only_one_h2_per_section() -> None:
    """Both buggy (h3-equals-title) and non-buggy sections in one
    manuscript; assert each contributes exactly one h2 heading."""
    sections = [
        _section("一、引言", "### 一、引言\n\n引言正文…"),
        _section(
            "二、文献综述",
            "### （一）书目著录路径\n\n小节正文…",
            section_id="historiography",
        ),
        _section(
            "三、研究方法",
            "### 三、研究方法\n\n方法正文…",
            section_id="sources-method",
        ),
    ]
    out = _manuscript_markdown(sections)
    assert out.count("## 一、引言") == 1
    assert out.count("## 二、文献综述") == 1
    assert out.count("## 三、研究方法") == 1
    # The first and third sections produced their own duplicate-headed
    # input but normalization stripped the h3 duplicates.
    assert out.count("### 一、引言") == 0
    assert out.count("### 三、研究方法") == 0
    # The legitimate subsection in section 2 stayed.
    assert out.count("### （一）书目著录路径") == 1


def test_blank_lines_before_h3_still_handled() -> None:
    """``\\n\\n### 一、引言`` (with leading blank lines) should also
    be normalized — the helper walks past blank lines first."""
    section = _section(
        "一、引言",
        "\n\n### 一、引言\n\n正文…",
    )
    out = _manuscript_markdown([section])
    assert "### 一、引言" not in out
    assert "## 一、引言" in out
