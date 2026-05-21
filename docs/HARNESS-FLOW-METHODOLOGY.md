# Harness Flow Methodology — appleseed-autoessay 对齐 appleseed-flow 路线

> **PR-D1 摸底文档**。本文是 harness 升级路线（PR-D 系列）的第一步，纯 fact-finding，无代码改动。
>
> codex 立场：AGREE-with-amendments（2026-05-03）。修订项全部消化。

## 1. 背景与北极星定位

`appleseed-autoessay` 当前是一套 11 阶段（proposal + 10 个 rerunnable phase）的多步 LLM 学术论文生成流水线。我们的姊妹项目 `appleseed-flow`（同 owner `dddabtc`）专注于"让 LLM 可靠地跑确定性流程"，给出了 8 条原则 P0–P7。其核心命题：

> "LLMs are probabilistic. Procedures are deterministic."
>
> "The LLM's capability ceiling is determined by the model; its reliability floor is determined by the harness's correctness."

我们的 11-phase 流水线本质就是一份 SOP，每个 phase 内部又是若干步 LLM 调用 + 工具调用。`backend/src/autoessay/harness/` 已经有 `HookRegistry` / `runner.py` / `validator.py` / `audit.py`，但 hook 注册的覆盖与强制性远未达到 appleseed-flow 标准。

### 1.1 北极星（HANDOFF §0.1）

> 让系统生成的论文，能成为可投中文文科顶刊的稿件。

这是 aspirational target，不是当前能力。当前定位：**evidence-grounded draft accelerator for an expert author who can iterate**。

### 1.2 北极星阶段性达成判据（4 条 — 与 codex 共识）

1. **5 个 paper_mode 全部 `available`**：empirical / theory_article / comparative_study / review_article / theory_review。当前仅 `case_analysis` 是 available；其余两个是 `developer_preview` 或 `coming_soon`。
2. **PR-D4 baseline + acceptance gate 跑通**：5 篇 frozen run artifact bundle + manuscript + evaluator vector + 用户确认依据。**用户确认前只能叫 `baseline_candidate`，CI gate 仅用 `baseline_confirmed`**。没有用户确认 → 北极星只能 candidate，不能宣布达成。
3. **5 类创新源每类至少 1 个 paper_mode 真跑出 demo 论文**：新材料 / 新视角（必须 framework_lens LLM enrichment 后）/ 新方法 / 新问题 / 新论证。**不接受纯 stub lens**。
4. **real-paper baseline 表完整**：每条 SHA / provider / 耗时 / 字节数 / artifact 完整性 / integrity P0 数；`lens-display.spec.ts` + `theory_article-mode-walk.spec.ts` + `comparative-walk.spec.ts` + `review-walk.spec.ts` + `theory-review-walk.spec.ts` 5 个 e2e spec 全部 real-paper config 真跑通。

D3 若被写进"4-hook 完整覆盖"叙述则必须完成；D5 不阻塞。

### 1.3 real-paper "跑通" 严判据（codex 共识）

每次 real-paper 必须满足：

- manuscript ≥ 25k bytes hard；≥30k bytes 为 warning target（历史 baseline 30,080，等多轮稳定 baseline 后再收紧）
- 8 种 export 格式齐（markdown / docx / html / bibtex / csl_json / 其余）
- terminal state 正常（不卡 RUNNING / FAILED_*）
- `integrity_summary.json` 0 P0
- case/empirical/comparative：≥ 5 个 evidence ledger 条目
- theory/theory_review：≥ 1 个真实 LLM framework_lens signal
- 无未解 FAILED_FIXABLE
- 耗时上限初期是 warning，非 hard fail（等 framework_lens LLM enrichment 后新 baseline 出来再收紧）

> PR-D2.2 已把字节数、export 数、blocking issue、ledger 条目等核心判据落进 `frontend/e2e/real-paper.spec.ts`；framework_lens 真实 LLM signal 仍等 C2c。

### 1.4 失败重试上限

real-paper 同一 failure class 最多 2 次 full rerun，第三次必须先有代码 / 环境修复。

---

## 2. appleseed-flow P0–P7 八条原则逐条对照

