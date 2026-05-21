"""TEST-only shadow baseline as approved evidence source."""

from __future__ import annotations

import json

from autoessay.agents.critic import CriticIssue, _filter_baseline_as_evidence_test_issues
from autoessay.agents.drafter import (
    SectionPlan,
    _section_prompt,
)
from autoessay.agents.final_rewrite import _baseline_as_evidence_test_rewrite_directive
from autoessay.agents.shadow_baseline import (
    BASELINE_AS_EVIDENCE_SOURCE_ID,
    ArgumentMapEntry,
    SectionPlanEntry,
    ShadowBaselineOutput,
    maybe_inject_baseline_as_evidence_source,
    persist_shadow_baseline,
    split_shadow_baseline_into_source_segments,
)
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.config import get_settings


def _baseline_output() -> ShadowBaselineOutput:
    manuscript = (
        "## 摘要\n\n"
        "本文以布雷顿森林体系为例，说明美元黄金兑换承诺的制度文本与执行约束之间的张力。\n\n"
        "## 一、引言\n\n"
        "第一段讨论官方承诺如何在危机阶段转化为央行协调与市场预期问题。\n\n"
        "第二段说明黄金池、互换安排与外汇管制共同改变了承诺的可信度。\n\n"
        "## 参考文献\n\n"
        "[1] Example. Should not become a source segment.\n"
    )
    return ShadowBaselineOutput(
        manuscript_markdown=manuscript,
        argument_map=[
            ArgumentMapEntry(
                section_id="introduction",
                central_claim="美元黄金兑换承诺的执行约束发生变化。",
                key_evidence=["黄金池", "互换安排"],
            )
        ],
        reference_candidates=[],
        section_plan=[
            SectionPlanEntry(
                section_id="introduction",
                title="一、引言",
                target_words=1000,
                key_argument="说明制度张力。",
            )
        ],
    )


def _shadow_source() -> NormalizedSource:
    return NormalizedSource(
        source_id=BASELINE_AS_EVIDENCE_SOURCE_ID,
        title="Shadow Baseline Evidence Dossier v001",
        authors=["AutoEssay"],
        year=None,
        venue="AutoEssay baseline evidence dossier",
        doi=None,
        url="autoessay-shadow-baseline://v001",
        pdf_url=None,
        abstract="TEST-only shadow baseline manuscript.",
        source_client="shadow_baseline",
        access_status=AccessStatus.OPEN,
        license=None,
        risk_flags=["baseline_as_evidence_test_only"],
        verification_status=VerificationStatus.VERIFIED,
    )


