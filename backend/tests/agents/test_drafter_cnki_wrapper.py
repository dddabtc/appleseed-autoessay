"""PR-259a — CNKI front/back matter wrapper.

gpt-5.5 baseline papers have ``摘要 / 关键词 / 一、引言 / … /
八、结论 / 参考文献`` structure. After the PR-256..PR-258c series
the body sections are correct, but 摘要 / 关键词 / 参考文献 were
never generated. This wrapper adds them for zh / ja papers without
touching the en flow (Western convention puts these in submission
metadata, not the manuscript file).

Codex round-2 verdict (PR-257c → renumbered PR-259a, AGREE Q2=B2):
wrapper, NOT a body-section refactor — keep paper_modes section
list clean.
"""

from __future__ import annotations

from autoessay.agents.drafter import (
    DraftedSection,
    SectionPlan,
    _case_channel_anchor_directive,
    _extract_case_channels,
    _format_gb7714_reference,
    _format_zh_abstract,
    _manuscript_markdown,
    _normalize_inline_citations_zh,
    _render_zh_back_matter,
    _render_zh_front_matter,
    _sanitize_baseline_as_evidence_source_mentions,
    _select_zh_keywords_from_kernel,
    _wrap_manuscript_with_cnki_matter,
)
from autoessay.config import get_settings

JIANGNAN_KERNEL = {
    "scope": "以 19 世纪后期江南刊本为限，仅含序跋与刻工题记。",
    "observed_puzzle": "既有研究在断代与文体归属上存在反复张力。",
    "tentative_question": "此组文献的断代依据如何被重新建立？",
}

JIANGNAN_THESIS = {
    "thesis_one_sentence": (
        "晚清江南刊本的朝代归属应作为可被刊刻同期证据重建的过程，而非沿用后出目录的既定结论。"
    ),
    "working_title": "晚清江南刊本断代依据的重建",
}


def _make_section(section_id: str, title: str, prose: str) -> DraftedSection:
    plan = SectionPlan(section_id=section_id, title=title, target_words=1000)
    return DraftedSection(
        section_id=section_id,
        title=title,
        prose=prose,
        claim_map=[],
        failed=False,
        warnings=[],
        word_count=len(prose),
        target_words=plan.target_words,
    )


class _FakeSource:
    def __init__(
        self,
        source_id: str,
        title: str,
        authors: str = "",
        year: str = "",
        venue: str = "",
        abstract: str = "",
    ) -> None:
        self.source_id = source_id
        self.title = title
        self.authors = authors
        self.year = year
        self.venue = venue
        self.abstract = abstract


# ----- _select_zh_keywords_from_kernel ----------------------------


def test_keywords_pulled_from_jiangnan_kernel_scope() -> None:
    keywords = _select_zh_keywords_from_kernel(JIANGNAN_KERNEL, target_count=6)
    assert len(keywords) <= 6
    # Distinctive 3-4 char terms from the scope sentence are present.
    expected_any_of = {"江南刊本", "刻工题记", "断代依据", "文体归属"}
    assert any(term in keywords for term in expected_any_of), (
        f"none of the expected entity-shaped terms were extracted: {keywords}"
    )


def test_keywords_drop_generic_discourse_words() -> None:
    keywords = _select_zh_keywords_from_kernel(JIANGNAN_KERNEL)
    for noise in ("研究", "本文", "如何", "依据", "需要", "重新"):
        assert noise not in keywords, f"generic discourse term {noise!r} should have been filtered"


def test_keywords_no_substring_duplicates() -> None:
    """If both ``江南`` and ``江南刊本`` are candidates, only the
    longer survives — substring-of-longer terms are dropped so the
    keyword line stays informative."""
    keywords = _select_zh_keywords_from_kernel(JIANGNAN_KERNEL)
    for shorter in keywords:
        for longer in keywords:
            if shorter != longer:
                assert shorter not in longer, (
                    f"keyword {shorter!r} is nested in {longer!r} — substring filter failed"
                )


def test_keywords_empty_kernel_returns_empty_list() -> None:
    assert _select_zh_keywords_from_kernel(None) == []
    assert _select_zh_keywords_from_kernel({}) == []
    assert _select_zh_keywords_from_kernel({"scope": ""}) == []