| # | 原则 | 当前位置 | 差距 | 落地阶段 |
|---|---|---|---|---|
| **P0** | 确定性逻辑编译执行；LLM 只处理不可压缩的不确定性（语言理解 / 参数选择） | 🟡 部分对齐 | `state_machine.py` + `phase_readiness.py` 是确定性壳，但 agent 内部"该跑哪一步"仍由 LLM 暗示；framework_lens 当前是纯 stub，无 LLM 也无任何确定性约束 | D2 / C2c / D3 |
| **P1** | 每个 step 走统一的 4 步链：inject context → call LLM → audit output → reject + retry / execute | ✅ phase agent 对齐 | PR-D2.2 后 9 个 phase agent 非 stub 路径统一走 `run_llm_step` / `run_tool_step`；剩余 2 个 standalone caller 见 §5，进 PR-D2.5 AuditSink | D2.2 / D2.5 |
| **P2** | **4 个 hook 点必填**：pre_llm / post_llm / pre_tool / post_tool | ❌ 未对齐 | 详见 §4 矩阵 — 4-hook 完整覆盖 0/11；LLM 双 hook 覆盖 3/11；任一 modality 双 hook 覆盖 4/11 | D2（pre/post_llm）/ D3（pre/post_tool） |
| **P3** | telescope 评测：smoke(5) → poc(20) → full(N) 阶梯式投入 | 🟡 部分 — 文科场景不照搬 | 已有 stub smoke + manual real-paper 各 1 套，但**缺 staged baseline suite**（"5 篇用户已确认达标"还没建） | D4 |
| **P4** | acceptance 必须超过 baseline+threshold 才合并 | ❌ 未对齐 | 当前没有 baseline 评分，全靠用户 review；CI 4 个 job 均不阻断"agent 输出质量回归" | D4 |
| **P5** | 实验产物固定结构（design.md / plan.md / thoughts.md / report.md / current_status.md） | 🟡 部分 | `docs/audit/state-machine-audit-2026-05-03.md` 是这种产物，但仅 audit 一次性写过；尚未做成"每次大改都按此 structure 落 docs/refactor/<topic>/" | 持续 |
| **P6** | CI 自动检查架构合规 | 🟡 部分 | `scripts/lint_harness_callers.py` 已进 PR backend job；hook 必填 lint 仍未做 | D2.2 / D3 |
| **P7** | YAML orchestration（autoresearch by YAML），不靠 ad-hoc Python loop | ❌ 不对齐 | `phase_rerun.py::PHASES` 元组是硬编码 Python；可但不必照搬 | D5（可选） |

---

## 3. `backend/src/autoessay/harness/` 现状清单

### 3.1 文件与 LOC（HEAD `e616ecc2`）

| 文件 | LOC | 职责 |
|---|---|---|
| `__init__.py` | 39 | 重导出（`AuditWriter` / `HookContext` / `HookRegistry` / `LLMCallRequest` / `run_llm_step` / `run_tool_step` / `hash_request` / `hash_text` 等） |
| `hooks.py` | 109 | `HookRegistry` 容器：4 种 register + iter 方法 |
| `runner.py` | 723 | hub：`run_llm_step` / `run_tool_step` + sentinel 类（`SchemaViolationError` / `ToolInvocationError` 都在这里）|
| `audit.py` | 486 | `AuditWriter`：DB + jsonl 双写审计；公开方法见 §3.3 |
| `validator.py` | 125 | schema 校验工具（`validate_response` 等） |
| `sentinels.py` | 189 | corrective-suffix retry 机制 + sentinel 注入辅助 |
| `dedup.py` | 638 | `DrafterLocalDedupHook`：post_llm hook 实现 |
| `types.py` | 93 | dataclass：`HookContext` / `HookResult` / `LLMCallRequest` / `LLMCallResponse` / `ToolCallRequest` 等 |

### 3.2 hooks.py — `HookRegistry` 公开 API

```python
class HookRegistry:
    def register_pre_llm(self, name: str, fn: PreHook) -> None
    def register_post_llm(self, name: str, fn: PostLLMHook) -> None
    def register_pre_tool(self, name: str, fn: PreHook) -> None
    def register_post_tool(self, name: str, fn: PostToolHook) -> None
    # iter_pre_llm / iter_post_llm / iter_pre_tool / iter_post_tool 用于 runner.py 遍历
```

注册函数都是 `(name, fn) -> None`：name 用于 audit 记录。

### 3.3 audit.py — `AuditWriter` 公开 API（修正版）

