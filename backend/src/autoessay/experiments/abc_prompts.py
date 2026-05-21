"""Prompt builders for ABC experiment generation arms."""
# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from autoessay.experiments.abc_extract import KernelMetadata


@dataclass(frozen=True)
class PromptBundle:
    """OpenAI-compatible chat prompt plus deterministic hash."""

    system: str
    user: str

    @property
    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]

    @property
    def sha256(self) -> str:
        return sha256(self.as_text().encode("utf-8")).hexdigest()

    def as_text(self) -> str:
        return f"system:\n{self.system}\n\nuser:\n{self.user}\n"


def build_b_prompt(
    *,
    kernel: KernelMetadata,
    package_md: str,
    humanizer_directive: str,
) -> PromptBundle:
    """Build the single-shot evidence-first manuscript prompt for arm B."""
    return PromptBundle(
        system=_system_prompt(),
        user="\n\n".join(
            [
                "任务：根据给定证据包，一次性写出完整中文学术论文。",
                _kernel_block(kernel),
                "硬性约束：",
                "- 只使用证据包中给出的 source_id / 文献条目作为可引用来源。",
                "- 不进行额外检索，不补充外部资料，不引用证据包之外的文献。",
                "- 输出必须是完整 Markdown：题名、摘要、关键词、正文、参考文献。",
                "- 正文中所有实质性经验或文献判断都要用方括号数字引文标记，如 [1]。",
                "- 参考文献表必须只列出正文实际引用过的来源。",
                "- 直接输出论文正文，不要解释写作过程。",
                "人类化写作指令：",
                humanizer_directive,
                "证据包：",
                "<EVIDENCE_PACKAGE>",
                package_md,
                "</EVIDENCE_PACKAGE>",
            ]
        ),
    )


def build_b_prime_prompt(
    *,
    package_md: str,
    base_b_manuscript: str,
    humanizer_directive: str,
) -> PromptBundle:
    """Build the one-pass self-critique targeted-revision prompt for arm B'."""
    return PromptBundle(
        system=_system_prompt(),
        user="\n\n".join(
            [
                "任务：对下列 B 稿执行一次 self-critique-and-targeted-revision。",
                "内部按两步完成：",
                (
                    "1. 先识别至少 3 处可改进的具体问题，类型可包括论证断裂、"
                    "引用错配、表述生硬、段落衔接弱或证据使用不充分。"
                ),
                "2. 再基于这些问题输出修订后的完整 Markdown 正文。",
                "硬性约束：",
                "- 必须实际执行修订动作；直接 echo B 稿原文不被接受。",
                "- 这是 targeted local fixes，不是重新写一篇新论文。",
                (
                    "- 保留原稿主论点、章节和引用编号体系；只修复论证断裂、"
                    "引用错配、段落衔接和局部表述问题。"
                ),
                (
                    "- 如果确实找不到 3 处实质问题，可以仅做 1-2 处润色，"
                    "如句序、词序或连接词调整，但不能 100% echo 原文。"
                ),
                "- 任何新增或改写的实质性判断都必须能由证据包支持。",
                "- 不进行额外检索，不引入证据包以外的文献。",
                (
                    "- 输出修订后的完整 Markdown 论文正文，不要输出 self-critique "
                    "内容、审稿意见或修改说明。"
                ),
                "人类化写作指令：",
                humanizer_directive,
                "证据包：",
                "<EVIDENCE_PACKAGE>",
                package_md,
                "</EVIDENCE_PACKAGE>",
                "B 稿：",
                "<BASE_B_MANUSCRIPT>",
                base_b_manuscript,
                "</BASE_B_MANUSCRIPT>",
            ]
        ),
    )


