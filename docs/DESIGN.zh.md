# 设计说明（appleseed-autoessay）

**语言：** [English](DESIGN.md) | 中文

## 1. 总体架构

```
+-------------+       HTTPS       +-------------------+        +-------------------+
|   浏览器    |  <------------->  |   reverse proxy   |  --->  |  frontend (Vite)  |
| (React/Vite)|                   +-------------------+        +-------------------+
+-------------+                            |                            |
                                           v                            v
                                    +--------------+           +-------------------+
                                    | OIDC IdP     |           |   api (FastAPI)   |
                                    | (generic)    |           |                   |
                                    +--------------+           +-------------------+
                                           |                            |  rq job
                                           v                            v
                                    +--------------+           +-------------------+
                                    | identity DB  |           |  worker (RQ)      |
                                    | (postgres)   |           |  + autoessay/...  |
                                    +--------------+           +-------------------+
                                                                       |
                                                                       v
                                                              +-----------------+
                                                              |  redis  | sqlite|
                                                              +-----------------+
                                                                       |
                                                                       v
                                                          外部 API:
                                                          - OpenAI-compatible
                                                            LLM gateway
                                                          - Literature metadata /
                                                            full-text providers
                                                          - Optional integrity /
                                                            originality provider
```

典型自托管 compose 服务：identity provider、identity database、`frontend`、`api`、`worker`、`redis`、`migrate`。`worker` 与 `migrate` 复用 api 镜像。

2 个容器镜像：`your-org/appleseed-autoessay-{api,frontend}`。CI 可在 push 到 main 时构建，并打 `${SHA}` 与 `:latest` 双 tag 推送。

## 2. 状态机

每个 run 维护一个 state（UPPER\_SNAKE\_CASE）。运行态在 `XXX_RUNNING` / `PROPOSAL_DRAFTING` / `REWRITE_RUNNING`，用户审阅态在 `USER_…_REVIEW`，例外有 `FAILED_FIXABLE` / `FAILED_NEEDS_USER` / `FAILED_VENDOR` / `FAILED_POLICY` / `CANCELLED`。

简化迁移图（仅展示主路径，`USER_*_REVIEW` 之间的回退由 phase rerun 实现）：

```
DOMAIN_LOADED
    |
    v
PROPOSAL_DRAFTING -> USER_PROPOSAL_REVIEW
    |                       |
    |                       v
    +------------------> SCOUT_RUNNING -> USER_SEARCH_REVIEW
                                |
                                v
                         CURATOR_RUNNING -> USER_DEEP_DIVE_REVIEW
                                |
                                v
                      SYNTHESIZER_RUNNING -> USER_FIELD_REVIEW
                                |
                                v
             TENSION_EXTRACTION_RUNNING -> USER_TENSION_REVIEW
                         |                    |
                         +--------------------+
                                |
                                v
                    FRAMEWORK_LENS_RUNNING -> USER_LENS_REVIEW
                         |                    |
                         +--------------------+
                                |
                                v
                        IDEATOR_RUNNING -> USER_NOVELTY_REVIEW
                                |
                                v
                        DRAFTER_RUNNING -> USER_REVISION_REVIEW
                                |
                       (stylist 可重跑；critic 入口会先跑 final_rewrite)
                                v
                 STYLIST_RUNNING -> USER_REVISION_REVIEW
                                |
                                v
                   REWRITE_RUNNING -> CRITIC_RUNNING
                                |
                                v
                  USER_EXTERNAL_SCAN_APPROVAL -> INTEGRITY_RUNNING
                                |
                                v
                        USER_FINAL_ACCEPTANCE -> EXPORTS_RUNNING -> DONE
```

## 2.A 阶段版本状态机（PR-A4 per-phase version model）

§2 描述 run-level 状态机；本节描述 PR-A4 系列引入的 per-phase 版本状态机。两套机器正交：run 处于 `DRAFTER_RUNNING` 时，drafter 卡片在已提交的 `RunHead` 上看到的 UI 状态独立于 run state。

### 2.A.1 5 条规则（来自用户 2026-05-02）