```python
class AuditWriter:
    def __init__(self, *, session, run_dir, agent_name, ...)
    @property
    def agent_invocation_id(self) -> str | None
    def start_invocation(self, ctx: HookContext) -> AgentInvocation
    def record_pending(...)            # LLM call 开始时记
    def record_tool_pending(...)       # tool call 开始时记
    def finish_attempt(...)            # LLM call 完成时记
    def finish_tool_attempt(...)       # tool call 完成时记
    def finish_invocation(...)         # 整个 agent 跑完时记
```

**注意**：HANDOFF / 历史 PR 描述里偶尔出现的 `write_record` / `verdict_for` / `make_audit_post_llm_hook` 等名称**不存在**；以本表为准。

### 3.4 Harness opt-in flag 清理状态

PR-D2.2 删除了 9 个 phase 的 harness opt-in 配置项。当前没有旧 harness
opt-in env，也没有对应的 `settings` dispatch。

| 类别 | 当前状态 |
|---|---|
| phase agent LLM path | 非 stub 默认走 `harness.run_llm_step` |
| integrity vendor path | 非 stub 默认走 `harness.run_tool_step` |
| stub 控制 | 仍由 `AUTOESSAY_<PHASE>_STUB` 控制；CI/stub e2e 继续用这些 flag |
| rollback | 通过 phase stub、revert 或后续 hotfix；不保留 direct LLM fallback |

这意味着 hub 已是唯一 LLM/tool agent 入口；新增 LLM caller 必须通过 §5.1 的
架构 lint。

---

## 4. 4-hook × 11-phase 覆盖矩阵（PR-D2.2 后）

数据来自 `grep -rn "register_pre_llm\|register_post_llm\|register_pre_tool\|register_post_tool" backend/src/autoessay/`。

| phase | pre_llm | post_llm | pre_tool | post_tool | 注 |
|---|---|---|---|---|---|
| proposal | 🟡 memory_read（条件注册）| ❌ | ❌ | ❌ | `proposal.py:381` |
| scout | 🟡 memory_read（条件注册）| ❌ | ❌ | ❌ | `scout.py:538` |
| curator | 🟡 memory_read（条件注册）| ❌ | ❌ | ❌ | `curator.py:938` |
| synthesizer | 🟡 memory_read（条件注册）| ❌ | ❌ | ❌ | `synthesizer.py:639` |
| framework_lens | ❌ | ❌ | ❌ | ❌ | 当前是 deterministic stub，无 LLM 调用，无 hook 注册 — C2c LLM enrichment 时补 |
| ideator | 🟡 memory_read（条件注册）| ❌ | ❌ | ❌ | `ideator.py:459` |
| drafter | 🟡 memory_read（条件） | ✅ sentinels + dedup | ❌ | ❌ | `drafter.py:282 / 293 / 967` |
| stylist | ✅ multiple | ✅ multiple | ❌ | ❌ | `stylist.py:840 / 848 / 852 / 856 / 860 / 990`（最完整） |
| critic | 🟡 memory_read（条件）| ✅ tool-call audit | ❌ | ❌ | `critic.py:415 / 493` |
| integrity | ❌ | ❌ | ✅ | ✅ | `integrity.py:520 / 524`（唯一有 pre_tool / post_tool） |
| exports | n/a | n/a | ❌ | ❌ | exporter 无 LLM 调用，纯 disk 操作；走 D3 时是否纳入 tool 化要先定边界 |

**🟡 标记说明**：`memory_read` 是 `make_memory_pre_llm_hook` 的注册，但**条件触发** —— 仅当 memory client 存在且配置启用时才挂 hook。**默认配置下不能算"已有 pre_llm"**；要算 P2 是否覆盖时只能算 ❌。

### 4.1 完整覆盖统计

- 4-hook 完整覆盖（pre_llm + post_llm + pre_tool + post_tool）：**0/11 phase**
- LLM 双 hook 覆盖（pre_llm + post_llm，不算 memory_read 条件注册）：**3/11**（drafter / stylist / critic）
- 任一 modality 双 hook 覆盖（LLM 双 hook 或 tool 双 hook）：**4/11**（再加 integrity 的 pre_tool / post_tool）

PR-D2.2 已关闭 P1 的 direct-path 岔路：非 stub phase agent 现在默认经
`run_llm_step` / `run_tool_step`。本表继续跟踪 P2 的显式 hook 覆盖；要达到
P2 的"4 hook 必填"，10/11 phase（exports 除外，它无 LLM）仍要补齐——这是
D3 及后续 hook 完整化的工作量。

