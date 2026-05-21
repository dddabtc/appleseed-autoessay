/**
 * Localized state labels per the codex-AGREEd 9-step grouping.
 *
 * The state machine has 22 phase states + a handful of terminal /
 * error states. Mapping every phase state to one of 9 user-visible
 * steps gives "(N/9) plain language" badges instead of bare
 * `PROPOSAL_DRAFTING` / `USER_FIELD_REVIEW` strings the user has to
 * decode. Terminal and error states get a label without the (N/9)
 * prefix.
 */

const TRANSLATION_KEY_PREFIX = "runs.state." as const;

/** Returns the translation key our i18n catalog uses for this state. */
export function runStateKey(state: string): string {
  return `${TRANSLATION_KEY_PREFIX}${state}`;
}

/**
 * Backend-mirror RUNNING_STATES (see
 * `backend/src/autoessay/phase_rerun.py::RUNNING_STATES`). Any
 * state where an agent is currently writing artifacts. UI must
 * suppress edit / mutate affordances while the run is in one of
 * these states — the corresponding PUT/POST endpoints all 409
 * with "another phase is currently running", so showing the
 * affordance just teaches the user to click and get rejected.
 *
 * This list also includes ``PROPOSAL_DRAFTING`` (special-cased
 * on the backend) which doesn't end in ``_RUNNING`` but is
 * semantically the same: an agent is mid-write.
 */
export const RUNNING_STATES = new Set<string>([
  "EXPRESS_RUNNING",
  "PROPOSAL_DRAFTING",
  "SCOUT_RUNNING",
  "CURATOR_RUNNING",
  "SYNTHESIZER_RUNNING",
  // PR-I3: backend ``phase_rerun.RUNNING_STATES`` and
  // ``main._PHASE_RUNNING_STATE`` both already include
  // ``TENSION_EXTRACTION_RUNNING``; the front end was missing it,
  // which meant ``isRunningState`` returned false during a stuck
  // tension_extraction run and ``StuckRunBanner`` would never fire
  // for that phase.
  "TENSION_EXTRACTION_RUNNING",
  "FRAMEWORK_LENS_RUNNING",
  "IDEATOR_RUNNING",
  "DRAFTER_RUNNING",
  "STYLIST_RUNNING",
  // Slice E final_rewrite phase, default-on via
  // AUTOESSAY_FINAL_REWRITE_ENABLED. Backend
  // ``phase_rerun.RUNNING_STATES`` and ``main._PHASE_RUNNING_STATE``
  // both register it; the front-end set was missing it, so
  // ``isRunningState`` returned false during a real rewrite run and
  // the StuckRunBanner / edit-affordance suppression never fired.
  "REWRITE_RUNNING",
  "CRITIC_RUNNING",
  "INTEGRITY_RUNNING",
  "EXPORTS_RUNNING",
]);

export function isRunningState(state: string | null | undefined): boolean {
  return state !== null && state !== undefined && RUNNING_STATES.has(state);
}

/**
 * Backend mirror of ``main._PHASE_RUNNING_STATE`` (reversed). Maps a
 * ``*_RUNNING`` (or ``PROPOSAL_DRAFTING``) state back to the
 * ``phase`` string the recover endpoint expects. Used by
 * ``StuckRunBanner`` (PR-I3) to know which phase to ask the
 * backend to recover.
 *
 * Adding a new running state requires adding both an entry here AND
 * an entry in ``backend/src/autoessay/main.py::_PHASE_RUNNING_STATE``;
 * the two must stay in sync or the recover button will 404 on the
 * unknown phase.
 */
export const RUNNING_STATE_TO_PHASE: Record<string, string> = {
  EXPRESS_RUNNING: "express",
  PROPOSAL_DRAFTING: "proposal",
  SCOUT_RUNNING: "scout",
  CURATOR_RUNNING: "curator",
  SYNTHESIZER_RUNNING: "synthesizer",
  TENSION_EXTRACTION_RUNNING: "tension_extraction",
  FRAMEWORK_LENS_RUNNING: "framework_lens",
  IDEATOR_RUNNING: "ideator",
  DRAFTER_RUNNING: "drafter",
  STYLIST_RUNNING: "stylist",
  REWRITE_RUNNING: "final_rewrite",
  CRITIC_RUNNING: "critic",
  INTEGRITY_RUNNING: "integrity",
  EXPORTS_RUNNING: "exports",
};

/**
 * Translate a backend state name to a human label. Falls back to the
 * raw state name when no translation is registered, so a brand-new
 * state added on the backend still renders something readable in the
 * UI before the i18n catalog catches up.
 */
export function formatRunState(
  t: (key: string) => string,
  state: string | null | undefined,
): string {
  if (!state) return "";
  const key = runStateKey(state);
  const translated = t(key);
  // useT returns the key itself when no entry is found.
  return translated === key ? state : translated;
}