def test_case_channel_anchor_extracts_three_channel_thesis() -> None:
    thesis = {
        "thesis_one_sentence": (
            "明末清初江南阳明心学的扩散，应理解为讲会、刻书与官学三类制度渠道并行运作的多轨过程。"
        )
    }

    assert _extract_case_channels(thesis, None) == ["讲会", "刻书", "官学"]
    directive = _case_channel_anchor_directive(
        "empirical-section-ii",
        selected_thesis=thesis,
        research_kernel=None,
    )
    assert "刻书" in directive
    assert "讲会" in directive
    assert "falling back to the first channel" in directive


# ----- _format_zh_abstract -----------------------------------------


def test_abstract_combines_thesis_and_intro_prose() -> None:
    intro = _make_section(
        "introduction",
        "一、引言",
        "本文以晚清江南刊本为对象，重检序跋与刻工题记中的断代信息。"
        "传统目录学常将刻本年代直接视为既成事实，但本文主张应回到"
        "刊刻同期证据进行重建。",
    )
    abstract = _format_zh_abstract(JIANGNAN_THESIS, [intro], target_chars=200)
    assert len(abstract) <= 200
    # Has both thesis fragment and intro fragment.
    assert "晚清江南刊本" in abstract
    assert "断代" in abstract


def test_abstract_with_no_intro_uses_thesis_only() -> None:
    abstract = _format_zh_abstract(JIANGNAN_THESIS, [], target_chars=200)
    assert "晚清江南刊本" in abstract


def test_abstract_with_empty_inputs_returns_empty() -> None:
    assert _format_zh_abstract(None, []) == ""
    assert _format_zh_abstract({}, []) == ""


def test_abstract_strips_intro_heading_line() -> None:
    """The drafter prose may include its own ``## 一、引言``
    heading line; the abstract must not echo it back."""
    intro = _make_section(
        "introduction",
        "一、引言",
        "## 一、引言\n\n这是引言的实际内容，应该被纳入摘要。",
    )
    abstract = _format_zh_abstract(JIANGNAN_THESIS, [intro], target_chars=200)
    assert "## 一、引言" not in abstract


# ----- _format_gb7714_reference ------------------------------------


def test_reference_format_full_metadata() -> None:
    src = _FakeSource(
        "crossref:10.x/example",
        title="晚清江南刊本断代研究",
        authors="陈虹虹",
        year="2025",
        venue="人文社科辑刊",
    )
    ref = _format_gb7714_reference(src, 1)
    assert ref.startswith("[1]")
    assert "陈虹虹" in ref
    assert "晚清江南刊本断代研究" in ref
    assert "人文社科辑刊" in ref
    assert "2025" in ref


def test_reference_format_minimal_metadata() -> None:
    """Missing authors / venue / year must not crash the renderer
    or produce stray empty period markers."""
    src = _FakeSource("crossref:10.x/empty", title="X")
    ref = _format_gb7714_reference(src, 7)
    assert ref.startswith("[7]")
    assert "X" in ref


def test_shadow_baseline_source_id_normalizes_to_numeric_citation() -> None:
    src = _FakeSource(
        "shadow_baseline_v001",
        title="Shadow Baseline Manuscript v001",
        authors="AutoEssay Shadow Baseline",
    )
    manuscript = "这一判断可由测试基线段落支撑[shadow_baseline_v001]。"
    normalized = _normalize_inline_citations_zh(manuscript, [src])  # type: ignore[list-item]
    assert normalized == "这一判断可由测试基线段落支撑[1]。"


def test_shadow_baseline_multi_source_marker_normalizes_when_first() -> None:
    shadow = _FakeSource(
        "shadow_baseline_v001",
        title="Shadow Baseline Manuscript v001",
        authors="AutoEssay Shadow Baseline",
    )
    official = _FakeSource(
        "official:imf:annual-report-1968",
        title="IMF Annual Report 1968",
        authors="IMF",
    )
    manuscript = (
        "这一判断同时依赖测试基线与官方年报"
        "[shadow_baseline_v001; official:imf:annual-report-1968]。"
    )
    normalized = _normalize_inline_citations_zh(
        manuscript,
        [shadow, official],  # type: ignore[list-item]
    )
    assert normalized == "这一判断同时依赖测试基线与官方年报[1][2]。"


