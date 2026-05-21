/**
 * Pure helpers for the phase-history modal (PR-A4.4).
 *
 * Extracted from WorkspacePage.tsx so vitest can table-test the
 * state derivation + action mapping without dragging in React.
 *
 * Codex AGREE-with-amendments 2026-05-02 (round-1 of frontend
 * review):
 *
 * 1. ``head_missing`` wins over every other flag.
 * 2. ``prompt_dirty`` wins over ``lineage_dirty`` (cancel /
 *    regenerate is the right primary action; lineage info still
 *    surfaces as secondary context).
 * 3. ``lineage_dirty`` alone → upstream_superseded.
 * 4. Else → generated.
 */
import type {
  PhaseHistoryEntry,
  PhaseHistoryVersionEntry,
} from "./api";

export type PhaseCardState =
  | "generated"
  | "prompt_edited"
  | "upstream_superseded"
  | "ungenerated";

export function deriveCardState(entry: PhaseHistoryEntry): PhaseCardState {
  if (entry.state_flags.head_missing) return "ungenerated";
  if (entry.state_flags.prompt_dirty) return "prompt_edited";
  if (entry.state_flags.lineage_dirty) return "upstream_superseded";
  return "generated";
}

export type PrimaryActionKey =
  | "rerun"
  | "edit_prompt"
  | "cancel_prompt"
  | "regenerate"
  | "activate_lineage_match"
  | "rerun_for_new_match"
  | "run_now"
  | "scroll_to_versions";

/**
 * Returns the ordered list of primary action keys for a card,
 * given its derived state and the entry's runnable_now /
 * versions data. Each action key maps to an i18n label + an
 * API call wired in the modal component.
 *
 * - generated: standard rerun + edit-prompt.
 * - prompt_edited: cancel + regenerate. (lineage_dirty info
 *   still rendered as secondary context, but doesn't change
 *   primary actions per codex amendment 5.)
 * - upstream_superseded: snap-to-upstream-match + rerun for new
 *   match.
 * - ungenerated: run_now (only if runnable_now=true) + scroll-to-
 *   versions (only if any versions exist to activate).
 */
// PR-I4.b A6: backend prompt registry only supports curator /
// synthesizer / ideator / critic / drafter / stylist (see
// `backend/src/autoessay/prompts.py`). Pre-fix `derivePrimaryActions`
// returned `edit_prompt` for every `generated` card regardless of
// phase, so scout / framework_lens / tension_extraction / integrity /
// exports cards rendered an "Edit prompt" button that 404'd on click.
// Mirror the registry here. Keep this list in sync with
// `WorkspacePage.tsx::PROMPT_EDITABLE_PHASES` (used by StaleBanner)
// and the backend `prompts.py` registry — PR-J2 will add a parity
// test that reads all three from one canonical source.
export const PROMPT_EDITABLE_PHASES: ReadonlySet<string> = new Set([
  "synthesizer",
  "ideator",
  "critic",
  "drafter",
  "stylist",
  "curator",
]);

export function derivePrimaryActions(
  entry: PhaseHistoryEntry,
): PrimaryActionKey[] {
  const state = deriveCardState(entry);
  switch (state) {
    case "generated":
      return PROMPT_EDITABLE_PHASES.has(entry.phase)
        ? ["rerun", "edit_prompt"]
        : ["rerun"];
    case "prompt_edited":
      return ["cancel_prompt", "regenerate"];
    case "upstream_superseded":
      return ["activate_lineage_match", "rerun_for_new_match"];
    case "ungenerated": {
      // Round-2 audit (always-render + disabled + hint): always
      // include run_now in the action list. The card decides
      // whether to disable it based on entry.runnable_now and
      // surfaces the dependency hint inline. Hiding it entirely
      // erased the affordance for unfinished phases — users could
      // not see "this phase exists, just not yet runnable."
      const actions: PrimaryActionKey[] = ["run_now"];
      if (entry.versions.length > 0) actions.push("scroll_to_versions");
      return actions;
    }
  }
}

/**
 * Maps a backend-supplied ``delete_block_reason`` to a stable
 * UI category the frontend i18n catalogue keys against. The
 * reason is NOT a closed enum (codex amendment 4); ``fork_point:<name>``
 * carries a branch name we render literally.
 *
 * Returns ``{ key, name? }``:
 * - key: i18n key suffix (e.g. "active_head" → ``workspace.history.delete_block.active_head``)
 * - name: optional dynamic value to interpolate (the branch
 *   name for ``fork_point``, the dependent label for
 *   ``downstream_dependent``)
 */
export interface DeleteBlockUI {
  key: string;
  interpolation: Record<string, string>;
}

export function describeDeleteBlock(
  version: PhaseHistoryVersionEntry,
): DeleteBlockUI | null {
  if (!version.delete_blocked) return null;
  const reason = version.delete_block_reason ?? "";
  if (reason === "active_head") {
    return { key: "active_head", interpolation: {} };
  }
  if (reason === "lineage_child") {
    return { key: "lineage_child", interpolation: {} };
  }
  if (reason.startsWith("fork_point:")) {
    return {
      key: "fork_point",
      interpolation: { branch: reason.slice("fork_point:".length) },
    };
  }
  // Fallback: anything else is a downstream-dependent label
  // (the backend writes "<phase> v<N>" or the
  // ``has_downstream_dependents`` summary).
  return { key: "downstream_dependent", interpolation: { name: reason } };
}

/**
 * Whether a [激活] button on a version row should be disabled.
 * Per codex amendment 4: status must be 'done' AND not the
 * current head.
 */
export function isActivateDisabled(version: PhaseHistoryVersionEntry): boolean {
  return version.is_head || version.status !== "done";
}

/**
 * Whether a [删除] button should be disabled.
 */
export function isDeleteDisabled(version: PhaseHistoryVersionEntry): boolean {
  return version.delete_blocked;
}

/**
 * Backend ``source`` field is an internal enum
 * (``agent`` / ``user_edit``). Per HANDOFF banned-term rules
 * (codex amendment 4) we never display ``agent`` to users.
 *
 * Mapping:
 * - "agent"      → key "default"   (rendered as nothing / pretend default)
 * - "user_edit"  → key "user_edit" (rendered as a "已用户编辑" badge)
 * - other        → key "other"     (rendered as the literal value lowercased)
 */
export function describeVersionSource(source: string): string {
  switch (source) {
    case "agent":
      return "default";
    case "user_edit":
      return "user_edit";
    default:
      return "other";
  }
}
