"""Prompt registry for per-phase prompt overrides.

Each phase has zero or more "prompt surfaces" the user can override.
A surface is identified by ``(phase, prompt_key)``. Single-call
agents (synthesizer / ideator / critic) expose only ``main``;
multi-call agents expose additional keys: curator's ``ranking``
(Stage 3.A.1) and drafter's per-section keys (Stage 3.A.2 —
``introduction`` / ``historiography`` / ``sources-method`` /
``empirical-section-i`` / ``empirical-section-ii`` /
``empirical-section-iii`` / ``discussion`` / ``conclusion``) on top
of ``main``.

The "default content" returned here is the STATIC INSTRUCTION /
TEMPLATE block — the part of the agent's full prompt that does NOT
change per-run. Dynamic context (sources, schema specs, claim lists,
etc.) is appended by the agent at LLM-call time and is NOT user-
editable. This split is what makes the override surface safe — the
user can rewrite the instructions without breaking schema parsing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: Default static instructions for the curator's ranking LLM call
#: (Stage 3.A.1). Drives BOTH the harness path
#: (:func:`autoessay.agents.curator._score_relevance_batches_via_harness`)
#: and the async fallback path
#: (:func:`autoessay.agents.curator._score_batch`). The override
#: replaces the instruction concept in the LLM system message; each
#: path's schema-binding sentence ("Return one strict JSON array" /
#: "Return JSON only") and ``language_directive`` stay outside the
#: editable surface because they are runtime correctness contracts.
CURATOR_RANKING_INSTRUCTIONS = (
    "Rank records for relevance, access legality, venue fit, and debate value. "
    "Score on a 0..1 scale. Do not request paywalled access."
)

#: Default static instructions for the synthesizer's source-summary
#: LLM call. Pulled from
#: :func:`autoessay.agents.synthesizer._summary_prompt`. The agent
#: appends the dynamic source/domain/proposal/schema context after.
SYNTHESIZER_MAIN_INSTRUCTIONS = (
    "You are Synthesizer. "
    "Extract question, thesis, evidence, method, and limits per source. "
    "When proposal context is present, prefer claims that clarify alignment or "
    "tension with the starting research question. "
    "Attach source IDs to every claim. "
    "Return one JSON object that matches the required schema exactly."
)

#: Default static instructions for the ideator's angle-cards LLM call.
#: Pulled from :func:`autoessay.agents.ideator._angle_prompt`. The
#: dynamic context (project title, claims, source notes, proposal,
#: discussion history) is always appended by the agent.
IDEATOR_MAIN_INSTRUCTIONS = (
    "For each angle give thesis, contribution, evidence base, missing "
    "evidence, journal fit, and risks. Refine the user's proposal when "
    "proposal context is present; do not propose unrelated angles. Use "
    "only claim_ids from the supplied claims."
)

#: Default static instructions for the critic's review LLM call. Pulled
#: from :func:`autoessay.agents.critic._critic_prompt`. The agent
#: always prepends "You are Critic." and appends the dynamic
#: draft/claim_map/evidence/schema context.
CRITIC_MAIN_INSTRUCTIONS = (
    "Find unsupported claims, weak transitions, missing counterarguments, "
    "and novelty overclaim. "
    "Separate thesis, structure, evidence, and prose issues. "
    "Recommend one next revision dimension through issue severity and "
    "dimension priority. "
    "Do not rewrite the paper."
)

#: Default static instructions for the drafter's per-section LLM call.
#: Combines what were previously three separate blocks in
#: :func:`autoessay.agents.drafter._section_prompt` (universal
#: argumentation rules + forbidden patterns + citation enforcement)
#: into one editable surface. Stage 3.A.2 also exposes per-section
#: overrides at ``(drafter, <section_id>)``; ``main`` here remains
#: the cross-section universal-rules surface, applied to every
#: section call within a single agent run.
DRAFTER_MAIN_INSTRUCTIONS = (
    # Argumentation requirements — paper-quality-spec §四.
    "全文必须围绕一个清楚的中心问题展开，不要写成材料汇编。本节须服务于该"
    "中心问题。每段开头要有中心句，段中给出理由或证据，段尾要有小结。每个"
    "重要学术判断都必须有文献依据。逻辑优先，不要堆概念。"
    # Forbidden patterns — writing-quality §六.
    "绝对规则：不要使用'众所周知'、'显而易见'、'意义重大'、'毋庸置疑'、"
    "'不言而喻'、'综上所述'式空话；不要把政策表述直接当作学术分析；"
    "不要把材料罗列当作论证；不要把文献综述写成读书笔记；不要把结论"
    "写成口号；不要为显得学术而堆大词。证据不足时降低结论强度，材料"
    "不足时明确说明限制——而不是强行下结论。"
    # Citation enforcement — the runtime evidence policy decides
    # which claim classes are strict vs soft; this static surface
    # keeps the source-id and placeholder invariants.
    "引用强制规则（按本次 evidence policy 执行）：区分 source_bound 与 analytic "
    "claim。凡 policy 要求 source-bound 的主张，claim_map 必须 cite 至少一个"
    "真实的 source_id（来自 Approved sources 列表）。若某条主张无法满足其 "
    "policy，直接省略该主张——不要写它，不要写 [UNCITED]、TODO_EVIDENCE "
    "或'该处需要补充文献'之类的占位字符。宁可缩短本节，也不要用未支持的"
    "主张充数。所有非空 source_ids 数组中的值必须出现在 Approved sources "
    "列表里；model-backed analytic claim 必须使用空 source_ids。"
    # Grounding enforcement — PR-G-Drafter-Grounding (codex P0 #5
    # F phase 2): real-paper rounds 4 + 6 + 7 all FAILED_POLICY at
    # exports because critic caught "method 声称用 IMF 内部备忘录 /
    # 美联储理事会会议纪要 / 伦敦黄金池季度结算记录 / 序跋与刻工题记"
    # that no cited source actually contained. Phase 1 added a
    # warning gate; this phase 2 prompt-level constraint asks the
    # drafter to NOT invent specific named archives in the first
    # place.
    "档案具体性强制规则：写 method、研究方法或材料/证据章节时，"
    "**不许出现具体命名档案、文献集、原始材料类型的名字**，"
    "除非该名字在 Approved sources 的 title / abstract / venue / "
    "source_note 中已经出现。例如：'IMF 内部备忘录'、'美联储理事会"
    "会议纪要'、'伦敦黄金池季度结算记录'、'序跋与刻工题记'、"
    "'国家档案局某档案集'这类具体名字若不在 Approved sources 里，"
    "**绝对不要写入正文**。可改为通用表述（'档案研究'、'一手材料'、"
    "'文献证据'）或直接省略相关方法说明。承诺自己没有的档案是 "
    "BLOCKER 级问题，会导致 export 阶段被拒。"
    # PR-260 — per-section length floor. Real-paper run #11 produced
    # a structurally complete CNKI manuscript at ~10K chars vs the
    # gpt-5.5 baseline's ~25K. Each body section averages ~1100 chars
    # (~600 zh chars) which is too thin for substantive academic
    # argument. The floor pushes the LLM to develop each section
    # with ≥3 paragraphs of structured argument; the upstream
    # ``max_tokens=4500`` (PR-257b) leaves headroom for it to land.
    "篇幅下限规则：本节正文应至少 1200 中文字符 / 800 英文 words，"
    "由不少于 3 个完整段落组成。每段须有中心句、理由 / 证据、小结，"
    "不要为凑字数堆叠资料。如果 Approved sources 不足以撑起这个篇"
    "幅，宁可保留较短的段落数也不要硬扩，但单段必须仍然完整。"
)

#: Per-section role hints used by the drafter for one LLM call per
#: section. Stable section_id keys (Roman alphabet) match the slugs
#: in `paper-quality-spec.md §五`. Each entry describes WHAT this
#: section must do. Defined here so the prompt registry can assemble
#: per-section default content from these hints + the type
#: directives below; the agent module re-exports them for tests.
DRAFTER_SECTION_ROLES: dict[str, str] = {
    "introduction": (
        "引言。从现实问题切入，说明为什么值得研究，简述既有解释路径与不足，"
        "提出本文研究问题，交代分析框架与可能贡献，简要交代全文结构。"
        "禁止空泛口号。"
    ),
    "historiography": (
        "文献综述。必须分类展开（按解释路径或学派分组），不能按作者罗列。"
        "每一类研究都要有评价（解释了什么，贡献是什么）；最后指出既有研究"
        "总体不足与本文的切入点。不要写成'某某认为...另一某某认为...'式读"
        "书笔记。"
    ),
    "sources-method": (
        "概念界定与研究设计。明确核心概念及其关系；说明本文采用的理论视角"
        "与分析框架；若可能，构建机制链条（结构条件 → 行动者策略 → 制度过"
        "程 → 结果表现）。然后说明本文是何种类型的论文（理论 / 案例 / 文本"
        "分析），以及材料来源、分析维度。如果没有一手数据，明说'无一手数"
        "据'，不要虚构访谈、问卷、模型结果。"
    ),
    "empirical-section-i": ("第一节正文：解释背景或结构条件。围绕中心论点推进，避免资料堆积。"),
    "empirical-section-ii": ("第二节正文：分析核心机制。围绕中心论点推进，避免资料堆积。"),
    "empirical-section-iii": ("第三节正文：分析结果或影响（必要时讨论张力／反向机制）。"),
    "discussion": (
        "讨论。本文发现与既有研究有什么不同；本文修正、补充或扩展了什么；"
        "现实含义；边界条件。不要重复结论。"
    ),
    "conclusion": (
        "结论。回答研究问题；概括核心发现；说明理论贡献与现实启示；说明研"
        "究局限；提出后续研究方向。**不要简单重复摘要**，也不要写成口号。"
    ),
}

#: Per-section additional directives appended after the role line in
#: the rendered prompt. Empty entries fall back to the no-directive
#: branch in the agent's renderer. Stage 3.A.2 folds these into the
#: per-section default content registered for ``(drafter, <id>)``.
DRAFTER_SECTION_TYPE_DIRECTIVES: dict[str, str] = {
    "historiography": (
        "本节务必采用'分类—评价—不足—切入点'结构。先按解释路径或理论视"
        "角对既有文献分组（每组至少 2 条文献），每组后写 1-2 句评价，再"
        "写既有研究的整体不足，最后明确本文的切入点。"
    ),
    "conclusion": (
        "本节绝不允许照搬摘要的句子。结论应当对研究问题给出明确、有保留"
        "的回答，并明确写出本文的局限，不要写成宣传语。"
    ),
}


def _drafter_section_default(section_id: str) -> str:
    """Default content for ``(drafter, <section_id>)`` overrides.

    Concatenates the section role hint and the optional type
    directive; both pieces end in 句号 and read naturally without a
    separator. Six of eight sections have an empty type directive,
    so this returns the role hint alone in those cases.
    """
    return DRAFTER_SECTION_ROLES[section_id] + DRAFTER_SECTION_TYPE_DIRECTIVES.get(section_id, "")


#: Default static instructions for the stylist's per-section LLM call.
#: Pulled from :func:`autoessay.agents.stylist._section_prompt`. The
#: separate full-manuscript second pass has its own surface
#: (:data:`STYLIST_REPOLISH_INSTRUCTIONS`, registered as
#: ``(stylist, "repolish")`` in Stage 3.A.3); ``main`` here covers
#: only per-section calls.
STYLIST_MAIN_INSTRUCTIONS = (
    "Revise prose only. Preserve claims, citations, order, and evidence. "
    "Do not copy prior-paper sentences. Output a diff summary. "
    "STRICT RULE: if the draft section is empty or too short to revise, "
    "return revised_prose equal to the input prose verbatim and edit_summary "
    '= ["no revision needed"]. Never fabricate apologetic placeholder text '
    'like "本节原始草稿为空" or "占位文本"; never insert filler sentences '
    "saying the section needs more material — those go through silently as "
    "rejected output."
)

#: Default static instructions for the stylist's full-manuscript
#: re-polish LLM call (Stage 3.A.3). Pulled from
#: :func:`autoessay.agents.stylist._repolish_prompt`. The override
#: replaces the conceptual instruction; the dynamic context
#: (lowest stop-slop dimension value, claim IDs, style profile,
#: manuscript text, schema spec) is appended after by the agent.
STYLIST_REPOLISH_INSTRUCTIONS = (
    "You are Stylist. Perform one prose-only re-polish of the full manuscript. "
    "Raise the lowest stop-slop dimension without changing claims, citations, "
    "section order, or evidence. Do not copy prior-paper sentences."
)


@dataclass(frozen=True)
class PromptSpec:
    """One overridable prompt surface for a phase."""

    phase: str
    prompt_key: str
    label: str
    default_content: str
    template_id: str
    supported: bool = True


#: Registry of every supported (phase, prompt_key) pair. Stage 2.B
#: lists only the synthesizer surface; subsequent stages add ideator,
#: critic, drafter (per-section), curator (per-batch), etc.
_REGISTRY: dict[tuple[str, str], PromptSpec] = {
    ("curator", "ranking"): PromptSpec(
        phase="curator",
        prompt_key="ranking",
        label="Curator ranking instructions (relevance batch)",
        default_content=CURATOR_RANKING_INSTRUCTIONS,
        template_id="curator.ranking_batch.v1",
    ),
    ("synthesizer", "main"): PromptSpec(
        phase="synthesizer",
        prompt_key="main",
        label="Synthesizer main instructions",
        default_content=SYNTHESIZER_MAIN_INSTRUCTIONS,
        template_id="synthesizer.source_note.v1",
    ),
    ("ideator", "main"): PromptSpec(
        phase="ideator",
        prompt_key="main",
        label="Ideator main instructions",
        default_content=IDEATOR_MAIN_INSTRUCTIONS,
        template_id="ideator.angle_cards.v1",
    ),
    ("critic", "main"): PromptSpec(
        phase="critic",
        prompt_key="main",
        label="Critic main instructions",
        default_content=CRITIC_MAIN_INSTRUCTIONS,
        template_id="critic.report.v1",
    ),
    ("drafter", "main"): PromptSpec(
        phase="drafter",
        prompt_key="main",
        label="Drafter main instructions (every section)",
        default_content=DRAFTER_MAIN_INSTRUCTIONS,
        template_id="drafter.section.v1",
    ),
    # Drafter per-section overrides (Stage 3.A.2). Default content is
    # the section-role hint concatenated with the optional section-
    # type directive; the override REPLACES both for that one
    # section while the universal `main` override still applies
    # cross-section.
    ("drafter", "introduction"): PromptSpec(
        phase="drafter",
        prompt_key="introduction",
        label="Drafter section: introduction",
        default_content=_drafter_section_default("introduction"),
        template_id="drafter.section.introduction.v1",
    ),
    ("drafter", "historiography"): PromptSpec(
        phase="drafter",
        prompt_key="historiography",
        label="Drafter section: historiography",
        default_content=_drafter_section_default("historiography"),
        template_id="drafter.section.historiography.v1",
    ),
    ("drafter", "sources-method"): PromptSpec(
        phase="drafter",
        prompt_key="sources-method",
        label="Drafter section: sources-method",
        default_content=_drafter_section_default("sources-method"),
        template_id="drafter.section.sources-method.v1",
    ),
    ("drafter", "empirical-section-i"): PromptSpec(
        phase="drafter",
        prompt_key="empirical-section-i",
        label="Drafter section: empirical-section-i",
        default_content=_drafter_section_default("empirical-section-i"),
        template_id="drafter.section.empirical-section-i.v1",
    ),
    ("drafter", "empirical-section-ii"): PromptSpec(
        phase="drafter",
        prompt_key="empirical-section-ii",
        label="Drafter section: empirical-section-ii",
        default_content=_drafter_section_default("empirical-section-ii"),
        template_id="drafter.section.empirical-section-ii.v1",
    ),
    ("drafter", "empirical-section-iii"): PromptSpec(
        phase="drafter",
        prompt_key="empirical-section-iii",
        label="Drafter section: empirical-section-iii",
        default_content=_drafter_section_default("empirical-section-iii"),
        template_id="drafter.section.empirical-section-iii.v1",
    ),
    ("drafter", "discussion"): PromptSpec(
        phase="drafter",
        prompt_key="discussion",
        label="Drafter section: discussion",
        default_content=_drafter_section_default("discussion"),
        template_id="drafter.section.discussion.v1",
    ),
    ("drafter", "conclusion"): PromptSpec(
        phase="drafter",
        prompt_key="conclusion",
        label="Drafter section: conclusion",
        default_content=_drafter_section_default("conclusion"),
        template_id="drafter.section.conclusion.v1",
    ),
    ("stylist", "main"): PromptSpec(
        phase="stylist",
        prompt_key="main",
        label="Stylist main instructions (every section)",
        default_content=STYLIST_MAIN_INSTRUCTIONS,
        template_id="stylist.section.v1",
    ),
    # Stylist full-manuscript re-polish second pass (Stage 3.A.3).
    # Independent of `main`: the user can edit either or both.
    ("stylist", "repolish"): PromptSpec(
        phase="stylist",
        prompt_key="repolish",
        label="Stylist re-polish instructions (full manuscript)",
        default_content=STYLIST_REPOLISH_INSTRUCTIONS,
        template_id="stylist.repolish.v1",
    ),
}


def get_phase_prompt_spec(phase: str, prompt_key: str = "main") -> PromptSpec | None:
    """Look up a prompt surface. ``None`` if the phase is not yet
    overridable (the API returns 404 for that case)."""
    return _REGISTRY.get((phase, prompt_key))


def supported_keys_for_phase(phase: str) -> list[str]:
    """All prompt_keys this phase supports overriding. Empty if none."""
    return sorted(key for (p, key) in _REGISTRY if p == phase)


def hash_content(content: str) -> str:
    """Canonical sha256 of the prompt content. Used for dedup,
    revision tokens, and the optimistic concurrency check on rerun."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "CRITIC_MAIN_INSTRUCTIONS",
    "CURATOR_RANKING_INSTRUCTIONS",
    "DRAFTER_MAIN_INSTRUCTIONS",
    "DRAFTER_SECTION_ROLES",
    "DRAFTER_SECTION_TYPE_DIRECTIVES",
    "IDEATOR_MAIN_INSTRUCTIONS",
    "PromptSpec",
    "STYLIST_MAIN_INSTRUCTIONS",
    "STYLIST_REPOLISH_INSTRUCTIONS",
    "SYNTHESIZER_MAIN_INSTRUCTIONS",
    "get_phase_prompt_spec",
    "hash_content",
    "supported_keys_for_phase",
]
