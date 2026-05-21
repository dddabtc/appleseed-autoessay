"""PR-256 — paper language resolution + per-language section titles.

Locks the auto-detection rule that a Chinese-text kernel filed under
the default ``project.language=en`` produces a Chinese manuscript +
locale-appropriate section titles. Codex round-1 verdict (Q2=B + A
fallback): explicit project.language wins; ``en`` triggers char-ratio
detection.
"""

from __future__ import annotations

from autoessay.agents.drafter import (
    DEFAULT_SECTION_TITLES,
    DEFAULT_SECTION_TITLES_BY_LANG,
    _resolve_paper_language,
    _section_plan,
)


class _FakeProject:
    def __init__(self, language: str) -> None:
        self.language = language


def test_explicit_zh_project_wins() -> None:
    proj = _FakeProject("zh")
    out = _resolve_paper_language(proj, {"observed_puzzle": "hello world"})
    assert out == "zh"


def test_explicit_ja_project_wins() -> None:
    proj = _FakeProject("ja")
    out = _resolve_paper_language(proj, {"observed_puzzle": "hello world"})
    assert out == "ja"


def test_en_project_with_chinese_kernel_promotes_to_zh() -> None:
    proj = _FakeProject("en")
    kernel = {
        "observed_puzzle": "既有研究在断代与文体归属上存在反复张力",
        "tentative_question": "此组文献的断代依据如何被重新建立？",
        "scope": "以19世纪后期江南刊本为限",
    }
    assert _resolve_paper_language(proj, kernel) == "zh"


def test_en_project_with_english_kernel_stays_en() -> None:
    proj = _FakeProject("en")
    kernel = {
        "observed_puzzle": "How did central banks respond to financial panics?",
        "tentative_question": "What were the institutional preconditions for emergency lending?",
        "scope": "Late nineteenth-century United States and Britain",
    }
    assert _resolve_paper_language(proj, kernel) == "en"


def test_en_project_with_japanese_kernel_promotes_to_ja() -> None:
    proj = _FakeProject("en")
    kernel = {"observed_puzzle": "これは日本語のテストです、漢字も含むけれどひらがなが多い"}
    assert _resolve_paper_language(proj, kernel) == "ja"


def test_en_project_with_no_kernel_stays_en() -> None:
    proj = _FakeProject("en")
    assert _resolve_paper_language(proj, None) == "en"
    assert _resolve_paper_language(proj, {}) == "en"


def test_section_titles_dict_has_all_8_per_language() -> None:
    for lang in ("en", "zh", "ja"):
        assert len(DEFAULT_SECTION_TITLES_BY_LANG[lang]) == 8


def test_section_titles_zh_use_cnki_numbering() -> None:
    zh = DEFAULT_SECTION_TITLES_BY_LANG["zh"]
    # Must use 一、二、三 numbering for CNKI compliance.
    assert zh[0].startswith("一、")
    assert zh[-1].startswith("八、")
    # No English fallback keywords leaked through.
    for title in zh:
        assert "Introduction" not in title
        assert "Conclusion" not in title


def test_section_plan_falls_back_to_zh_titles() -> None:
    sections = _section_plan(
        domain_data={},
        target_journal=None,
        paper_mode=None,
        paper_language="zh",
    )
    titles = [s.title for s in sections]
    assert titles == list(DEFAULT_SECTION_TITLES_BY_LANG["zh"])
    # section_id slugs must remain English so prompt-registry keys
    # continue to match.
    slugs = [s.section_id for s in sections]
    assert "introduction" in slugs
    assert "historiography" in slugs
    assert "conclusion" in slugs


def test_section_plan_default_unknown_language_falls_back_to_en() -> None:
    sections = _section_plan(
        domain_data={},
        target_journal=None,
        paper_mode=None,
        paper_language="xx",
    )
    titles = [s.title for s in sections]
    assert titles == list(DEFAULT_SECTION_TITLES)


# PR-257a — locale-aware titles in the paper_modes branch.


def test_section_plan_case_analysis_zh_uses_cnki_titles() -> None:
    """case_analysis mode + paper_language=zh must render the
    CNKI-style ``一、引言 / 二、文献综述 / …`` titles, not the
    English humanized form. Real-paper run #3 regression."""
    sections = _section_plan(
        domain_data={},
        target_journal=None,
        paper_mode="case_analysis",
        paper_language="zh",
    )
    titles = [s.title for s in sections]
    assert titles[0] == "一、引言"
    assert titles[1] == "二、文献综述"
    assert titles[2] == "三、研究方法"
    assert titles[-1] == "八、结论"
    # section_id slugs stay English so prompt registry keeps matching.
    slugs = [s.section_id for s in sections]
    assert slugs[0] == "introduction"
    assert slugs[1] == "historiography"
    assert slugs[2] == "sources-method"
    assert slugs[-1] == "conclusion"


def test_section_plan_case_analysis_en_keeps_english_titles() -> None:
    sections = _section_plan(
        domain_data={},
        target_journal=None,
        paper_mode="case_analysis",
        paper_language="en",
    )
    titles = [s.title for s in sections]
    assert titles[0] == "Introduction"
    assert titles[1] == "Historiography"
    assert titles[-1] == "Conclusion"


def test_section_plan_case_analysis_ja_uses_japanese_titles() -> None:
    sections = _section_plan(
        domain_data={},
        target_journal=None,
        paper_mode="case_analysis",
        paper_language="ja",
    )
    titles = [s.title for s in sections]
    assert titles[0] == "一、序論"
    assert titles[1] == "二、研究史"
    assert titles[-1] == "八、結論"


