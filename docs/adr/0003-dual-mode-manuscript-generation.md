# ADR-0003: Dual-mode Manuscript Generation

Date: 2026-05-18

Status: Draft for review

Decision owner: codex drafts, reviewer provides adversarial review, user signs off before any product-code PR ships

## Context

The independent replication run in `docs/experiments/abc-architecture-comparison/results-replication/` is now the binding evidence base for this decision.

The replicated A/E/G result says:

- Arm A, the current production Appleseed 13-phase path, has an overall median score of `5.67`.
- Arm E, ARS `academic-paper` full-mode simulated as one single LLM manuscript call, has an overall median score of `7.25`.
- E beats A on `10/10` fresh kernels.
- The replication verdict states this is a binding architectural verdict for the protocol: the 13-phase production Appleseed stack underperforms the ARS single-call route.
- Token cost is not close: A uses roughly `0.85M-1.49M` tokens per kernel in the replication run, while E uses roughly `28.7K-32.0K` tokens. The planning shorthand is `deep ~= 1.17M` tokens and `express ~= 30K` tokens per run.

The current production manuscript path is the 13-phase Appleseed pipeline:

```text
proposal -> scout -> curator -> synthesizer -> tension_extraction
-> framework_lens -> ideator -> drafter -> stylist -> final_rewrite
-> critic -> integrity -> exports
```

This path is tightly coupled to:

- `paired_runner` / critic candidate selection and repair loops;
- the 5-part critic rubric and review artifacts;
- review-gate UX between phases;
- `auto_advance` / auto-pilot, which drives the same review gates automatically;
- phase-version history, run heads, phase locks, retries, failure recovery, and export handoff.

The user decision is not to delete the 13-phase path. The product should expose two explicit versions and let the user choose.

## Decision

We will support two manuscript generation modes side by side:

- `mode=express`: ARS `academic-paper` full-mode, single-call manuscript generation.
- `mode=deep`: the existing 13-phase Appleseed authoring workflow.

`deep` remains the compatibility mode for all existing and in-flight runs. `express` becomes the default selection for new runs after the UI mode selector ships.

The default is `express` because the replicated evidence is decisive on quality and cost: E beats A on all 10 kernels and is roughly two orders of magnitude cheaper in token use. The UX trade-off is that `deep` has better inspectability, phase preview, and manual gates. That trade-off should be made visible in the selector, not hidden by keeping the weaker and more expensive path as default.

Rollout strategy chooses option (b): P2 directly defaults new runs to `express`, with a server-side emergency rollback flag. Keeping P2 defaulted to `deep` would keep the lower-scoring, higher-cost path as the main product path after binding replication evidence. A 50/50 A/B default test would add operational complexity and slow the product decision for a small initial run volume. The rollback path is configuration, not code revert.

`MANUSCRIPT_DEFAULT_MODE` is the server-side default-mode feature flag:

- Allowed values: `express` or `deep`.
- Default value: `express`.
- It controls only the new-run UI preselection and the backend fallback when a create-run API request omits `mode`.
- It does not mutate `generation_mode` on any already-created run.
- Setting it to `deep` is the emergency rollback path for new-run defaults only; it is not runtime fallback from a failed express run.

The backend should use a semi-independent task router:

- Add a persisted generation mode to each run. The API/user-facing field is `mode`; the database column is `generation_mode` to avoid collision with the existing `paper_mode` field.
- `mode=deep` dispatches to the current state machine with minimal changes.
- `mode=express` dispatches to a separate express manuscript runner that reuses shared run ownership, auth, events, locks, artifact storage, provenance, and token accounting, but does not impersonate the 13-phase pipeline.
- The router must reject unknown modes and must not silently fall back from one mode to the other.

The production express flow is:

```text
ARS academic-paper single-call -> audit-only critic -> integrity audit-only
-> humanizer -> export
```

The replicated E arm was a bare ARS manuscript used for architecture evidence. Production `express` must not stop at that raw output. It invokes the existing integrity, humanizer, and export modules without changing their internals. The integrity invocation is audit-only for express: it records findings for provenance/transparency and must not route express back into deep repair gates.