---

## 5. 直接 `client.chat_completion(` 调用清单（blocklist）

allowlist：`harness/runner.py`、`llm_client.py` 自身、`tests/` 下的 fakes。
PR-D2.2 后 blocklist 内直接调用从 **14 处降到 2 处**。

| # | 文件 | 类别 | 当前处理 |
|---|---|---|---|
| 1 | `safety/input_guard.py` | safety filter | 临时 allowlist；PR-D2.5 迁到 AuditSink |
| 2 | `stop_slop/score.py` | stylist sub-call | 临时 allowlist；PR-D2.5 迁到 AuditSink |

9 个 phase agent 的 legacy direct branch 已删除；4 个 sub-call
（material_diagnostic / self_check / detailed_outline / manuscript_compose）已走 hub。
`agents/integrity.py` 走 `run_tool_step`。
`agents/framework_lens.py` 不在此列（当前无 LLM 调用，C2c 时补）。
`agents/exporter.py` / `agents/research_role_classifier.py` / `agents/literature_usage.py` / `agents/_language.py` 不在此列（无 LLM 调用）。

### 5.1 CI 架构 lint 设计草案（D2c 范围）

不能只 grep `client.chat_completion(`（变量重命名就漏）。建议：

- **执行入口**：`scripts/lint_harness_callers.py`，PR `pr.yml` 的 backend job 跑 `python scripts/lint_harness_callers.py`。
- **实现**：AST lint（`ast.parse` + `NodeVisitor` 找 `Attribute(attr='chat_completion')`）。
- **当前 allowlist**：`harness/runner.py` / `llm_client.py` / `safety/input_guard.py` / `stop_slop/score.py`。后两项必须在 PR-D2.5 移除。

---

## 6. PR-D 路线（细化版，与 codex 共识）

### 6.1 PR-D1 — 本文档

✅ 范围：本 `docs/HARNESS-FLOW-METHODOLOGY.md` + 关联 HANDOFF.md / CHANGELOG.md 调整。
✅ 验证：`(cd frontend && npm run typecheck)` + `ruff format --check backend/`（仅防文档相对路径错；纯文档无需 real-paper）。
✅ 落地后 tag `milestone-harness-d1`。

### 6.2 PR-D2 — `run_llm_step` 强制化

**状态（PR-D2.2）**：phase-agent legacy direct path 已删除；架构 lint 进
`pr.yml`；allowlist 外 direct `\.chat_completion\(` 为 0。剩余 direct caller 是
PR-D2.5 的 `safety/input_guard.py` 和 `stop_slop/score.py`。

**子阶段（同一 PR 内的阶段性 commit，不分别 merge）**：

- **D2a**：6 个 strict-JSON agent + 4 个 sub-call 全部走 `run_llm_step`（proposal / scout / curator / synthesizer / ideator / critic / detailed_outline / manuscript_compose / material_diagnostic / self_check）+ 注册 `pre_llm` / `post_llm` 强校验 hook
- **D2b**：drafter / stylist 主调用走 `run_llm_step`；safety/input_guard + stop_slop/score 留 D2.5 AuditSink
- **D2c**：删除 9 个 phase harness opt-in flag + legacy 分支；`scripts/lint_harness_callers.py` 进 CI；`pr.yml` 的 backend job 跑此 lint
- **D2d**：tests 里大量基于 flag/legacy 直连的 invariant 测试同步重写或删；harness 单测扩展（每个 agent 至少 1 个 hook 注册路径覆盖测试）

**验证**：
- full backend pytest（数量 575+，预计 D2 后增至 ~600）
- ruff / mypy / playwright 默认 stub config 全绿
- **本地跑 `playwright.real.config.ts e2e/real-paper.spec.ts` 真打 LLM**：必须满足 §1.3 严判据（≥25k bytes hard / ≥30k warning / 8 export / 0 P0 / ≥5 ledger / 无未解 FAILED_FIXABLE / 耗时 warning）

**落地后 tag `milestone-harness-d2`**。

### 6.3 PR-C2c — framework_lens LLM enrichment

**为什么提前到 D2 后 / D4 前**：当前 framework_lens 是 deterministic stub（`framework_lens.py::compose_framework_lens` 直接调 `_stub_signals`），不打 LLM。北极星判据 #3 "5 类创新源每类至少 1 个 demo" 里"新视角（理论镜框）"必须 framework_lens LLM enrichment 后才能算真实 LLM 验证。否则 D4 baseline freeze 时把 stub artifact 写进 gate，gate 永远在 stub 上 happy-path。