| # | 规则 | 实现锚点 |
|---|---|---|
| 1 | 每个 phase 独立版本号；不存在主项目版本号 | `phase_versions.version_no` 按 (run, phase) 单调递增（PR-A4.1a/b） |
| 2 | 改有下游的节点 → 强制写新版本 + 下游标"未生成" | `commit_phase_version` 总分配新 pv；`_cascade_phase_after_upstream_change` 删除下游 RunHead 行（找不到 lineage 全等候选时） |
| 3 | 改叶子节点 → replace 或 new；语义分两路 | **直接编辑产物**（modal / proposal save）走 `replace_phase_version` 或 user-edit 新版本，提交后**直接进入 `generated`**（不进入未生成态）。**改 prompt / context 的草稿**（`PhasePromptDraft`）在 LLM 重跑前让卡片显示 `prompt_edited` 态，重跑后才回到 `generated` |
| 4 | 已发布版本仅可反依赖顺序删除 | `delete_phase_version` 在 RunHead / `phase_version_inputs.upstream_pv_id` / `parent_pv_id` / `branches.forked_from_pv_id`（含软删）任一引用时拒绝 |
| 5 | 切版本 → 下游沿 lineage 自动级联，找不到则下游头清空 | `activate_version` 调 `_cascade_phase_after_upstream_change`；候选筛选用 `_lineage_matches`（**全等**，不是子集），找不到时 `DELETE FROM run_heads`（schema 上 `version_id` 是 NOT NULL，所以是删行不是置 NULL） |

### 2.A.2 4 个 UI 状态（来自 3 flag 计算）

后端 `GET /api/runs/{id}/phase-history` 返回每 phase 三个 flag；前端 `deriveCardState`（`frontend/src/lib/phaseHistoryState.ts`）把它们坍缩成 4 个用户可见状态：

| state | head_missing | prompt_dirty | lineage_dirty |
|---|---|---|---|
| `ungenerated` | true | * | * |
| `prompt_edited` | false | true | * |
| `upstream_superseded` | false | false | true |
| `generated` | false | false | false |

`*` = 不关心；`head_missing` 优先级最高，`prompt_dirty` 次之（覆盖 `lineage_dirty`，因为撤销 prompt 编辑后才能讨论 lineage 是否仍 stale）。当 `prompt_edited` + `lineage_dirty` 同时成立时 modal 只显示 cancel/regenerate 主操作 + 一条独立 advisory（不显示 `activate_lineage_match`）。

3 个 flag 的精确定义（`backend/src/autoessay/phase_history.py`）：
- `head_missing`: (run, branch, phase) 没有 `RunHead` 行。
- `prompt_dirty`: 至少一条 `PhasePromptDraft` 的 `content_hash` 与当前 head pv 的 `PhaseVersionPrompt.content_hash`（同 prompt_key）不同。head 没有该 key 但 draft 存在也算 dirty。
- `lineage_dirty`: head pv 的 `phase_version_inputs.upstream_pv_id` 与当前 upstream `RunHead.version_id` 不再相等（任一上游 phase 不匹配即 dirty）。

**重要：phase 卡片状态来自已提交的 `RunHead`，不是 in-flight pv 的 status。** 首跑时卡片保持 `ungenerated` 直到 `commit_phase_version` 写 RunHead；rerun 时旧 head 保持 `generated`，直到新 pv 提交（成功）或失败（旧 head 不动）。

### 2.A.3 操作 → 状态转移

按"修改图"分两组。前组改 phase 版本 / head 指针；后组改 prompt 草稿。

**Version / head 操作（4 个）**：

