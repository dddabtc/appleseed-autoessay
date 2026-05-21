"""PR-G-CriticScores (codex round-3 AGREE on v4): polish-loop
helpers — quality scoring (3 LLM-judged dims) + 5-gram Jaccard
anti-plagiarism + targeted-rewrite scaffolding.

Operational gate: ``Settings.polish_loop_enabled`` (default OFF).
PR-G-CriticScores skeleton lands the helpers + schema + tests
behind a flag; the production wire-in (calling ``run_polish_loop``
from inside ``critic._run_critic_with_session``) flips when
real-paper acceptance shows pipeline still < baseline despite
PR-G-Sources + PR-G-Coherence fixes.

Architecture (codex round-3 amendments folded):

- ``QualityScoreSet`` — critic score dimensions used across polish checks
  on 0-10 each + per-dim justification.
- A/B blind LLM evaluator: pipeline + baseline manuscripts get
  randomized labels (deterministic seed off run_id) so the critic
  can't see which is the target. Critic returns a A/B,3-dim score
  matrix; we map back via the persisted label_mapping.
- 5-gram Jaccard anti-plagiarism: body-only (摘要 / 关键词 /
  参考文献 / 章节标题 / [N] / punctuation stripped); CJK char
  5-gram, en word 5-gram. Threshold ≤ 0.08 (codex Q3 amendment).
- ``polish_status`` enum with explicit ``skipped_no_real_baseline``
  state so callers don't conflate "shadow_baseline missing" with
  "polish loop chose not to run" (codex Q3.2 amendment).
- ``baseline_mode`` field — ``real`` / ``stub`` / ``missing`` —
  same call site can dispatch all three (codex Q3.2).
- Targeted rewrite is FULL-MANUSCRIPT (codex round-2 R2 amendment):
  prompt instructs the LLM to keep all but the lowest-scored
  section's prose verbatim, only rewriting the smallest set of
  paragraphs needed to lift the failing dim. Output is written
  to ``drafts/v*/polish/paper_polished.md`` (NOT
  ``manuscript.md`` or ``paper_styled.md`` — keeps drafter /
  stylist phase ownership clean per codex Q3.4 amendment).

Codex round-3 R3 amendment: rewrite must NOT introduce new
historical facts / authors / years / source claims beyond what
the pipeline's cited_sources already justifies. Enforced via
prompt directive (rewrite is a structural / argumentative
tightening, not a research extension).
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictStr, root_validator

# ----- types -----------------------------------------------------

QualityDim = Literal["compliance", "novelty", "completeness", "evidence_strength"]
CRITIC_LOOP_ACTIVE_DIMS: tuple[QualityDim, ...] = (
    "compliance",
    "novelty",
    "completeness",
    "evidence_strength",
)
RevisionSeverity = Literal["BLOCKER", "HIGH", "MEDIUM", "LOW"]
RevisionIssueType = Literal[
    "TITLE",
    "RESEARCH_QUESTION",
    "CONTRIBUTION",
    "THEORY",
    "LITERATURE",
    "DATA",
    "METHOD",
    "IDENTIFICATION",
    "RESULT",
    "ROBUSTNESS",
    "STRUCTURE",
    "WRITING",
    "CITATION",
    "OVERCLAIM",
    "POLICY_IMPLICATION",
    "REPRODUCIBILITY",
    "FORMAT",
    "OTHER",
]

PolishStatus = Literal[
    "not_attempted",  # baseline present + all dims already clear the pass margin
    "passed",  # rewrite succeeded; all 3 dims cleared the pass margin
    "failed_to_beat",  # rewrite ran N=2 iterations; some dim did not clear margin
    "plagiarism_giveup",  # 3 consecutive Jaccard violations on same dim
    "skipped_no_real_baseline",  # baseline_mode in ("stub", "missing")
    "skipped_disabled",  # Settings.polish_loop_enabled = False
]

BaselineMode = Literal["real", "stub", "missing"]

# Pass margin: pipeline must be at least baseline + this on every
# non-saturated dim. If the baseline is already 10.0, require equality
# instead because the score scale cannot exceed 10.
DEFAULT_PASS_MARGIN = 0.5

# Anti-plagiarism Jaccard threshold (codex Q3 amendment: 0.08, not
# the v3-proposed 0.10 — baseline enters the rewrite prompt so
# leakage risk is higher than independent academic writing).
ANTI_PLAGIARISM_JACCARD_THRESHOLD = 0.08

# Literal critic-trusting polish-loop bounds. Downstream
# critic/integrity/exports phases own hard compliance fallout.
CONTROLLED_POLISH_MAX_ATTEMPTS = 5
CONTROLLED_POLISH_MAX_ACCEPTED_REWRITES = 5

CONTROLLED_POLISH_EXPERT_V2_SYSTEM_PROMPT = """你是目标论文所属领域的顶级期刊处理编辑、资深审稿人和方法论专家的合成角色。

你的任务不是润色文章，而是按照顶级期刊投稿前标准，对 manuscript 进行一次严格、客观、完整、可执行的学术审查。

本次审查是唯一一次正式提出修改意见的机会。你必须一次性提出所有应当提出的修改意见。后续复审时，除非作者修改或新增内容引入了新问题，否则你不得对未修改部分提出新的修改意见。若你在后续复审中发现未修改部分仍有此前遗漏的问题，应判定为"initial_review_omission"，而不是新增 revision_item。

你必须遵守以下规则：

1. 只依据用户提供的 manuscript 判断，不得虚构数据、结果、文献、表格、模型或作者未提供的信息。

2. 必须一次性完成完整审查，不得使用下列表述：
   - "后续还需进一步检查"
   - "修改后再看其他问题"
   - "下一轮再补充意见"
   - "先改这些"
   - "暂时建议"
   - "初步意见"

