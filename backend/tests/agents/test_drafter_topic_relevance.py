"""PR-258a — drafter topic-relevance scoring + prompt directive.

Real-paper run #6 manuscript was 60% off-topic Dutch monetary-
policy / EMU content for a 19th-century Jiangnan publishing study,
because the curator picked Oudenampsen 2025 and the drafter
prompt's existing ``anchor_check`` rule was too permissive
("conceptual continuation" caught any methodological abstraction).
The integrity gate caught the drift at exports and produced a
``FAILED_POLICY`` that left the run unrecoverable.

Codex round-3 verdict (PR-258a, AGREE Q1=A+B, Q2=3-bin scoring):
extract topic keywords from project_title + research_kernel; tag
each shortlist source with ``topic_relevance`` (high/medium/low);
ban LLM from citing ``low``; restrict ``medium`` to
background/methodology; explicit regression fixture that mixes an
Oudenampsen-style off-topic source into a Jiangnan kernel and
asserts the prompt bans it.
"""

from __future__ import annotations

import json

from autoessay.agents.drafter import (
    DraftedSection,
    _apply_material_scope_guard_to_sections,
    _approved_source_summaries,
    _check_claim_grounding,
    _evidence_strength_directive,
    _extract_topic_keywords,
    _material_scope_guard_directive,
    _material_scope_guard_summary,
    _score_source_topic_relevance,
    _topic_relevance_directive,
)

JIANGNAN_KERNEL = {
    "observed_puzzle": ("既有研究在断代与文体归属上存在反复张力，需要重新检视一手材料以厘清边界。"),
    "tentative_question": "此组文献的断代依据如何被重新建立？",
    "scope": "以 19 世纪后期江南刊本为限，仅含序跋与刻工题记。",
}


class _FakeSource:
    """Minimal NormalizedSource shape — only the fields the scoring
    helpers read."""

    def __init__(
        self,
        source_id: str,
        title: str,
        abstract: str = "",
        authors: str = "",
        year: str = "2025",
        venue: str = "",
    ) -> None:
        self.source_id = source_id
        self.title = title
        self.abstract = abstract
        self.authors = authors
        self.year = year
        self.venue = venue


# ----- _extract_topic_keywords ------------------------------------


def test_keyword_extraction_from_chinese_kernel_yields_expected_2grams() -> None:
    keywords = _extract_topic_keywords("[PWTEST] run", JIANGNAN_KERNEL)
    assert "江南" in keywords
    assert "晚清" not in keywords  # kernel uses "19 世纪后期", not "晚清"
    assert "刊本" in keywords
    assert "序跋" in keywords
    assert "刻工" in keywords
    assert "题记" in keywords
    assert "断代" in keywords


def test_keyword_extraction_bridges_zh_to_en_synonyms() -> None:
    """When the kernel contains ``江南`` / ``刊本`` (zh), an English
    source title containing ``Jiangnan`` / ``imprint`` must still
    score as relevant. This is what makes cross-lingual matching
    work without an embedding lookup."""
    keywords = _extract_topic_keywords("", JIANGNAN_KERNEL)
    # English bridges seeded for zh entities present in the kernel.
    assert "jiangnan" in keywords
    assert "preface" in keywords or "prefaces" in keywords
    assert "colophon" in keywords or "colophons" in keywords


def test_keyword_extraction_bridges_bretton_kernel_terms() -> None:
    keywords = _extract_topic_keywords(
        "",
        {
            "observed_puzzle": "布雷顿森林体系的金本位安排在 1960 年代被掏空。",
            "tentative_question": "美元—黄金承诺的实际约束力如何失效？",
            "scope": "限定黄金池与美元可兑换问题。",
        },
    )

    assert "bretton woods" in keywords
    assert "gold standard" in keywords
    assert "convertibility" in keywords


def test_keyword_extraction_drops_generic_stopwords() -> None:
    """Generic financial / fiscal / European nouns must NOT enter
    the topic set — that's the exact path that let Oudenampsen's
    Dutch fiscal-policy paper leak into walk6."""
    keywords = _extract_topic_keywords(
        "A fiscal-policy study of monetary integration in Europe",
        None,
    )
    for stopword in ("fiscal", "policy", "monetary", "european", "study"):
        assert stopword not in keywords


def test_keyword_extraction_empty_kernel_returns_empty_set() -> None:
    assert _extract_topic_keywords("", None) == set()
    assert _extract_topic_keywords("", {}) == set()