| 操作 | 端点 | 主要影响 |
|---|---|---|
| `rerun` | `POST /api/runs/{id}/phases/{phase}/rerun` 或 `POST /api/runs/{id}/{phase}` (start_*) | 总写新 pv；新 head 指向新版本；下游级联（找到 lineage 全等候选则切到该候选；否则下游 head 删除） |
| `activate` | `POST /api/runs/{id}/phases/{phase}/versions/{pv}/activate`，及 `POST /api/runs/{id}/phases/{phase}/versions/activate-lineage-match`（A4.4 加，自动找匹配候选） | head 指针改向已存在 pv；下游级联同 rerun |
| `delete` | `DELETE /api/runs/{id}/phases/{phase}/versions/{pv}` | 反依赖检查通过后删除（`phase_versions` 无 `deleted_at` 字段，硬删），同步清理 `phase_version_prompts` / `artifacts_v2` / `phase_version_inputs` 行 + 归档目录 |
| `fork` | `POST /api/runs/{id}/branches`（base_pv_id 必为 `done` 状态） | 创建新分支根于指定 pv；新分支下游 phase 走 `reachable_pv_ids_for_branch`（含 `forked_from_pv_id` 种子）匹配 lineage |

**Prompt-draft 操作（2 个，A4.4 完整闭环）**：

| 操作 | 端点 | 影响 |
|---|---|---|
| `save prompt draft` | `PUT /api/runs/{id}/phases/{phase}/prompt` | 写 / 更新 `PhasePromptDraft` 行；下次该 phase 计算 `prompt_dirty` 为 true → 卡片切到 `prompt_edited` |
| `cancel prompt drafts (phase-wide)` | `DELETE /api/runs/{id}/phases/{phase}/prompts/drafts` | 删除该 phase 所有 draft 行；幂等。`prompt_dirty` 复评为 false → 卡片回到 `lineage_dirty` 决定的态（通常 `generated`） |

### 2.A.4 级联激活（rule 5 实现细节）

钻石 lineage 示例：

```
scout v1 ──┬──→ curator v1 ──→ synthesizer v1
           │
           └──→ ideator v1
```

切 scout 到不同版本后，`_cascade_phase_after_upstream_change` 按 `_PHASE_RUNNERS` 顺序处理每个下游 phase：

1. 计算当前上游头向量 `expected = {upstream_phase: RunHead.version_id, ...}`。
2. 在 `reachable_pv_ids_for_branch(branch)` 范围内（含 fork 来源继承）扫描该 phase 的 `done` 候选。
3. 用 `_lineage_matches(candidate.lineage, expected)` —— **必须 `lineage == expected` 全等**（不是子集），否则历史钻石分支会错误命中（codex 2026-05-02 修正）。
4. 多个匹配时取 `version_no` 最大；找不到则 `DELETE FROM run_heads WHERE (run, branch, phase) = ...`（`version_id` 列是 NOT NULL，所以删行而非置 NULL）。
5. 文件物化（purge legacy 路径 + restore 新 head 产物）**只对级联触及的下游 phase**执行；上游不动（PR-A4.3 收窄范围以保护 pre-migration vanilla 文件 / orphan upstream 产物）。

### 2.A.5 prompt 快照不可变

`phase_version_prompts` 是 per-pv 的 prompt 内容快照，主键为 `(phase_version_id, prompt_key)`；`source` 是 CHECK 约束的列（取值 `'default'` / `'override'`），不在主键里。**写入后只读**，`prompt_dirty` 检测以这些快照为基线。两条入口：

- agent run（`commit_phase_version`）：从 `_resolve_phase_prompts` 拿默认或覆盖结果，写一行 `source='default'` 或 `'override'`。
- user-edit version（`apply_phase_user_edit`）：PR-A4.2 修正 — 之前不写快照，导致 user-edit 版本被对照"无 prompt"会永远 `prompt_dirty`。现在 user-edit 也调 `_resolve_phase_prompts` 并写快照。

### 2.A.6 `runnable_now` 的判定

`PhaseHistoryEntry.runnable_now` 仅在 run 处于该 phase 的"可启动前置态"时为 true。后端用 `phase_rerun.PHASE_INPUT_STATES` 映射（如 `synthesizer` 的前置态是 `USER_DEEP_DIVE_REVIEW`），同时排除所有 `*_RUNNING` 态以防 double-start。前端 `derivePrimaryActions` 仅在 `runnable_now == true` 时才暴露 `run_now` 主操作。

### 2.A.7 `branches.stale_from_phase` 兼容字段