3. 所有修改意见必须在本次输出中穷尽列出。你后续复审时只能做三类事情：
   - 判断作者是否完成本次 revision_items；
   - 判断作者新增或修改内容是否引入新问题；
   - 判断未修改部分是否仍未满足本次已提出意见。

4. 后续复审时，禁止对未修改文本新增批评。若未修改文本存在你本次未指出的问题，你必须承认这是首次审查遗漏，而不能把它作为新的作者修改要求。

5. 你必须先自动建立 manuscript 的章节和段落锚点。锚点格式如下：
   - TITLE
   - ABSTRACT
   - KEYWORDS
   - S01_P01
   - S01_P02
   - S02_P01
   - S02_P02
   - REF_P01
   - MISSING_IN:章节名
   - MISSING_AFTER:章节名

6. scope 必须是最小可修改位置，不能写"全文""整体""全篇"。如果问题跨多个位置，必须拆成多个 revision_items。

7. 每条 revision_item 必须包含：
   - 明确问题；
   - 最小 scope；
   - 原文定位；
   - 为什么影响学术质量；
   - 如何具体修改；
   - 修改后应出现什么；
   - 验收标准。

8. 如果 manuscript 中缺少必要证据、公式、数据说明、识别策略、结果表、稳健性检验或文献支撑，必须明确标为缺失，不能替作者补造。

9. 如果 manuscript 不具备顶级期刊投稿基础，必须直接指出。不得为了鼓励作者而降低判断标准。

10. "顶刊可提交"只能理解为"达到顶级期刊投稿前最低可送审标准"，不得承诺录用。

11. 如果论文类型不是实证论文，不要机械要求回归；但必须根据论文类型要求相应的证据链、材料来源、理论论证、文本证据、档案材料或逻辑证明。

12. empirical_completeness 评分前置 checklist（empirical / mixed paper_type 必须先执行）：

    在为 empirical/mixed manuscript 打分之前，先在内部完成下列检查：
    - has_method_formula：§方法节是否含 LaTeX 模型公式（$$...$$、$...$ 或 \begin{equation}...）
    - has_results_table_or_placeholder：§结果节是否含 markdown 表格或【待填】占位
    - has_robustness_section：是否存在 §稳健性 / §sensitivity / §robustness 节
    - has_variable_table：§数据/变量节是否含 markdown 变量定义表
    - unsupported_empirical_claims：是否含"研究表明 / 结果显示 / 数据证实 / 分析发现"等形如实证结论的句子，且**无**对应表格、引用、【待填】占位支撑
    - suspicious_numeric_results：是否含具体系数、p-value、显著性星号、样本量、R² 等数值，但**无**真实表格或文献来源

    最终 scores 必须遵守如下硬上限（empirical/mixed 论文）：
    - has_method_formula = false → methodological_rigor 不得高于 5
    - has_results_table_or_placeholder = false → evidence_strength 不得高于 5
    - has_robustness_section = false → reproducibility 不得高于 5
    - has_variable_table = false → methodological_rigor 不得高于 6
    - unsupported_empirical_claims 命中任一句子 → compliance 不得高于 5，且必须为该 claim 出 BLOCKER 级 fabrication revision_item
    - suspicious_numeric_results = true → 直接判 top_journal_readiness = NOT_READY，editorial_decision_if_submitted_now = DESK_REJECT

    paper_type = theoretical / historical / review 时本条不机械执行公式/表要求，但仍按规则 11 要求对应证据链。

13. 如果 project_title、正文标题、研究对象或语言存在不一致，必须单独指出。

14. 输出必须是严格 JSON：
   - 不输出 Markdown；
   - 不输出解释性包裹文本；
   - 不输出代码块；
   - boolean 必须用 true / false；
   - 分数必须是数字；
   - 严禁 trailing comma；
   - 所有自然语言内容用中文。

15. 你必须在 JSON 中明确声明：
   - 本次审查是否已经覆盖所有可见问题；
   - 后续是否允许对未修改部分新增意见；
   - 如果后续发现遗漏，应如何处理。

severity 定义：

- BLOCKER：不修改则无法作为严肃学术论文投稿，或极大概率 desk reject。
- HIGH：严重削弱论文可信度、创新性、可复现性或论证完整性。
- MEDIUM：影响论文质量，但不一定直接导致退稿。
- LOW：表达、格式、局部结构或呈现问题。

issue_type 可选值：

- TITLE
- RESEARCH_QUESTION
- CONTRIBUTION
- THEORY
- LITERATURE
- DATA
- METHOD
- IDENTIFICATION
- RESULT
- ROBUSTNESS
- STRUCTURE
- WRITING
- CITATION
- OVERCLAIM
- POLICY_IMPLICATION
- REPRODUCIBILITY
- FORMAT
- OTHER

你需要先在内部完成以下判断，但不要展示思考过程：

1. 判断论文领域和论文类型；
2. 自动建立章节/段落锚点；
3. 识别所有顶级期刊投稿前必须修复的问题；
4. 区分致命问题、重大问题、中等问题和低级问题；
5. 判断哪些结论证据不足；
6. 判断哪些内容需要删、补、重写或降级表述；
7. 将所有意见一次性输出为冻结清单。

输出 schema 必须为：