def test_shadow_baseline_multi_source_marker_with_crossref_doi_not_read_as_author_year() -> None:
    shadow = _FakeSource(
        "shadow_baseline_v001",
        title="Shadow Baseline Evidence Dossier v001",
        authors="AutoEssay Shadow Baseline",
    )
    crossref = _FakeSource(
        "crossref:10.1111/ehr.70106",
        title="Persistence in a changing world",
        authors="Monnet",
        year="2026",
    )
    manuscript = (
        "分期判断同时依赖基线材料和经济史文献[shadow_baseline_v001; crossref:10.1111/ehr.70106]。"
    )

    normalized = _normalize_inline_citations_zh(
        manuscript,
        [shadow, crossref],  # type: ignore[list-item]
    )

    assert normalized == "分期判断同时依赖基线材料和经济史文献[1][2]。"


def test_chinese_institution_author_year_citations_normalize() -> None:
    imf = _FakeSource(
        "official:imf:annual-report-1968",
        title="International Monetary Fund Annual Report 1968",
        authors="International Monetary Fund",
        year="1968",
    )
    fed = _FakeSource(
        "official:fraser:bog-minutes-1968-03-20",
        title="Minutes of the Board of Governors of the Federal Reserve System",
        authors="Board of Governors of the Federal Reserve System",
        year="1968",
    )
    manuscript = (
        "这说明市场关闭与旧指令撤销属于同一危机窗口"
        "（国际货币基金组织，1968；美国联邦储备系统理事会，1968）。"
    )

    normalized = _normalize_inline_citations_zh(
        manuscript,
        [fed, imf],  # type: ignore[list-item]
    )

    assert normalized == "这说明市场关闭与旧指令撤销属于同一危机窗口[2][1]。"


def test_shadow_baseline_bare_source_id_sanitized_in_test_mode(monkeypatch) -> None:
    monkeypatch.setenv("AUTOESSAY_BASELINE_AS_EVIDENCE_TEST", "1")
    get_settings.cache_clear()

    rendered = _sanitize_baseline_as_evidence_source_mentions(
        "shadow_baseline_v001给出的分析框架可支撑分期判断[1]。"
    )

    assert rendered == "所引材料给出的分析框架可支撑分期判断[1]。"


def test_shadow_baseline_bracket_marker_not_sanitized_before_normalization(monkeypatch) -> None:
    monkeypatch.setenv("AUTOESSAY_BASELINE_AS_EVIDENCE_TEST", "1")
    get_settings.cache_clear()

    rendered = _sanitize_baseline_as_evidence_source_mentions(
        "这一判断可由[shadow_baseline_v001]支持。"
    )

    assert rendered == "这一判断可由[shadow_baseline_v001]支持。"


# ----- _render_zh_front_matter -------------------------------------


def test_front_matter_includes_both_blocks_when_data_present() -> None:
    intro = _make_section("introduction", "一、引言", "本文研究晚清江南刊本断代。")
    rendered = _render_zh_front_matter(JIANGNAN_THESIS, [intro], JIANGNAN_KERNEL)
    assert "## 摘要" in rendered
    assert "## 关键词" in rendered
    # 关键词 line uses Chinese fullwidth semicolon separator.
    assert "；" in rendered


def test_front_matter_empty_when_nothing_to_render() -> None:
    """No thesis + no intro + no kernel → empty string (caller
    skips wrapper rather than emit a heading-only block)."""
    assert _render_zh_front_matter(None, [], None) == ""


def test_bretton_kernel_keywords_do_not_disappear() -> None:
    keywords = _select_zh_keywords_from_kernel(
        {
            "observed_puzzle": "战后布雷顿森林体系的金本位安排在制度文本上长期保留。",
            "tentative_question": "布雷顿森林金本位承诺的实际约束力如何断定其失效节点？",
            "scope": "限定 1960-1971 年美元—黄金兑换通道与黄金池记录。",
        }
    )

    assert keywords
    assert any("布雷顿森林" in item for item in keywords)
    assert any("金本位" in item for item in keywords)