# ----- _score_source_topic_relevance ------------------------------


def test_score_relevant_chinese_source_is_high() -> None:
    keywords = _extract_topic_keywords("", JIANGNAN_KERNEL)
    source = _FakeSource(
        "stub:1",
        title="晚清江南刊本序跋题记考",
        abstract="对19世纪后期江南刊本的序跋与刻工题记进行汇考与断代。",
    )
    assert _score_source_topic_relevance(source, keywords) == "high"


def test_score_relevant_english_source_via_bridge_is_high() -> None:
    keywords = _extract_topic_keywords("", JIANGNAN_KERNEL)
    source = _FakeSource(
        "stub:2",
        title="Late Qing Print Culture in Jiangnan: Imprints, Colophons, and Engravers",
        abstract="A study of nineteenth-century editions and prefaces from the Yangtze region.",
    )
    assert _score_source_topic_relevance(source, keywords) == "high"


def test_score_off_topic_dutch_fiscal_paper_is_low() -> None:
    """Regression for real-paper run #6: an Oudenampsen-style Dutch
    fiscal-policy paper must score ``low`` so the drafter prompt
    bans it from citation."""
    keywords = _extract_topic_keywords("", JIANGNAN_KERNEL)
    oudenampsen_like = _FakeSource(
        "crossref:fake-oudenampsen",
        title=(
            "Public Choice Theory, Fiscal Hawkishness, and the European "
            "Monetary Union: A Dutch Perspective"
        ),
        abstract=(
            "This paper examines how Dutch policy elites translated "
            "public-choice theory into fiscal hawkishness during the "
            "EMU negotiations of the 1990s."
        ),
    )
    assert _score_source_topic_relevance(oudenampsen_like, keywords) == "low"


def test_score_partial_match_is_medium() -> None:
    """A source that hits 1-2 keywords (e.g. only ``19th-century``
    but no Jiangnan / publishing terms) should be ``medium`` — usable
    only as background, never as main argument."""
    keywords = _extract_topic_keywords("", JIANGNAN_KERNEL)
    source = _FakeSource(
        "stub:3",
        title="Nineteenth-century historiography and methodological reflections",
        abstract="A general essay on writing 19th-century history.",
    )
    score = _score_source_topic_relevance(source, keywords)
    assert score == "medium"


def test_score_with_empty_keywords_is_low() -> None:
    """When the kernel was empty (no keywords) we can't score
    anything, so all sources get ``low`` — callers must check the
    keyword set wasn't empty before trusting any score."""
    source = _FakeSource("stub:4", title="Anything")
    assert _score_source_topic_relevance(source, set()) == "low"


# ----- _approved_source_summaries integration ---------------------


def test_approved_source_summaries_no_topic_keywords_omits_field() -> None:
    """Back-compat: callers that don't pass ``topic_keywords`` get
    summaries without the ``topic_relevance`` field, so legacy tests
    + non-drafter consumers keep working."""
    src = _FakeSource("stub:5", title="X")
    out = _approved_source_summaries([src], {})
    assert "topic_relevance" not in out[0]


def test_approved_source_summaries_with_topic_keywords_tags_each() -> None:
    keywords = _extract_topic_keywords("", JIANGNAN_KERNEL)
    on_topic = _FakeSource("on:1", title="晚清江南刊本序跋研究")
    off_topic = _FakeSource(
        "off:1",
        title="Public Choice and EMU in the Netherlands",
        abstract="Dutch fiscal policy.",
    )
    out = _approved_source_summaries([on_topic, off_topic], {}, topic_keywords=keywords)
    assert out[0]["topic_relevance"] == "high"
    assert out[1]["topic_relevance"] == "low"


def test_approved_source_summaries_marks_metadata_only_source_limits() -> None:
    src = _FakeSource("meta:1", title="Verified book record")
    src.access_status = "metadata_only"
    src.risk_flags = ["metadata_only_no_full_text"]

    out = _approved_source_summaries([src], {})

    assert out[0]["evidence_access"] == "metadata_only"
    assert "bibliographic positioning" in out[0]["source_use_limit"]


# ----- _topic_relevance_directive ---------------------------------


def test_directive_is_empty_when_no_relevance_field() -> None:
    """Back-compat: legacy summaries (no topic_relevance) → empty
    directive, so the prompt body length is unchanged for callers
    that haven't migrated."""
    summaries = [{"source_id": "s1", "title": "X"}]
    assert _topic_relevance_directive(summaries) == ""