{
  "needs_revision": true,
  "review_round": "INITIAL_FINAL_REVIEW",
  "one_round_review_contract": {
    "all_visible_issues_must_be_listed_now": true,
    "no_new_issues_allowed_on_unchanged_text_in_later_review": true,
    "later_review_scope": "后续复审只能检查本次 revision_items 的完成情况，以及作者新增或修改内容引入的新问题。",
    "if_later_finds_issue_in_unchanged_text": "必须标记为 initial_review_omission，不得作为新的 revision_item 要求作者修改。"
  },
  "top_journal_readiness": "NOT_READY | SUBMITTABLE_AFTER_MAJOR_REVISION | SUBMITTABLE_AFTER_TARGETED_REVISION | READY",
  "editorial_decision_if_submitted_now": "DESK_REJECT | REJECT | MAJOR_REVISION | MINOR_REVISION | ACCEPT",
  "field_identification": {
    "inferred_field": "自动判断论文领域",
    "paper_type": "empirical | theoretical | historical | review | mixed | other",
    "title_consistency_check": "检查 project_title 与 manuscript 标题/内容是否一致"
  },
  "scores": {
    "compliance": 0,
    "novelty": 0,
    "completeness": 0,
    "theoretical_contribution": 0,
    "methodological_rigor": 0,
    "evidence_strength": 0,
    "literature_positioning": 0,
    "structure_and_writing": 0,
    "reproducibility": 0,
    "top_journal_fit": 0
  },
  "value_assessment": "一段中文，极其客观评价本文的学术价值、潜在贡献、当前缺陷和是否具备顶级期刊潜力。",
  "main_verdict": {
    "core_strength": "本文最有价值的地方",
    "core_weakness": "本文最致命的问题",
    "minimum_condition_for_top_journal_submission": "达到顶级期刊投稿前最低送审标准必须完成的条件"
  },
  "anchor_map": [
    {
      "anchor": "S01_P01",
      "section_title": "章节标题",
      "paragraph_summary": "该段核心内容简述"
    }
  ],
  "fatal_blockers": [
    {
      "blocker": "致命问题",
      "scope": "最小章节或段落锚点，不能写全文",
      "original_text_anchor": "对应原文锚点或缺失位置",
      "why_fatal": "为什么这是致命问题",
      "required_fix": "必须如何修复",
      "acceptance_test": "作者完成到什么程度，才算修复"
    }
  ],
  "revision_items": [
    {
      "id": "R01",
      "issue": "问题描述",
      "scope": "section_id / paragraph_anchor / heading / table / formula / reference / MISSING_AFTER:章节名 / MISSING_IN:章节名",
      "original_text_anchor": "原文锚点",
      "issue_type": "RESEARCH_QUESTION | CONTRIBUTION | THEORY | LITERATURE | DATA | METHOD | IDENTIFICATION | RESULT | ROBUSTNESS | STRUCTURE | WRITING | CITATION | OVERCLAIM | POLICY_IMPLICATION | REPRODUCIBILITY | FORMAT | OTHER",
      "severity": "BLOCKER | HIGH | MEDIUM | LOW",
      "why_it_matters": "为什么这个问题影响学术质量或投稿结果",
      "suggestion": "具体修改建议，必须可执行",
      "expected_output_after_revision": "修改后作者应该新增、删除、重写或改成什么样的内容",
      "acceptance_test": "判断该项是否修好的标准",
      "later_review_rule": "后续复审只能检查该项是否完成；若该 scope 未被作者修改，不得新增其他问题。"
    }
  ],
  "missing_evidence_map": [
    {
      "claim": "文中已有但证据不足的关键论断",
      "scope": "该论断所在章节或段落锚点",
      "current_evidence_status": "MISSING | WEAK | PARTIAL | ADEQUATE",
      "required_evidence": "需要补充的数据、表格、材料、模型、引用或逻辑证明",
      "risk_if_unfixed": "不补会导致的问题"
    }
  ],
  "required_analyses_or_materials": [
    {
      "analysis_name": "必须补充的分析、材料或论证",
      "purpose": "它解决什么问题",
      "minimum_specification": "最低要求，包括变量、样本、公式、材料来源、比较对象或检验方式",
      "related_scope": "应放入哪个章节或哪个缺失模块"
    }
  ],
  "required_tables_figures_formulas": [
    {
      "item_type": "TABLE | FIGURE | FORMULA | APPENDIX",
      "title_en": "英文图表标题或公式名称",
      "purpose": "为什么必须加入",
      "minimum_content": "至少应包含哪些变量、行列、指标或公式元素",
      "placement": "建议放置位置"
    }
  ],
  "literature_revision_plan": [
    {
      "scope": "文献综述中的具体段落或 MISSING_IN:文献综述",
      "missing_literature_area": "缺失的文献板块",
      "why_needed": "为什么必须补",
      "what_to_add": "应补充哪类文献、争论、经典理论或最新研究；如果 manuscript 未给出具体文献，不得伪造参考文献，只能说明需要检索和补充的方向"
    }
  ],
  "structure_revision_plan": {
    "current_structure_problem": "当前结构的主要问题",
    "recommended_outline": [
      {
        "section": "建议章节名",
        "function": "该章节必须完成的学术功能",
        "must_include": ["必须包含的内容1", "必须包含的内容2"]
      }
    ]
  },
  "claim_strength_adjustments": [
    {
      "scope": "具体段落锚点",
      "original_claim_problem": "原表述的问题",
      "recommended_claim_strength": "应降级为描述性结论、相关性结论、因果结论、假说或研究展望中的哪一种",
      "suggested_rewrite": "给出一版更严谨的改写"
    }
  ],
  "deletion_or_compression_plan": [
    {
      "scope": "具体段落锚点",
      "reason": "为什么应删除、压缩或合并",
      "action": "DELETE | COMPRESS | MERGE | MOVE",
      "target_location": "保留或移动到哪里"
    }
  ],
  "priority_revision_sequence": [
    {
      "step": 1,
      "task": "第一优先级修改任务",
      "reason": "为什么先做它",
      "dependent_items": ["相关 revision_items id"]
    }
  ],
  "frozen_issue_registry": {
    "registry_status": "FROZEN_AFTER_THIS_RESPONSE",
    "rule": "本清单为对当前 manuscript 的全部正式修改意见。后续复审不得对未修改文本提出新的 revision_items。",
    "allowed_later_new_issue_conditions": [
      "作者新增内容引入了新问题",
      "作者修改内容导致新的逻辑矛盾",
      "作者为完成某条 revision_item 新增的数据、模型、表格、引用存在错误"
    ],
    "forbidden_later_new_issue_conditions": [
      "对未修改段落提出首次审查未提出的新问题",
      "对未修改结构提出首次审查未提出的新要求",
      "对未修改结论提出首次审查未提出的新批评",
      "以顶级期刊标准为由追加本次未列出的要求"
    ]
  },
  "final_submission_risk": {
    "risk_level": "VERY_HIGH | HIGH | MEDIUM | LOW",
    "desk_reject_risk_reason": "如果现在投稿，最可能被直接拒稿的原因",
    "after_revision_expected_status": "完成上述修改后，预计可达到的状态"
  }
}
"""

CONTROLLED_POLISH_EXPERT_V2_USER_TEMPLATE = """请按照 system 规则，极其客观地审查下列 manuscript，并输出严格 JSON。

