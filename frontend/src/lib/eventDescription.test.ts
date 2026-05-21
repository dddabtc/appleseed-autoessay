import { describe, expect, it } from "vitest";

import type { RunEvent } from "./api";
import { describeEvent } from "./eventDescription";

// We don't exercise i18n correctness here — that's covered by HelpPage
// and CorpusPage tests. These tests cover dispatch logic and template-
// fill behavior inside describeEvent. Translator stub returns
// template-style strings so we can grep the substituted output.
function stubT(key: string): string {
  const fakeCatalog: Record<string, string> = {
    "phase.scout": "Literature search",
    "phase.curator": "Curation",
    "phase.synthesizer": "Synthesis",
    "phase.ideator": "Novelty",
    "phase.drafter": "Draft",
    "phase.stylist": "Style",
    "phase.critic": "Review",
    "phase.integrity": "Integrity",
    "phase.exports": "Exports",
    "phase.proposal": "Proposal",
    "console.timeline.run_created": "Essay run created.",
    "console.timeline.run_cancelled": "Essay run cancelled.",
    "console.timeline.state_transition": "Status: {from} → {to}.",
    "console.timeline.state_set": "Status: {to}.",
    "console.timeline.state_changed": "Status changed.",
    "console.timeline.phase_started": "{phase} started.",
    "console.timeline.phase_done": "{phase} finished.",
    "console.timeline.phase_failed": "{phase} failed.",
    "console.timeline.phase_failed_with_reason": "{phase} failed: {reason}",
    "console.timeline.phase_waiting": "{phase} is waiting for your input.",
    "console.timeline.source_progress": "Source {source}: {status}.",
    "console.timeline.section_progress": "Section {section}: {status}.",
    "console.timeline.proposal_saved": "Proposal saved.",
    "console.timeline.source_uploaded": "You uploaded a source PDF.",
    "console.timeline.checkpoint_recorded": "Review choice saved.",
    "console.timeline.force_approve": "You force-approved and continued.",
    "console.timeline.force_approve_with_reason":
      "You force-approved and continued. Reason: {reason}",
    "console.timeline.phase_lock_force_cleared":
      "Operations cleared a stuck lock on {phase}.",
    "console.timeline.scan_kinds_skipped": "Skipped checks: {kinds}.",
  };
  return fakeCatalog[key] ?? key;
}

const makeEvent = (
  event_type: string,
  payload: Record<string, unknown> = {},
): RunEvent => ({
  id: "ev_" + event_type,
  run_id: "run_test",
  event_type,
  payload,
  created_at: "2026-04-30T10:00:00.000Z",
});

describe("describeEvent", () => {
  it("phase_started fills in the translated phase label", () => {
    const out = describeEvent(stubT, makeEvent("phase_started", { phase: "drafter" }));
    expect(out).toBe("Draft started.");
  });

  it("phase_failed with reason renders both", () => {
    const out = describeEvent(
      stubT,
      makeEvent("phase_failed", {
        phase: "integrity",
        reason: "vendor 503",
      }),
    );
    expect(out).toBe("Integrity failed: vendor 503");
  });

  it("phase_failed without reason uses the short variant", () => {
    const out = describeEvent(stubT, makeEvent("phase_failed", { phase: "drafter" }));
    expect(out).toBe("Draft failed.");
  });

  it("state_transition fills both ends and uses arrow", () => {
    const out = describeEvent(
      stubT,
      makeEvent("state_transition", {
        from: "DRAFTER_RUNNING",
        to: "USER_DRAFT_REVIEW",
      }),
    );
    expect(out).toBe("Status: DRAFTER_RUNNING → USER_DRAFT_REVIEW.");
  });

  it("state_transition with only to uses state_set variant", () => {
    const out = describeEvent(
      stubT,
      makeEvent("state_transition", { to: "DRAFTER_RUNNING" }),
    );
    expect(out).toBe("Status: DRAFTER_RUNNING.");
  });

  it("source_progress fills source id and status", () => {
    const out = describeEvent(
      stubT,
      makeEvent("source_progress", {
        source_id: "doi:10.1234/abc",
        status: "fetched",
      }),
    );
    expect(out).toBe("Source doi:10.1234/abc: fetched.");
  });

  it("force_approve with reason includes the reason", () => {
    const out = describeEvent(
      stubT,
      makeEvent("force_approve", { reason: "manual review confirmed" }),
    );
    expect(out).toBe(
      "You force-approved and continued. Reason: manual review confirmed",
    );
  });

  it("scan_kinds_skipped joins arrays into a comma-separated list", () => {
    const out = describeEvent(
      stubT,
      makeEvent("scan_kinds_skipped", {
        scan_kinds: ["plagiarism", "ai_detection"],
      }),
    );
    expect(out).toBe("Skipped checks: plagiarism, ai_detection.");
  });

  it("unknown event_type falls back to the raw type name", () => {
    const out = describeEvent(
      stubT,
      makeEvent("some_future_event_we_dont_know_yet", { foo: "bar" }),
    );
    expect(out).toBe("some_future_event_we_dont_know_yet");
  });

  it("unknown phase id is preserved verbatim", () => {
    const out = describeEvent(
      stubT,
      makeEvent("phase_started", { phase: "wat_phase" }),
    );
    expect(out).toBe("wat_phase started.");
  });
});