def test_abstract_truncation_ends_on_complete_sentence() -> None:
    intro = _make_section(
        "introduction",
        "一、引言",
        "这是第一句，用来说明问题背景。这里是第二句，用来说明研究方法。这里是第三句。",
    )

    abstract = _format_zh_abstract(None, [intro], target_chars=24)

    assert not abstract.endswith("…")
    assert abstract.endswith(("。", "！", "？", "；"))


def test_manuscript_markdown_omits_html_anchors() -> None:
    intro = _make_section("introduction", "一、引言", "## 一、引言\n\n正文。")

    rendered = _manuscript_markdown([intro])

    assert '<a id="introduction"></a>' not in rendered
    assert "## 一、引言" in rendered


def test_manuscript_markdown_prepends_parent_title_before_subsections() -> None:
    historiography = _make_section(
        "historiography",
        "二、文献综述",
        "### （一）书目著录路径\n\n这一类研究先处理书目条目。",
    )

    rendered = _manuscript_markdown([historiography])

    assert rendered.startswith("## 二、文献综述\n\n### （一）书目著录路径")


def test_manuscript_markdown_normalizes_wrong_top_level_title() -> None:
    method = _make_section("sources-method", "三、研究方法", "## Method\n\n正文。")

    rendered = _manuscript_markdown([method])

    assert rendered.startswith("## 三、研究方法")
    assert "## Method" not in rendered


# ----- _render_zh_back_matter --------------------------------------


def test_back_matter_lists_cited_sources_in_gb7714() -> None:
    sources = [
        _FakeSource("a", title="第一篇", authors="张三", year="2024"),
        _FakeSource("b", title="第二篇", authors="李四", year="2025"),
    ]
    back = _render_zh_back_matter(sources)
    assert back.startswith("## 参考文献")
    assert "[1]" in back
    assert "[2]" in back
    assert "张三" in back
    assert "李四" in back


def test_back_matter_empty_for_empty_cited_sources() -> None:
    assert _render_zh_back_matter([]) == ""


# ----- end-to-end wrapper ------------------------------------------


def test_wrapper_zh_adds_front_and_back_matter() -> None:
    intro = _make_section("introduction", "一、引言", "本文研究晚清江南刊本断代。")
    body = '<a id="introduction"></a>\n## 一、引言\n\n本文研究晚清江南刊本断代。\n'
    sources = [_FakeSource("a", title="参考一", authors="张三", year="2024")]
    out = _wrap_manuscript_with_cnki_matter(
        body,
        paper_language="zh",
        selected_thesis=JIANGNAN_THESIS,
        sections=[intro],
        research_kernel=JIANGNAN_KERNEL,
        cited_sources=sources,
    )
    assert "## 摘要" in out
    assert "## 关键词" in out
    assert "## 一、引言" in out
    assert "## 参考文献" in out
    # Body must come AFTER 摘要/关键词 and BEFORE 参考文献.
    abstract_pos = out.index("## 摘要")
    body_pos = out.index("## 一、引言")
    refs_pos = out.index("## 参考文献")
    assert abstract_pos < body_pos < refs_pos


def test_wrapper_en_passes_through_unchanged() -> None:
    """en convention: abstract goes into submission form, refs into
    citations.bib. Manuscript file stays body-only."""
    body = "## Introduction\n\nThis is the body.\n"
    out = _wrap_manuscript_with_cnki_matter(
        body,
        paper_language="en",
        selected_thesis=JIANGNAN_THESIS,
        sections=[],
        research_kernel=JIANGNAN_KERNEL,
        cited_sources=[_FakeSource("a", title="Ref", authors="Smith", year="2024")],
    )
    assert "摘要" not in out
    assert "参考文献" not in out
    assert out == body


