# 需求说明（appleseed-autoessay）

## 概述

appleseed-autoessay 是一个 Web 学术写作助手。它把学术论文的产出流程拆成可观察、可中断、可重做的若干阶段：先做选题与文献发现，再让用户审阅检索候选和深读清单，再做内容综合与角度筛选，再生成正文与风格修订，最后做评审、完整性检查和导出。每个阶段产出可见的中间产物，用户可以在阶段之间介入、编辑、重跑、上传资料或开新分支。

## 用户角色

- **作者（user）**：撰写论文的主体。系统按 OIDC 登录后单用户使用；当前不支持多人协作。
- **管理员**：负责部署、配置 OIDC、env 文件、备份。和作者可以是同一人，也可以分离。
- **运维**：观察 watchdog、构建/部署流水线、镜像更新。

## 核心场景（6 条用户故事）

1. **创建一篇论文**：作者输入题目与领域，系统创建项目与一次新的运行（run），调度 proposal 阶段写出选题方案。作者审阅方案、按需修订并接受，运行进入文献检索。
2. **审阅与修订中间产物**：每个阶段完成后系统进入对应的「USER\_…\_REVIEW」状态。作者可以阅读阶段产出的文件（候选文献、深读清单、综合 claims、角度卡片、章节草稿、风格修订报告等），修改本地草稿或提示词，然后点继续/重跑。
3. **审阅与补充文献**：作者在 `USER_SEARCH_REVIEW` 审阅粗检索候选，在 `USER_DEEP_DIVE_REVIEW` 审阅深读清单；可通过、拒绝、置顶来源，也可对下载失败的单条文献上传 PDF。
4. **重跑某个阶段并保留版本历史**：作者发现某阶段输出不满意，单独重跑该阶段。系统保留历史版本（phase\_versions）、维护当前 head（run\_heads），并标记下游为 stale。下游必须按顺序刷新。
5. **新建分支并激活**：作者在某个阶段版本上 fork 出新分支，与 main 分支独立尝试不同方向。每个分支维护自己的 head 与 stale 状态。激活后续运行只影响当前分支。
6. **导出成稿**：完整性检查通过后，作者点导出，系统生成 DOCX/Markdown/HTML 与引用清单等文件。

## 功能需求

