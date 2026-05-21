# Design Notes (appleseed-autoessay)

**Languages:** English | [中文](DESIGN.zh.md)

## 1. Overall Architecture

```
+-------------+       HTTPS       +-------------------+        +-------------------+
|   browser   |  <------------->  |   reverse proxy   |  --->  |  frontend (Vite)  |
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
                                                          External APIs:
                                                          - OpenAI-compatible
                                                            LLM gateway
                                                          - Literature metadata /
                                                            full-text providers
                                                          - Optional integrity /
                                                            originality provider
```

Typical self-hosted compose services: identity provider, identity database, `frontend`, `api`, `worker`, `redis`, and `migrate`. `worker` and `migrate` reuse the api image.

Two container images are expected: `your-org/appleseed-autoessay-{api,frontend}`. CI can build them on pushes to main and publish both `${SHA}` and `:latest` tags.

## 2. State Machine

Each run keeps a single state in `UPPER_SNAKE_CASE`. Running states use `XXX_RUNNING`, `PROPOSAL_DRAFTING`, or `REWRITE_RUNNING`; user-review states use `USER_..._REVIEW`; exceptions include `FAILED_FIXABLE`, `FAILED_NEEDS_USER`, `FAILED_VENDOR`, `FAILED_POLICY`, and `CANCELLED`.

Simplified transition graph, showing only the main path. Rollbacks between `USER_*_REVIEW` states are handled by phase reruns.

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
                       (stylist can rerun; critic first runs final_rewrite)
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

## 2.A Per-Phase Version State Machine (PR-A4 Model)

Section 2 describes the run-level state machine. This section describes the per-phase version model introduced by the PR-A4 series. The two machines are orthogonal: while a run is in `DRAFTER_RUNNING`, the drafter card's UI state for the committed `RunHead` is independent of the run state.

### 2.A.1 Five Rules

| # | Rule | Implementation anchor |
|---|---|---|
| 1 | Each phase has an independent version sequence; there is no project-wide version number. | `phase_versions.version_no` increases monotonically per (run, phase). |
| 2 | Changing a node with downstream dependencies must create a new version and mark downstream nodes as ungenerated. | `commit_phase_version` always allocates a new phase version; `_cascade_phase_after_upstream_change` deletes downstream `RunHead` rows when it cannot find an exact lineage match. |
| 3 | Changing a leaf node can replace or create a new version; semantics split by operation. | Direct artifact edits (modal / proposal save) use `replace_phase_version` or create a user-edit version, then go directly to `generated`. Prompt or context drafts (`PhasePromptDraft`) show `prompt_edited` until an LLM rerun produces a new version. |
| 4 | Published versions can only be deleted in reverse dependency order. | `delete_phase_version` rejects deletion when referenced by RunHead, `phase_version_inputs.upstream_pv_id`, `parent_pv_id`, or `branches.forked_from_pv_id`, including soft-deleted branches. |
| 5 | Activating a version cascades downstream along lineage; if no match exists, downstream heads are cleared. | `activate_version` calls `_cascade_phase_after_upstream_change`; candidates are filtered with exact `_lineage_matches`, and missing matches delete `run_heads` rows because `version_id` is NOT NULL. |

### 2.A.2 Four UI States From Three Flags

`GET /api/runs/{id}/phase-history` returns three flags per phase. The frontend `deriveCardState` helper in `frontend/src/lib/phaseHistoryState.ts` collapses them into four user-visible states.

| state | head_missing | prompt_dirty | lineage_dirty |
|---|---|---|---|
| `ungenerated` | true | * | * |
| `prompt_edited` | false | true | * |
| `upstream_superseded` | false | false | true |
| `generated` | false | false | false |

`*` means "do not care". `head_missing` has highest priority, then `prompt_dirty`; this intentionally masks `lineage_dirty` because prompt edits must be resolved before lineage staleness can be acted on. When both `prompt_edited` and `lineage_dirty` are true, the modal shows only cancel/regenerate primary actions plus a separate advisory, not `activate_lineage_match`.