def test_wrapper_ja_uses_japanese_headings() -> None:
    """ja papers use 要旨 / キーワード headings instead of zh
    terms. We re-use the zh keyword extractor since the kernel
    here mixes ja text with the same Han-character entities the
    extractor knows; for a kernel with no extracted terms the
    キーワード block is just skipped (same back-compat as zh)."""
    body = "## 一、序論\n\n本文の本体。\n"
    out = _wrap_manuscript_with_cnki_matter(
        body,
        paper_language="ja",
        selected_thesis={"thesis_one_sentence": "本論の主張。"},
        sections=[_make_section("introduction", "一、序論", "本論の本体。")],
        # Kernel includes terms the keyword extractor recognizes
        # so the キーワード block actually renders.
        research_kernel={"scope": "江南刊本の序跋と刻工題記の研究"},
        cited_sources=[_FakeSource("a", title="参考", authors="山田", year="2024")],
    )
    # ja uses 要旨 / キーワード headings instead of zh terms.
    assert "## 要旨" in out
    assert "## キーワード" in out
    assert "## 摘要" not in out
    assert "## 关键词" not in out
    # 参考文献 is shared zh+ja convention so the heading stays the same.
    assert "## 参考文献" in out


# ----- PR-259b: inline citation normalization ---------------------


def test_normalize_inline_citations_western_author_year() -> None:
    """``(Eisenstein 1980)`` → ``[1]`` when Eisenstein/1980 is the
    first cited source in the references list."""
    sources = [
        _FakeSource("crossref:1", title="Print Culture", authors="Eisenstein", year="1980"),
        _FakeSource("crossref:2", title="X", authors="Petryszak", year="2023"),
    ]
    body = "This is the body. (Eisenstein 1980) is the foundational text."
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "(Eisenstein 1980)" not in out
    assert "Petryszak" not in out  # source 2 not cited inline → unchanged


def test_normalize_inline_citations_chinese_full_width_parens() -> None:
    """zh papers use full-width ``（陈虹虹 2025）`` — the same
    rewriter must catch them."""
    sources = [
        _FakeSource("crossref:a", title="X", authors="陈虹虹", year="2025"),
    ]
    body = "本文沿用既有论述（陈虹虹 2025）。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "（陈虹虹 2025）" not in out


def test_normalize_inline_citations_multi_author_picks_first_surname() -> None:
    """``(Smith and Jones 2024)`` and ``(Koad et al. 2025)`` should
    match on the first surname only."""
    sources = [
        _FakeSource("crossref:a", title="X", authors="Smith", year="2024"),
        _FakeSource("crossref:b", title="Y", authors="Koad", year="2025"),
    ]
    body = "Foundational work (Smith and Jones 2024) and recent (Koad et al. 2025)."
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "[2]" in out
    assert "Smith and Jones" not in out
    assert "Koad et al." not in out


def test_normalize_inline_citations_crossref_doi_marker() -> None:
    """``[crossref:10.x/abc]`` → ``[N]`` when that DOI is in
    cited_sources."""
    sources = [
        _FakeSource("crossref:10.1234/abc", title="X", authors="Smith", year="2024"),
    ]
    body = "Body with [crossref:10.1234/abc] inline marker."
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "[crossref:10.1234/abc]" not in out


def test_normalize_inline_citations_crossref_case_insensitive_doi() -> None:
    """DOI matching must be case-insensitive — crossref
    canonicalizes to lowercase but body text often preserves
    publisher capitalization."""
    sources = [
        _FakeSource("crossref:10.1234/abc", title="X", authors="Smith", year="2024"),
    ]
    body = "Body with [crossref:10.1234/ABC] uppercase variant."
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out


def test_normalize_inline_citations_fullwidth_source_id_brackets() -> None:
    """LLMs sometimes emit Chinese full-width source wrappers like
    ``〔https://openalex.org/W...〕`` or ``［crossref:...］``. These
    must normalize before blind scoring sees raw source IDs."""
    sources = [
        _FakeSource("https://openalex.org/W4408733095", title="X", authors="Cento", year="2025"),
        _FakeSource("crossref:10.1111/ehr.70106", title="Y", authors="Monnet", year="2026"),
    ]
    body = (
        "黄金窗口压力已有讨论〔https://openalex.org/W4408733095〕；"
        "黄金背书与自主性关系另见［crossref:10.1111/ehr.70106］。"
    )

    out = _normalize_inline_citations_zh(body, sources)

    assert "〔https://openalex.org/W4408733095〕" not in out
    assert "［crossref:10.1111/ehr.70106］" not in out
    assert "[1]" in out
    assert "[2]" in out


