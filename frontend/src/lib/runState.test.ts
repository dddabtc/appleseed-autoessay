import { describe, expect, it } from "vitest";

import {
  RUNNING_STATES,
  formatRunState,
  isRunningState,
  runStateKey,
} from "./runState";

const en = (key: string): string => {
  const table: Record<string, string> = {
    "runs.state.PROPOSAL_DRAFTING": "(1/9) Drafting proposal…",
    "runs.state.DRAFTER_RUNNING": "(6/9) Drafting paper…",
    "runs.state.EXPORTS_DONE": "Done",
    "runs.state.CANCELLED": "Cancelled",
    "runs.state.FAILED_VENDOR": "External service failure",
  };
  return key in table ? table[key] : key;
};

describe("formatRunState", () => {
  it("returns the localized label for a known phase state", () => {
    expect(formatRunState(en, "PROPOSAL_DRAFTING")).toBe(
      "(1/9) Drafting proposal…",
    );
    // Locking the codex-corrected numbering: PROPOSAL_DRAFTING is
    // step 1, not step 2.
    expect(formatRunState(en, "PROPOSAL_DRAFTING")).toMatch(/^\(1\/9\)/);
    expect(formatRunState(en, "DRAFTER_RUNNING")).toBe("(6/9) Drafting paper…");
  });

  it("strips the (N/9) prefix for terminal/error states", () => {
    expect(formatRunState(en, "EXPORTS_DONE")).toBe("Done");
    expect(formatRunState(en, "CANCELLED")).toBe("Cancelled");
    expect(formatRunState(en, "FAILED_VENDOR")).toBe(
      "External service failure",
    );
  });

  it("falls back to the raw state when no key registered", () => {
    expect(formatRunState(en, "BRAND_NEW_STATE")).toBe("BRAND_NEW_STATE");
  });

  it("handles null / undefined / empty state", () => {
    expect(formatRunState(en, null)).toBe("");
    expect(formatRunState(en, undefined)).toBe("");
    expect(formatRunState(en, "")).toBe("");
  });

  it("runStateKey produces stable prefix", () => {
    expect(runStateKey("DRAFTER_RUNNING")).toBe("runs.state.DRAFTER_RUNNING");
  });
});

describe("RUNNING_STATES + isRunningState", () => {
  it("includes every backend RUNNING state + PROPOSAL_DRAFTING", () => {
    const expected = [
      "PROPOSAL_DRAFTING",
      "SCOUT_RUNNING",
      "CURATOR_RUNNING",
      "SYNTHESIZER_RUNNING",
      // PR-I3: tension_extraction was already in backend RUNNING
      // states; front-end set was missing it.
      "TENSION_EXTRACTION_RUNNING",
      "FRAMEWORK_LENS_RUNNING",
      "IDEATOR_RUNNING",
      "DRAFTER_RUNNING",
      "STYLIST_RUNNING",
      // Slice E final_rewrite phase (default-on via
      // ``AUTOESSAY_FINAL_REWRITE_ENABLED``). Backend
      // ``phase_rerun.RUNNING_STATES`` and
      // ``main._PHASE_RUNNING_STATE`` already register it; the
      // front-end set used to be missing it (regression fixed in the
      // unified PhaseRunningBanner PR).
      "REWRITE_RUNNING",
      "CRITIC_RUNNING",
      "INTEGRITY_RUNNING",
      "EXPORTS_RUNNING",
    ];
    for (const s of expected) {
      expect(RUNNING_STATES.has(s)).toBe(true);
    }
    // The set must NOT include quiescent USER_*_REVIEW states.
    expect(RUNNING_STATES.has("USER_FIELD_REVIEW")).toBe(false);
    expect(RUNNING_STATES.has("USER_NOVELTY_REVIEW")).toBe(false);
    expect(RUNNING_STATES.has("USER_LENS_REVIEW")).toBe(false);
  });

  it("isRunningState returns true for RUNNING / DRAFTING states", () => {
    expect(isRunningState("IDEATOR_RUNNING")).toBe(true);
    expect(isRunningState("PROPOSAL_DRAFTING")).toBe(true);
    expect(isRunningState("FRAMEWORK_LENS_RUNNING")).toBe(true);
    expect(isRunningState("TENSION_EXTRACTION_RUNNING")).toBe(true);
    expect(isRunningState("REWRITE_RUNNING")).toBe(true);
  });

  it("isRunningState returns false for quiescent / error states", () => {
    expect(isRunningState("USER_FIELD_REVIEW")).toBe(false);
    expect(isRunningState("USER_LENS_REVIEW")).toBe(false);
    expect(isRunningState("EXPORTS_DONE")).toBe(false);
    expect(isRunningState("FAILED_FIXABLE")).toBe(false);
  });

  it("isRunningState handles null / undefined / unknown", () => {
    expect(isRunningState(null)).toBe(false);
    expect(isRunningState(undefined)).toBe(false);
    expect(isRunningState("BRAND_NEW_STATE")).toBe(false);
  });
});