PR-A2 引入的 `branches.stale_from_phase` 单指针字段不再是 phase-history modal 的状态权威：本节描述的 4-状态计算 / 3-flag 推导才是。但**遗留路径仍在读写它**：

读取路径：
- `WorkspacePage.tsx::StaleBanner`（`run?.stale_from_phase` line ~1762）+ `BranchSwitcher` 分支条目末尾的 `●` 标记（line ~3975）。
- `WorkspacePage.tsx` ProposalSubview 的 `hasDraftRun` 计算（line ~1879，跨 modal 用作"是否有未确认的 draft 运行"信号）。
- `phase_user_edit.py::apply_phase_user_edit`（line ~227）调 `get_branch_stale()` 阻止编辑 stale phase 的下游产物。
- `phase_rerun.py`（line ~206）在重跑前置依赖排序时参考它。

写入路径：
- `set_branch_stale` 在 fork / replace / commit / cascade activate 等路径上仍写它，确保上述读路径有数据。

它是 **兼容路径上的源**而非 cache。彻底删除前需要先把上述 4 条读路径全切到 3-flag 计算（或 phase-history payload），可作为 PR-A5（或下次 schema 大改）的子目标。

`backend/src/autoessay/main.py` 中 `_PHASE_RUNNERS` 的实际顺序：

```
proposal → scout → curator → synthesizer → tension_extraction → framework_lens
→ ideator → drafter → stylist → final_rewrite → critic → integrity → exports
```

`tension_extraction` 默认关闭，关闭时主路径从 `synthesizer` 直接进入 `framework_lens` 或 `ideator`。`final_rewrite` 默认开启；当开启时，`POST /critic` 先进入 `REWRITE_RUNNING`，完成 polish loop + critic loop 后再进入 `CRITIC_RUNNING`。`exports` 阶段的模块文件名是 `agents/exporter.py`，但注册的 phase 名是 `exports`。

每个阶段的职责：

| 阶段 | 输入 | 输出 | 主要文件 |
|---|---|---|---|
| proposal | 项目题目 + 领域 | 选题方案 markdown | `proposal/proposal.md` |
| scout | 选题方案 | 候选文献 jsonl | `discovery/scout_report.md`、`discovery/skim_candidates.jsonl` |
| curator | 候选文献 | shortlist + 全文 | `sources/shortlist.json`、`sources/fulltext/*.pdf` |
| synthesizer | shortlist | 每条文献的 claim 列表 | `synthesis/claims.jsonl`、`synthesis/source_notes/*` |
| tension_extraction | claims | 张力结构（可选） | `synthesis/tension_extraction.json` |
| framework_lens | claims + proposal | 框架透镜信号 | `synthesis/framework_lens.json` |
| ideator | claims + proposal | 候选写作角度 | `novelty/angle_cards.json` |
| drafter | 选定角度 | 章节草稿 + claim\_map | `drafts/v???/manuscript.md`、`drafts/v???/claim_map.jsonl` |
| stylist | 草稿 | 经修订的草稿 | `drafts/v???/style/*` |
| final_rewrite | stylist 输出 | polish 后稿件 + critic-loop 选择稿 | `drafts/v???/polish/*`、`reviews/critic_loop.json` |
| critic | 草稿 | 评审报告 | `reviews/*` |
| integrity | 草稿 + claim\_map | 诚信报告 | `integrity/integrity_summary.json` |
| exports | 已通过的草稿 | 多格式成稿 | `exports/manifest.json`、`exports/*.pdf` 等 |

## 4. 持久化

四张关键表：

- `phase_versions`：每次 phase 成功执行（含 vanilla 首跑，PR-A4.1b 起）一行，记录 `run_id` / `phase` / `version_no` / `branch_id` / `input_snapshot_hash` / `prompt_hash` 等。
- `phase_version_prompts`：每个 phase\_version 的 prompt 快照，主键 `(phase_version_id, prompt_key)`；`source` 是 CHECK 列（取值 `'default'` / `'override'`）。详见 §2.A.5。
- `run_heads`：每个 (run, branch, phase) 的当前 head 指针。
- `branches`：分支元数据，包含 `parent_branch_id` / `forked_from_pv_id` / `is_active` / `stale_from_phase`（phase-history modal 不再以 `stale_from_phase` 为状态权威；遗留 StaleBanner / phase_rerun 仍读，详见 §2.A.7）。