The post-generation express critic is a single-shot audit, not a repair loop:

- It runs once after the ARS manuscript call.
- It produces a structured audit report for citation traceability, target word count, and style compliance.
- Audit failure does not block shipping the manuscript artifact.
- Audit pass/fail and summary details must be shown in the express transparency panel.
- It is not the 5-rubric critic repair loop and it does not create phase previews.

The frontend should use a top-level mode selector on new-run creation:

- It should be separate from the existing `paper_mode` selector. `paper_mode` controls article/research shape such as case analysis or theory article; `mode` controls generation architecture.
- The preselected value should come from `MANUSCRIPT_DEFAULT_MODE`; with default config, `express` is preselected once P2 ships.
- `deep` should remain equally visible as the audit-heavy, phase-reviewable option.
- A one-time first-login preference is rejected because the choice is run-specific and affects cost, latency, transparency, and artifact structure.
- Existing run workspaces should show the stored mode as read-only once generation begins.

## Risks And Mitigations

### State Machine Boundary

Risk: Express mode could accidentally enter `paired_runner`, `critic_loop`, review gates, or auto-pilot paths designed for the 13-phase state machine.

Mitigation: `express` is not a synthetic shortcut through `drafter -> stylist -> final_rewrite -> critic`. It has its own runner and mode-prefixed states/events such as `EXPRESS_RUNNING` and `EXPRESS_DONE`; it must not reuse `DRAFTER_RUNNING`, `REWRITE_RUNNING`, or `CRITIC_RUNNING`. It bypasses `paired_runner`, `critic_loop`, the 5-critic-rubric repair loop, review gates, and `auto_advance`. This is an explicit product trade-off: express buys quality/cost/latency but gives up phase-level review and critic-loop repair.

For clarity, `auto_advance=true` must be rejected for `mode=express`. It must not appear as enabled-but-ignored.

### Data Model

Risk: Overloading the existing `paper_mode` would confuse article shape with architecture mode.

Mitigation: Add a separate persisted run field. Existing rows are backfilled as `deep`; the database default is `deep` for migration safety and old writers during deploy; runtime API fallback after P1/P2 code ships is controlled by `MANUSCRIPT_DEFAULT_MODE`. New UI-created runs send an explicit `mode`. `RunResponse` and run-list payloads expose the stored mode so the UI can render it without inference.

P1 migration strategy:

- Use a single Alembic transaction for small SQLite or Postgres deployments:

  ```sql
  ALTER TABLE runs
    ADD COLUMN generation_mode VARCHAR(16) DEFAULT 'deep' NOT NULL;
  ```

- Deploy order should be migration first, then app code that reads/writes `generation_mode`, so old app instances can keep creating `deep` rows through the database default during a rolling deploy.
- If the table is materially larger at deploy time or the active SQL dialect cannot add a non-null default column quickly, switch to a two-stage migration: add nullable column, batch backfill `generation_mode='deep'`, then set `NOT NULL` and default in a second migration.
- Rollback before dependent app code is deployed:

  ```sql
  ALTER TABLE runs DROP COLUMN generation_mode;
  ```

  On SQLite versions without native `DROP COLUMN`, the Alembic rollback must use `batch_alter_table` table rebuild. After app code depends on the column, rollback order is app revert first, then schema rollback.

Mode should be immutable after generation begins. Changing architecture mid-run would invalidate phase history, artifact expectations, cost accounting, and recovery semantics.

### LLM Cost

Risk: Deep runs are materially more expensive, and users cannot make an informed choice without seeing the cost difference.

Mitigation: P1 must record prompt/completion/total tokens for express in the same audit vocabulary used by experiment artifacts. P2 should show an estimated cost/latency band in the selector. P3 should add actual per-run cost telemetry and a small comparison report across modes.

Express also has a hard budget guardrail:

- `express_token_cap` defaults to `100K` total tokens per run.
- The cap covers the ARS manuscript call and the post-generation audit-only critic call.
- The runner should refuse the run before dispatch if estimated prompt plus requested output budget exceeds the cap.
- If provider-reported usage exceeds the cap after a call, the run fails as `express_budget_exceeded`.
- The runner must not silently truncate output and must not silently fall back to `deep`.

Cost meter is not a blocker for a backend-only P1, but it is required before broad user-facing rollout.

### Express Failure Protocol

Risk: A single-call express runner could fail in ways that are ambiguous to users or expensive to retry.

Mitigation: Express failure states must be explicit:

- Retry limit is one retry for retryable transport/provider errors. Budget failures, cancellation, and deterministic validation failures are not retried.
- Cancellation: if the user cancels during express generation, the runner must cancel the in-flight LLM request when the client/provider supports it, ignore any late response, and mark the run `express_cancelled`.
- Timeout: default single-call timeout is 5 minutes. Timeout marks the run failed as `express_timeout`; the user may explicitly rerun express or create a new deep run.
- Partial/truncated output: if the provider returns a partial completion, finish-reason truncation, or output that fails manuscript completeness checks, mark `express_truncated` and do not ship it as the manuscript artifact.
- No failure path may silently convert the run to `deep`.

### In-flight Runs

Risk: Migration could strand existing 13-phase runs or change their behavior.

Mitigation: All existing and in-flight runs are marked `deep`; their state machine, locks, retries, phase history, review gates, and auto-pilot behavior continue unchanged. Express is only selectable at run creation before any generation starts.

### Transparency Loss

Risk: Express mode has no phase preview UX, so users lose visibility into source selection, draft evolution, and repair decisions.

Mitigation: Express must provide a compact transparency panel:

- stored prompt/provenance summary;
- token usage;
- model/provider;
- generated outline/section map when parseable;
- citation/word-count/style audit summary from the post-generation audit-only critic;
- final manuscript preview;
- explicit "regenerate express" and "start a new deep run" paths instead of hidden phase reruns.

Prompt-internal checks from the ARS call may be stored as provenance, but they do not replace the independent audit-only critic. This does not recreate deep review gates. It gives enough inspection surface to make express auditable without pretending it is phase-based.

### Silent Fallback

Risk: A failed express run could silently re-run through deep, or an omitted mode could silently choose a mode the user did not intend.

Mitigation: No runtime fallback between modes. If express fails, the run fails as express with clear recovery options. The user may explicitly create a new deep run or rerun express. The new UI always sends `mode`; backend compatibility for clients that omit `mode` is governed by `MANUSCRIPT_DEFAULT_MODE`, and the chosen value is persisted immediately on run creation.

## Phase Roadmap

### P1: Backend Mode Routing And Express Runner

Scope:

- Add persisted run generation mode with `deep` backfill.
- Add `MANUSCRIPT_DEFAULT_MODE` config with default `express`; use it only for omitted create-run `mode` and UI default discovery.
- Add API validation for `mode=express|deep`.
- Add a mode router that keeps deep on the existing state machine and routes express to a separate runner.
- Implement express runner enough to produce a canonical manuscript artifact, one post-generation audit-only critic report, provenance, token usage, run events, and terminal success/failure state.
- Enforce `express_token_cap` default `100K`, one retry maximum, 5 minute timeout, cancellation handling, and explicit `express_budget_exceeded`, `express_cancelled`, `express_timeout`, and `express_truncated` failure outcomes.
- Invoke existing integrity, humanizer, and export paths after express manuscript generation without changing those modules' internals.
- Add unit and API tests for mode validation, backfill behavior, in-flight deep compatibility, and no silent fallback.

Deliverable: backend can create and complete an express run through an internal/API path, while existing deep runs behave unchanged.

Estimated PR count: 2 PRs.

### P2: UI Mode Selector And Workspace Disclosure

Scope:

- Add top-level "Generation mode" selector to new-run creation.
- Default new runs from `MANUSCRIPT_DEFAULT_MODE`; default deployment config selects `express`.
- Keep `deep` visible and selectable.
- Show read-only mode badges in run list and workspace.
- Add express-specific workspace result view with the compact transparency panel.
- Ensure `paper_mode` and generation `mode` are visually and semantically distinct.

Deliverable: users can explicitly choose express or deep before starting a run, and can see which mode a run used afterward.

Estimated PR count: 1-2 PRs.

### P3: Data-driven Closure

Scope:

- Add per-run cost/latency/quality telemetry by mode.
- Build a lightweight mode comparison report using real production-like runs.
- Define thresholds for whether express becomes the long-term default, whether deep remains first-class, and whether any hybrid should be reconsidered.
- Write an ADR delta if the rollout data changes the default, scope, or review-gate policy.

Deliverable: production evidence replaces experiment-only evidence for the long-term default decision.

Estimated PR count: 2 PRs.

## Scope Cuts

This ADR does not authorize:

- production auth, Casdoor/native-auth, session, or user model changes;
- broad database redesign beyond the minimal mode column and necessary indexes/defaults;
- changes to humanizer, integrity, or export internals;
- changes to `paired_runner` internals;
- changes to the current deep state-machine order;
- converting ARS F arm / true ARS multi-stage into production;
- making express pretend to have 13 phase previews;
- runtime fallback from express to deep or deep to express.

The F arm exclusion is not permanent. P3 should reconsider true multi-stage ARS only if production telemetry shows `express` quality is materially weak in a sub-domain, for example a median quality score below `6.0` for literature, philosophy, or another stable segment with enough samples. That trigger authorizes a new production experiment proposal for F; it does not make F part of P1 or P2.

## Alternatives Considered

### A. Cut 13-phase And Move Everything To ARS

Rejected by user decision. It follows the evidence most aggressively, but it removes reviewability, phase history, source workflow, and existing deep-run UX too abruptly.

### B. Dual-mode Express + Deep

Accepted. It respects the binding ARS evidence while keeping the existing 13-phase path available for users who need phase-level review, auditability, and manual control.

### C. Use ARS As A Replacement For One 13-phase Step

Rejected for this ADR. A hybrid could place ARS inside `drafter`, `final_rewrite`, or `critic`, but that would preserve much of the complex state machine while blurring artifact ownership. It also fails to test the clearest evidence-backed path: ARS single-call as its own explicit mode.

### D. First-login Global Preference

Rejected. A global preference is too sticky for a choice that depends on a run's cost tolerance, transparency needs, and deadline. The selector belongs at run creation.

## Reviewer Audit Checklist

Reviewer should audit whether the ADR:

1. Grounds the decision in `results-replication`, not only the older P0 reset.
2. States the E vs A result accurately: median `7.25` vs `5.67`, E>A on `10/10` kernels.
3. Justifies `express` as the default without hiding the transparency trade-off.
4. Keeps `mode` separate from existing `paper_mode`.
5. Requires explicit mode selection in the new UX and rejects silent runtime fallback.
6. Preserves legacy and in-flight deep runs.
7. Defines whether mode is mutable and where immutability begins.
8. Draws a hard backend boundary between express runner and deep state machine.
9. Explicitly says whether `paired_runner`, `critic_loop`, 5-rubric critic, review gates, and auto-pilot apply to express.
10. Defines enough express artifacts for audit, debugging, and user trust.
11. Handles token/cost accounting and the `express ~= 30K` vs `deep ~= 1.17M` gap.
12. Covers express failure, retry, cancellation, timeout, truncation, token cap, and no-fallback behavior.
13. Keeps P1/P2/P3 separable and reviewable as independent PRs.
14. Avoids scope creep into auth, export, humanizer, integrity, `paired_runner`, or F-arm productionization.
15. Names the UI entry point clearly enough that implementation cannot bury mode choice in secondary settings.
16. Defines `MANUSCRIPT_DEFAULT_MODE` as a default-selection feature flag, not a stored-run mutator.
17. Leaves reviewer questions open for critique instead of prescribing reviewer approval.