Precise flag definitions in `backend/src/autoessay/phase_history.py`:

- `head_missing`: there is no `RunHead` row for (run, branch, phase).
- `prompt_dirty`: at least one `PhasePromptDraft.content_hash` differs from the current head phase version's `PhaseVersionPrompt.content_hash` for the same `prompt_key`. A draft also counts as dirty when the head has no matching key.
- `lineage_dirty`: the head phase version's `phase_version_inputs.upstream_pv_id` no longer equals the current upstream `RunHead.version_id`; any upstream phase mismatch makes it dirty.

Important: phase-card state comes from the committed `RunHead`, not from an in-flight phase version status. On the first run, the card remains `ungenerated` until `commit_phase_version` writes the `RunHead`. On rerun, the old head stays `generated` until the new phase version commits successfully; failures leave the old head untouched.

### 2.A.3 Operations To State Transitions

Operations are grouped by whether they change phase versions / head pointers or prompt drafts.

Version / head operations:

| Operation | Endpoint | Main effect |
|---|---|---|
| `rerun` | `POST /api/runs/{id}/phases/{phase}/rerun` or `POST /api/runs/{id}/{phase}` (`start_*`) | Always writes a new phase version; the new head points at it; downstream heads cascade to an exact lineage match or are deleted. |
| `activate` | `POST /api/runs/{id}/phases/{phase}/versions/{pv}/activate` and `POST /api/runs/{id}/phases/{phase}/versions/activate-lineage-match` | Moves the head pointer to an existing phase version; downstream cascade is the same as rerun. |
| `delete` | `DELETE /api/runs/{id}/phases/{phase}/versions/{pv}` | Deletes only after reverse-dependency checks pass; `phase_versions` has no `deleted_at`, so deletion is hard delete and also cleans `phase_version_prompts`, `artifacts_v2`, `phase_version_inputs`, and archive directories. |
| `fork` | `POST /api/runs/{id}/branches` with `base_pv_id` in `done` status | Creates a new branch rooted at the selected phase version; downstream matching uses `reachable_pv_ids_for_branch`, including the `forked_from_pv_id` seed. |

Prompt-draft operations:

| Operation | Endpoint | Effect |
|---|---|---|
| `save prompt draft` | `PUT /api/runs/{id}/phases/{phase}/prompt` | Creates or updates `PhasePromptDraft`; the next phase-history calculation reports `prompt_dirty=true`, so the card becomes `prompt_edited`. |
| `cancel prompt drafts (phase-wide)` | `DELETE /api/runs/{id}/phases/{phase}/prompts/drafts` | Deletes all drafts for the phase, idempotently. Recalculation sets `prompt_dirty=false`, so the card falls back to the state determined by `lineage_dirty`, usually `generated`. |

### 2.A.4 Cascade Activation

Diamond lineage example:

```
scout v1 ---+---> curator v1 ---> synthesizer v1
            |
            +---> ideator v1
```

After switching scout to another version, `_cascade_phase_after_upstream_change` processes each downstream phase in `_PHASE_RUNNERS` order:

1. Compute the current upstream head vector, `expected = {upstream_phase: RunHead.version_id, ...}`.
2. Scan `done` candidates for the phase within `reachable_pv_ids_for_branch(branch)`, including inherited fork roots.
3. Apply `_lineage_matches(candidate.lineage, expected)`. The comparison must be exact equality, not a subset, or historical diamond branches can match incorrectly.
4. If multiple candidates match, choose the largest `version_no`; if none match, `DELETE FROM run_heads WHERE (run, branch, phase) = ...`.
5. File materialization (purging legacy paths and restoring new head artifacts) runs only for downstream phases touched by the cascade. The upstream phase is not modified.

### 2.A.5 Immutable Prompt Snapshots

`phase_version_prompts` stores prompt snapshots per phase version. The primary key is `(phase_version_id, prompt_key)`; `source` is a checked column with values `'default'` or `'override'`, not part of the key. Rows are read-only after insertion, and `prompt_dirty` compares drafts against these snapshots.