文件物化路径仍以 `run.run_dir / <legacy_dir>` 为主（`scout/`、`sources/`、`synthesis/`、`drafts/v???/` 等）；切换 head 与 fork 时按 `phase_version` 重新挂载文件。

## 5. 提示词覆盖（Stage 2.B + 3.A）

`backend/src/autoessay/prompts.py` 的 `_REGISTRY` 是 `(phase, prompt_key) → PromptSpec` 的字典。当前 16 条：

| phase | 支持的 prompt_key |
|---|---|
| synthesizer | main |
| ideator | main |
| critic | main |
| drafter | main, introduction, historiography, sources-method, empirical-section-i, empirical-section-ii, empirical-section-iii, discussion, conclusion |
| stylist | main, repolish |
| curator | ranking |

每个 key 注册一段静态默认文本（`default_content`）。用户在 UI 编辑后保存到 `phase_prompt_drafts`。重跑时 `_resolve_phase_prompts` 把 (默认或覆盖) 的解析结果传给 agent，并写入 `phase_version_prompts` 作为版本快照。

API 形态：

- `GET /api/runs/{run_id}/phases/{phase}/prompt[?prompt_key=]`：返回默认+草稿；省略 prompt\_key 时若 `(phase, "main")` 不支持但有其他 key 则 fall back 到首个 key（discovery fallback，Stage 3.A.4）；显式空串 `?prompt_key=` 严格 404。
- `PUT .../prompt`：保存或删除当前 (phase, prompt\_key) 草稿。
- `POST .../rerun`：重跑该阶段，body 可带 `draft_hash` + `prompt_key` 做并发检查。

## 6. Memory 与 Hooks

LLM 调用走 `harness/run_llm_step`，支持 pre/post hook 链。常见 hook：

- `memory_pre_llm`（`autoessay.memory.make_memory_pre_llm_hook`）：从 `appleseed-memory` 读取相关记忆塞进 system message。
- `citation_whitelist`（drafter）：检查输出 claim\_map 中的 source\_id 都在 approved 集合内。
- `local_dedup`（drafter）：检查段落与本地语料的 n-gram 重叠度。
- `ngram_guard`（stylist）：检查修订后的段落是否抄袭了 prior-paper 的字面表达。
- audit writer：把请求/响应/parse 结果写到 `run_dir/audit/*.jsonl`。

## 7. 运行时防护（Stage 3.E follow-up）

一次运行时韧性审计暴露了两类问题：用户在缺少前置选择时启动下游 phase，以及部分段落 schema 校验失败时整段 drafter phase 被判失败。后续整改把这些场景收敛到更明确的 readiness、防并发和降级完成路径。

四层防护（按代码路径自上而下）：

### 7.1 Phase 共享 readiness 注册表

文件：`backend/src/autoessay/phase_readiness.py`。每个 phase 一个 `<phase>_ready(run, session) -> (ok, reason)`：

| phase | 校验内容 |
|---|---|
| curator | `discovery/skim_candidates.jsonl` 或 `sources/shortlist.json` 至少存在一个非空 |
| synthesizer | `sources/shortlist.json` 非空 |
| ideator | `synthesis/claims.jsonl` 非空 |
| drafter | `has_selected_angle`（`novelty/selected_thesis.json` 或最近的 `USER_NOVELTY_REVIEW` checkpoint 含非空 `angle_id`） |
| stylist | `stylist_artifacts_ready`（`drafts/v???/manuscript.md` 非空 + `claim_map.jsonl` + `citations.bib`） |
| critic | `drafts/v???/style/paper_styled.md` 非空 |
| integrity | `latest_external_scan_decision.approve == True` |
| exports | `drafts/v???/style/paper_styled.md` 非空 |