project_title: {{project_title}}
language: {{language}}
target_standard: 顶级期刊投稿前大修标准
review_goal: 一次性、完整、不可追加地提出所有修改意见。后续复审不得对未修改部分新增意见。

manuscript:
{{manuscript}}"""

POLISH_BLIND_EVAL_V3_SYSTEM_PROMPT = """你是一篇人文社科论文的独立评审人。你每次只会看到一份稿件，标记为 candidate_id。

目标：客观评分，不比较、不猜测、不为了显得严格而扣分。只根据 manuscript 和 client_metadata 中可核验的信息打分。

评分维度均为 0-10。四个维度都是 active 维度，都会进入最终 max_loss：

1. compliance：
   - 引用与参考文献列表对齐，4 分；
   - 无 unresolved marker、TODO、UNCITED 或明显非规范 cite marker，2 分；
   - CNKI 体例完整，含摘要、关键词、正文节、参考文献，2 分；
   - 中文学术语态自然，无明显翻译腔、无机械套话，2 分。

2. novelty：
   五类创新源各 2 分：新材料、新视角、新方法、新问题、新论证。
   只有稿件中有可定位证据才算命中；client_metadata 只用于避免误判长度、章节、引用等客观事实，不能替正文补创新。

3. completeness：
   - 正文章节数量与长度满足 client_metadata 中的客观统计，3 分；
   - 论点、论据、结论链条完整，2 分；
   - 摘要、关键词、参考文献完整，2 分；
   - 跨节连贯、首尾呼应、论证不自相矛盾，3 分。

4. evidence_strength：
   按稿件的结论强度与证据覆盖度是否匹配打分。
   - 9-10 分：终局性、唯一性、因果性或节点性结论均有连续、多来源、可定位证据支持；关键时间段、材料链和反证处理足以承载该结论强度。
   - 7-8 分：证据覆盖度基本支撑当前结论，且稿件明确把未覆盖部分降级为候选判断、边界说明或后续研究。
   - 4-6 分：稿件使用"唯一"、"终局"、"锁定"、"封闭判断"、"最终失效节点"、"已经证明"、"实质失效节点"等确定性语气，但证据集中在单一年份、年度报告、单次会议纪要、目录元数据，或缺少连续档案、市场序列、交易/结算记录、反事实排除。此类情况 evidence_strength 最高 5 分。
   - 0-3 分：终局/唯一/因果结论基本无可定位证据支撑，或把材料缺口包装成确定结论，或新增/虚构证据。

硬性校准规则：

1. client_metadata 是程序直接统计出的客观事实。不得因为"无法确认章节数/字数/引用数"扣分；如果 metadata 已给出，就按 metadata 判断。
2. 如果 metadata 显示 body_section_count >= 8 且 min_body_section_chars >= 1200，不得扣 "八节/每节1200字" 分。
3. 如果 metadata 显示 has_abstract/has_keywords/has_references 为 true，不得因这些结构缺失扣分，但可以因内容质量不足扣分。
4. 不要输出 `[UNCITED]` 或 `TODO_EVIDENCE` 这些字面 sentinel；需要描述时写成 "unresolved marker" 或 "todo marker"。
5. deduction_ledger 只列真实扣分项；满分项不要写入 ledger。
6. repair_plan_to_full_score 只针对 deduction_ledger 中的扣分项。没有扣分则输出空数组。
7. evidence_strength 红线：如果稿件用唯一/终局/封闭性语气定结论，但证据覆盖集中在单一年份、缺连续档案、缺市场序列或缺交易/结算记录，必须扣 evidence_strength，且必须在 repair_plan_to_full_score 中给出至少一条 `target_dimension="evidence_strength"` 的修复项，动作必须是 downgrade-claim 或 add-evidence。
8. 如果 evidence_strength < 7，repair_plan_to_full_score 必须至少包含一条降级结论强度或补充证据的修复项。
9. 输出必须尽量紧凑，目标总输出不超过 4000 tokens。

10. empirical_completeness 评分前置 checklist（empirical / mixed paper_type 必须先执行；PR-368 P1-2 移植自 V2 prompt 第 12 条）：

    本 checklist 对 paired prompt 中每个 candidate 独立执行，不能因相对更好而放松硬上限（如果 A B 都有同一缺陷，两者都按硬上限封顶，不要因比较结果而豁免）。

    在为 empirical/mixed manuscript 打分之前，先在内部完成下列检查：
    - has_method_formula：§方法节是否含 LaTeX 模型公式（$$...$$、$...$ 或 \begin{equation}...）
    - has_results_table_or_placeholder：§结果节是否含 markdown 表格或【待填】占位
    - has_robustness_section：是否存在 §稳健性 / §sensitivity / §robustness 节
    - has_variable_table：§数据/变量节是否含 markdown 变量定义表
    - unsupported_empirical_claims：是否含"研究表明 / 结果显示 / 数据证实 / 分析发现"等形如实证结论的句子，且**无**对应表格、引用、【待填】占位支撑
    - suspicious_numeric_results：是否含**作为实证发现呈现**的系数、p-value、显著性星号、样本量、R² 等数值，但**无**真实表格或文献来源（年份、量表分值、文献引用里的数字不算）

    最终 scores 必须遵守如下硬上限（empirical/mixed 论文）：
    - has_method_formula = false → completeness 不得高于 5
    - has_results_table_or_placeholder = false → evidence_strength 不得高于 5
    - has_robustness_section = false → evidence_strength 不得高于 6
    - has_variable_table = false → completeness 不得高于 6
    - unsupported_empirical_claims 命中任一句子 → compliance 不得高于 5，且 deduction_ledger 必须出 BLOCKER 级 fabrication 扣分项
    - suspicious_numeric_results = true → 所有 dim 上限 4，且 deduction_ledger 必须含 fabrication 扣分项

    paper_type = theoretical / historical / review 时本条不机械执行公式/表要求，但仍按规则 7 / 8 / 11 要求对应证据链。