**范围**（C2c PR 时再深化设计）：
- `compose_framework_lens` 增加 LLM enrichment pass：基于 `paper_mode` + `synthesizer.json` 4-track partition + shortlist 的 `theoretical_lens` 类源，让 LLM 生成 framework_lens signals（不再 fall back stub）
- 注册 `pre_llm` + `post_llm` hook（D2 后所有调用必经 hub）
- 新 stub flag 行为：`AUTOESSAY_FRAMEWORK_LENS_STUB=1` 时仍走旧确定性路径（CI 用）；默认走 LLM
- e2e：`lens-display.spec.ts`（stub config）+ real-paper 跑出 ≥1 真实 lens signal

**落地后 tag `milestone-lens-llm`**。

### 6.4 PR-D4 — baseline + acceptance gate

**完成定义**：
- `evaluate_paper.py` 评分器：FR-7 integrity + Stop-Slop + claim density + citation diff vector
- CI `acceptance.yml`：每个改动 agent 行为的 PR 必须跑一遍 baselines；vector 不能跌破阈值
- 5 篇 baseline 论文按选项 B 推进：
  - 自动跑 5-10 篇 demo（每个 paper_mode × 1-2 篇），存 frozen run artifact bundle
  - **命名严格**：仅 `baseline_candidate`
  - **CI gate 仅用 `baseline_confirmed`**（用户确认升级后才 blocking）
  - skeleton 阶段所有 candidate 仅产 advisory 报告，不阻断 PR

**落地后 tag `milestone-acceptance-gate`**（即使全部 candidate 仍未 confirmed，gate skeleton 跑通即可打 tag）。

### 6.5 PR-D3 — `harness/tools.py` + pre_tool / post_tool 完整化

**边界（codex 提示先定）**：
- ✅ `framework_lens.py`、`exporter.py`、各 agent `_write_*`：纳入 tool 化
- ❓ `harness/audit.py` / `harness/dedup.py`：是否也 tool 化？默认 NO（它们是 hook 实现而非 phase artifact 写入），D3 设计 memo 时再决
- 抽 `harness/tools.py`：`write_artifact(path, content, kind)` / `read_artifact` / `list_artifacts`
- pre_tool 校验：路径在 run_dir 内；kind 与扩展名匹配；前置依赖检查
- post_tool 校验：写入后 stat + 内容校验；触发 phase_artifact 行写入

**验证**：full backend pytest + real-paper（确认 phase artifact 写入仍齐全）。**落地后 tag `milestone-harness-d3`**。

### 6.6 PR-D5 — YAML orchestration（可选）

不阻塞北极星。`flows/<mode>.yaml` 取代 `phase_rerun.py::PHASES` 元组；paper_modes registry 通过 YAML flow 选择。

---

## 7. 路线依赖图（D 系列 + C 系列耦合点）

```
D1 (本文) ── tag milestone-harness-d1
   │
   ▼
D2 (LLM hub 强制 + flag 移除 + lint) ── tag milestone-harness-d2
   │
   ├──► E1 (lens-display e2e, stub) ──┐
   ├──► G1 (lens 版本归属修正)         │
   │                                   │
   ▼                                   │
C2c (framework_lens LLM enrichment) ── tag milestone-lens-llm
   │                                   │
   ▼                                   │
D4 (baseline + acceptance gate) ── tag milestone-acceptance-gate
   │                                   │
   ├──► C3 (9-tension taxonomy) ──── tag milestone-tension
   │     │
   │     ├──► E2 (theory_article-mode-walk real)
   │     ├──► F1 (Lens 编辑 affordance)
   │     ├──► E3 / E4 / E5 (剩余 e2e)
   │     │
   │     ▼
   │  empirical + theory_article 升级 available
   │     │
   │     ▼
   │  ── tag milestone-modes-2of5-available
   │     │
   │     ▼
   │  C4 (comparative_study) ── tag milestone-comparative
   │     │
   │     ▼
   │  C5 (review_article) ── tag milestone-review
   │     │
   │     ▼
   │  C5b (theory_review) ── tag milestone-theory-review
   │     │
   │     ▼
   │  ── tag north-star-stage-1
   │
   ▼
D3 (4-hook 完整覆盖) ── tag milestone-harness-d3
   │
   ▼
D5 (YAML, 可选)
```