`assert_phase_ready` 把 `(ok, reason)` 转换为 409 + `detail`。所有 `start_*` 和 `rerun_phase` 都调用同一个 `assert_phase_ready`，所以失败恢复路径不会绕过 `start_*` 的守卫。

文献阶段还有两条额外强约束：`start_curator` 要求 latest valid `USER_SEARCH_REVIEW` source-review checkpoint；`start_synthesizer` 要求 latest valid `USER_DEEP_DIVE_REVIEW` source-review checkpoint。checkpoint 的 `decision_payload` 可存 dict 或 list 形态的 source ids；无 checkpoint、空选择、过期 artifact 都会按 409 处理。

`activate_phase_version` 不调 readiness（它只翻 head pointer，不重跑 agent）。

### 7.2 Drafter 容错完成

文件：`backend/src/autoessay/agents/drafter.py`，单 section 重试预算 `AUTOESSAY_DRAFTER_MAX_CORRECTIVE_RETRIES`（默认 4）。退出语义：

| stub 数 / 总段数 | severity | run state |
|---|---|---|
| 0 / N | `null`（无） | `phase_done` |
| 1 ≤ s ≤ N/2 | `amber_minor` | `phase_done` |
| N/2 < s < N | `amber_major` | `phase_done` |
| s == N | `fail_all_stubbed` | `FAILED_FIXABLE` |

部分 stub 不再算 phase 失败，下游 stylist 可以照常运行。`draft_metadata.json` 加入 `section_statuses[].is_stubbed` 与 `stubbed_section_ids`，UI 据此渲染 amber badge。

### 7.3 原子 phase-start 锁

文件：`backend/src/autoessay/phase_lock.py`，alembic `014_phase_lock`。三列：

```sql
runs.active_phase_lock              VARCHAR(64)  -- 当前持锁的 phase 名
runs.active_phase_lock_job_id       VARCHAR(64)  -- 持锁人 token
runs.active_phase_lock_claimed_at   DATETIME     -- 起占时间，运维可见性
```

获取（`claim_phase_lock`）：单行 `UPDATE runs SET ... WHERE id=:run_id AND active_phase_lock IS NULL`，rowcount=0 即占用失败 → 409。释放（`release_phase_lock`）：owner-checked，`UPDATE ... WHERE active_phase_lock=:phase AND active_phase_lock_job_id=:job_id`，crash 后归来的旧 worker 不会清掉新锁。

`run_X` agent 公共入口都 `with phase_lock_release_on_exit(run_id, phase, lock_token, session=db_session):` 包裹，无论成功 / FAIL_FIXABLE / 异常都释放。`session=db_session` 路径让 sync-worker 模式（含 tests）使用同一会话避开 cross-DB 写。

工作流：

```
start_drafter → assert_phase_ready → claim_phase_lock(token=T1)
              → enqueue_drafter_job(run_id, lock_token=T1)
              → 200 Accepted

worker pickup → run_drafter(run_id, lock_token=T1)
              → with phase_lock_release_on_exit: ... agent body ...
              → finally: release_phase_lock WHERE job_id=T1
```

逃生口：`POST /api/runs/{id}/clear-phase-lock` 调用 `force_clear_phase_lock`（无 owner check），写一条 `phase_lock_force_cleared` 审计事件。

`RunResponse.active_phase_lock: ActivePhaseLockResponse | None` 把 `{phase, job_id, claimed_at}` 暴露给前端，UI 据此渲染"phase X 已运行 N 分钟"+ clear 按钮。

### 7.4 失败状态恢复 UI

`frontend/src/pages/WorkspacePage.tsx::FailureResolutionBanner`，按状态分发动作。SSE 收到 `state_transition` / `phase_failed` 后会刷新 banner；URL 直接进入 blocked run 时，`resolveLandingSubview` 会按 failed phase 打开对应 tab。