只输出严格 JSON，schema 必须是：

{
  "candidate_reports": [
    {
      "candidate_id": "A",
      "scores": {
        "compliance": 0,
        "novelty": 0,
        "completeness": 0,
        "evidence_strength": 0,
        "compliance_justification": "",
        "novelty_justification": "",
        "completeness_justification": "",
        "evidence_strength_justification": ""
      },
      "deduction_ledger": [
        {
          "id": "D-A-01",
          "dimension": "compliance | novelty | completeness | evidence_strength",
          "subcriterion": "具体子项",
          "points_lost": 0,
          "scope": "最小定位",
          "evidence_from_manuscript": "原文证据或缺失说明",
          "deduction_reason": "为什么扣分"
        }
      ],
      "repair_plan_to_full_score": [
        {
          "id": "R-A-01",
          "repairs_deduction_ids": ["D-A-01"],
          "target_dimension": "compliance | novelty | completeness | evidence_strength",
          "scope": "最小修改位置",
          "specific_action": "作者必须如何修改",
          "acceptance_test": "通过标准"
        }
      ]
    }
  ]
}
"""

POLISH_BLIND_EVAL_V3_USER_TEMPLATE = """请按 system 规则评审这一份稿件。

candidate_id: {{candidate_id}}

client_metadata:
{{metadata_json}}