| 编号 | 描述 |
|---|---|
| FR-1 | 13 个内部运行阶段按顺序运行：proposal → scout → curator → synthesizer → tension_extraction → framework_lens → ideator → drafter → stylist → final_rewrite → critic → integrity → exports。其中 `tension_extraction` 可按配置跳过。 |
| FR-2 | 单阶段重跑：用户可以在阶段已完成后重新运行该阶段，前提是上游不处于 stale 状态。 |
| FR-3 | 每次阶段成功执行都会创建 phase\_version 行；run\_head 指向当前激活的版本。 |
| FR-4 | 用户可以在某个 phase\_version 上 fork 出新分支；分支独立维护 head 与 stale。 |
| FR-5 | 提示词覆盖：单调用智能体（synthesizer/ideator/critic）有 `main` 一个 key；多调用智能体有更多 key —— curator 是 `ranking`、drafter 是 `main` + 8 个 section\_id（introduction、historiography、sources-method、empirical-section-i/ii/iii、discussion、conclusion）、stylist 是 `main` + `repolish`。每个 key 对应一段静态指令文本，用户可以在 UI 中编辑并保存为该次运行的覆盖。 |
| FR-6 | 阶段版本快照：每次执行记录解析后的提示词（默认或覆盖）于 phase\_version\_prompts，便于审计与版本对比。 |
| FR-7 | 诚信检查：integrity 阶段产生 `integrity_summary.json`，包含外部查重比例、AI-flavor 软信号、Stop-Slop 评分。低于阈值则用户必须批准外部扫描或修订。 |
| FR-8 | 导出：exports 阶段生成 manifest 与多种格式的成稿；前端提供下载入口。 |
| FR-9 | 鉴权：通过 Casdoor OIDC 登录，单用户运行模式下无个人化界面元素，但所有 API 仍走鉴权（开发态可以 `AUTOESSAY_AUTH_BYPASS=1` 跳过）。 |
| FR-10 | 作者 roster：用户在系统内维护一个作者列表，并把作者绑定到具体项目。 |
| FR-11 | 同时活跃论文上限：单用户最多 3 篇活跃论文。项目软删除和 run 级软删除都会释放对应可用槽位；run 可单独恢复。 |
| FR-12 | **Phase 启动 readiness 校验**：每个 `start_*` 端点和 `rerun_phase` 必须在转 state 之前校验该 phase 的确定性前置条件（curator 无 skim 候选时、synthesizer 无 shortlist 时、drafter 无 selected angle 时等），返回 409 + 不变更 state，避免 agent 11ms 内 FAIL_FIXABLE 污染。详见 `phase_readiness.py`。（Stage 3.E follow-up） |
| FR-13 | **Drafter 容错完成**：drafter 单 section schema 校验失败的重试预算可配置（`AUTOESSAY_DRAFTER_MAX_CORRECTIVE_RETRIES`，默认 4）；只有"全部 section 都成 stub"才转 FAILED_FIXABLE，部分 stub 转 phase_done 并附带 severity（amber_minor / amber_major）+ `stubbed_section_ids`。下游 stylist 可继续运行；UI 用 amber 提示用户复核。（Stage 3.E follow-up） |
| FR-14 | **原子 phase-start 锁**：`start_*` 端点和 `rerun_phase` 必须在事务内单行 `UPDATE WHERE active_phase_lock IS NULL` 占用锁，第二次点击（多 tab/curl/race）返回 409。释放必须 owner-checked（持锁人 token 匹配），避免 stale worker 清掉新锁。提供 `POST /api/runs/{id}/clear-phase-lock` 作为 zombie 锁清理逃生口。（Stage 3.E follow-up） |
| FR-15 | **失败状态可视恢复**：所有终态失败（`FAILED_FIXABLE`、`FAILED_VENDOR`、`FAILED_NEEDS_USER`、`FAILED_POLICY`、`CANCELLED`）必须在 UI 提供可见解释；可恢复的状态（`FAILED_FIXABLE`、`FAILED_VENDOR`）必须提供一键动作按钮（rerun phase / retry external scan / skip integrity），不需要 admin SQL 干预。（Stage 3.E follow-up） |
| FR-16 | **文献审阅强契约**：`start_curator` 必须要求最新有效的 `USER_SEARCH_REVIEW` 审阅记录；`start_synthesizer` 必须要求最新有效的 `USER_DEEP_DIVE_REVIEW` 审阅记录，否则返回 409。 |
| FR-17 | **文献下载与上传**：curator 对直接 PDF URL 保持原路径；对 DOI/landing URL 先通过 fulltext resolver 找直接 PDF，再交给 fetcher；http 下载失败时可按配置使用 headless browser fallback。前端必须同时提供全局 PDF 上传和 per-item PDF 上传。 |
| FR-18 | **最终修稿与质量观察**：`final_rewrite` 默认开启，包含 bounded polish loop 与 critic loop；north-star gate 在 critic phase 内作为 sidecar 质量指标记录，不阻断用户流程。exports policy fail 最多回到 final_rewrite 修复 2 次。 |
| FR-19 | **阻塞态工作台定位**：进入 `FAILED_POLICY` / `FAILED_FIXABLE` / `USER_*_REVIEW` 等阻塞或审阅状态时，前端应根据失败 phase 或当前 state 打开最相关 tab，并通过 SSE 及时刷新恢复按钮。 |

## 非功能需求

- **性能**：本地 SQLite + 单进程 FastAPI 即可承担 v0.1.0 的负载。drafter / stylist / final_rewrite / critic 是主要耗时项，开发与回归测试走 stub 或同步 worker。
- **可观测性**：systemd watchdog 每 60s 验活；`/healthz` 与 `/readyz` 暴露状态；事件持久化到 `run_events`，前端通过 SSE 订阅。`RunResponse.active_phase_lock = {phase, job_id, claimed_at}` 暴露 phase-start 锁的占用情况，运维可 spot-check `WHERE active_phase_lock IS NOT NULL` 的所有 run。
- **数据隐私**：用户的语料库（corpus）与已编辑提示词存在本地 SQLite；外部查重接口接收正文摘要，不外发原始 prompt 元数据。
- **可恢复**：阶段执行有 `commit_phase_version` / `fail_phase_version` 二态，失败时回滚文件并恢复运行状态。Phase-start 锁在 agent 退出（成功/失败/异常）时全部 owner-checked release；worker 崩溃留下的 zombie 锁可由 `POST /api/runs/{id}/clear-phase-lock` 清理。
- **运行时韧性**（Stage 3.E follow-up）：
  - 误点击单点防御：双层 readiness（API 端 `assert_phase_ready` + 前端按钮 disabled）+ 锁（双 tab race）+ tolerant drafter（部分 LLM 失败不致使整 phase 失败）+ FailureResolutionBanner（失败可恢复）。
  - 任意阶段卡死的兜底：systemd watchdog 重启容器、`POST /api/runs/{id}/clear-phase-lock` 清锁、`gh workflow run deploy.yml -f image_tag=...` 一键回滚。
- **测试**：截至 2026-05-11，full local CI 为 backend pytest 1543 + frontend vitest 133；Playwright stub/real-paper 走手动命令。PR 必须提交 `.ci-attestation.json`，GitHub Actions 只做 attestation / guardrails 等轻量检查。
