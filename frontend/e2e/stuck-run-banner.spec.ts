import { expect, test, type APIRequestContext } from "@playwright/test";

// PR-I3: Playwright smoke for StuckRunBanner.
//
// Coverage philosophy. The banner has two trigger conditions:
//   (a) run state is in RUNNING_STATES, AND
//   (b) the most recent phase event for that phase is older than
//       STUCK_RUN_IDLE_THRESHOLD_SECONDS (15 min).
//
// In CI stub mode every phase auto-completes synchronously, so we
// cannot keep a real run in any *_RUNNING state long enough to
// observe a stale event chain end-to-end. The full positive case
// (banner appears on a 16-min-stale stuck run + recover button
// drives the run to FAILED_FIXABLE) is exercised via:
//   - backend pytest: backend/tests/test_recover_endpoint.py
//     (5 tests: gate fires / gate refuses with discriminator /
//     unknown phase / unknown run / state mismatch)
//   - frontend vitest: frontend/src/components/StuckRunBanner.test.tsx
//     (7 tests with vi.useFakeTimers, covering the 15min threshold,
//     fallback chain to active_phase_lock_claimed_at and
//     run.updated_at, tension_extraction reverse map, threshold
//     parity with backend)
//
// The Playwright spec below covers the negative case at the e2e
// boundary: on a real freshly-created run the banner must NOT
// render — the gate must hold, no false positives. The
// data-testid stuck-run-banner is checked to be absent.
//
// The full positive-case Playwright spec (with backend test-only
// seeding of a backdated phase event) is deferred to PR-I3.b along
// with the worker SIGKILL detector that will exercise the same
// code path from the worker side.

async function createRun(
  request: APIRequestContext,
): Promise<{ id: string }> {
  const projectResp = await request.post("/api/projects", {
    data: {
      title: `Playwright stuck-run smoke ${Date.now()}`,
      domain_id: "financial_history",
      language: "en",
    },
  });
  expect(projectResp.ok()).toBeTruthy();
  const project = await projectResp.json();

  const runResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "deep" },
  });
  expect(runResp.ok()).toBeTruthy();
  return (await runResp.json()) as { id: string };
}

test("stuck-run banner does not render on a fresh run", async ({
  page,
  request,
}) => {
  const run = await createRun(request);
  await page.goto(`/runs/${run.id}`);
  await expect(page.locator('[data-testid="workspace-root"]')).toBeVisible({
    timeout: 30_000,
  });

  // The run is brand new (DOMAIN_LOADED or similar), no stale phase
  // event chain, no active phase lock, no *_RUNNING state. The
  // banner gate must NOT fire.
  await expect(
    page.locator('[data-testid="stuck-run-banner"]'),
  ).toHaveCount(0);
});
