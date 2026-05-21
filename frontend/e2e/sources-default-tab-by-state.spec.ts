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
      title: `sources-tab ${Math.random().toString(36).slice(2, 8)}`,
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

async function fetchRunState(
  request: APIRequestContext,
  runId: string,
): Promise<string> {
  const response = await request.get(`/api/runs/${runId}`);
  expect(response.ok(), `GET /runs/${runId}`).toBeTruthy();
  return (await response.json()).state;
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
    lastState = await fetchRunState(request, runId);
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
  phase: "proposal" | "scout",
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

test("USER_SEARCH_REVIEW opens the Skimmed Scout candidates tab by default", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const { runId } = await createProjectAndRun(request);
  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  const discoveryResp = await request.get(`/api/runs/${runId}/discovery`);
  expect(discoveryResp.ok(), `GET /discovery ${discoveryResp.status()}`).toBeTruthy();
  const discovery = await discoveryResp.json();
  expect(discovery.skim_candidates.length).toBeGreaterThan(0);

  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("workspace-tab-sources")).toHaveAttribute(
    "data-active",
    "true",
    { timeout: 10_000 },
  );
  await expect(page.getByTestId("workspace-sources-tab-skimmed")).toHaveAttribute(
    "data-active",
    "true",
    { timeout: 15_000 },
  );
  await expect(page.getByTestId("workspace-sources-scout-candidates-notice")).toBeVisible();
  await expect(
    page.getByTestId("workspace-subview-area").getByTestId("phase-action-curator"),
  ).toBeDisabled();
  await expect(page.getByTestId("workspace-source-review-panel")).toBeVisible();
});

test("blocked source view explains the failed phase before curation can run", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const { runId } = await createProjectAndRun(request);
  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  const injectResp = await request.post(`/api/test/runs/${runId}/fail-phase`, {
    data: { phase: "exports", failure_state: "FAILED_POLICY" },
  });
  expect(
    injectResp.ok(),
    `inject exports FAILED_POLICY: ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();
  await waitForRunState(request, runId, "FAILED_POLICY");

  await page.goto(`/runs/${runId}`);
  await page.getByTestId("workspace-tab-sources").click();
  await expect(page.getByTestId("workspace-sources-curator-disabled-hint")).toContainText(
    "exports",
  );
  await expect(
    page.getByTestId("workspace-subview-area").getByTestId("phase-action-curator"),
  ).toBeDisabled();
});