There are two write paths:

- Agent run (`commit_phase_version`): `_resolve_phase_prompts` selects default or override content, passes it to the agent, and writes a snapshot row with `source='default'` or `source='override'`.
- User-edit version (`apply_phase_user_edit`): user-edit versions also call `_resolve_phase_prompts` and write prompt snapshots, so they do not stay permanently `prompt_dirty` by comparing against a missing prompt.

### 2.A.6 `runnable_now`

`PhaseHistoryEntry.runnable_now` is true only when the run is in that phase's startable predecessor state. The backend uses the `phase_rerun.PHASE_INPUT_STATES` mapping, such as `USER_DEEP_DIVE_REVIEW` for `synthesizer`, and excludes all `*_RUNNING` states to prevent double-starts. The frontend `derivePrimaryActions` helper exposes `run_now` only when `runnable_now == true`.

### 2.A.7 `branches.stale_from_phase` Compatibility Field

The PR-A2 `branches.stale_from_phase` single-pointer field is no longer authoritative for the phase-history modal. The four-state / three-flag derivation described above is authoritative there. Legacy paths still read and write it:

Read paths:

- `WorkspacePage.tsx::StaleBanner` and the branch-switcher marker.
- `WorkspacePage.tsx` `ProposalSubview` `hasDraftRun`, used across modals as a signal that an unconfirmed draft run exists.
- `phase_user_edit.py::apply_phase_user_edit`, which calls `get_branch_stale()` to block editing downstream artifacts of a stale phase.
- `phase_rerun.py`, which still references it while ordering rerun prerequisites.

Write paths:

- `set_branch_stale` still writes it from fork, replace, commit, and cascade-activate paths so legacy reads remain meaningful.

It is a compatibility source, not merely a cache. Removing it requires first moving the above read paths to the three-flag calculation or the phase-history payload.

The actual `_PHASE_RUNNERS` order in `backend/src/autoessay/main.py`:

```
proposal -> scout -> curator -> synthesizer -> tension_extraction -> framework_lens
-> ideator -> drafter -> stylist -> final_rewrite -> critic -> integrity -> exports
```

`tension_extraction` is disabled by default. When disabled, the main path moves from `synthesizer` directly to `framework_lens` or `ideator`. `final_rewrite` is enabled by default; when enabled, `POST /critic` first enters `REWRITE_RUNNING`, completes the polish loop and critic loop, then enters `CRITIC_RUNNING`. The exports stage implementation file is `agents/exporter.py`, while the registered phase name is `exports`.

Phase responsibilities:

| Phase | Input | Output | Main files |
|---|---|---|---|
| proposal | Project title + domain | Topic proposal markdown | `proposal/proposal.md` |
| scout | Topic proposal | Candidate literature jsonl | `discovery/scout_report.md`, `discovery/skim_candidates.jsonl` |
| curator | Candidate literature | Shortlist + full text | `sources/shortlist.json`, `sources/fulltext/*.pdf` |
| synthesizer | Shortlist | Claims per source | `synthesis/claims.jsonl`, `synthesis/source_notes/*` |
| tension_extraction | Claims | Tension structure, optional | `synthesis/tension_extraction.json` |
| framework_lens | Claims + proposal | Framework-lens signals | `synthesis/framework_lens.json` |
| ideator | Claims + proposal | Candidate writing angles | `novelty/angle_cards.json` |
| drafter | Selected angle | Section draft + claim map | `drafts/v???/manuscript.md`, `drafts/v???/claim_map.jsonl` |
| stylist | Draft | Revised draft | `drafts/v???/style/*` |
| final_rewrite | Stylist output | Polished manuscript + critic-loop selected draft | `drafts/v???/polish/*`, `reviews/critic_loop.json` |
| critic | Draft | Review report | `reviews/*` |
| integrity | Draft + claim map | Integrity report | `integrity/integrity_summary.json` |
| exports | Accepted draft | Multi-format deliverables | `exports/manifest.json`, `exports/*.pdf`, etc. |

