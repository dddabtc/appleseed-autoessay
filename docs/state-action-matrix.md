# State / Action / Endpoint Matrix

This table is the human-readable companion to
[`state_action_matrix.json`](./state_action_matrix.json). The JSON file
is authoritative and is checked by backend and frontend tests.

| Action | Source state(s) | Endpoint | Expected state / status | UI key / testid | Guard notes |
| --- | --- | --- | --- | --- | --- |
| Start proposal drafting | `DOMAIN_LOADED`, `USER_PROPOSAL_REVIEW` | `POST /api/runs/{run_id}/proposal` | `PROPOSAL_DRAFTING` | `proposal` / `phase-action-proposal` | User draft is safety-checked. |
| Accept proposal and start Scout | `USER_PROPOSAL_REVIEW` | `POST /api/runs/{run_id}/checkpoints/{checkpoint_type}` with `USER_PROPOSAL_REVIEW` | `SCOUT_RUNNING` | `proposal-accept` / `phase-action-proposal-accept`, `proposal-accept-button` | Checkpoint acceptance is the visible Scout path. |
| Start Scout directly | `DOMAIN_LOADED`, `USER_PROPOSAL_REVIEW` | `POST /api/runs/{run_id}/scout` | `SCOUT_RUNNING` | None | Used by proposal-less runs and retry dispatch. |
| Start curator | `USER_SEARCH_REVIEW` | `POST /api/runs/{run_id}/curator` | `CURATOR_RUNNING` | `curator` / `phase-action-curator` | Requires latest accepted `USER_SEARCH_REVIEW` source checkpoint. |
| Start synthesizer | `USER_DEEP_DIVE_REVIEW` | `POST /api/runs/{run_id}/synthesizer` | `SYNTHESIZER_RUNNING` | `synthesizer` / `phase-action-synthesizer` | Requires latest accepted `USER_DEEP_DIVE_REVIEW` source checkpoint. |
| Start tension extraction | `USER_FIELD_REVIEW`, `USER_TENSION_REVIEW` | `POST /api/runs/{run_id}/tension_extraction` | `TENSION_EXTRACTION_RUNNING` | None | Optional; gated by tension taxonomy settings and synthesis inputs. |
| Start framework lens | `USER_FIELD_REVIEW` | `POST /api/runs/{run_id}/framework_lens` | `FRAMEWORK_LENS_RUNNING` | `framework-lens` / `phase-action-framework-lens` | Lens is mandatory before ideator for `theory_article`. |
| Start ideator | `USER_FIELD_REVIEW`, `USER_LENS_REVIEW`, `USER_TENSION_REVIEW` | `POST /api/runs/{run_id}/ideator` | `IDEATOR_RUNNING` | `ideator` / `phase-action-ideator` | UI exposes field-review and lens-review paths; backend also accepts post-tension path. |
| Start drafter | `USER_NOVELTY_REVIEW`, `DRAFTER_RUNNING` | `POST /api/runs/{run_id}/drafter` | `DRAFTER_RUNNING` | `drafter` / `phase-action-drafter` | Requires selected novelty angle. |
| Start stylist | `DRAFTER_RUNNING`, `USER_REVISION_REVIEW` | `POST /api/runs/{run_id}/stylist` | `STYLIST_RUNNING` | `stylist` / `phase-action-stylist` | Sidebar waits for observed drafter `phase_done`; secondary status testid is `phase-action-stylist-waiting`. |
| Start final rewrite / critic | `USER_REVISION_REVIEW` | `POST /api/runs/{run_id}/critic` | `REWRITE_RUNNING` or `CRITIC_RUNNING` | `critic` / `phase-action-critic` | `AUTOESSAY_FINAL_REWRITE_ENABLED` selects rewrite-first vs direct critic. |
| Start integrity | `USER_EXTERNAL_SCAN_APPROVAL`, `FAILED_VENDOR` | `POST /api/runs/{run_id}/integrity` | `INTEGRITY_RUNNING` | None | Requires approved external scan checkpoint. |
| Start exports | `USER_FINAL_ACCEPTANCE` | `POST /api/runs/{run_id}/export` | `EXPORTS_RUNNING` | None | Requires export readiness. |
| Retry failed phase | `FAILED_FIXABLE` | `POST /api/runs/{run_id}/phases/{phase}/retry` | Resolver chooses start or rerun | `retry-{phase}` / `phase-action-retry-{phase}` | Backend retry resolver is scoped to `FAILED_FIXABLE`. |
| Retry failed needs-user phase | `FAILED_NEEDS_USER` | `POST /api/runs/{run_id}/phases/{phase}/retry` | `422` | `retry-{phase}` / `phase-action-retry-{phase}` | Current backend rejects because the resolver is `FAILED_FIXABLE` only. |
| Retry failed policy phase | `FAILED_POLICY` | `POST /api/runs/{run_id}/phases/{phase}/rerun` | `409` | `retry-{phase}` / `phase-action-retry-{phase}` disabled | Must use force-approve or policy-specific handling. |
| Force-approve failed phase | `FAILED_FIXABLE`, `FAILED_NEEDS_USER`, `FAILED_VENDOR`, `FAILED_POLICY` | `POST /api/runs/{run_id}/force-approve` | Phase-aware user review state | `force-approve-open-modal`, `force-approve-confirm` | Target state is computed from failed phase artifacts. |
| Generic failed-state back-edge | `FAILED_FIXABLE`, `FAILED_NEEDS_USER`, `FAILED_VENDOR`, `FAILED_POLICY` | `POST /api/runs/{run_id}/transitions` | `409` for `USER_*` targets | None | Generic transition endpoint must not bypass phase-aware recovery. |