def test_directive_lists_low_source_ids_as_banned() -> None:
    summaries = [
        {"source_id": "off:1", "topic_relevance": "low"},
        {"source_id": "off:2", "topic_relevance": "low"},
        {"source_id": "on:1", "topic_relevance": "high"},
    ]
    directive = _topic_relevance_directive(summaries)
    assert "MUST NOT cite" in directive
    assert "off:1" in directive
    assert "off:2" in directive
    # high-relevance sources NOT listed in the ban list.
    assert json.dumps(["off:1", "off:2"], sort_keys=True) in directive


def test_directive_lists_metadata_only_source_ids_as_limited() -> None:
    summaries = [
        {
            "source_id": "meta:1",
            "topic_relevance": "high",
            "evidence_access": "metadata_only",
        }
    ]

    directive = _topic_relevance_directive(summaries)

    assert "metadata_only_directive" in directive
    assert "meta:1" in directive
    assert "not usable text evidence" in directive


def test_directive_marks_medium_as_background_only() -> None:
    summaries = [
        {"source_id": "med:1", "topic_relevance": "medium"},
        {"source_id": "on:1", "topic_relevance": "high"},
    ]
    directive = _topic_relevance_directive(summaries)
    assert "background" in directive
    assert "med:1" in directive


def test_directive_with_no_low_or_medium_keeps_general_warning_only() -> None:
    """All sources high → directive still emitted (since
    topic_relevance is present) but contains no ban / restriction
    list. The trailing "if no usable high source, omit citation"
    sentence stays."""
    summaries = [
        {"source_id": "on:1", "topic_relevance": "high"},
        {"source_id": "on:2", "topic_relevance": "high"},
    ]
    directive = _topic_relevance_directive(summaries)
    assert "topic_adherence_directive" in directive
    assert "MUST NOT cite" not in directive
    assert "background" not in directive
    assert "omit the citation" in directive


def test_evidence_strength_directive_rescopes_missing_archive_chain() -> None:
    thesis = {
        "missing_evidence": (
            "缺少可直接界定失效节点的一手档案链条，例如 IMF 内部备忘录、"
            "美联储理事会会议纪要与黄金池季度结算记录。"
        ),
        "risks": ["若仅依赖二手综述，容易过度断定具体失效时点。"],
    }
    sources = [
        {
            "source_id": "crossref:archive-catalogue",
            "evidence_access": "metadata_only",
        }
    ]

    directive = _evidence_strength_directive(
        thesis,
        sources,
        {
            "scope": (
                "限定 1960-1971 年美元—黄金兑换通道，以 IMF 内部备忘录、"
                "美联储理事会会议纪要与黄金池季度结算记录为主。"
            )
        },
    )

    assert "evidence_strength_directive" in directive
    assert "hard limit" in directive
    assert "definitive month/date node" in directive
    assert "crossref:archive-catalogue" in directive


def test_evidence_strength_directive_empty_without_missing_signal() -> None:
    directive = _evidence_strength_directive(
        {"thesis_one_sentence": "现有文献能够支持一个稳健结论。"},
        [{"source_id": "src1", "evidence_access": "text_available"}],
        {"scope": "普通文献综述。"},
    )

    assert directive == ""


def test_material_scope_guard_directive_from_insufficient_diagnostic() -> None:
    diagnostic = {
        "sufficient": False,
        "recommended_action": "iterate",
        "missing_materials": [
            "IMF 内部备忘录",
            "美联储会议纪要",
            "伦敦黄金池季度结算记录",
        ],
        "risks": ["当前材料只能支持压力累积，不能锁定唯一节点。"],
        "candidate_titles": ["1968年前后的候选观察窗口"],
        "rationale": "缺少连续一手档案链。",
    }

    directive = _material_scope_guard_directive(
        diagnostic,
        selected_thesis={"missing_evidence": "缺少一手档案。"},
        research_kernel={"scope": "布雷顿森林美元黄金承诺。"},
    )

    assert "material_scope_guard" in directive
    assert "NOT sufficient" in directive
    assert "候选观察窗口" in directive
    assert "IMF 内部备忘录" in directive