## 4. Persistence

Four key tables:

- `phase_versions`: one row per successful phase execution, including vanilla first runs. It records `run_id`, `phase`, `version_no`, `branch_id`, `input_snapshot_hash`, `prompt_hash`, and related metadata.
- `phase_version_prompts`: prompt snapshots for each phase version. The primary key is `(phase_version_id, prompt_key)`; `source` is a checked column with values `'default'` or `'override'`. See Section 2.A.5.
- `run_heads`: the current head pointer for each (run, branch, phase).
- `branches`: branch metadata, including `parent_branch_id`, `forked_from_pv_id`, `is_active`, and `stale_from_phase`. The phase-history modal no longer treats `stale_from_phase` as authoritative, but legacy stale banners and rerun paths still read it; see Section 2.A.7.

File materialization still uses `run.run_dir / <legacy_dir>` paths such as `scout/`, `sources/`, `synthesis/`, and `drafts/v???/`. Switching heads and forking remount files from `phase_version` artifacts.

## 5. Prompt Overrides (Stage 2.B + 3.A)

`backend/src/autoessay/prompts.py` stores `_REGISTRY` as a `(phase, prompt_key) -> PromptSpec` dictionary. It currently contains 16 entries:

| phase | Supported `prompt_key` values |
|---|---|
| synthesizer | main |
| ideator | main |
| critic | main |
| drafter | main, introduction, historiography, sources-method, empirical-section-i, empirical-section-ii, empirical-section-iii, discussion, conclusion |
| stylist | main, repolish |
| curator | ranking |

Each key registers static default text in `default_content`. User edits are saved into `phase_prompt_drafts`. On rerun, `_resolve_phase_prompts` passes the resolved default or override content to the agent and writes it into `phase_version_prompts` as a version snapshot.

API shape:

- `GET /api/runs/{run_id}/phases/{phase}/prompt[?prompt_key=]`: returns default plus draft content. If `prompt_key` is omitted and `(phase, "main")` is unsupported but another key exists, the endpoint falls back to the first key. An explicit empty string `?prompt_key=` returns strict 404.
- `PUT .../prompt`: saves or deletes the current `(phase, prompt_key)` draft.
- `POST .../rerun`: reruns the phase. The body may include `draft_hash` and `prompt_key` for concurrency checks.

## 6. Memory And Hooks

LLM calls go through `harness/run_llm_step` and support pre/post hook chains. Common hooks:

- `memory_pre_llm` (`autoessay.memory.make_memory_pre_llm_hook`): reads relevant memory from `appleseed-memory` and inserts it into the system message.
- `citation_whitelist` (drafter): checks that source IDs in the output `claim_map` are in the approved set.
- `local_dedup` (drafter): checks n-gram overlap between paragraphs and local corpora.
- `ngram_guard` (stylist): checks whether revised paragraphs copy prior-paper wording too closely.
- Audit writer: writes request, response, and parse results to `run_dir/audit/*.jsonl`.

## 7. Runtime Guards

A runtime resilience review exposed two classes of issues: starting downstream phases without required user choices, and failing a whole drafter phase when only some sections failed schema validation. Follow-up work narrowed these cases into explicit readiness checks, concurrency guards, and degraded-completion behavior.

Four guard layers, from entrypoint down:

### 7.1 Shared Phase Readiness Registry

File: `backend/src/autoessay/phase_readiness.py`. Each phase has a `<phase>_ready(run, session) -> (ok, reason)` function:

| phase | Checks |
|---|---|
| curator | `discovery/skim_candidates.jsonl` or `sources/shortlist.json` has at least one non-empty source |
| synthesizer | `sources/shortlist.json` is non-empty |
| ideator | `synthesis/claims.jsonl` is non-empty |
| drafter | `has_selected_angle` (`novelty/selected_thesis.json` or latest `USER_NOVELTY_REVIEW` checkpoint has a non-empty `angle_id`) |
| stylist | `stylist_artifacts_ready` (`drafts/v???/manuscript.md` is non-empty, with `claim_map.jsonl` and `citations.bib`) |
| critic | `drafts/v???/style/paper_styled.md` is non-empty |
| integrity | `latest_external_scan_decision.approve == True` |
| exports | `drafts/v???/style/paper_styled.md` is non-empty |