def test_normalize_inline_citations_parenthesized_source_id() -> None:
    sources = [
        _FakeSource(
            "crossref:10.1525/9780520921474",
            title="X",
            authors="Elman",
            year="2000",
        ),
    ]
    body = "制度背景可由相关研究界定（crossref:10.1525/9780520921474）。"

    out = _normalize_inline_citations_zh(body, sources)

    assert out == "制度背景可由相关研究界定[1]。"


def test_normalize_inline_citations_official_source_ids() -> None:
    sources = [
        _FakeSource(
            "official:fraser:bog-minutes-1968-03-20",
            title="Fed minutes",
            authors="Board of Governors",
            year="1968",
        ),
        _FakeSource(
            "official:imf:annual-report-1968",
            title="IMF Annual Report",
            authors="International Monetary Fund",
            year="1968",
        ),
    ]
    body = (
        "联储纪要可见〔official:fraser:bog-minutes-1968-03-20〕，"
        "IMF 年报亦可见 [official:imf:annual-report-1968]。"
    )

    out = _normalize_inline_citations_zh(body, sources)

    assert "official:" not in out
    assert "[1]" in out
    assert "[2]" in out


def test_normalize_inline_citations_fullwidth_numeric_brackets() -> None:
    sources = [
        _FakeSource("crossref:10.1234/a", title="X", authors="Smith", year="2024"),
        _FakeSource("crossref:10.1234/b", title="Y", authors="Jones", year="2025"),
    ]
    body = "已有研究可见〔1〕，另一处参见［2］，还可参照【1】。"

    out = _normalize_inline_citations_zh(body, sources)

    assert "〔1〕" not in out
    assert "［2］" not in out
    assert "【1】" not in out
    assert "[1]" in out
    assert "[2]" in out


def test_normalize_inline_citations_unmatched_marker_left_alone() -> None:
    """If the cited author/year isn't in the references list,
    leave the original marker so we don't corrupt intentional
    contextual mentions."""
    sources = [
        _FakeSource("crossref:1", title="X", authors="Eisenstein", year="1980"),
    ]
    body = "Cf. (Mukherjee 2019) for a different view."
    out = _normalize_inline_citations_zh(body, sources)
    assert "(Mukherjee 2019)" in out
    assert "[1]" not in out


def test_normalize_inline_citations_empty_cited_sources_noop() -> None:
    body = "Body with (Eisenstein 1980) cite."
    out = _normalize_inline_citations_zh(body, [])
    assert out == body


def test_normalize_inline_citations_preserves_non_cite_parens() -> None:
    """Plain parenthetical text without a year shouldn't be
    touched."""
    sources = [
        _FakeSource("crossref:1", title="X", authors="Smith", year="2024"),
    ]
    body = "This is text (with a side note) and (Smith 2024) cite."
    out = _normalize_inline_citations_zh(body, sources)
    assert "(with a side note)" in out
    assert "[1]" in out


def test_normalize_inline_citations_year_only_paren_left_alone() -> None:
    sources = [
        _FakeSource("crossref:1", title="X", authors="Smith", year="2024"),
    ]
    body = "Published in (2024) and (Smith 2024) cited the original."
    out = _normalize_inline_citations_zh(body, sources)
    assert "(2024)" in out
    assert "[1]" in out


def test_wrapper_zh_with_no_cited_sources_skips_back_matter() -> None:
    intro = _make_section("introduction", "一、引言", "本文研究。")
    body = "## 一、引言\n\n本文研究。\n"
    out = _wrap_manuscript_with_cnki_matter(
        body,
        paper_language="zh",
        selected_thesis=JIANGNAN_THESIS,
        sections=[intro],
        research_kernel=JIANGNAN_KERNEL,
        cited_sources=[],
    )
    assert "## 摘要" in out  # front matter still present
    assert "## 参考文献" not in out  # back matter skipped