def test_material_scope_guard_summary_does_not_apply_when_proceed() -> None:
    summary = _material_scope_guard_summary(
        {
            "sufficient": True,
            "recommended_action": "proceed",
            "missing_materials": ["档案"],
        },
        selected_thesis={},
        research_kernel={},
    )

    assert summary["applied"] is False


def test_material_scope_guard_rewrites_overclaimed_method_section() -> None:
    section = DraftedSection(
        section_id="sources-method",
        title="三、研究方法",
        prose=(
            "## 三、研究方法\n\n"
            "本文采用档案化的过程追踪方法，重建失效节点的形成过程，并判断"
            "1968年前后更可能是失效节点。"
        ),
        claim_map=[
            {
                "paragraph_id": "sources-method-p001",
                "claim_text": ("本文采用档案化的过程追踪方法，重建失效节点的形成过程。"),
                "source_ids": ["crossref:archive-catalogue"],
                "evidence_status": "source_bound",
            }
        ],
        failed=False,
        warnings=[],
        word_count=20,
        target_words=300,
    )

    guarded = _apply_material_scope_guard_to_sections([section])

    assert "材料边界必须先说明" in guarded[0].prose
    assert "本文提出档案化过程追踪的研究设计" in guarded[0].prose
    assert "候选观察窗口" in guarded[0].prose
    assert "重建失效节点的形成过程" not in guarded[0].prose
    assert any(
        claim["paragraph_id"] == "sources-method-material-scope" for claim in guarded[0].claim_map
    )
    assert "研究设计" in guarded[0].claim_map[0]["claim_text"]


def test_material_scope_guard_inserted_paragraph_is_not_bretton_specific() -> None:
    section = DraftedSection(
        section_id="sources-method",
        title="三、研究方法",
        prose="## 三、研究方法\n\n本文采用文本分析与制度语境比较。",
        claim_map=[],
        failed=False,
        warnings=[],
        word_count=12,
        target_words=300,
    )

    guarded = _apply_material_scope_guard_to_sections(
        [section],
        selected_thesis={"thesis_one_sentence": "解释明末清初江南阳明心学传播路径的多线过程。"},
        research_kernel={"scope": "限定 1573-1644 年江南地区。"},
    )

    assert "材料边界必须先说明" in guarded[0].prose
    assert "阳明心学传播路径" in guarded[0].prose
    assert "1968" not in guarded[0].prose
    assert "失效节点" not in guarded[0].prose
    inserted = guarded[0].claim_map[-1]
    assert inserted["paragraph_id"] == "sources-method-material-scope"
    assert "1968" not in inserted["claim_text"]
    assert "失效节点" not in inserted["claim_text"]


def test_material_scope_guard_retitles_empirical_sections_when_cases_are_insufficient() -> None:
    section = DraftedSection(
        section_id="empirical-section-i",
        title="四、案例分析（一）",
        prose="## 四、案例分析（一）\n\n本节说明序跋时间线索的证据条件。",
        claim_map=[],
        failed=False,
        warnings=[],
        word_count=18,
        target_words=300,
    )

    guarded = _apply_material_scope_guard_to_sections([section])

    assert guarded[0].title == "四、证据类型分析（一）"
    assert guarded[0].prose.startswith("## 四、证据类型分析（一）")
    assert "## 四、案例分析（一）" not in guarded[0].prose


def test_material_scope_guard_keeps_plain_empirical_section_titles() -> None:
    section = DraftedSection(
        section_id="empirical-section-i",
        title="Empirical Section I",
        prose="## Empirical Section I\n\nThis section reports a scoped evidentiary relation.",
        claim_map=[],
        failed=False,
        warnings=[],
        word_count=12,
        target_words=300,
    )

    guarded = _apply_material_scope_guard_to_sections([section])

    assert guarded[0].title == "Empirical Section I"
    assert guarded[0].prose.startswith("## Empirical Section I")


def test_grounding_scanner_skips_material_limitation_statement(tmp_path) -> None:
    section = DraftedSection(
        section_id="sources-method",
        title="三、研究方法",
        prose="",
        claim_map=[
            {
                "paragraph_id": "sources-method-material-scope",
                "claim_text": "现有材料不足以证明 IMF 内部备忘录之间的连续一手档案链。",
                "source_ids": [],
                "evidence_status": "model_backed",
            }
        ],
        failed=False,
        warnings=[],
        word_count=0,
        target_words=100,
    )

    diagnostic = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[],
        run_dir=tmp_path,
    )

    assert diagnostic["weakly_grounded_count"] == 0