`assert_phase_ready` converts `(ok, reason)` into HTTP 409 with `detail`. All `start_*` and `rerun_phase` paths call the same `assert_phase_ready`, so recovery paths cannot bypass the normal `start_*` guard.

The literature phases add two stricter constraints: `start_curator` requires the latest valid `USER_SEARCH_REVIEW` source-review checkpoint, and `start_synthesizer` requires the latest valid `USER_DEEP_DIVE_REVIEW` source-review checkpoint. `decision_payload` may store source IDs as a dict or list. Missing checkpoints, empty selections, and stale artifacts are all handled as 409s.

`activate_phase_version` does not call readiness because it only moves a head pointer and does not rerun an agent.

### 7.2 Drafter Degraded Completion

File: `backend/src/autoessay/agents/drafter.py`. Each section has a corrective retry budget controlled by `AUTOESSAY_DRAFTER_MAX_CORRECTIVE_RETRIES`, default 4. Exit semantics:

| Stub count / total sections | severity | run state |
|---|---|---|
| 0 / N | `null` | `phase_done` |
| 1 <= s <= N/2 | `amber_minor` | `phase_done` |
| N/2 < s < N | `amber_major` | `phase_done` |
| s == N | `fail_all_stubbed` | `FAILED_FIXABLE` |

Partial stubs no longer fail the phase. Downstream stylist can continue. `draft_metadata.json` includes `section_statuses[].is_stubbed` and `stubbed_section_ids`, which the UI renders as amber badges.

### 7.3 Atomic Phase-Start Lock

File: `backend/src/autoessay/phase_lock.py`, alembic `014_phase_lock`. Three columns:

```sql
runs.active_phase_lock              VARCHAR(64)  -- phase currently holding the lock
runs.active_phase_lock_job_id       VARCHAR(64)  -- owner token
runs.active_phase_lock_claimed_at   DATETIME     -- operational visibility
```

Acquire (`claim_phase_lock`): one-row `UPDATE runs SET ... WHERE id=:run_id AND active_phase_lock IS NULL`; `rowcount=0` means busy and returns 409. Release (`release_phase_lock`): owner-checked `UPDATE ... WHERE active_phase_lock=:phase AND active_phase_lock_job_id=:job_id`, so an old worker returning after a crash cannot clear a newer lock.

All `run_X` agent entrypoints wrap their bodies with `with phase_lock_release_on_exit(run_id, phase, lock_token, session=db_session):`; success, `FAIL_FIXABLE`, and exceptions all release. Passing `session=db_session` lets sync-worker mode, including tests, use the same session and avoid cross-database writes.

Workflow:

```
start_drafter -> assert_phase_ready -> claim_phase_lock(token=T1)
              -> enqueue_drafter_job(run_id, lock_token=T1)
              -> 200 Accepted

worker pickup -> run_drafter(run_id, lock_token=T1)
              -> with phase_lock_release_on_exit: ... agent body ...
              -> finally: release_phase_lock WHERE job_id=T1
```

Escape hatch: `POST /api/runs/{id}/clear-phase-lock` calls `force_clear_phase_lock` without owner checks and writes a `phase_lock_force_cleared` audit event.

`RunResponse.active_phase_lock: ActivePhaseLockResponse | None` exposes `{phase, job_id, claimed_at}` to the frontend, so the UI can show "phase X has been running for N minutes" plus a clear button.

### 7.4 Failure Recovery UI

`frontend/src/pages/WorkspacePage.tsx::FailureResolutionBanner` dispatches actions by state. SSE `state_transition` and `phase_failed` events refresh the banner; direct navigation into a blocked run makes `resolveLandingSubview` open the relevant tab for the failed phase.

