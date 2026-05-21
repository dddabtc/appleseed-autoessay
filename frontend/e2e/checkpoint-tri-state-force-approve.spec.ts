import { expect, test, type APIRequestContext } from "@playwright/test";

type RunCtx = {
  projectId: string;
  runId: string;
};

let createdProjectIds: string[] = [];

async function createProjectAndRun(
  request: APIRequestContext,
): Promise<RunCtx> {
  const projectResp = await request.post("/api/projects", {
    data: {
      title: `checkpoint tri-state ${Date.now()}`,
      domain_id: "financial_history",
      language: "en",
    },
  });
  expect(projectResp.ok(), `POST /projects: ${projectResp.status()}`).toBeTruthy();
  const project = await projectResp.json();
  createdProjectIds.push(project.id);

  const runResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "deep" },
  });
  expect(runResp.ok(), `POST /runs: ${runResp.status()}`).toBeTruthy();
  const run = await runResp.json();
  return { projectId: project.id, runId: run.id };
}

async function fetchRun(
  request: APIRequestContext,
  runId: string,
): Promise<Record<string, unknown>> {
  const response = await request.get(`/api/runs/${runId}`);
  expect(response.ok(), `GET /runs/${runId}`).toBeTruthy();
  return await response.json();
}

async function waitForRunState(
  request: APIRequestContext,
  runId: string,
  expectedState: string,
  timeoutMs = 30_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastState = "";
  while (Date.now() < deadline) {
    const run = await fetchRun(request, runId);
    lastState = String(run.state);
    if (lastState === expectedState) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `state did not reach ${expectedState} within ${timeoutMs}ms (last=${lastState})`,
  );
}

async function startPhase(
  request: APIRequestContext,
  runId: string,
  phase: "proposal" | "scout" | "curator",
  expectedState: string,
): Promise<void> {
  const response = await request.post(`/api/runs/${runId}/${phase}`, {
    data: {},
  });
  expect(response.status(), `POST /${phase}`).toBe(202);
  await waitForRunState(request, runId, expectedState);
}

test.afterEach(async ({ request }) => {
  for (const projectId of createdProjectIds) {
    try {
      await request.delete(`/api/projects/${projectId}`);
    } catch {
      // Keep the assertion failure visible if cleanup fails.
    }
  }
  createdProjectIds = [];
});

test("empty search-review checkpoint blocks curator instead of falling back to all Scout candidates", async ({
  request,
}) => {
  test.setTimeout(360_000);
  const { runId } = await createProjectAndRun(request);
  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  const checkpointResp = await request.post(
    `/api/runs/${runId}/checkpoints/USER_SEARCH_REVIEW`,
    {
      data: {
        status: "ACCEPTED",
        decision_payload: { source_ids: [] },
      },
    },
  );
  expect(
    checkpointResp.ok(),
    `empty search checkpoint: ${checkpointResp.status()} ${await checkpointResp.text()}`,
  ).toBeTruthy();

  await startPhase(request, runId, "curator", "FAILED_FIXABLE");
  const run = await fetchRun(request, runId);
  const lastEvent = run.last_event as
    | { payload?: { guidance?: string } }
    | null
    | undefined;
  const payload = lastEvent?.payload;
  expect(payload?.guidance ?? "").toContain("approved no sources");
});

test("FAILED_POLICY force-approve routes to the failed phase review state", async ({
  request,
}) => {
  test.setTimeout(360_000);
  const { runId } = await createProjectAndRun(request);
  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  const injectResp = await request.post(`/api/test/runs/${runId}/fail-phase`, {
    data: { phase: "scout", failure_state: "FAILED_POLICY" },
  });
  expect(
    injectResp.ok(),
    `inject scout FAILED_POLICY: ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();
  await waitForRunState(request, runId, "FAILED_POLICY");

  const failedRun = await fetchRun(request, runId);
  const hint = failedRun.force_approve as { target_state?: string } | null;
  expect(hint?.target_state).toBe("USER_SEARCH_REVIEW");

  const forceResp = await request.post(`/api/runs/${runId}/force-approve`, {
    data: { reason: "reviewed scout policy output manually" },
  });
  expect(forceResp.ok(), `force approve: ${forceResp.status()}`).toBeTruthy();
  const forcedRun = await forceResp.json();
  expect(forcedRun.state).toBe("USER_SEARCH_REVIEW");
});

test("FAILED_POLICY disables direct retry action in the workspace", async ({
  page,
  request,
}) => {
  test.setTimeout(360_000);
  const { runId } = await createProjectAndRun(request);
  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  const injectResp = await request.post(`/api/test/runs/${runId}/fail-phase`, {
    data: { phase: "scout", failure_state: "FAILED_POLICY" },
  });
  expect(
    injectResp.ok(),
    `inject scout FAILED_POLICY: ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();
  await waitForRunState(request, runId, "FAILED_POLICY");

  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("workspace-root")).toHaveAttribute(
    "data-run-state",
    "FAILED_POLICY",
    { timeout: 10_000 },
  );
  const visibleRetryButton = page
    .locator('[data-testid="phase-action-retry-scout"]:visible')
    .first();
  await expect(visibleRetryButton).toBeVisible();
  const retryButtons = page.getByTestId("phase-action-retry-scout");
  const retryButtonCount = await retryButtons.count();
  expect(retryButtonCount).toBeGreaterThan(0);
  for (let idx = 0; idx < retryButtonCount; idx += 1) {
    await expect(retryButtons.nth(idx)).toBeDisabled();
  }
  await expect(
    page.locator('[data-testid="phase-action-retry-scout-reason"]:visible').first(),
  ).toBeVisible();
  await expect(
    page.locator('[data-testid="force-approve-open-modal"]:visible').first(),
  ).toBeVisible();
  const forceApproveButtons = page.getByTestId("force-approve-open-modal");
  const forceApproveButtonCount = await forceApproveButtons.count();
  expect(forceApproveButtonCount).toBeGreaterThan(0);
  for (let idx = 0; idx < forceApproveButtonCount; idx += 1) {
    await expect(forceApproveButtons.nth(idx)).toBeEnabled();
  }
});

test("generic transition cannot bypass failed-state force-approve recovery", async ({
  request,
}) => {
  test.setTimeout(360_000);
  const { runId } = await createProjectAndRun(request);
  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  const injectResp = await request.post(`/api/test/runs/${runId}/fail-phase`, {
    data: { phase: "scout", failure_state: "FAILED_POLICY" },
  });
  expect(
    injectResp.ok(),
    `inject scout FAILED_POLICY: ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();
  await waitForRunState(request, runId, "FAILED_POLICY");

  const transitionResp = await request.post(`/api/runs/${runId}/transitions`, {
    data: { to_state: "USER_SEARCH_REVIEW", reason: "manual bypass" },
  });
  expect(transitionResp.status()).toBe(409);
  expect(await transitionResp.text()).toContain("force-approve");

  const forceResp = await request.post(`/api/runs/${runId}/force-approve`, {
    data: { reason: "reviewed scout policy output manually" },
  });
  expect(forceResp.ok(), `force approve: ${forceResp.status()}`).toBeTruthy();
  const forcedRun = await forceResp.json();
  expect(forcedRun.state).toBe("USER_SEARCH_REVIEW");
});