def test_grounding_scanner_still_flags_positive_archive_claim(tmp_path) -> None:
    section = DraftedSection(
        section_id="sources-method",
        title="三、研究方法",
        prose="",
        claim_map=[
            {
                "paragraph_id": "sources-method-p001",
                "claim_text": "本文依据 IMF 内部备忘录重建失效节点。",
                "source_ids": ["src:background"],
                "evidence_status": "source_bound",
            }
        ],
        failed=False,
        warnings=[],
        word_count=0,
        target_words=100,
    )

    class _Source:
        source_id = "src:background"
        title = "Background study"
        abstract = "No primary archive."
        venue = ""

    diagnostic = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[_Source()],
        run_dir=tmp_path,
    )

    assert diagnostic["weakly_grounded_count"] == 1


# ----- end-to-end regression --------------------------------------


def test_full_pipeline_oudenampsen_in_jiangnan_shortlist_gets_banned() -> None:
    """End-to-end regression for real-paper run #6.

    Given a 19c Jiangnan publishing kernel + a shortlist mixing
    an on-topic Brokaw-style source with an off-topic Oudenampsen-
    style Dutch fiscal-policy paper, the prompt directive must:
    (1) tag the Oudenampsen source as ``low``,
    (2) explicitly list its source_id in the ban list, and
    (3) NOT list the Brokaw source in the ban list.

    This is the one test codex's round-3 verdict explicitly asked
    for ("PR-258a 应加一个 regression fixture").
    """
    keywords = _extract_topic_keywords("[PWTEST] 江南刊本", JIANGNAN_KERNEL)

    brokaw_like = _FakeSource(
        source_id="crossref:brokaw-jiangnan-print",
        title="Commerce in Culture: The Sibao Book Trade in the Qing and Republican Eras",
        abstract=(
            "A study of the social history of book publishing in late "
            "imperial Jiangnan, including engravers, prefaces, and "
            "colophons."
        ),
    )
    oudenampsen_like = _FakeSource(
        source_id="crossref:oudenampsen-emu",
        title=("Public Choice Theory, Fiscal Hawkishness, and the European Monetary Union"),
        abstract=("Dutch policy elites and the EMU negotiations of the 1990s."),
    )

    summaries = _approved_source_summaries(
        [brokaw_like, oudenampsen_like],
        {},
        topic_keywords=keywords,
    )
    by_id = {entry["source_id"]: entry for entry in summaries}

    assert by_id["crossref:brokaw-jiangnan-print"]["topic_relevance"] == "high"
    assert by_id["crossref:oudenampsen-emu"]["topic_relevance"] == "low"

    directive = _topic_relevance_directive(summaries)
    assert "crossref:oudenampsen-emu" in directive
    assert "crossref:brokaw-jiangnan-print" not in directive
    assert "MUST NOT cite" in directive


# ----- PR-258b: post-LLM low-relevance source filter --------------


def test_drafted_section_strips_low_relevance_source_ids() -> None:
    """When the LLM ignores the topic_relevance directive and cites
    a banned source anyway, ``_drafted_section_from_raw`` must drop
    those source_ids from the claim_map. Real-paper run #7
    surfaced introduction-p003 citing 5 breast-cancer DOIs despite
    the prompt saying not to."""
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
            "section_title": "一、引言",
            "prose": "本文研究的是江南刊本的断代依据。",
            "claim_map": [
                {
                    "paragraph_id": "introduction-p001",
                    "claim_text": "晚清江南刊本的断代依据可重新建立。",
                    # mix of on-topic + off-topic source_ids
                    "source_ids": [
                        "crossref:brokaw-on-topic",
                        "crossref:breast-cancer-1",
                        "crossref:breast-cancer-2",
                    ],
                },
            ],
        },
    )

    class _Source:
        def __init__(self, sid: str) -> None:
            self.source_id = sid

    shortlist = [
        _Source("crossref:brokaw-on-topic"),
        _Source("crossref:breast-cancer-1"),
        _Source("crossref:breast-cancer-2"),
    ]
    low_relevance = {"crossref:breast-cancer-1", "crossref:breast-cancer-2"}

    drafted = _drafted_section_from_raw(
        raw,
        section,
        shortlist,
        low_relevance_source_ids=low_relevance,
    )
    assert drafted is not None
    # The on-topic source survives.
    assert drafted.claim_map[0]["source_ids"] == ["crossref:brokaw-on-topic"]
    assert drafted.claim_map[0]["uncited"] is False
    # Warnings record what was dropped — visible for debugging the
    # LLM's directive non-compliance.
    assert any("low-relevance" in w for w in drafted.warnings)