| State | Action |
|---|---|
| `FAILED_FIXABLE` | "Retry phase" -> `/phases/{phase}/retry` backend resolver |
| `FAILED_VENDOR` | "Retry external scan" -> `startIntegrity`; "Skip integrity" -> `transitionRun(USER_FINAL_ACCEPTANCE)` |
| `FAILED_NEEDS_USER` | Amber copy that depends on payload context; no generic action yet |
| `FAILED_POLICY` | Direct retry disabled; use force-approve or phase-review-specific path |
| `CANCELLED` | Amber copy only; designed as terminal |

`DegradedDraftBanner` handles the amber partial-stub signal from Section 7.2 by reading `lastEvent.payload.severity` to distinguish minor from major.

### 7.5 Full-Text And User Upload Protection

Full-text retrieval has two layers:

1. `curator` first checks whether a candidate already has a direct PDF URL. If not, it calls a full-text resolver, bounded HTML parsing, and a bounded browser fallback to find a direct PDF before handing it to `pdf_fetcher`.
2. `pdf_fetcher` tries `httpx` first, then uses headless Chromium when `AUTOESSAY_PDF_FETCH_BROWSER_FALLBACK` is enabled, default true.

User-uploaded files live under `sources/uploads/` with metadata in `sources/user_upload_sources.json`. Scout and curator reruns use replacement semantics but only clean non-user-owned `sources/fulltext/` cache files. User-uploaded PDFs are outside cascade purge scope. Before rerun, the frontend shows a destructive confirmation listing affected candidates, shortlist entries, manual upload requests, and downstream artifact counts, while noting that user-uploaded PDFs are retained.

### 7.6 final_rewrite Quality Path

The deployable setting `AUTOESSAY_FINAL_REWRITE_ENABLED=1` enables the final_rewrite path. When `start_critic` is called from `USER_REVISION_REVIEW`, it first claims the `final_rewrite` lock:

1. Polish loop: uses v2 expert critic output for bounded targeted rewrite attempts and writes audit data to `drafts/v???/polish/polish_loop.json`.
2. Critic loop: runs review and rewrite for up to `AUTOESSAY_CRITIC_LOOP_ITERATIONS` iterations and chooses the best draft by quality metrics.
3. North-star gate sidecar: records independent blind A/B quality metrics inside the critic phase. Pass, fail, and unscorable outcomes do not block the user flow.

If exports fails policy checks, failure guidance becomes a new blocker for the polish executor for up to `AUTOESSAY_EXPORTS_POLICY_MAX_POLISH_RETRIES` attempts. If it still fails, the run remains `FAILED_POLICY`.

## 8. Frontend Testability Contract (`testid`)

Every new interactive UI element--`<button>`, `<input>`, `<textarea>`, `<select>`, tab button, and modal dialog--must include a `data-testid` attribute so Playwright e2e specs can use `page.locator('[data-testid="..."]')` instead of i18n strings.

Naming conventions:

- Use kebab case: `failure-resolution-banner`, `prompt-save-and-rerun`, `history-modal-close`.
- Use templates for dynamic elements: `phase-action-${action.key}`, `workspace-tab-${tab.id}`, `history-version-${phase}-${entry.version_no}`.
- Add behavioral suffixes such as `-button`, `-modal`, or `-textarea` only when needed to avoid ambiguity; prefer clean test IDs.

State attributes: in addition to `data-testid`, expose important state through `data-*` attributes:

- `data-run-state` and `data-run-id` on `workspace-root`, the entrypoint for specs waiting on state-machine progress.
- `data-last-event-type`, `data-last-event-phase`, and `data-last-event-at` for event-stream observability.
- `data-failed-phase` and `data-failure-state` on `FailureResolutionBanner`.
- `data-active="true|false"` for tabs and version rows.
- `data-is-active`, `data-version-id`, `data-version-no`, and `data-status` on `PhaseVersionRow`.

When adding components, if an existing i18n string is locatable but test IDs are incomplete, add test IDs instead of making specs depend on copy.

## 9. Deployment Shape

See `DEPLOYMENT.md`.