> ℹ️ 上图保留 D 系列为主干。PR-C 系列的具体设计（C3 张力分类 9 类细节、C4 comparative scaffold、C5 综述章节计划等）不在本文档范围，落到各自 PR 的设计 memo 里。

---

## 8. 风险与未决项

1. **D4 baseline 5 篇取证**：当前仓库 / 历史记录里没有"用户已确认达标"的明确证据。选项 B（自动跑 candidate + 等用户确认）是 reviewer 共识的方案，但用户在"全自动到北极星"指令下不再被中间打扰；最终 confirm 一定要等用户回头时统一处理。期间 D4 gate 仅 advisory 不 blocking。

2. **`real-paper.spec.ts` 判据已收紧，仍需真实跑通取证**：PR-D2.2 已把 §1.3 的核心判据落进 spec（≥25k bytes hard / ≥30k warning / 8 export / 0 P0 / ≥5 ledger / 无未解 FAILED_FIXABLE / 耗时 warning）。D2 完成前仍需本地 real-paper 真打 LLM 记录 metric。

3. **D3 tool 化边界**：`framework_lens.py` / `exporter.py` / 各 agent `_write_*` 必须纳入。`harness/audit.py` / `harness/dedup.py` 是否纳入需 D3 设计 memo 时定。`exports` phase 是否 tool 化（它本身就是导出 → 文件）也要在 D3 时单独决。

4. **memory hooks 是条件注册**：`make_memory_pre_llm_hook` 仅在 memory client 存在且配置启用时挂 hook。默认运行时不能算"已有 pre_llm"。D2 验证时 P2 覆盖率统计要排除 memory_read 条件注册。

5. **D2 后 tests 大量重写**：PR-D2.2 已把基于 harness opt-in flag 与 legacy 直连分支的 invariant 测试改为 hub-only 断言。后续新增测试应直接验证 hub artifact、hook、retry 与 audit 行为。

6. **C2c 的 LLM enrichment prompt + schema**：framework_lens LLM 增强后输出的 signals schema 需向 ideator / drafter 兼容，并保留 PR-G1 的单所有权边界：新 lens artifact 写 `schema_version=2` + `synthesizer_input_ref`，不得再写 `synthesis/synthesizer.json::framework_lens_summary_ref`。C2c 与 G1 顺序耦合：G1 必须在 C2c 之前 / 同 PR 落地。

7. **real-paper 真打 LLM 成本**：每次 ~30K tokens × 3 provider × 重试。D2 / C2c / D4 / C3 / E2 / F1 / C4 / C5 / C5b 各至少 1 次。预估 9-12 次跑到北极星（包含偶发 failure rerun）。

8. **不要把 PR-C 详细设计塞进本文档**（codex 强调）。本文是 harness methodology fact-finding；C 系列具体 schema / prompt / agent 行为留到各自 PR 的设计 memo。

---

## 9. 接手须知（短）

- 本文是 PR-D 系列的 fact-finding 基线。**改动 harness / 加 hook / 迁移 caller 之前先回来读 §3-§5**。
- 数据冻结于 HEAD `e616ecc2` (2026-05-03)。每次 D 系列 PR 落地后**必须刷新 §4-§5 的矩阵和清单**（HANDOFF.md §10.x 也同步刷新）。
- §6 路线随 codex 共识演化；下一个 PR 的具体范围以那个 PR 的设计 memo + codex AGREE 为准。
- 北极星判据（§1.2）改动需用户确认。

---

## 10. 文档维护

- 每个 PR-D 落地后：
  - 更新 §3.4 / §4 / §5 的事实数据
  - 在 §6 对应小节加"已落地（PR #N，YYYY-MM-DD）"标
  - HANDOFF.md §14 / §16 同步刷
  - memory `project_appleseed_state.md` 同步刷
  - 打 tag `milestone-<name>`

- 用户回头确认 baseline 时：升级 §1.2 #2 表述并刷新 §6.4。

---

参考：
- `appleseed-flow` README（P0–P7 八条原则源头）
- `docs/HANDOFF.md` §0 / §11 / §14 / §16
- `docs/DESIGN.md` §2.A（per-phase 版本状态机）
- `docs/audit/state-machine-audit-2026-05-03.md`
- `backend/src/autoessay/harness/` 实际代码（HEAD `e616ecc2`）