def build_b_prime_anti_echo_retry_prompt(
    *,
    package_md: str,
    base_b_manuscript: str,
    humanizer_directive: str,
) -> PromptBundle:
    """Build the B' retry prompt used only after a byte-identical echo."""
    return PromptBundle(
        system=_system_prompt(),
        user="\n\n".join(
            [
                "任务：重新执行 B' self-critique-and-targeted-revision。",
                (
                    "上一轮输出与 B 稿逐字节相同，已经被实验驱动拒绝。"
                    "本轮必须输出可见修订后的完整 Markdown 正文。"
                ),
                "硬性约束：",
                "- 不得 100% echo B 稿原文。",
                "- 至少执行 5 处局部修订，优先选择论证衔接、段落转承、句序和措辞。",
                "- 保留原稿主论点、章节结构、引用编号体系和参考文献集合。",
                "- 不重新写一篇新论文，不引入证据包以外的文献。",
                "- 任何新增或改写的实质性判断都必须能由证据包支持。",
                "- 如果原稿已经较好，也要做不改变论旨的局部行文修订，确保字节不同。",
                (
                    "- 输出修订后的完整 Markdown 论文正文，不要输出 self-critique "
                    "内容、审稿意见、修改清单或解释。"
                ),
                "人类化写作指令：",
                humanizer_directive,
                "证据包：",
                "<EVIDENCE_PACKAGE>",
                package_md,
                "</EVIDENCE_PACKAGE>",
                "B 稿：",
                "<BASE_B_MANUSCRIPT>",
                base_b_manuscript,
                "</BASE_B_MANUSCRIPT>",
            ]
        ),
    )


def build_c_prompt(
    *,
    kernel: KernelMetadata,
    humanizer_directive: str,
) -> PromptBundle:
    """Build the no-retrieval naked single-shot prompt for arm C."""
    return PromptBundle(
        system=_system_prompt(),
        user="\n\n".join(
            [
                "任务：仅根据以下题目、研究核和目标期刊风格，一次性写出完整中文学术论文。",
                _kernel_block(kernel),
                "硬性约束：",
                "- 不进行检索。",
                "- 不使用任何外部来源包、source pool、合成 claims 或其他实验产物。",
                "- 可以基于模型已有知识写参考文献；引用可靠性将作为实验测量项。",
                "- 输出必须是完整 Markdown：题名、摘要、关键词、正文、参考文献。",
                "- 直接输出论文正文，不要解释写作过程。",
                "人类化写作指令：",
                humanizer_directive,
            ]
        ),
    )


def build_e_ars_prompt(
    *,
    kernel: KernelMetadata,
    ars_full_mode_prompt: str,
    humanizer_directive: str,
) -> PromptBundle:
    """Build the ARS academic-paper full-mode single-call simulation prompt."""
    return PromptBundle(
        system=(
            "你是 ARS academic-research-skills 的 academic-paper skill 写作者，"
            "以 full mode 的写作标准生成中文学术论文。"
            "本实验只模拟 academic-paper skill 的单次写作调用；"
            "不要声称已经执行外部检索、10-stage academic-pipeline、评审面板或多轮 agent 编排。"
            "不要输出隐藏推理、流程说明、自评分、pre-commitment 或 Writer Decision。"
        ),
        user="\n\n".join(
            [
                "任务：根据给定题目和研究核，用 ARS academic-paper full mode 的写作标准，一次性写出完整中文学术论文。",
                _kernel_block(kernel),
                "实验边界：",
                "- 这是 E arm：ARS academic-paper full mode 的 single-call 写作模拟。",
                "- 不读取 A/B/B_prime/C 稿件，不读取 ABC front-half evidence package。",
                "- 不执行 10-stage academic-pipeline，不执行 live web search，不调用额外 agents。",
                "- 可以使用模型已有学术知识组织参考文献；引用真实性和可追溯性会由 blind judge 评分。",
                "- 若无法确信 DOI，不要编造 DOI。",
                "最终输出硬性要求：",
                "- 只输出最终 Markdown 论文正文。",
                "- 必须包含题名、中文摘要、关键词、正文、结论、参考文献。",
                "- 必须包含局限性、Data Availability Statement、Ethics Declaration、Author Contributions (CRediT)、Conflict of Interest Statement、Funding Acknowledgment、AI disclosure statement。",
                "- 不输出“## Dimension Scores”“## Failure Condition Checks”“## Writer Decision”或任何实验/arm/provenance 标记。",
                "- 正文保持中文学术论文体例；英文术语可保留英文。",
                "人类化写作指令：",
                humanizer_directive,
                "ARS academic-paper full-mode instruction excerpt:",
                "<ARS_FULL_MODE_INSTRUCTIONS>",
                ars_full_mode_prompt,
                "</ARS_FULL_MODE_INSTRUCTIONS>",
            ]
        ),
    )