def test_drafted_section_all_low_relevance_substitutes_non_low_fallback() -> None:
    """PR-258c: if every source the LLM cited is low-relevance AND
    the shortlist still has at least one non-low source, substitute
    the first non-low whitelist source instead of collapsing to
    ``[UNCITED]``. Real-paper run #8 hit ``failed_policy`` at
    exports because the integrity gate rejects ``[UNCITED]`` even
    with TODO_EVIDENCE in prose. The substitution keeps the run
    exportable; the warning records the drop + substitution so
    operators can spot it."""
    from autoessay.agents.drafter import (
        RawSectionDraft,
        SectionPlan,
        _drafted_section_from_raw,
    )

    section = SectionPlan(
        section_id="empirical-section-i",
        title="四、案例分析（一）",
        target_words=1000,
    )
    raw = RawSectionDraft.parse_obj(
        {
            "section_id": "empirical-section-i",
            "section_title": "四、案例分析（一）",
            "prose": "晚清江南刊本断代研究。",
            "claim_map": [
                {
                    "paragraph_id": "empirical-section-i-p002",
                    "claim_text": "断代依据须经多源互证。",
                    "source_ids": [
                        "crossref:breast-cancer-1",
                        "crossref:breast-cancer-2",
                    ],
                },
            ],
        },
    )

    class _Source:
        def __init__(self, sid: str) -> None:
            self.source_id = sid

    shortlist = [
        _Source("crossref:brokaw-on-topic"),
        _Source("crossref:breast-cancer-1"),
        _Source("crossref:breast-cancer-2"),
    ]
    drafted = _drafted_section_from_raw(
        raw,
        section,
        shortlist,
        low_relevance_source_ids={"crossref:breast-cancer-1", "crossref:breast-cancer-2"},
    )
    assert drafted is not None
    # Substitution happened — not [UNCITED].
    assert drafted.claim_map[0]["uncited"] is False
    assert drafted.claim_map[0]["source_ids"] == ["crossref:brokaw-on-topic"]
    assert any("substituted" in w for w in drafted.warnings)


def test_drafted_section_all_low_with_no_fallback_keeps_uncited() -> None:
    """Edge case: shortlist contains ONLY low-relevance sources
    (all curator picks were off-topic). The substitution can't
    happen so the claim collapses to ``[UNCITED]`` + TODO_EVIDENCE.
    The run will fail at exports — that's the correct outcome
    because there's truly no on-topic source to back the claim."""
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
            "section_title": "一、引言",
            "prose": "晚清江南刊本断代研究。",
            "claim_map": [
                {
                    "paragraph_id": "introduction-p001",
                    "claim_text": "断代依据须经多源互证。",
                    "source_ids": ["crossref:breast-cancer-1"],
                },
            ],
        },
    )

    class _Source:
        source_id = "crossref:breast-cancer-1"

    drafted = _drafted_section_from_raw(
        raw,
        section,
        [_Source()],
        low_relevance_source_ids={"crossref:breast-cancer-1"},
    )
    assert drafted is not None
    # No fallback available — claim does collapse.
    assert drafted.claim_map[0]["uncited"] is True
    assert drafted.claim_map[0]["source_ids"] == ["[UNCITED]"]
    assert "TODO_EVIDENCE" in drafted.prose


def test_drafted_section_legacy_call_without_low_relevance_kwarg_unchanged() -> None:
    """Back-compat: callers (and tests) that don't pass
    ``low_relevance_source_ids`` get the original behavior — no
    filtering at all."""
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
            "section_title": "一、引言",
            "prose": "晚清江南刊本研究。",
            "claim_map": [
                {
                    "paragraph_id": "introduction-p001",
                    "claim_text": "x.",
                    "source_ids": ["crossref:any-source"],
                },
            ],
        },
    )

    class _Source:
        source_id = "crossref:any-source"

    drafted = _drafted_section_from_raw(raw, section, [_Source()])
    assert drafted is not None
    assert drafted.claim_map[0]["source_ids"] == ["crossref:any-source"]
    assert not any("low-relevance" in w for w in drafted.warnings)
