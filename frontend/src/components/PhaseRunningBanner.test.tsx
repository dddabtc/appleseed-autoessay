import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { RunEvent } from "../lib/api";
import { PhaseRunningBanner } from "./PhaseRunningBanner";

function event(payload: Record<string, unknown>, id = "evt_1"): RunEvent {
  return {
    id,
    run_id: "run_test",
    event_type: "section_progress",
    payload,
    created_at: "2026-05-08T07:30:00.000Z",
  };
}

describe("PhaseRunningBanner", () => {
  it("renders the per-phase banner with role=status and pulse dot", () => {
    const html = renderToString(<PhaseRunningBanner phase="drafter" />);
    expect(html).toContain('data-testid="phase-running-banner-drafter"');
    expect(html).toContain('role="status"');
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain("animate-pulse");
  });

  it("falls back to a starting hint when no progress is provided", () => {
    const html = renderToString(<PhaseRunningBanner phase="critic" />);
    // The banner step line carries a deterministic testid for e2e
    // assertions ("starting up…" copy varies per locale).
    expect(html).toContain('data-testid="phase-running-banner-critic-step"');
  });

  it("derives a 'currently working on step N+1' line from progress events", () => {
    // 6/8 sections done — banner should hint "currently on step 7".
    const html = renderToString(
      <PhaseRunningBanner
        phase="drafter"
        progress={[
          event({ section_title: "六、案例", completed: 6, total: 8 }, "evt_a"),
        ]}
      />,
    );
    expect(html).toContain("7");
    expect(html).toContain("8");
  });

  it("switches to a finalizing hint when completed >= total", () => {
    const html = renderToString(
      <PhaseRunningBanner
        phase="drafter"
        progress={[
          event({ section_title: "八、结论", completed: 8, total: 8 }, "evt_b"),
        ]}
      />,
    );
    // The finalizing line replaces the in-progress step rather than
    // showing "step 9 / 8" — codex Q6 guard against off-by-one. The
    // step <p> carries a stable testid so we can scope the assertion
    // to that element rather than scanning the whole HTML (Tailwind
    // colour tokens contain digit fragments).
    const stepMatch = html.match(
      /data-testid="phase-running-banner-drafter-step"[^>]*>([^<]+)</,
    );
    expect(stepMatch).not.toBeNull();
    expect(stepMatch?.[1]).not.toContain("9");
  });

  it("renders progress for tension_extraction without crashing on missing total", () => {
    const html = renderToString(
      <PhaseRunningBanner
        phase="tension_extraction"
        progress={[event({ completed: 2 }, "evt_c")]}
      />,
    );
    expect(html).toContain(
      'data-testid="phase-running-banner-tension_extraction"',
    );
  });

  it("supports the slice-E final_rewrite phase", () => {
    const html = renderToString(<PhaseRunningBanner phase="final_rewrite" />);
    expect(html).toContain('data-testid="phase-running-banner-final_rewrite"');
  });

  it("returns null when completed=true so a finished phase no longer pulses", () => {
    // Regression guard for PR #317: drafter / stylist / critic / etc.
    // end at their *_RUNNING state on phase_done waiting for the user
    // to start the next phase. Without this gate the banner kept
    // displaying "正在撰写草稿…" after the agent had finished.
    const html = renderToString(
      <PhaseRunningBanner
        phase="drafter"
        completed
        progress={[
          event({ section_title: "八、结论", completed: 8, total: 8 }, "evt_d"),
        ]}
      />,
    );
    expect(html).toBe("");
  });
});
