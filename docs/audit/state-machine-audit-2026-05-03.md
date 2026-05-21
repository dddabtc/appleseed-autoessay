# State machine + version management + UI affordance audit (2026-05-03)

**Trigger**: User reported that piecemeal fixes (PR #148 replace_eligible, PR #150 edit-content button, drafter→stylist gating) kept exposing more bugs of the same class. Root cause: state machine inter-phase deps + version management + UI affordance gating were never consistently audited end-to-end.

User direction (verbatim): "你的整个状态机需要仔细全查一遍。这个你的智商可能不够，让 codex 与你一起查。"

Two rounds of codex audits captured below as the working spec for the master fix PR(s) following this commit.

---

## Round 1 — UI ↔ backend authority + state machine + version management mismatches

Codex enumerated **25 mismatch rows** + a handful of OK-but-noted items. Reproducing the MISMATCH rows verbatim (with file:line refs preserved):

| # | location | who shows / triggers | when shown | when accepted | mismatch |
|---|---|---|---|---|---|
| 1 | Proposal replace/save: `WorkspacePage.tsx:4622`, `4862`, `main.py:2039` | ProposalSubview save mode exposes `replace` when `canEdit` | Any non-running state with proposal text/version | Backend rejects `replace` once downstream output exists | Same class as PR #148 generic `replace_eligible` bug |
| 2 | Framework lens start: `api.ts:1225`, `main.py:2482`, `state_machine.py:176`, `framework_lens.py:82` | No normal frontend `startFrameworkLens` affordance exists | Should show at `USER_FIELD_REVIEW` when `should_run_framework_lens` is true | Backend accepts | **Required lens phase unreachable from normal UI** |
| 3 | Ideator start before lens: `WorkspacePage.tsx:1695`, `5452`, `main.py:2444` | Sidebar + NoveltySubview start ideator | `USER_FIELD_REVIEW` | Backend accepts USER_FIELD_REVIEW or USER_LENS_REVIEW | UI/backend allow skipping mandatory lens for theory_article |
| 4 | Ideator start after lens: `WorkspacePage.tsx:714`, `5452` | No visible NoveltySubview action after lens | `showNovelty` omits `USER_LENS_REVIEW` | Backend accepts | Once lens completes, valid next transition is hidden |
| 5 | Lens states routing: `WorkspacePage.tsx:315`, `707`, `714` | Default subview routes lens states to novelty | `FRAMEWORK_LENS_RUNNING` / `USER_LENS_REVIEW` | State machine supports both | Default routes to a subview whose visibility gate omits those states |
| 6 | Phase-history `run_now` for lens/ideator: `phase_history.py:361`, `WorkspacePage.tsx:4121` | PhaseHistoryModal run_now posts phase endpoint | Server marks runnable from raw state only | framework_lens / ideator accepted without should_run decision | History affordance can run wrong phase |
| 7 | Framework lens failure retry: `WorkspacePage.tsx:217`, `346`, `main.py:2792`, `phase_rerun.py:220` | Failure banner retry / navigation | `FAILED_FIXABLE` from framework lens | Fallback uses rerun, but rerun requires completed output | First-attempt failure cannot be retried from UI |
| 8 | Stylist start: `WorkspacePage.tsx:1723`, `5826`, `main.py:2559`, `phase_readiness.py:100` | Sidebar and StyleSubview | Sidebar requires `drafterCompleted`; StyleSubview only checks DRAFTER_RUNNING / USER_REVISION_REVIEW | Backend accepts state but readiness requires drafter artifacts | StyleSubview exposes start while drafter is mid-flight (already in progress on this branch) |
| 9 | Critic start: `WorkspacePage.tsx:1745`, `5972`, `main.py:2590`, `phase_readiness.py:105` | Sidebar + ReviewSubview | `USER_REVISION_REVIEW` | Backend requires non-empty `paper_styled.md` | No frontend `stylistCompleted` / style-artifact gate |
| 10 | Integrity request revision: `WorkspacePage.tsx:6175`, `1469`, `main.py:5176` | IntegritySubview request revision | `USER_INTEGRITY_REVIEW` | Backend transitions to DRAFTER_RUNNING but does not enqueue drafter | UI shows running draft state with no worker driving it |
| 11 | Failure retry sidebar: `WorkspacePage.tsx:1622`, `1640`, `phase_rerun.py:220` | Sidebar failed-phase action | Any FAILED_* with failedPhaseAction | Calls rerunPhase; backend requires completed output | First-attempt failures with no sentinel output 409 instead of start/retry |
| 12 | Generic edit stale propagation: `main.py:3132`, `phase_user_edit.py:433` | Edit modal save mode=new | UI/GET sees downstream via branch heads or legacy files | Save path marks stale using branch-head downstream only | Legacy downstream files force new-version UI but save without equivalent stale mark |
| 13 | Phase prompt edit/cancel: `WorkspacePage.tsx:4083`, `main.py:3559`, `3704` | PhaseHistory prompt actions | Modal disables via caller context, not endpoint | Backend mutation endpoints have no RUNNING_STATES guard | Direct/stale prompt mutation can occur during a running phase |
| 14 | Stale banner rerun: `WorkspacePage.tsx:1900`, `2750`, `phase_rerun.py:212` | StaleBanner rerun | Any stale_from_phase, no current-state gate | Backend rejects all RUNNING_STATES | Visible rerun affordance can 409 while another phase is running |
| 15 | Phase history delete: `WorkspacePage.tsx:3945`, `main.py:3299`, `phase_version.py:1046` | PhaseHistoryModal delete | UI disables while running and if active/downstream/parent/fork-blocked | Backend enforces graph blocks but no running guard | Backend authority allows deletion during active agent work |
| 16 | Phase version replace exclusivity: `phase_version.py:601`, `628`, `1114` | Replace-mode backend authority | Replace allowed if no non-deleted branch uses pv as fork point | Delete blocks fork points including soft-deleted branches | Replace can mutate a pv that delete later treats as protected |
| 17 | Framework lens version ownership: `phase_version.py:95`, `101`, `framework_lens.py:230` | Framework lens agent writes artifacts | Lens run writes framework_lens.json and mutates synthesizer.json hook | Version ownership archives lens json only; synthesizer owns synthesizer.json | Lens commit doesn't own all files it mutates |
| 18 | Branch switch: `WorkspacePage.tsx:517`, `4173`, `main.py:4176` | BranchSwitcher select | Always enabled when branch list renders | Backend switches/materializes active branch with no running/lock guard | Branch materialization can race active phase writes |
| 19 | Branch create/delete: `main.py:4035`, `4220` | PhaseHistory fork / branch controls | UI gated mainly by modal allDisabled for fork | Backend create/delete has no running-state authority | Direct backend calls can mutate branch graph mid-run |
| 20 | Research kernel edit: `WorkspacePage.tsx:3488`, `8018`, `main.py:2098`, `2144` | Sidebar kernel edit modal | Button/modal save not gated by current running state | Backend rejects running/phase lock | UI lets user compose/save into a guaranteed 409 |
| 21 | Source upload: `WorkspacePage.tsx:677`, `7406`, `main.py:4833` | SourcesSubview upload PDF | Any state where SourcesSubview is visible | Backend accepts any non-deleted run; no running guard / stale mark | Source corpus can change mid/later pipeline without state authority |
| 22 | Research role update: `WorkspacePage.tsx:7660`, `main.py:4344`, `4411` | Source row research-role radios | Any visible source row | Backend accepts any state and marks synthesizer stale if file exists | UI warning/gating is weaker than backend stale authority, no running guard |
| 23 | Evidence ledger override: `WorkspacePage.tsx:6743`, `7081`, `main.py:4548` | Evidence ledger override buttons | UI only inside synthesis review area | Backend accepts any state and creates synthesis dir if needed | Backend over-permissive, doesn't propagate downstream stale state |
| 24 | Novelty discussion: `WorkspacePage.tsx:5610`, `5686`, `main.py:4651` | Novelty chat | UI disables unless USER_NOVELTY_REVIEW | Backend accepts any mutable run with existing angle cards and rewrites novelty artifacts | Backend lacks UI/state-machine guard |
| 25 | Framework lens agent cancellation: `agents/framework_lens.py:69`, `155` | Backend sync agent runner | Triggered by start/rerun | Guard checks only USER_FIELD_REVIEW | No `assert_run_active` cancellation guard in lens runner |

**Categories**: state-machine (3, 4, 5), agent-guard (8, 9, 25), version (16, 17), frontend affordance (1, 2, 14, 20, others), stale-propagation (12, 21, 22, 23), backend authority gaps (13, 15, 18, 19, 24).

Round-1 verdict: at least 5 mismatches per major category, as expected.

---

## Round 2 — Discoverability (always-render + disabled + hint)

User principle (verbatim): "如果各个 phase 的状态是未启用状态，不应该隐藏，而是显示出来，提示依赖节点尚未完成。否则用户不知道都有什么功能。"

Codex enumerated **28 phase-action surfaces** all using the wrong `currentState === "X" ? <button> : null` ternary-hide pattern. Reproducing the table verbatim:

| component / button | current pattern | should be | disabled hint copy |
|---|---|---|---|
| Workspace tabs | future tabs filtered out by `show*` flags (`WorkspacePage.tsx:671-756`, `1989-1991`); no `lens` tab in `WorkspaceSubview` (`279-290`) | render every phase tab including `lens` at all times | `需要先完成 {phase} 节点` |
| ProposalSubview / generate proposal | only at DOMAIN_LOADED (`4715-4735`) | always render, enable only DOMAIN_LOADED / valid rerun | `需要先完成领域加载节点` / `等待提案生成完成` |
| ProposalSubview / regenerate | hidden unless `hasProposal && canEdit && !isPostAcceptEdit` (`4622`, `4921-4938`) | always render once Proposal tab exists | `需要先生成提案节点` |
| ProposalSubview / accept proposal | hidden with same gate (`4921-4965`) | always render; disabled until proposal exists + state is USER_PROPOSAL_REVIEW | `需要先生成提案节点；确认后将启动文献检索节点` |
| SourcesSubview / start curator | hidden unless USER_SEARCH_REVIEW (`7418-7429`) | always render | `需要先完成文献检索节点` / `等待文献检索完成` / `需要至少一条候选文献；可上传来源` |
| SynthesisSubview / start synthesizer | hidden unless USER_DEEP_DIVE_REVIEW (`6716-6727`) | always render | `需要先完成文献筛选节点` / `等待文献筛选完成 ({completed}/{total})` |
| SynthesisSubview / claims + ledger tabs | whole area hidden unless USER_FIELD_REVIEW (`6767-6880`) | render tabs always | `需要先完成综合节点后查看综合结论和证据账本` |
| SynthesisSubview / dual-track lens details | hidden unless `partition.lens.length > 0` (`6946-6963`) | render section as empty/disabled | `暂无理论镜框证据；可在「文献」页调整来源层级` |
| **FrameworkLensSubview / lens tab** | DOES NOT EXIST yet; lens states route to Novelty (`315-322`); backend endpoint exists (`main.py:2482-2523`) | add Lens tab + start lens button | Theory mode: `理论论文模式必须经过框架镜框节点；请点击「框架镜框」节点开始` |
| Framework lens / theory_article | backend says theory mode is non-skippable (`framework_lens.py:11-19`, `97-102`) | UI must not call it optional for theory_article | `理论论文模式必须先完成框架镜框节点` |
| NoveltySubview / start ideator | hidden unless USER_FIELD_REVIEW (`5453-5464`); backend accepts USER_FIELD_REVIEW or USER_LENS_REVIEW | always render; enable per dual-input rules | `需要先完成综合节点` / `理论论文模式必须先完成框架镜框节点` / `等待框架镜框完成` |
| NoveltySubview / accept current angle | always rendered but disabled outside USER_NOVELTY_REVIEW with no hint (`5465-5483`) | keep, add hint | `需要先完成新颖性节点` / `回到「新颖性」页选择一个角度` |
| NoveltySubview / per-card make current + select | hidden unless USER_NOVELTY_REVIEW (`5568-5590`) | render disabled controls when cards exist | `需要先完成新颖性节点后选择角度` |
| NoveltySubview / discussion chat | form visible but disabled outside USER_NOVELTY_REVIEW, no hint (`5611-5615`, `5689-5693`) | add hint near form | `需要先完成新颖性节点后才能讨论角度` |
| DraftSubview | no phase-action button (`5703-5800`) | acceptable if sidebar always shows Drafter | `回到「新颖性」页选择一个角度后启动草稿节点` |
| StyleSubview / start stylist | branch hides until DRAFTER_RUNNING && drafterCompleted or USER_REVISION_REVIEW (`5831-5864`); also missing i18n key `workspace.style.waiting_for_drafter` | always render the stylist button | `草稿必须先生成完整稿件 ({completed}/{total} 段)` |
| ReviewSubview / start critic | hidden unless USER_REVISION_REVIEW (`5996-6007`) | always render; enable only after stylist output ready | `需要先完成文风节点` / `等待文风完成 ({completed}/{total} 段)` |
| ReviewSubview / approve external scan | whole panel hidden unless USER_EXTERNAL_SCAN_APPROVAL (`6083-6139`) | render panel/button disabled until critic done | `需要先完成评审节点` / `至少选择一种外部检测类型` |
| ReviewSubview / skip external scan | hidden with same panel | always render disabled | `需要先完成评审节点` / `填写跳过说明后才能跳过外部检测` |
| IntegritySubview / accept integrity | hidden unless USER_INTEGRITY_REVIEW (`6189-6198`) | always render | `需要先完成完整性检测节点` / `等待完整性检测完成` |
| IntegritySubview / request revision | hidden unless USER_INTEGRITY_REVIEW (`6189-6208`) | always render | `需要先完成完整性检测节点；选择需修订的检测结果` |
| ExportSubview / accept final draft | hidden unless USER_FINAL_ACCEPTANCE (`6361-6373`) | always render | `需要先完成完整性节点` / `至少选择一种导出格式` |
| ExportSubview / run exports | hidden unless USER_FINAL_ACCEPTANCE (`6374-6384`) | always render | `需要先完成最终确认节点` |
| WorkspaceStatusPanel / phase menu | `phaseActions` built from current-state ternaries then filtered (`1654-1767`); panel hides whole menu if empty (`3453-3478`) | build full ordered action list with disabled + disabledReason | per row: `需要先完成 {predecessor} 节点`; stale: `需要先重跑 {stalePhase} 节点` |
| PhaseHistoryModal / primary rerun-edit actions | actions disable on running/inFlight with no reason (`3825-3857`) | add reason when disabled | `等待当前节点运行完成` / `需要先重跑 {stalePhase} 节点` |
| PhaseHistoryModal / ungenerated run_now | absent if `runnable_now=false` (`phaseHistoryState.ts:71-75`, `WorkspacePage.tsx:3858-3862`) | always render disabled | `需要先完成 {dependency} 节点` |
| PhaseHistoryModal version row / activate | always rendered, disabled with no hint | add inline reason | `该版本已是当前版本` / `只有成功版本可以激活` / `等待当前节点运行完成` |
| PhaseHistoryModal version row / delete | always rendered; reason in title-only | make reason visible | `需要先解除版本依赖：{reason}` |
| StaleBanner / rerun stale phase | banner appears only when stale_from_phase (`1900-1912`) | feed stale state into every downstream button | `正在重跑 {phase} 节点` / `需要先重跑 {phase} 节点` |
| FailureResolutionBanner / retry | retry only for FAILED_FIXABLE (`2931-2953`) | show retry disabled for non-retryable with reason | `该失败类型不能直接重试；请按上方提示补充输入或修改内容` |
| FailureResolutionBanner / vendor retry + skip | only FAILED_VENDOR (`2955-2986`) | keep visible; disabled outside vendor failure | `只有外部服务失败时才能重试或跳过外部检测` |
| FailureResolutionBanner / force approve | only when applicable (`2988-3018`) | render disabled when not applicable | `当前状态没有可安全强制批准的目标` / `请输入至少 5 个字的强制批准理由` |

**i18n cost**: ~34 hint keys + 2 phase-name keys (`workspace.tab.lens`, `phase.framework_lens`) under `workspace.phase_action.disabled_hint.*` namespace, en/zh/ja parity.

---

## Tiered ship plan

Implementation will land across multiple PRs (50+ touchpoints can't be reviewed safely in one):

### Tier 1 — Lens path reachability + completion gates ✅ DONE
- Add `lens` to `WorkspaceSubview` enum + new `FrameworkLensSubview` component (afe2751)
- `defaultSubviewForState` routes `FRAMEWORK_LENS_RUNNING` / `USER_LENS_REVIEW` to `lens` subview (afe2751)
- Sidebar phase-action menu adds `framework_lens` with proper readiness gating (afe2751)
- Ideator visibility extended to USER_LENS_REVIEW (round 1 #4) (afe2751)
- StyleSubview drafterCompleted gate (round 1 #8) (53c7fee)
- Backend authority gaps: research_kernel edit returns precondition failure during RUNNING (round 1 #20) — already gated in current main

### Tier 2 — Always-render + disabled + hint refactor (mostly done)
- ✅ Review/Integrity/Export Subview always-render (fc33243)
- ✅ Sources/Synthesis/Novelty Subview always-render (5f6b31c)
- ✅ PhaseHistoryModal: visible disabled reasons, run_now always rendered (6fb2000)
- 22 new i18n keys across en/zh/ja so far
- DEFERRED: WorkspaceStatusPanel sidebar full ordered list (cost/value tradeoff — Subview already covers discoverability; sidebar would balloon to 11+ buttons)
- DEFERRED: ProposalSubview always-render (low value; users see this view at start, not later)
- DEFERRED: NoveltySubview per-card make-current + discussion always-render (form already visible/disabled with no hint — minor)
- DEFERRED: StaleBanner downstream-button hint propagation (banner already shows clear path; subviews already RUNNING-gate)

### Tier 3 — Backend authority hardening (mostly done)
- ✅ RUNNING_STATES guard on novelty discussion endpoint (round 1 #24) (0c1895e)
- ✅ RUNNING_STATES guard on phase prompt edit + cancel (round 1 #13) (b6a7b41)
- ✅ RUNNING_STATES guard on source upload (round 1 #21) (f5f89e2)
- ✅ RUNNING_STATES guard on research_role update (round 1 #22) (f5f89e2)
- ✅ RUNNING_STATES guard on evidence ledger override (round 1 #23) (f5f89e2)
- ✅ RUNNING_STATES guard on branch create/switch/delete (round 1 #18, #19) (1ee4cf6)
- ✅ Framework lens cancellation guard via assert_run_active (round 1 #25) (798bb4a)
- ✅ Stale propagation: source upload + ledger override → mark synthesizer stale (round 1 #21, #23 tail) (cc1681c)
- DEFERRED: Replace_eligible vs delete-block fork-point asymmetry (round 1 #16) — needs design discussion
- DEFERRED: Framework lens version ownership of synthesizer.json hook (round 1 #17) — needs design discussion (snapshot hook in lens artifact?)
- DEFERRED: Phase-history runnable_now consults should_run for lens/ideator (round 1 #6)
- DEFERRED: First-attempt failure retry path (round 1 #7, #11) — substantial code-path change

### Tier 4 — Polish + theory_article unlock + drafter rewiring
- theory_article status flip (only when all of C2.b is in)
- drafter section_plan rewires to read paper_modes.spec
- AngleCard schema + ideator referential integrity (lens names filter)
- Frontend lens tab full implementation (signals list, applicability text, edit affordance)
- New e2e specs (lens-display, theory_article-mode-walk)

### Future
- DESIGN.md §2.A.x update to capture lens phase + non-skippable theory_article rule
- HANDOFF.md §11 update at next refresh

---

## Codex outputs preserved

- Round 1 raw output: `/tmp/round1.txt` (300 lines)
- Round 2 raw output: `/tmp/round2.txt` (350 lines)

These should be reproducible by re-running:

```
codex exec --skip-git-repo-check --cd ~/appleseed-autoessay --color never < /tmp/state-machine-audit.md
codex exec --skip-git-repo-check --cd ~/appleseed-autoessay --color never < /tmp/state-machine-audit-round2.md
```

against the same git revision (HEAD = 9569c23 + the in-progress branch
`audit/state-machine-comprehensive` with the StyleSubview drafterCompleted patch).
