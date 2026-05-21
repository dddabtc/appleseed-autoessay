import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RunEvent } from "../lib/api";
import {
  STUCK_RUN_IDLE_THRESHOLD_SECONDS,
  StuckRunBanner,
} from "./StuckRunBanner";

const FIXED_NOW_MS = Date.UTC(2026, 4, 4, 12, 0, 0);

function isoMinutesAgo(minutes: number): string {
  return new Date(FIXED_NOW_MS - minutes * 60_000).toISOString();
}

function noop(): void {
  /* test stub */
}

describe("StuckRunBanner gating", () => {
  beforeEach(() => {
    vi.useFakeTimers({ now: FIXED_NOW_MS });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders when last phase event is older than the idle threshold", () => {
    const events: RunEvent[] = [
      {
        id: "evt_old",
        run_id: "run_test",
        event_type: "phase_started",
        payload: { phase: "synthesizer" },
        created_at: isoMinutesAgo(20),
      },
    ];
    const html = renderToString(
      <StuckRunBanner
        runId="run_test"
        currentState="SYNTHESIZER_RUNNING"
        recentEvents={events}
        activePhaseLockClaimedAt={isoMinutesAgo(20)}
        runUpdatedAt={isoMinutesAgo(20)}
        onRecovered={noop}
      />,
    );
    expect(html).toContain('data-testid="stuck-run-banner"');
    expect(html).toContain('data-stuck-phase="synthesizer"');
    expect(html).toContain('data-testid="stuck-run-banner-recover-button"');
    // Threshold is 15min and we set the last event 20min ago, so the
    // body text must report >= 20 minutes idle.
    expect(html).toMatch(/20|21/);
  });

  it("does NOT render when last phase event is within the idle threshold", () => {
    const events: RunEvent[] = [
      {
        id: "evt_fresh",
        run_id: "run_test",
        event_type: "phase_started",
        payload: { phase: "synthesizer" },
        created_at: isoMinutesAgo(0.5), // 30s ago
      },
    ];
    const html = renderToString(
      <StuckRunBanner
        runId="run_test"
        currentState="SYNTHESIZER_RUNNING"
        recentEvents={events}
        activePhaseLockClaimedAt={isoMinutesAgo(0.5)}
        runUpdatedAt={isoMinutesAgo(0.5)}
        onRecovered={noop}
      />,
    );
    expect(html).not.toContain('data-testid="stuck-run-banner"');
  });

  it("does NOT render for an unknown / non-running state", () => {
    const html = renderToString(
      <StuckRunBanner
        runId="run_test"
        currentState="USER_FIELD_REVIEW"
        recentEvents={[]}
        activePhaseLockClaimedAt={isoMinutesAgo(60)}
        runUpdatedAt={isoMinutesAgo(60)}
        onRecovered={noop}
      />,
    );
    expect(html).not.toContain('data-testid="stuck-run-banner"');
  });

  it("falls back to active_phase_lock claim time when no event matches phase", () => {
    // Codex amendment#3 fallback chain: event-payload phase mismatch
    // → use active_phase_lock.claimed_at next.
    const events: RunEvent[] = [
      {
        id: "evt_unrelated",
        run_id: "run_test",
        event_type: "state_transition",
        payload: { phase: "scout" }, // wrong phase
        created_at: isoMinutesAgo(5),
      },
    ];
    const html = renderToString(
      <StuckRunBanner
        runId="run_test"
        currentState="SYNTHESIZER_RUNNING"
        recentEvents={events}
        activePhaseLockClaimedAt={isoMinutesAgo(20)}
        runUpdatedAt={isoMinutesAgo(1)}
        onRecovered={noop}
      />,
    );
    expect(html).toContain('data-testid="stuck-run-banner"');
  });

  it("falls back to run.updated_at when no lock and no matching event", () => {
    const html = renderToString(
      <StuckRunBanner
        runId="run_test"
        currentState="SYNTHESIZER_RUNNING"
        recentEvents={[]}
        activePhaseLockClaimedAt={null}
        runUpdatedAt={isoMinutesAgo(20)}
        onRecovered={noop}
      />,
    );
    expect(html).toContain('data-testid="stuck-run-banner"');
  });

  it("derives the correct phase for tension_extraction running state", () => {
    // Codex amendment#1: TENSION_EXTRACTION_RUNNING was missing from
    // front-end RUNNING_STATES. Verify the reverse map handles it now.
    const events: RunEvent[] = [
      {
        id: "evt_old",
        run_id: "run_test",
        event_type: "phase_started",
        payload: { phase: "tension_extraction" },
        created_at: isoMinutesAgo(20),
      },
    ];
    const html = renderToString(
      <StuckRunBanner
        runId="run_test"
        currentState="TENSION_EXTRACTION_RUNNING"
        recentEvents={events}
        activePhaseLockClaimedAt={isoMinutesAgo(20)}
        runUpdatedAt={isoMinutesAgo(20)}
        onRecovered={noop}
      />,
    );
    expect(html).toContain('data-stuck-phase="tension_extraction"');
  });

  it("STUCK_RUN_IDLE_THRESHOLD_SECONDS matches backend default (15 min)", () => {
    // Backend ``_ZOMBIE_PHASE_IDLE_SECONDS_DEFAULT`` is 15 * 60 in
    // ``main.py``. Front + back must stay in sync — otherwise UI fires
    // banner too early and the recover endpoint just 409s.
    expect(STUCK_RUN_IDLE_THRESHOLD_SECONDS).toBe(15 * 60);
  });
});
