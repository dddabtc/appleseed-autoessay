from __future__ import annotations

from types import SimpleNamespace

from autoessay.agents._evidence_policy import EvidencePolicies
from autoessay.agents.drafter import (
    RawSectionDraft,
    SectionPlan,
    _drafted_section_from_raw,
    _make_citation_whitelist_hook,
    _section_prompt,
)
from autoessay.config import get_settings
from autoessay.harness import AuditVerdict, HookContext


def _reset_policy_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for key in (
        "AUTOESSAY_VERIFY_BY_SOURCE_DRAFTING_SOURCE_BOUND",
        "AUTOESSAY_VERIFY_BY_SOURCE_DRAFTING_ANALYTIC",
        "AUTOESSAY_VERIFY_BY_SOURCE_FINAL",
        "AUTOESSAY_EVIDENCE_WHITELIST_DRAFTING",
        "AUTOESSAY_EVIDENCE_WHITELIST_FINAL",
    ):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()


def test_section_prompt_drafting_injects_soft_directives(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _reset_policy_env(monkeypatch)

    prompt = _section_prompt(
        section=SectionPlan(section_id="conclusion", title="Conclusion", target_words=600),
        selected_thesis={"thesis_one_sentence": "central claim"},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        prior_supported_claims_digest="body claim digest",
        phase_mode="drafting",
    )

    assert "their policy is `soft`" in prompt
    assert "evidence_status=model_backed" in prompt
    assert "优先参考" in prompt
    assert "唯一可援引" not in prompt


def test_section_prompt_final_injects_strict_directives(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _reset_policy_env(monkeypatch)

    prompt = _section_prompt(
        section=SectionPlan(section_id="conclusion", title="Conclusion", target_words=600),
        selected_thesis={"thesis_one_sentence": "central claim"},
        source_notes={},
        shortlist=[],
        domain_data={},
        target_journal=None,
        suffix="",
        prior_supported_claims_digest="body claim digest",
        phase_mode="final",
    )

    assert "their policy is `strict`" in prompt
    assert "唯一可援引" in prompt
    assert "不得首次引入新的年份" in prompt


def test_citation_whitelist_hook_soft_accepts_model_backed_without_sources() -> None:
    result = _soft_hook()(
        _ctx(),
        SimpleNamespace(parsed=_parsed_model_backed(source_ids=[])),
    )

    assert result.verdict is None
    assert result.annotations["checked_claims"] == 1
    assert "warnings" in result.annotations


def test_citation_whitelist_hook_soft_rejects_model_backed_with_sources() -> None:
    result = _soft_hook()(
        _ctx(),
        SimpleNamespace(parsed=_parsed_model_backed(source_ids=["s1"])),
    )

    assert result.verdict == AuditVerdict.REJECTED_SCHEMA_VIOLATION
    assert "model_backed claim must use source_ids=[]" in result.annotations["errors"][0]


def test_citation_whitelist_hook_strict_rejects_model_backed_without_sources() -> None:
    strict = EvidencePolicies(
        phase="final",
        verify_source_bound="strict",
        verify_analytic="strict",
        whitelist="strict",
    )
    hook = _make_citation_whitelist_hook({"s1"}, policies=strict)

    result = hook(_ctx(), SimpleNamespace(parsed=_parsed_model_backed(source_ids=[])))

    assert result.verdict == AuditVerdict.REJECTED_SCHEMA_VIOLATION
    assert "analytic policy is strict" in result.annotations["errors"][0]


def test_uncited_method_claims_are_persisted_as_model_backed() -> None:
    raw = RawSectionDraft.parse_obj(
        {
            "section_id": "sources-method",
            "section_title": "三、研究方法",
            "prose": (
                "本文将失效拆为文本层、操作层、法律层三层。"
                "本文采用过程追踪法。"
                "本文不引入访谈、问卷或自建样本，材料限于可核验文本。"
            ),
            "claim_map": [
                {
                    "paragraph_id": "sources-method-p001",
                    "claim_text": "本文将失效拆为文本层、操作层、法律层三层。",
                    "source_ids": ["[UNCITED]"],
                },
                {
                    "paragraph_id": "sources-method-p002",
                    "claim_text": "本文采用过程追踪法，把年度、季度和会议节点放在同一序列中比较。",
                    "source_ids": ["[UNCITED]"],
                },
                {
                    "paragraph_id": "sources-method-p003",
                    "claim_text": "本文不引入访谈、问卷或自建样本，材料限于可核验文本。",
                    "source_ids": ["[UNCITED]"],
                },
            ],
        },
    )

    drafted = _drafted_section_from_raw(
        raw,
        SectionPlan(section_id="sources-method", title="三、研究方法", target_words=900),
        [_Source("src-1")],
    )

    assert drafted is not None
    assert "TODO_EVIDENCE" not in drafted.prose
    assert [claim["source_ids"] for claim in drafted.claim_map] == [[], [], []]
    assert {claim["evidence_status"] for claim in drafted.claim_map} == {"model_backed"}
    assert {claim["confidence"] for claim in drafted.claim_map} == {"medium"}
    assert all(claim["uncited"] is False for claim in drafted.claim_map)


def test_uncited_sources_method_scope_claims_are_persisted_as_model_backed() -> None:
    raw = RawSectionDraft.parse_obj(
        {
            "section_id": "sources-method",
            "section_title": "三、资料与方法",
            "prose": (
                "本文将研究材料边界限定为可核验的一手文本与内部材料，因此结论只能写成候选判断和待验证路径。"
                "本文采用多源互证的证据链设计，单一年度材料或单次声明不足以定点。"
                "若后文只能证明1971年的正式废止，则1968年春已失效只能保留为候选结论。"
            ),
            "claim_map": [
                {
                    "paragraph_id": "sources-method-p001",
                    "claim_text": (
                        "本文将研究材料边界限定为可核验的一手文本与内部材料，"
                        "因此结论只能写成候选判断和待验证路径。"
                    ),
                    "source_ids": ["[UNCITED]"],
                },
                {
                    "paragraph_id": "sources-method-p002",
                    "claim_text": (
                        "本文采用多源互证的证据链设计，单一年度材料或单次声明不足以定点。"
                    ),
                    "source_ids": ["[UNCITED]"],
                },
                {
                    "paragraph_id": "sources-method-p003",
                    "claim_text": (
                        "若后文只能证明1971年的正式废止，则1968年春已失效只能保留为候选结论。"
                    ),
                    "source_ids": ["[UNCITED]"],
                },
            ],
        },
    )

    drafted = _drafted_section_from_raw(
        raw,
        SectionPlan(section_id="sources-method", title="三、资料与方法", target_words=900),
        [_Source("src-1")],
    )

    assert drafted is not None
    assert "TODO_EVIDENCE" not in drafted.prose
    assert [claim["source_ids"] for claim in drafted.claim_map] == [[], [], []]
    assert {claim["evidence_status"] for claim in drafted.claim_map} == {"model_backed"}
    assert all(claim["uncited"] is False for claim in drafted.claim_map)


def test_uncited_sources_method_factual_claim_still_fails_as_uncited() -> None:
    raw = RawSectionDraft.parse_obj(
        {
            "section_id": "sources-method",
            "section_title": "三、资料与方法",
            "prose": "1968年3月20日的美联储会议纪要已经证明黄金政策实质性失效。",
            "claim_map": [
                {
                    "paragraph_id": "sources-method-p001",
                    "claim_text": "1968年3月20日的美联储会议纪要已经证明黄金政策实质性失效。",
                    "source_ids": ["[UNCITED]"],
                },
            ],
        },
    )

    drafted = _drafted_section_from_raw(
        raw,
        SectionPlan(section_id="sources-method", title="三、资料与方法", target_words=900),
        [_Source("src-1")],
    )

    assert drafted is not None
    assert drafted.claim_map[0]["source_ids"] == ["[UNCITED]"]
    assert drafted.claim_map[0]["evidence_status"] == "source_bound"
    assert drafted.claim_map[0]["uncited"] is True
    assert "TODO_EVIDENCE" in drafted.prose


def test_uncited_factual_claim_still_fails_as_uncited() -> None:
    raw = RawSectionDraft.parse_obj(
        {
            "section_id": "empirical-section-i",
            "section_title": "四、案例分析（一）",
            "prose": "1961 年黄金池启动后立即改变了美元兑金承诺。",
            "claim_map": [
                {
                    "paragraph_id": "empirical-section-i-p001",
                    "claim_text": "1961 年黄金池启动后立即改变了美元兑金承诺。",
                    "source_ids": ["[UNCITED]"],
                },
            ],
        },
    )

    drafted = _drafted_section_from_raw(
        raw,
        SectionPlan(section_id="empirical-section-i", title="四、案例分析（一）", target_words=900),
        [_Source("src-1")],
    )

    assert drafted is not None
    assert drafted.claim_map[0]["source_ids"] == ["[UNCITED]"]
    assert drafted.claim_map[0]["evidence_status"] == "source_bound"
    assert drafted.claim_map[0]["uncited"] is True
    assert "TODO_EVIDENCE" in drafted.prose


def test_uncited_literature_positioning_claim_is_model_backed() -> None:
    raw = RawSectionDraft.parse_obj(
        {
            "section_id": "introduction",
            "section_title": "一、引言",
            "prose": (
                "关于1971年终止与随后制度重组的讨论，更强调法理封口与新秩序叙事，"
                "因此适合作为背景，而不宜直接替代对1968年操作性失效的判断。"
            ),
            "claim_map": [
                {
                    "paragraph_id": "introduction-p001",
                    "claim_text": (
                        "关于1971年终止与随后制度重组的讨论，更强调法理封口与新秩序叙事，"
                        "因此适合作为背景，而不宜直接替代对1968年操作性失效的判断。"
                    ),
                    "source_ids": ["[UNCITED]"],
                },
            ],
        },
    )

    drafted = _drafted_section_from_raw(
        raw,
        SectionPlan(section_id="introduction", title="一、引言", target_words=900),
        [_Source("src-1")],
    )

    assert drafted is not None
    assert "TODO_EVIDENCE" not in drafted.prose
    assert drafted.claim_map[0]["source_ids"] == []
    assert drafted.claim_map[0]["evidence_status"] == "model_backed"
    assert drafted.claim_map[0]["uncited"] is False


def _soft_hook():
    policies = EvidencePolicies(
        phase="drafting",
        verify_source_bound="strict",
        verify_analytic="soft",
        whitelist="soft",
    )
    return _make_citation_whitelist_hook({"s1"}, policies=policies)


class _Source:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id


def _parsed_model_backed(source_ids: list[str]) -> dict[str, object]:
    return {
        "section_id": "discussion",
        "section_title": "Discussion",
        "prose": "Analytic synthesis.",
        "claim_map": [
            {
                "paragraph_id": "discussion-p001",
                "claim_text": "Analytic synthesis.",
                "source_ids": source_ids,
                "evidence_status": "model_backed",
                "confidence": "medium",
            },
        ],
    }


def _ctx() -> HookContext:
    return HookContext(
        run_id="run-policy",
        phase="drafter",
        step_id="drafter.section",
        user_id="user",
        attempt=1,
        prompt_template_id="drafter.section.test",
        prompt_filled="prompt",
        prompt_hash="hash",
        project_title="Project",
    )
