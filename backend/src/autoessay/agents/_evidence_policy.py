"""Per-phase evidence and conclusion-whitelist policy helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from autoessay.config import Settings

PhaseMode = Literal["drafting", "final"]
EvidencePolicy = Literal["strict", "soft", "off"]

_STRICT_CONCLUSION_WHITELIST_DIRECTIVE = (
    "结论段只能综合正文已经展示、且由 claim_map / cited sources 支撑"
    "的判断。**不得首次引入新的年份、年代、阶段划分、起点 / 终点、"
    "程度判断、因果判断或文献实体**。凡含具体时点、程度词、因果词的"
    "句子，必须能对应到正文已展示的证据；否则**降级**为更宽、更弱、"
    "更条件化的表述（例如「1960 年代后期出现明确松动」→「1960 年代"
    "出现可观察的松动迹象」），但**降级后的表述仍必须能对应到正文中"
    "实际展示的证据 / digest 中的某条判断**——否则直接**删除**该"
    "断言，不要把无证据的强判断改成无证据的弱判断。\n"
    "输出前自行检查并修正，不要在正文中解释这些规则。下面的『正文已"
    "展示证据 (Supported claims digest)』块若提供，是结论段唯一可"
    "援引的范围；超出此范围的判断必须删除或降级。"
)

_CONCLUSION_WHITELIST_DIRECTIVES: dict[EvidencePolicy, str] = {
    "strict": _STRICT_CONCLUSION_WHITELIST_DIRECTIVE,
    "soft": (
        "结论段应优先综合正文已展示证据；若提出更广泛的判断 / 推理时，"
        "请在 claim_map 标 `evidence_status=model_backed` + "
        "`confidence=low/medium/high`，**不要在 prose 中加任何 "
        "(model_backed) 标记**——claim_map 是元数据通道。下游 critic "
        "会评估推理强度。事实 / 年份 / 数字 / 引文必须仍源自正文。"
    ),
    "off": "",
}

_STRICT_COHERENCE_RULE_9 = (
    "9. **最后一个 `##` 一级标题以下的全部内容（即结论段）受额外"
    "保护**：现有判断的**强度**绝对不能加强；**且**不得新增更"
    "具体的时点 / 程度判断、因果判断或阶段划分。禁止情况包括但"
    "不限于：把「出现迹象」改成「明确出现」、「可能」改成「确实」、"
    "把已限定的时间区间收窄为具体年份 / 阶段、把因果暗示（「与…相关」"
    "/「伴随…」）改成因果断言（「导致」/「使得」）、把无阶段划分的"
    "叙述改成「第一阶段 / 第二阶段」式划分。同义改写、删冗余、补转折"
    "可以；提升判断强度 / 新增更具体的时点 / 程度判断 / 因果判断 / "
    "阶段划分一律不可。"
)


@dataclass(frozen=True)
class EvidencePolicies:
    """Snapshot of per-phase evidence/whitelist policies.

    Build once via ``from_settings(phase)`` at the start of drafter /
    final_rewrite, pass through helpers and prompt builders.
    """

    phase: PhaseMode
    verify_source_bound: EvidencePolicy
    verify_analytic: EvidencePolicy
    whitelist: EvidencePolicy

    @classmethod
    def from_settings(cls, phase: PhaseMode, settings: Settings) -> EvidencePolicies:
        if phase == "drafting":
            return cls(
                phase=phase,
                verify_source_bound=settings.verify_by_source_drafting_source_bound,
                verify_analytic=settings.verify_by_source_drafting_analytic,
                whitelist=settings.evidence_whitelist_drafting,
            )
        return cls(
            phase=phase,
            verify_source_bound=settings.verify_by_source_final,
            verify_analytic=settings.verify_by_source_final,
            whitelist=settings.evidence_whitelist_final,
        )

    @property
    def whitelist_directive(self) -> str:
        """Conclusion section type directive based on whitelist policy."""

        return _CONCLUSION_WHITELIST_DIRECTIVES[self.whitelist]

    @property
    def coherence_rule_9(self) -> str:
        """GLOBAL_COHERENCE_SYSTEM_PROMPT rule 9 wording by whitelist policy."""

        if self.whitelist == "strict":
            return _STRICT_COHERENCE_RULE_9
        if self.whitelist == "soft":
            return (
                "9. 结论段为软保护区：尽量不要加强判断强度，也不要新增更具体"
                "的时点 / 程度判断、因果判断或阶段划分。若为了连贯性必须"
                "做更广泛的概括，只能保持事实 / 年份 / 数字 / 文献实体来自"
                "原稿；下游会产生 evidence whitelist warning event，但本轮"
                "不因分析性概括直接失败。"
            )
        return ""

    def supported_claims_block(self, prior_supported_claims_digest: str) -> str:
        """Runtime injected text for conclusion."""

        digest = prior_supported_claims_digest.strip()
        if not digest or self.whitelist == "off":
            return ""
        if self.whitelist == "strict":
            return (
                "正文已展示证据 (Supported claims digest, 本块为本节唯一"
                "可援引的判断范围): "
                f"{digest} "
                "结论段中含具体时点 / 程度词 / 因果词的句子必须能对应到"
                "上面 digest 列出的某条判断；否则**降级**到与某条 digest 判断"
                "强度一致的更宽 / 更弱 / 更条件化表述（降级后仍必须能对应到"
                "digest），或者**删除**该断言。不得出现既不对应 digest "
                "也未在正文中展示证据的强判断。"
            )
        return (
            "正文已展示证据 (Supported claims digest, 结论应优先参考): "
            f"{digest} "
            "结论可以在正文证据基础上做分析性综合；若超出 digest 做更广泛"
            "推理，claim_map 必须标 `evidence_status=model_backed` + "
            "`confidence=low/medium/high`，且 prose 不得出现 (model_backed) "
            "标记。事实 / 年份 / 数字 / 文献实体仍必须来自正文证据。"
        )

    def section_directive_prefix(self) -> str:
        """Directive declaring source-bound vs analytic claim policy."""

        return (
            "Evidence policy: source_bound claims are factual claims about "
            "facts / years / numbers / source contents / named entities; "
            f"their policy is `{self.verify_source_bound}`. Analytic claims are "
            "reasoning / comparison / conceptual synthesis; "
            f"their policy is `{self.verify_analytic}`. "
            "If a source_bound claim is strict, cite at least one source_id from "
            "Approved sources. If an analytic claim is soft, it may be "
            "model-backed only in claim_map metadata: set "
            "`evidence_status=model_backed`, `confidence=low/medium/high`, and "
            "`source_ids=[]`; never write `(model_backed)` in prose. If analytic "
            "claims are strict, they must be source_bound with shortlist citations. "
            "All non-empty source_ids must appear in Approved sources."
        )

    def event_payload(self) -> dict[str, str]:
        return {
            "phase_mode": self.phase,
            "verify_source_bound": self.verify_source_bound,
            "verify_analytic": self.verify_analytic,
            "whitelist": self.whitelist,
        }
