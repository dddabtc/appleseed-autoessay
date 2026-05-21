/**
 * PR-I4.a → PR-I5 — Smart retry routing helper.
 *
 * History:
 *
 *   - PR-I4.a (initial): static frontend heuristic that picked
 *     start vs rerun by failure_class, with a one-shot 409 fallback.
 *     Bridged the user-visible split-brain bug while a real backend
 *     resolver was designed.
 *   - PR-I5 (current): backend ``POST /retry`` resolver
 *     (``main.py::retry_failed_phase``) has full server-side context
 *     (has_completed_output, latest event payload, lock state) and
 *     authoritatively picks the path. The frontend just calls it
 *     and consumes the result.
 *
 * Why we keep this module: callers (the workspace status-panel
 * handler + FailureResolutionBanner button) want a single uniform
 * `(runId, phase) → result` shape, plus the export of
 * `pickRetryStrategy` for the unit tests + tooling that documents
 * the static classification (helpful as documentation + a regression
 * net if we ever need to revert the backend resolver).
 *
 * Migration plan: PR-I5 leaves smartRetry as a thin wrapper around
 * the backend endpoint. `failureClass` and `startCaller` args are
 * retained on the public API for backwards compatibility with the
 * call sites and `pickRetryStrategy` tests, but they are no longer
 * consulted at runtime — the backend determines routing and either
 * dispatches the right path itself or returns a 422 with a clear
 * `code` discriminator (`not_failed_fixable` / `phase_mismatch` /
 * `guidance_required`). Callers should surface that discriminator
 * verbatim instead of falling back to start/rerun client-side.
 */

import { retryFailedPhaseEndpoint, type RetryResponse } from "./api";

// Failure classes emitted by graceful agent paths — the phase wrote
// some artifact (or had at least transitioned out of the running
// state cleanly) before deciding it could not finish.
//
// - ``failed_fixable``: agent emitted graceful "user-fixable" failure
//   (e.g., synthesizer "0 of 6 sources processed").
// - ``failed_vendor``: external service rejection that the agent
//   classified as recoverable.
// - ``failed_policy``: policy gate rejection (e.g., integrity blocker).
//
// These all imply ``rerun_phase`` is the right path: artifact may
// exist + assert_can_rerun is the readiness check, not state-rewind.
const GRACEFUL_FAILURE_CLASSES: ReadonlySet<string> = new Set([
  "failed_fixable",
  "failed_vendor",
  "failed_policy",
]);

// Failure classes emitted when the worker died mid-flight (no clean
// transition out, partial sentinel may be on disk):
//
// - ``zombie_recovered``: PR-I1 / PR-I2.a / PR-I3 ``/recover`` saw
//   a stuck *_RUNNING run and force-transitioned it to FAILED_FIXABLE.
// - ``phase_runtime_error``: PR-I2.b common failure boundary caught
//   an unhandled Python exception in ``run_with_versioning``.
//
// These match backend ``_PARTIAL_FAILURE_CLASSES`` (``main.py``).
// PR-I3.b's ``_recover_failed_fixable_for_phase`` rewinds state on
// these classes even when ``has_completed_output=True``, so
// ``start_<phase>`` is the right path.
const PARTIAL_FAILURE_CLASSES: ReadonlySet<string> = new Set([
  "zombie_recovered",
  "phase_runtime_error",
]);

export type RetryStrategy = "start" | "rerun";

/**
 * Pick the primary retry strategy for a phase based on its latest
 * ``phase_failed`` event's ``failure_class``. Defaults to ``"start"``
 * when the class is unknown.
 *
 * PR-I5 note: this function is no longer consulted at runtime by
 * `smartRetry` — the backend resolver is authoritative. Kept for
 * regression tests + documentation of the static classification.
 */
export function pickRetryStrategy(
  failureClass: string | null | undefined,
): RetryStrategy {
  if (typeof failureClass !== "string" || failureClass.length === 0) {
    return "start";
  }
  if (PARTIAL_FAILURE_CLASSES.has(failureClass)) return "start";
  if (GRACEFUL_FAILURE_CLASSES.has(failureClass)) return "rerun";
  return "start";
}

/**
 * Call the backend retry resolver. The backend authoritatively
 * picks start vs rerun via its decision tree (`main.py::
 * retry_failed_phase`). On success returns the resolver's
 * `RetryResponse` so the UI can label the action it took.
 *
 * `failureClass` and `startCaller` args are retained for backwards
 * compatibility with the call sites that already pass them (PR-I4.a
 * shape). They are no longer used — drop them in PR-I5 followups
 * once call sites are updated.
 */
export async function smartRetry(args: {
  runId: string;
  phase: string;
  failureClass?: string | null;
  startCaller?: (runId: string, phase: string) => Promise<unknown>;
}): Promise<RetryResponse> {
  const { runId, phase } = args;
  return retryFailedPhaseEndpoint(runId, phase);
}