def test_baseline_as_evidence_default_off_does_not_mutate_run_dir(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOESSAY_BASELINE_AS_EVIDENCE_TEST", raising=False)
    get_settings.cache_clear()
    persist_shadow_baseline(tmp_path, _baseline_output())

    assert maybe_inject_baseline_as_evidence_source(tmp_path) is False
    assert not (tmp_path / "sources" / "shortlist.json").exists()
    assert not (
        tmp_path / "synthesis" / "source_notes" / f"{BASELINE_AS_EVIDENCE_SOURCE_ID}.json"
    ).exists()


def test_baseline_as_evidence_on_upserts_shortlist_and_source_note(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_BASELINE_AS_EVIDENCE_TEST", "1")
    get_settings.cache_clear()
    persist_shadow_baseline(tmp_path, _baseline_output())

    assert maybe_inject_baseline_as_evidence_source(tmp_path) is True

    shortlist = json.loads((tmp_path / "sources" / "shortlist.json").read_text("utf-8"))
    injected = [item for item in shortlist if item["source_id"] == BASELINE_AS_EVIDENCE_SOURCE_ID]
    assert len(injected) == 1
    assert injected[0]["title"] == "Shadow Baseline Evidence Dossier v001"
    assert "TEST" not in injected[0]["title"]
    assert injected[0]["verification_status"] == "verified"
    assert injected[0]["url"] == "autoessay-shadow-baseline://v001"

    note_path = tmp_path / "synthesis" / "source_notes" / f"{BASELINE_AS_EVIDENCE_SOURCE_ID}.json"
    note = json.loads(note_path.read_text("utf-8"))
    assert note["title"] == "Shadow Baseline Evidence Dossier v001"
    assert note["baseline_as_evidence_test"] is True
    assert note["segments"]
    assert "参考文献" not in json.dumps(note["segments"], ensure_ascii=False)


def test_shadow_baseline_segments_decode_literal_newline_artifacts() -> None:
    paragraph_a = (
        "第一段讨论布雷顿森林体系中的美元黄金兑换约束如何变化，"
        "并且反复说明制度文本、市场价格和官方兑换通道之间的差异。" * 12
    )
    paragraph_b = (
        "第二段说明伦敦黄金池、互换安排与临时融资如何改变承诺执行方式，"
        "并要求后续论文在引用时使用转述而不是照抄。" * 12
    )
    manuscript = (
        "一、引言\\n"
        f"{paragraph_a}"
        "\\n\\n二、制度运行\\n"
        f"{paragraph_b}"
        "\\n\\n参考文献\\n[1] Example. Bibliography should be excluded."
    )

    segments = split_shadow_baseline_into_source_segments(manuscript)

    assert segments
    assert len(segments) >= 2
    assert all("\\n" not in segment["text"] for segment in segments)
    assert "参考文献" not in json.dumps(segments, ensure_ascii=False)


def test_drafter_test_mode_baseline_source_keeps_material_scope_guard(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_BASELINE_AS_EVIDENCE_TEST", "1")
    get_settings.cache_clear()

    prompt = _section_prompt(
        section=SectionPlan(section_id="introduction", title="一、引言", target_words=1000),
        selected_thesis={
            "thesis_one_sentence": "讨论金本位承诺何时失效。",
            "missing_evidence": "缺少档案备忘录与结算记录。",
        },
        source_notes={
            BASELINE_AS_EVIDENCE_SOURCE_ID: {
                "segments": [
                    {
                        "segment_id": "sb-p001",
                        "text": (
                            "本文区分1965—1966年软失效、1968年3月硬失效与1971年法律终止，"
                            "并以历史制度主义和过程追踪解释制度文本、官僚执行与市场价格的脱钩。"
                        ),
                    }
                ]
            }
        },
        shortlist=[_shadow_source()],
        domain_data={},
        target_journal=None,
        suffix="",
        material_diagnostic={
            "sufficient": False,
            "recommended_action": "iterate",
            "rationale": "non-baseline retrieval is thin",
        },
    )

    assert "baseline_as_evidence_test_directive" in prompt
    assert "material_scope_guard" in prompt
    assert "evidence_strength_directive" in prompt
    assert "1965-1966 soft failure" not in prompt
    assert "March 1968 hard failure" not in prompt


def test_drafter_test_mode_directive_does_not_force_baseline_argument(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_BASELINE_AS_EVIDENCE_TEST", "1")
    get_settings.cache_clear()

    prompt = _section_prompt(
        section=SectionPlan(section_id="introduction", title="一、引言", target_words=1000),
        selected_thesis={"thesis_one_sentence": "讨论金本位承诺何时失效。"},
        source_notes={BASELINE_AS_EVIDENCE_SOURCE_ID: {"segments": []}},
        shortlist=[_shadow_source()],
        domain_data={},
        target_journal=None,
        suffix="",
    )

    assert "baseline_as_evidence_test_directive" in prompt
    assert "historical-institutionalist" not in prompt
    assert "three-stage argument" not in prompt
    assert "March 1968 hard failure" not in prompt


def test_drafter_prompt_renders_cjk_json_without_unicode_escape_bloat(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_BASELINE_AS_EVIDENCE_TEST", "1")
    get_settings.cache_clear()

    prompt = _section_prompt(
        section=SectionPlan(section_id="introduction", title="一、引言", target_words=1000),
        selected_thesis={"thesis_one_sentence": "布雷顿森林体系的黄金承诺需要重新分期。"},
        source_notes={
            BASELINE_AS_EVIDENCE_SOURCE_ID: {
                "thesis": "中文摘要应以原文进入 drafter prompt，而不是展开成 unicode escape。",
                "segments": [
                    {
                        "segment_id": "sb-p001",
                        "text": "布雷顿森林体系、伦敦黄金池与美元黄金兑换承诺之间存在执行张力。",
                    }
                ],
            }
        },
        shortlist=[_shadow_source()],
        domain_data={},
        target_journal=None,
        suffix="",
    )

    assert "布雷顿森林体系" in prompt
    assert "\\u5e03\\u96f7\\u987f" not in prompt

    start = prompt.index("Approved sources: ") + len("Approved sources: ")
    end = prompt.index(". Evidence policy:", start)
    approved_sources = json.loads(prompt[start:end])
    assert approved_sources[0]["source_id"] == BASELINE_AS_EVIDENCE_SOURCE_ID
    assert "中文摘要" in approved_sources[0]["one_line_summary"]


def test_final_rewrite_test_mode_emits_baseline_directive(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_BASELINE_AS_EVIDENCE_TEST", "1")
    get_settings.cache_clear()
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    (sources_dir / "shortlist.json").write_text(
        json.dumps([{"source_id": BASELINE_AS_EVIDENCE_SOURCE_ID}]),
        encoding="utf-8",
    )

    directive = _baseline_as_evidence_test_rewrite_directive(tmp_path)

    assert "baseline_as_evidence_test_rewrite_directive" in directive
    assert "shadow_baseline_v001 is a legal approved TEST source" in directive
    assert "March 1968 hard failure" not in directive
    assert "historical-institutionalist" not in directive


def test_critic_filter_drops_shadow_test_source_legality_objections() -> None:
    issues = [
        CriticIssue(
            issue_id="critic_shadow_test",
            severity="BLOCKER",
            dimension="evidence",
            paragraph_id=None,
            source_ids=["shadow_baseline_v001"],
            description="测试源条目不应作为证据。",
            suggested_action="VERIFY_CITATION",
        ),
        CriticIssue(
            issue_id="critic_shadow_mixed_test",
            severity="BLOCKER",
            dimension="evidence",
            paragraph_id=None,
            source_ids=[
                "shadow_baseline_v001",
                "official:imf:archives-gold-study-hirsch-1966-1968",
            ],
            description="shadow_baseline_v001 与其他来源混用，核验来源状态不合规。",
            suggested_action="VERIFY_CITATION",
        ),
        CriticIssue(
            issue_id="critic_real_source",
            severity="BLOCKER",
            dimension="evidence",
            paragraph_id=None,
            source_ids=["official:imf:archives-gold-study-hirsch-1966-1968"],
            description="仅有书目信息，不能支撑正文断言。",
            suggested_action="VERIFY_CITATION",
        ),
        CriticIssue(
            issue_id="critic_shadow_substantive",
            severity="BLOCKER",
            dimension="evidence",
            paragraph_id=None,
            source_ids=["shadow_baseline_v001"],
            description="这一段的央行协调断言没有被引用段落实际支持。",
            suggested_action="ADD_EVIDENCE",
        ),
        CriticIssue(
            issue_id="critic_shadow_missing_samples",
            severity="BLOCKER",
            dimension="evidence",
            paragraph_id=None,
            source_ids=["shadow_baseline_v001"],
            description="全篇缺少可核验的一手样本链条，尚不足以支撑 case_analysis。",
            suggested_action="ADD_EVIDENCE",
        ),
        CriticIssue(
            issue_id="critic_real_missing_samples",
            severity="BLOCKER",
            dimension="evidence",
            paragraph_id=None,
            source_ids=["official:imf:archives-gold-study-hirsch-1966-1968"],
            description="全篇缺少可核验的一手样本链条，尚不足以支撑 case_analysis。",
            suggested_action="ADD_EVIDENCE",
        ),
    ]

    filtered = _filter_baseline_as_evidence_test_issues(issues)

    assert [issue.issue_id for issue in filtered] == [
        "critic_real_source",
        "critic_shadow_substantive",
        "critic_real_missing_samples",
    ]