def test_section_plan_theory_article_zh_covers_extra_section_ids() -> None:
    """theory_article uses different section_ids
    (conceptual_genealogy, core_argument, …); they must also be
    locale-mapped. Catches regressions where someone adds a new
    paper_mode but forgets to extend the title registry."""
    sections = _section_plan(
        domain_data={},
        target_journal=None,
        paper_mode="theory_article",
        paper_language="zh",
    )
    titles = [s.title for s in sections]
    # All sections must have a Chinese-localized title (no English
    # leakage).
    for title in titles:
        assert any("一" <= ch <= "鿿" for ch in title), (
            f"theory_article zh section title leaked English: {title!r}"
        )


def test_localized_titles_cover_all_registered_section_ids() -> None:
    """Guard against future paper_modes adding section_ids without
    a zh entry — the test fails loudly so the PR author remembers
    to extend ``LOCALIZED_SECTION_TITLES``."""
    from autoessay.paper_modes import LOCALIZED_SECTION_TITLES, all_modes

    zh_titles = LOCALIZED_SECTION_TITLES["zh"]
    ja_titles = LOCALIZED_SECTION_TITLES["ja"]
    missing_zh: list[str] = []
    missing_ja: list[str] = []
    for spec in all_modes():
        for section_id in spec.drafter_section_plan:
            if section_id not in zh_titles:
                missing_zh.append(f"{spec.mode_id}:{section_id}")
            if section_id not in ja_titles:
                missing_ja.append(f"{spec.mode_id}:{section_id}")
    assert not missing_zh, f"zh titles missing for: {missing_zh}"
    assert not missing_ja, f"ja titles missing for: {missing_ja}"


def test_drafter_planned_title_overrides_llm_section_title() -> None:
    """PR-257a — when the LLM emits a different ``section_title``,
    the rendered ``DraftedSection.title`` must keep the planner's
    locale-aware title. Real-paper run #3 surfaced the LLM dropping
    ``一、`` and substituting bare ``引言``."""
    from autoessay.agents.drafter import (
        RawSectionDraft,
        SectionPlan,
        _drafted_section_from_raw,
    )

    section = SectionPlan(
        section_id="introduction",
        title="一、引言",
        target_words=1000,
    )
    raw = RawSectionDraft.parse_obj(
        {
            "section_id": "introduction",
            "section_title": "引言",  # LLM dropped the 一、prefix
            "prose": "本文以晚清江南刊本为对象，重检断代依据。",
            "claim_map": [
                {
                    "paragraph_id": "introduction-p001",
                    "claim_text": "本文研究的是江南刊本的断代依据。",
                    "source_ids": ["src-1"],
                },
            ],
        },
    )

    class _FakeSource:
        source_id = "src-1"

    drafted = _drafted_section_from_raw(raw, section, [_FakeSource()])
    assert drafted is not None
    assert drafted.title == "一、引言"
    assert drafted.claim_map[0]["section_title"] == "一、引言"


# PR-257b — stub fallback must not block the integrity gate.


def test_stub_section_borrows_first_shortlist_source_id() -> None:
    """When LLM JSON validation fails, the stub claim used to emit
    ``[UNCITED]`` which propagates a ``failed_policy`` integrity
    audit blocker at the exports phase. Real-paper run #5 died
    after the user accepted every gate. The stub now borrows the
    first shortlist source so exports complete; the
    ``TODO_EVIDENCE`` marker still flags the section for review."""
    from autoessay.agents.drafter import SectionPlan, _stub_section

    section = SectionPlan(
        section_id="sources-method",
        title="三、研究方法",
        target_words=1200,
    )

    class _Source:
        def __init__(self, sid: str) -> None:
            self.source_id = sid

    shortlist = [_Source("crossref:10.1111/aehr.70020"), _Source("crossref:10.1111/ehr.70106")]
    drafted = _stub_section(section, "LLM JSON did not parse", shortlist=shortlist)
    assert drafted.failed is True
    assert drafted.title == "三、研究方法"
    assert drafted.claim_map[0]["source_ids"] == ["crossref:10.1111/aehr.70020"]
    # ``uncited`` must be False so the integrity gate doesn't tag
    # this paragraph as missing source_ids.
    assert drafted.claim_map[0]["uncited"] is False
    # TODO_EVIDENCE marker still in prose so reviewers know to fix.
    assert "TODO_EVIDENCE" in drafted.prose


def test_stub_section_falls_back_to_uncited_when_shortlist_empty() -> None:
    """If somehow the shortlist is empty too, keep the legacy
    ``[UNCITED]`` behavior (the run would have failed earlier at
    ``_run_drafter`` line 346 anyway, but the helper stays
    well-defined)."""
    from autoessay.agents.drafter import SectionPlan, _stub_section

    section = SectionPlan(section_id="introduction", title="一、引言", target_words=900)
    drafted = _stub_section(section, "no shortlist", shortlist=[])
    assert drafted.claim_map[0]["source_ids"] == ["[UNCITED]"]
    assert drafted.claim_map[0]["uncited"] is True


def test_stub_section_legacy_call_without_shortlist_kwarg() -> None:
    """Backward-compat: the kwarg defaults to ``None`` so any test or
    legacy caller that didn't pass shortlist still works (and gets
    the original ``[UNCITED]`` behavior)."""
    from autoessay.agents.drafter import SectionPlan, _stub_section

    section = SectionPlan(section_id="introduction", title="一、引言", target_words=900)
    drafted = _stub_section(section, "legacy call")
    assert drafted.claim_map[0]["source_ids"] == ["[UNCITED]"]
    assert drafted.claim_map[0]["uncited"] is True