def build_g_ars_front_half_prompt(
    *,
    kernel: KernelMetadata,
    package_md: str,
    ars_full_mode_prompt: str,
    humanizer_directive: str,
) -> PromptBundle:
    """Build the cut-after-front-half ARS single-call prompt for arm G."""
    return PromptBundle(
        system=(
            "你是 ARS academic-research-skills 的 academic-paper skill 写作者，"
            "但本实验只允许你使用给定的 appleseed front-half evidence package "
            "作为可引用来源。"
            "你必须一次性生成最终中文学术论文；不要声称执行了 appleseed 后段、"
            "10-stage academic-pipeline、外部检索、评审面板或多轮 agent 编排。"
            "不要输出隐藏推理、流程说明、自评分、pre-commitment 或 Writer Decision。"
        ),
        user="\n\n".join(
            [
                "任务：采用 ARS academic-paper full mode 的写作标准，并只基于给定 front-half source pool，一次性写出完整中文学术论文。",
                _kernel_block(kernel),
                "实验边界：",
                "- 这是 G arm：appleseed 只保留 front-half evidence package；ideator/drafter/stylist/final_rewrite/critic/integrity/export 全部砍掉。",
                "- 不读取 A/B/B_prime/C/E/F 稿件。",
                "- 不执行 live web search，不补充 evidence package 之外的可引用来源。",
                "- front-half package 中的 scout/sources/synthesis 只能作为来源池和论证素材；不得把其中的 workflow 元数据写入正文。",
                "- 可以用 ARS 的结构、问题意识、claim discipline、引用规范来组织论文，但引用必须回到 evidence package 中可见来源。",
                "- 若 evidence package 不足以支持某个实质判断，删除、降格为假说，或移入局限性；不要用模型记忆补 source。",
                "- 若无法确信 DOI，不要编造 DOI。",
                "最终输出硬性要求：",
                "- 只输出最终 Markdown 论文正文。",
                "- 必须包含题名、中文摘要、关键词、正文、结论、参考文献。",
                "- 必须包含局限性、Data Availability Statement、Ethics Declaration、Author Contributions (CRediT)、Conflict of Interest Statement、Funding Acknowledgment、AI disclosure statement。",
                "- 正文所有实质性文献判断都使用方括号数字引文，如 [1]；参考文献只列正文实际引用且来自 evidence package 的来源。",
                "- 不输出“## Dimension Scores”“## Failure Condition Checks”“## Writer Decision”或任何实验/arm/provenance 标记。",
                "- 正文保持中文学术论文体例；英文术语可保留英文。",
                "内部写作检查（不要输出）：",
                "- 做一次 claim-to-source self-audit：每个核心 claim 是否能追溯到 evidence package 的 source/claim/synthesis。",
                "- 删除没有来源支撑的强经验断言。",
                "- 检查参考文献编号和正文引用是否一致。",
                "人类化写作指令：",
                humanizer_directive,
                "ARS academic-paper full-mode instruction excerpt:",
                "<ARS_FULL_MODE_INSTRUCTIONS>",
                ars_full_mode_prompt,
                "</ARS_FULL_MODE_INSTRUCTIONS>",
                "appleseed front-half evidence package:",
                "<EVIDENCE_PACKAGE>",
                package_md,
                "</EVIDENCE_PACKAGE>",
            ]
        ),
    )


def _system_prompt() -> str:
    return (
        "你是中文学术论文写作者。遵守实验边界，只根据用户消息中允许的输入写作。"
        "不要输出隐藏推理、过程说明或自我评价。"
    )


def _kernel_block(kernel: KernelMetadata) -> str:
    return "\n".join(
        [
            "论文输入：",
            f"- 题目：{kernel.title}",
            f"- 目标期刊：{kernel.target_journal or '未指定'}",
            "- 研究核：",
            _json_block(kernel.research_kernel),
        ]
    )


def _json_block(payload: Mapping[str, object]) -> str:
    return "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"
