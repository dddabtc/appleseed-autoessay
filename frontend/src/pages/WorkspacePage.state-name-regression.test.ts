import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCE_PATH = join(__dirname, "WorkspacePage.tsx");

describe("WorkspacePage run-state name discipline", () => {
  it('does not reference a literal "DONE" state — state machine has EXPORTS_DONE, not DONE', () => {
    // 2026-05-12 regression: WorkspacePage stylist + novelty hint logic
    // checked currentState === "DONE", but the production state machine
    // (see backend/src/autoessay/state_machine.py) only has
    // "EXPORTS_DONE". The dead branch made EXPORTS_DONE runs render the
    // "上游节点尚未完成，无法启动文风润色" message instead of
    // "already_done".
    //
    // This static check catches any future re-introduction of the typo.
    // Allowed strings:
    //   - "EXPORTS_DONE"
    //   - "PROPOSAL_DONE" / "DRAFTER_DONE" / etc. (if added later)
    //   - "DONE" as a substring of a different identifier
    // Banned: a bare literal "DONE" string in a state-comparison context.
    const src = readFileSync(SOURCE_PATH, "utf-8");
    // Match: "DONE"  with quotes on both sides, but NOT prefixed by a
    // letter / digit / underscore (which would mean it's part of a
    // longer identifier like EXPORTS_DONE).
    const bareDone = src.match(/(?<![A-Z_])"DONE"/g);
    if (bareDone && bareDone.length > 0) {
      throw new Error(
        `WorkspacePage.tsx contains bare "DONE" state literal(s) — should be ` +
          `"EXPORTS_DONE" or another fully-qualified state name. Offending ` +
          `matches: ${JSON.stringify(bareDone)}`,
      );
    }
    expect(bareDone).toBeNull();
  });

  it("stylist already_done branch covers EXPORTS_DONE (and EXPORTS_RUNNING)", () => {
    // The fix landed by 2026-05-12 PR-363: the stylist hint conditional
    // must cover EXPORTS_DONE so a finished run does not advertise that
    // upstream is pending. Verify the relevant state names are present
    // inside the file (lightweight smoke for the conditional).
    const src = readFileSync(SOURCE_PATH, "utf-8");
    // Confirm the relevant clauses still reference EXPORTS_DONE
    const exportsDoneOccurrences = (src.match(/"EXPORTS_DONE"/g) || []).length;
    expect(exportsDoneOccurrences).toBeGreaterThanOrEqual(2);
  });

  it("PR-394: pastIdeatorStates covers every canonical post-ideator state", () => {
    // Real reproduction 2026-05-13 (Test #3 of the 4-test matrix):
    // auto-pilot in REWRITE_RUNNING showed "上游节点尚未完成" because
    // ``pastIdeatorStates`` was missing REWRITE_RUNNING (PR-360 slice
    // E added the state, the set wasn't updated). Also had stale
    // ``USER_DEEP_DIVE_REVIEW`` (pre-ideator, copy-paste bug) and
    // ``USER_DRAFT_REVIEW`` (not a canonical state).
    //
    // Pin every canonical post-ideator state present + the misplaced
    // ones absent. Codex AGREE 2026-05-13.
    const src = readFileSync(SOURCE_PATH, "utf-8");
    const startMarker = "const pastIdeatorStates = new Set<string | undefined>([";
    const startIdx = src.indexOf(startMarker);
    expect(startIdx).toBeGreaterThan(0);
    const endIdx = src.indexOf("]);", startIdx);
    expect(endIdx).toBeGreaterThan(startIdx);
    const block = src.slice(startIdx, endIdx);

    for (const state of [
      "USER_NOVELTY_REVIEW",
      "DRAFTER_RUNNING",
      "STYLIST_RUNNING",
      "USER_REVISION_REVIEW",
      "REWRITE_RUNNING",
      "CRITIC_RUNNING",
      "USER_EXTERNAL_SCAN_APPROVAL",
      "INTEGRITY_RUNNING",
      "USER_INTEGRITY_REVIEW",
      "USER_FINAL_ACCEPTANCE",
      "EXPORTS_RUNNING",
      "EXPORTS_DONE",
    ]) {
      expect(block, `pastIdeatorStates must include "${state}"`).toContain(
        `"${state}"`,
      );
    }

    // Misplaced states must NOT be in the set:
    expect(block).not.toContain('"USER_DEEP_DIVE_REVIEW"');
    expect(block).not.toContain('"USER_DRAFT_REVIEW"');
  });

  it("PR-394: framework_lens hint suppressed at post-lens states", () => {
    // Second user-reported instance of the same drift, 2026-05-13
    // (Test #4 mid-pipeline at USER_EXTERNAL_SCAN_APPROVAL still
    // showed "需要先完成综合节点后才能启动框架镜框"). Two root causes:
    // (a) ``synthesizerCompleted`` derived from recent events window
    //     went False once the synthesizer phase_done event aged out.
    // (b) The hint chain had no early-out for post-lens states.
    // Fix: add ``postSynthesizerStates`` (state-based fallback) +
    // ``postLensStates`` (suppress hint when lens phase is done).
    const src = readFileSync(SOURCE_PATH, "utf-8");
    expect(src).toContain("const postSynthesizerStates = new Set");
    expect(src).toContain("const postLensStates = new Set");
    // Spot-check critical states are in postLensStates.
    const postLensIdx = src.indexOf("const postLensStates = new Set");
    const postLensEnd = src.indexOf("]);", postLensIdx);
    const postLensBlock = src.slice(postLensIdx, postLensEnd);
    for (const state of [
      "REWRITE_RUNNING",
      "CRITIC_RUNNING",
      "USER_EXTERNAL_SCAN_APPROVAL",
      "USER_INTEGRITY_REVIEW",
      "USER_FINAL_ACCEPTANCE",
      "EXPORTS_DONE",
    ]) {
      expect(postLensBlock).toContain(`"${state}"`);
    }
  });

  it("PR-394: stylist already_done branch covers REWRITE_RUNNING", () => {
    // Twin of the pastIdeatorStates bug — REWRITE_RUNNING (post-stylist,
    // pre-critic) was missing from the stylist already_done branch so
    // the style tab also showed a stale upstream_pending hint.
    const src = readFileSync(SOURCE_PATH, "utf-8");
    // The fix wraps REWRITE_RUNNING into the already_done branch alongside
    // the other post-stylist canonical states.
    // Find the stylistHint branch (locator: ``stylistHint =``) and
    // assert REWRITE_RUNNING appears in the same conditional chain
    // that resolves to ``already_done`` (rather than the trailing
    // ``upstream_pending`` else branch).
    const stylistHintIdx = src.indexOf("const stylistHint =");
    expect(stylistHintIdx).toBeGreaterThan(0);
    const endIdx = src.indexOf(
      'workspace.style.disabled.upstream_pending',
      stylistHintIdx,
    );
    expect(endIdx).toBeGreaterThan(stylistHintIdx);
    const block = src.slice(stylistHintIdx, endIdx);
    expect(block).toContain('"REWRITE_RUNNING"');
    expect(block).toContain('workspace.style.disabled.already_done');
  });
});