| 状态 | 动作 |
|---|---|
| `FAILED_FIXABLE` | "Retry phase" → `/phases/{phase}/retry` backend resolver |
| `FAILED_VENDOR` | "Retry external scan" → `startIntegrity` / "Skip integrity" → `transitionRun(USER_FINAL_ACCEPTANCE)` |
| `FAILED_NEEDS_USER` | amber 文案，依赖 payload 上下文（暂无通用动作） |
| `FAILED_POLICY` | 禁用直接 retry；走 force-approve 或 phase review 专用路径 |
| `CANCELLED` | amber 文案，无动作（按设计是终态） |

`DegradedDraftBanner` 处理 7.2 的 amber 部分 stub 信号——读 `lastEvent.payload.severity` 决定是 minor 还是 major。

### 7.5 文献全文与用户上传保护

文献全文获取分两层：

1. `curator` 先判断候选是否已有 direct PDF URL；没有时调用 fulltext resolver，对 DOI / landing URL 做有界 HTML 解析和有界 browser fallback，找到 direct PDF 后再交给 `pdf_fetcher`。
2. `pdf_fetcher` 先走 `httpx`，失败后按 `AUTOESSAY_PDF_FETCH_BROWSER_FALLBACK`（默认 true）尝试 headless Chromium。

用户上传文件走 `sources/uploads/` 和 `sources/user_upload_sources.json`。Scout / curator rerun 是 replacement 语义，但只清理非 user-owned `sources/fulltext/` cache；用户上传 PDF 不在 cascade purge 范围。rerun 前端会弹 destructive confirm，列出受影响的候选、shortlist、manual upload request 和下游产物数量，并说明 user-uploaded PDFs retained。

### 7.6 final_rewrite 三层质量路径

部署可配置项 `AUTOESSAY_FINAL_REWRITE_ENABLED=1` 会开启 final_rewrite 路径。`start_critic` 在 `USER_REVISION_REVIEW` 时会先 claim `final_rewrite` lock：

1. **polish loop**：根据 v2 expert critic 输出做最多 bounded attempts 的聚焦改写，audit 写入 `drafts/v???/polish/polish_loop.json`。
2. **critic loop**：对 candidate 做最多 `AUTOESSAY_CRITIC_LOOP_ITERATIONS` 次 review→rewrite，按质量指标选择最佳稿件。
3. **north-star gate sidecar**：在 critic phase 内记录独立 blind A/B 质量指标，写入 audit/event；pass/fail/unscorable 均不阻断用户流程。

Exports policy fail 会把 failure guidance 作为新的 blocker 回到 polish executor，最多 `AUTOESSAY_EXPORTS_POLICY_MAX_POLISH_RETRIES` 次；仍失败才保持 `FAILED_POLICY`。

## 8. 前端可测试性约定（testid）

所有新加的可交互 UI 元素 —— `<button>`、`<input>`、`<textarea>`、`<select>`、tab 按钮、模态对话框 —— **必须**带 `data-testid` 属性，方便 Playwright e2e spec 通过 `page.locator('[data-testid="..."]')` 而不是 i18n 字符串定位。

命名约定：

- 短横线连接：`failure-resolution-banner`、`prompt-save-and-rerun`、`history-modal-close`
- 模板化（动态生成）：`phase-action-${action.key}`、`workspace-tab-${tab.id}`、`history-version-${phase}-${entry.version_no}`
- 行为后缀：`-button`、`-modal`、`-textarea` 仅在歧义时加；纯 testid 优先

数据属性扩展：除 `data-testid` 外，关键状态也用 `data-*` 暴露给 spec：

- `data-run-state`、`data-run-id` 在 `workspace-root`（spec 等待状态机推进的入口）
- `data-last-event-type`、`data-last-event-phase`、`data-last-event-at`（事件流的可观测点）
- `data-failed-phase`、`data-failure-state`（FailureResolutionBanner）
- `data-active="true|false"`（tab、版本行）
- `data-is-active`、`data-version-id`、`data-version-no`、`data-status`（PhaseVersionRow）

写新组件时如发现既有 i18n 字符串能被 spec 抓但 testid 不齐，**补 testid** 比改 spec 字符串依赖更耐改。

## 9. 部署形态

见 `DEPLOYMENT.md`。