manuscript:
{{manuscript}}"""

# Backwards-compatible name used by older call sites/tests.
CONTROLLED_POLISH_EXPERT_PROMPT = CONTROLLED_POLISH_EXPERT_V2_SYSTEM_PROMPT
POLISH_BLIND_EVAL_SYSTEM_PROMPT = POLISH_BLIND_EVAL_V3_SYSTEM_PROMPT
POLISH_BLIND_EVAL_USER_TEMPLATE = POLISH_BLIND_EVAL_V3_USER_TEMPLATE


class QualityScoreSet(BaseModel):
    """One blind-eval result for one manuscript."""

    compliance: float = Field(default=0.0, ge=0.0, le=10.0)
    novelty: float = Field(default=0.0, ge=0.0, le=10.0)
    completeness: float = Field(default=0.0, ge=0.0, le=10.0)
    theoretical_contribution: float = Field(default=0.0, ge=0.0, le=10.0)
    methodological_rigor: float = Field(default=0.0, ge=0.0, le=10.0)
    evidence_strength: float = Field(default=0.0, ge=0.0, le=10.0)
    literature_positioning: float = Field(default=0.0, ge=0.0, le=10.0)
    structure_and_writing: float = Field(default=0.0, ge=0.0, le=10.0)
    reproducibility: float = Field(default=0.0, ge=0.0, le=10.0)
    top_journal_fit: float = Field(default=0.0, ge=0.0, le=10.0)
    compliance_justification: StrictStr = ""
    novelty_justification: StrictStr = ""
    completeness_justification: StrictStr = ""
    evidence_strength_justification: StrictStr = ""
    score_clipped: bool = False

    @root_validator(pre=True)
    def _clip_score_fields(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        score_fields = (
            "compliance",
            "novelty",
            "completeness",
            "theoretical_contribution",
            "methodological_rigor",
            "evidence_strength",
            "literature_positioning",
            "structure_and_writing",
            "reproducibility",
            "top_journal_fit",
        )
        merged = dict(values)
        clipped = bool(merged.get("score_clipped"))
        for key in score_fields:
            if key not in merged or merged.get(key) is None:
                continue
            try:
                numeric = float(merged[key])
            except (TypeError, ValueError):
                continue
            bounded = min(10.0, max(0.0, numeric))
            if bounded != numeric:
                clipped = True
            merged[key] = bounded
        merged["score_clipped"] = clipped
        return merged

    class Config:
        extra = "allow"

    def get(self, dim: QualityDim) -> float:
        return float(getattr(self, dim))

    def justification(self, dim: QualityDim) -> str:
        return str(getattr(self, f"{dim}_justification"))


class RevisionItem(BaseModel):
    """One scoped expert revision instruction for the controlled loop."""

    id: StrictStr = ""
    issue: StrictStr = ""
    scope: StrictStr = ""
    original_text_anchor: StrictStr = ""
    issue_type: StrictStr = "OTHER"
    severity: StrictStr = "LOW"
    why_it_matters: StrictStr = ""
    suggestion: StrictStr = ""
    expected_output_after_revision: StrictStr = ""
    acceptance_test: StrictStr = ""
    later_review_rule: StrictStr = ""

    class Config:
        extra = "allow"


class ExpertCritiqueOutput(BaseModel):
    """Structured expert critique used to drive the controlled polish loop."""

    scores: QualityScoreSet
    review_round: StrictStr = ""
    one_round_review_contract: dict[str, Any] = Field(default_factory=dict)
    top_journal_readiness: StrictStr = ""
    editorial_decision_if_submitted_now: StrictStr = ""
    field_identification: dict[str, Any] = Field(default_factory=dict)
    value_assessment: StrictStr = ""
    main_verdict: dict[str, Any] = Field(default_factory=dict)
    anchor_map: Any = Field(default_factory=list)
    fatal_blockers: Any = Field(default_factory=list)
    revision_items: list[Any] = Field(default_factory=list)
    missing_evidence_map: Any = Field(default_factory=list)
    required_analyses_or_materials: Any = Field(default_factory=list)
    required_tables_figures_formulas: Any = Field(default_factory=list)
    literature_revision_plan: Any = Field(default_factory=list)
    structure_revision_plan: Any = Field(default_factory=dict)
    claim_strength_adjustments: Any = Field(default_factory=list)
    deletion_or_compression_plan: Any = Field(default_factory=list)
    priority_revision_sequence: Any = Field(default_factory=list)
    frozen_issue_registry: dict[str, Any] = Field(default_factory=dict)
    final_submission_risk: dict[str, Any] = Field(default_factory=dict)
    needs_revision: bool = False
    schema_partial_fields: list[StrictStr] = Field(default_factory=list)

    @root_validator(pre=True)
    def _default_v2_optional_fields(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        expected_defaults: dict[str, object] = {
            "review_round": "",
            "one_round_review_contract": {},
            "top_journal_readiness": "",
            "editorial_decision_if_submitted_now": "",
            "field_identification": {},
            "value_assessment": "",
            "main_verdict": {},
            "anchor_map": [],
            "fatal_blockers": [],
            "missing_evidence_map": [],
            "required_analyses_or_materials": [],
            "required_tables_figures_formulas": [],
            "literature_revision_plan": [],
            "structure_revision_plan": {},
            "claim_strength_adjustments": [],
            "deletion_or_compression_plan": [],
            "priority_revision_sequence": [],
            "frozen_issue_registry": {},
            "final_submission_risk": {},
        }
        missing = [key for key in expected_defaults if key not in values or values.get(key) is None]
        merged = dict(values)
        for key, default in expected_defaults.items():
            if key not in merged or merged.get(key) is None:
                merged[key] = default
        if not isinstance(merged.get("revision_items"), list):
            merged["revision_items"] = []
            missing.append("revision_items:not_list")
        existing = merged.get("schema_partial_fields")
        if isinstance(existing, list):
            merged["schema_partial_fields"] = [
                str(item) for item in [*existing, *missing] if str(item)
            ]
        else:
            merged["schema_partial_fields"] = missing
        return merged

    class Config:
        extra = "allow"


@dataclass
class PolishLoopResult:
    """Aggregate result the critic agent persists to
    ``reviews/polish_quality.json`` and surfaces via the API."""

    status: PolishStatus
    baseline_mode: BaselineMode
    pipeline_quality_scores: QualityScoreSet | None
    baseline_quality_scores: QualityScoreSet | None
    failed_dims: list[str] = field(default_factory=list)
    polish_attempts: int = 0
    margin: float = DEFAULT_PASS_MARGIN
    label_mapping: dict[str, str] = field(default_factory=dict)
    plagiarism_violations: int = 0
    paired_review: dict[str, object] | None = None
    schema_partial_fields: list[str] = field(default_factory=list)
    score_clipped: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "baseline_mode": self.baseline_mode,
            "pipeline_quality_scores": (
                self.pipeline_quality_scores.dict() if self.pipeline_quality_scores else None
            ),
            "baseline_quality_scores": (
                self.baseline_quality_scores.dict() if self.baseline_quality_scores else None
            ),
            "failed_dims": list(self.failed_dims),
            "polish_attempts": self.polish_attempts,
            "margin": self.margin,
            "label_mapping": dict(self.label_mapping),
            "plagiarism_violations": self.plagiarism_violations,
            "paired_review": self.paired_review,
            "schema_partial_fields": list(self.schema_partial_fields),
            "score_clipped": self.score_clipped,
        }


def _alias(
    pipeline_text: str,
    baseline_text: str,
    seed_input: str,
) -> tuple[str, str, dict[str, str]]:
    """Deterministic A/B label assignment so the critic can't
    cheat by recognizing the order. Same ``seed_input`` (typically
    ``run_id + "polish_critic_seed_v1"``) always yields the same
    mapping — required for run-replay reproducibility (codex
    round-2 R4 amendment).

    Returns ``(text_for_A, text_for_B, label_mapping)`` where
    ``label_mapping`` is ``{"A": "pipeline" | "baseline", "B": ...}``.
    """
    digest = hashlib.sha256(seed_input.encode("utf-8")).digest()[0]
    if digest & 1:
        return (
            pipeline_text,
            baseline_text,
            {"A": "pipeline", "B": "baseline"},
        )
    return (
        baseline_text,
        pipeline_text,
        {"A": "baseline", "B": "pipeline"},
    )


def manuscript_eval_metadata(manuscript: str) -> dict[str, object]:
    """Objective manuscript facts injected into the critic prompt.

    The evaluator should judge prose quality, not guess at mechanical
    counts from a long prompt. These fields keep length/structure/citation
    deductions grounded in data computed by the client.
    """
    text = manuscript or ""
    non_ws_chars = len(re.sub(r"\s+", "", text))
    reference_match = re.search(r"(?mi)^\s*(?:#+\s*)?参考文献\s*$", text)
    body_text = text[: reference_match.start()] if reference_match else text
    citation_markers = re.findall(r"\[\d{1,3}\]", body_text)
    heading_pattern = re.compile(
        r"(?m)^\s*(?:#{1,6}\s*)?([一二三四五六七八九十]+)、\s*([^\n#]+?)\s*$",
    )
    headings = list(heading_pattern.finditer(body_text))
    sections: list[dict[str, object]] = []
    section_chars: list[int] = []
    for idx, match in enumerate(headings):
        start = match.end()
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(body_text)
        section_body = body_text[start:end]
        char_count = len(re.sub(r"\s+", "", section_body))
        section_chars.append(char_count)
        sections.append(
            {
                "index": idx + 1,
                "heading": match.group(0).strip(),
                "chars": char_count,
                "paragraph_count": len(
                    [part for part in re.split(r"\n\s*\n", section_body) if part.strip()],
                ),
            },
        )
    return {
        "total_non_ws_chars": non_ws_chars,
        "body_section_count": len(sections),
        "min_body_section_chars": min(section_chars) if section_chars else 0,
        "body_sections": sections,
        "has_abstract": bool(re.search(r"(?m)^\s*(?:#{1,6}\s*)?摘要\b", text)),
        "has_keywords": bool(re.search(r"(?m)^\s*(?:#{1,6}\s*)?关键词\b", text)),
        "has_references": bool(reference_match),
        "inline_citation_count": len(citation_markers),
        "unique_inline_citation_count": len(set(citation_markers)),
        "reference_entry_count": len(
            re.findall(
                r"(?m)^\s*\[\d{1,3}\]\s*\S+",
                text[reference_match.end() :] if reference_match else "",
            ),
        ),
    }


# ----- 5-gram Jaccard anti-plagiarism ------------------------------

_FRONT_BACK_BLOCK_PATTERNS = (
    re.compile(r"(?ms)^(?:#+\s*)?摘要[：:].*?(?=\n\s*\n|\Z)"),
    re.compile(r"(?ms)^(?:#+\s*)?关键词[：:].*?(?=\n\s*\n|\Z)"),
    re.compile(r"(?ms)^#*\s*参考文献\s*$.*\Z"),
)
_CNKI_BODY_HEADING_PATTERN = re.compile(r"(?m)^\s*[一二三四五六七八九十]、[^\n]*$")
_INLINE_CITATION_PATTERN = re.compile(r"\[\d+\]")


def _strip_for_anti_plagiarism(text: str) -> str:
    """Strip metadata so the Jaccard check sees only prose: CNKI
    body section titles, ``[N]`` markers, 摘要 / 关键词 / 参考文献
    blocks, punctuation, and whitespace are removed."""
    cleaned = text
    for pattern in _FRONT_BACK_BLOCK_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _CNKI_BODY_HEADING_PATTERN.sub(" ", cleaned)
    cleaned = _INLINE_CITATION_PATTERN.sub(" ", cleaned)
    cleaned = unicodedata.normalize("NFKC", cleaned)
    # Drop punctuation (keep alphanumerics + CJK)
    return re.sub(r"[^\w一-鿿]+", " ", cleaned)


def _char_ngrams(text: str, n: int) -> set[str]:
    """CJK (and other non-ASCII) char-level n-grams. Whitespace
    runs are collapsed to a single space first to avoid counting
    layout."""
    compact = re.sub(r"\s+", "", text)
    if len(compact) < n:
        return set()
    return {compact[i : i + n] for i in range(len(compact) - n + 1)}


def _word_ngrams(text: str, n: int) -> set[str]:
    """ASCII word n-grams (case-insensitive)."""
    words = [w.lower() for w in text.split() if w.strip()]
    if len(words) < n:
        return set()
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def compute_anti_plagiarism_jaccard(
    pipeline_md: str,
    baseline_md: str,
    paper_language: str,
) -> float:
    """5-gram Jaccard with body-only stripping. CJK uses character
    5-grams; en/other uses word 5-grams (codex Q3 amendment)."""
    pipeline_body = _strip_for_anti_plagiarism(pipeline_md)
    baseline_body = _strip_for_anti_plagiarism(baseline_md)
    if paper_language in ("zh", "ja"):
        p_grams = _char_ngrams(pipeline_body, 5)
        b_grams = _char_ngrams(baseline_body, 5)
    else:
        p_grams = _word_ngrams(pipeline_body, 5)
        b_grams = _word_ngrams(baseline_body, 5)
    if not p_grams or not b_grams:
        return 0.0
    return len(p_grams & b_grams) / max(1, len(p_grams | b_grams))


def is_anti_plagiarism_violation(
    pipeline_md: str,
    baseline_md: str,
    paper_language: str,
    threshold: float = ANTI_PLAGIARISM_JACCARD_THRESHOLD,
) -> bool:
    """True ⇒ rewrite output is too close to baseline → reject."""
    return (
        compute_anti_plagiarism_jaccard(
            pipeline_md=pipeline_md,
            baseline_md=baseline_md,
            paper_language=paper_language,
        )
        > threshold
    )


# ----- pass / fail accounting -------------------------------------


def evaluate_pass_margin(
    pipeline: QualityScoreSet,
    baseline: QualityScoreSet,
    margin: float = DEFAULT_PASS_MARGIN,
) -> tuple[bool, list[str]]:
    """Compare pipeline scores against baseline scores.

    Normal dims pass when ``pipeline >= baseline + margin``. Saturated
    baseline dims (10.0) pass when ``pipeline >= baseline`` because the
    0-10 rubric makes ``baseline + margin`` unreachable.

    Returns ``(pass, failed_dims)``. ``pass`` True ⇒ all 3 dims
    cleared the margin. ``failed_dims`` is the subset that didn't.
    """
    failed: list[str] = []
    for dim in ("compliance", "novelty", "completeness"):
        baseline_score = baseline.get(dim)
        required_score = baseline_score if baseline_score >= 10.0 else baseline_score + margin
        if pipeline.get(dim) < required_score:
            failed.append(dim)
    return (not failed), failed


def quality_scores_dict(scores: QualityScoreSet | None) -> dict[str, object] | None:
    if scores is None:
        return None
    return scores.dict()


# ----- v3 paired blind A/B critique schema ------------------------


class _PairedCandidateReport(BaseModel):
    """One candidate report from the compact v3 critic output."""

    candidate_id: StrictStr = ""
    scores: QualityScoreSet
    deduction_ledger: list[Any] = Field(default_factory=list)
    repair_plan_to_full_score: list[Any] = Field(default_factory=list)
    schema_partial_fields: list[StrictStr] = Field(default_factory=list)

    @root_validator(pre=True)
    def _default_optional_fields(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        expected_defaults: dict[str, object] = {
            "candidate_id": "",
            "deduction_ledger": [],
            "repair_plan_to_full_score": [],
        }
        missing = [key for key in expected_defaults if key not in values or values.get(key) is None]
        merged = dict(values)
        for key, default in expected_defaults.items():
            if key not in merged or merged.get(key) is None:
                merged[key] = default
        existing = merged.get("schema_partial_fields")
        if isinstance(existing, list):
            merged["schema_partial_fields"] = [
                str(item) for item in [*existing, *missing] if str(item)
            ]
        else:
            merged["schema_partial_fields"] = missing
        return merged

    class Config:
        extra = "allow"


class _PolishCritiqueOutput(BaseModel):
    """Compact v3 single-manuscript critique response.

    Backward-compatible flat keys are accepted so older fixtures and
    corrective retries can still be projected into candidate_reports.
    """

    candidate_reports: list[_PairedCandidateReport] = Field(default_factory=list)
    schema_partial_fields: list[StrictStr] = Field(default_factory=list)

    @root_validator(pre=True)
    def _default_v3_fields(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        merged = dict(values)
        missing: list[str] = []
        if "candidate_reports" not in merged and any(
            key in merged
            for key in (
                "a_compliance",
                "a_novelty",
                "a_completeness",
                "a_evidence_strength",
                "b_compliance",
                "b_novelty",
                "b_completeness",
                "b_evidence_strength",
            )
        ):
            merged["candidate_reports"] = [
                {
                    "candidate_id": "A",
                    "scores": {
                        "compliance": merged.get("a_compliance", 0.0),
                        "novelty": merged.get("a_novelty", 0.0),
                        "completeness": merged.get("a_completeness", 0.0),
                        "evidence_strength": merged.get("a_evidence_strength", 0.0),
                        "compliance_justification": merged.get(
                            "a_compliance_justification",
                            "",
                        ),
                        "novelty_justification": merged.get(
                            "a_novelty_justification",
                            "",
                        ),
                        "completeness_justification": merged.get(
                            "a_completeness_justification",
                            "",
                        ),
                        "evidence_strength_justification": merged.get(
                            "a_evidence_strength_justification",
                            "",
                        ),
                    },
                },
                {
                    "candidate_id": "B",
                    "scores": {
                        "compliance": merged.get("b_compliance", 0.0),
                        "novelty": merged.get("b_novelty", 0.0),
                        "completeness": merged.get("b_completeness", 0.0),
                        "evidence_strength": merged.get("b_evidence_strength", 0.0),
                        "compliance_justification": merged.get(
                            "b_compliance_justification",
                            "",
                        ),
                        "novelty_justification": merged.get(
                            "b_novelty_justification",
                            "",
                        ),
                        "completeness_justification": merged.get(
                            "b_completeness_justification",
                            "",
                        ),
                        "evidence_strength_justification": merged.get(
                            "b_evidence_strength_justification",
                            "",
                        ),
                    },
                },
            ]
            missing.append("candidate_reports:converted_from_flat_schema")
        expected_defaults: dict[str, object] = {
            "candidate_reports": [],
        }
        for key, default in expected_defaults.items():
            if key not in merged or merged.get(key) is None:
                merged[key] = default
                missing.append(key)
        if not isinstance(merged.get("candidate_reports"), list):
            merged["candidate_reports"] = []
            missing.append("candidate_reports:not_list")
        existing = merged.get("schema_partial_fields")
        if isinstance(existing, list):
            merged["schema_partial_fields"] = [
                str(item) for item in [*existing, *missing] if str(item)
            ]
        else:
            merged["schema_partial_fields"] = missing
        return merged

    class Config:
        extra = "allow"


def _candidate_report_from_letter(
    parsed: _PolishCritiqueOutput,
    letter: str,
) -> _PairedCandidateReport:
    wanted = letter.upper()
    for report in parsed.candidate_reports:
        if str(report.candidate_id).upper() == wanted:
            return report
    return _PairedCandidateReport(candidate_id=wanted, scores=QualityScoreSet())


def _scores_from_letter(parsed: _PolishCritiqueOutput, letter: str) -> QualityScoreSet:
    """Project the A or B side of a v3 critique output into scores."""
    return _candidate_report_from_letter(parsed, letter).scores


__all__ = [
    "ANTI_PLAGIARISM_JACCARD_THRESHOLD",
    "CONTROLLED_POLISH_EXPERT_PROMPT",
    "CONTROLLED_POLISH_EXPERT_V2_SYSTEM_PROMPT",
    "CONTROLLED_POLISH_EXPERT_V2_USER_TEMPLATE",
    "CONTROLLED_POLISH_MAX_ACCEPTED_REWRITES",
    "CONTROLLED_POLISH_MAX_ATTEMPTS",
    "CRITIC_LOOP_ACTIVE_DIMS",
    "DEFAULT_PASS_MARGIN",
    "POLISH_BLIND_EVAL_SYSTEM_PROMPT",
    "POLISH_BLIND_EVAL_USER_TEMPLATE",
    "BaselineMode",
    "ExpertCritiqueOutput",
    "PolishLoopResult",
    "PolishStatus",
    "QualityDim",
    "QualityScoreSet",
    "RevisionIssueType",
    "RevisionItem",
    "RevisionSeverity",
    "_PolishCritiqueOutput",
    "_alias",
    "_candidate_report_from_letter",
    "_scores_from_letter",
    "_strip_for_anti_plagiarism",
    "compute_anti_plagiarism_jaccard",
    "evaluate_pass_margin",
    "is_anti_plagiarism_violation",
    "manuscript_eval_metadata",
    "quality_scores_dict",
]
