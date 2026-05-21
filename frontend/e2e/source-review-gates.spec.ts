import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

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
      title: `source review gates ${Date.now()}`,
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
  return (await response.json()).state as string;
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

async function waitForRunStateOneOf(
  request: APIRequestContext,
  runId: string,
  expectedStates: string[],
  timeoutMs = 30_000,
): Promise<string> {
  const expected = new Set(expectedStates);
  const deadline = Date.now() + timeoutMs;
  let lastState = "";
  while (Date.now() < deadline) {
    lastState = await fetchRunState(request, runId);
    if (expected.has(lastState)) return lastState;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `state did not reach one of ${expectedStates.join(", ")} within ${timeoutMs}ms (last=${lastState})`,
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

async function waitForWorkspaceState(
  page: Page,
  expectedState: string,
): Promise<void> {
  await expect(page.getByTestId("workspace-root")).toHaveAttribute(
    "data-run-state",
    expectedState,
    { timeout: 10_000 },
  );
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

test("source review gates save search and deep-dive decisions before continuing", async ({
  page,
  request,
}) => {
  test.setTimeout(600_000);
  const { runId } = await createProjectAndRun(request);
  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  await page.goto(`/runs/${runId}`);
  await waitForWorkspaceState(page, "USER_SEARCH_REVIEW");
  await expect(page.getByTestId("workspace-sources-tab-skimmed")).toHaveAttribute(
    "data-active",
    "true",
    { timeout: 10_000 },
  );
  await expect(page.getByTestId("workspace-source-review-panel")).toBeVisible();
  const subview = page.getByTestId("workspace-subview-area");
  const curatorButton = subview.getByTestId("phase-action-curator");
  await expect(curatorButton).toBeDisabled();
  const directCuratorResp = await request.post(`/api/runs/${runId}/curator`, {
    data: {},
  });
  expect(directCuratorResp.status()).toBe(409);
  expect(await directCuratorResp.text()).toContain("USER_SEARCH_REVIEW");

  await subview
    .locator('[data-testid^="source-row-"][data-testid$="-review-approved-button"]')
    .first()
    .click();
  await expect(curatorButton).toBeEnabled();
  await curatorButton.click();
  await waitForRunState(request, runId, "USER_DEEP_DIVE_REVIEW");
  await waitForWorkspaceState(page, "USER_DEEP_DIVE_REVIEW");

  const sourcesResp = await request.get(`/api/runs/${runId}/sources`);
  expect(sourcesResp.ok(), `GET /sources ${sourcesResp.status()}`).toBeTruthy();
  const sources = await sourcesResp.json();
  expect(sources.shortlist).toHaveLength(1);

  await expect(page.getByTestId("workspace-sources-tab-shortlist")).toHaveAttribute(
    "data-active",
    "true",
    { timeout: 10_000 },
  );
  await expect(page.getByTestId("workspace-source-review-panel")).toBeVisible();
  const synthesizerButton = subview.getByTestId("phase-action-synthesizer");
  const directSynthResp = await request.post(`/api/runs/${runId}/synthesizer`, {
    data: {},
  });
  expect(directSynthResp.status()).toBe(409);
  expect(await directSynthResp.text()).toContain("USER_DEEP_DIVE_REVIEW");
  await subview
    .locator('[data-testid^="source-row-"][data-testid$="-review-rejected-button"]')
    .first()
    .click();
  await expect(synthesizerButton).toBeDisabled();
  await expect(
    page.getByTestId("workspace-sources-synthesizer-disabled-hint"),
  ).toBeVisible();

  await subview
    .locator('[data-testid^="source-row-"][data-testid$="-review-approved-button"]')
    .first()
    .click();
  await expect(synthesizerButton).toBeEnabled();
  await synthesizerButton.click();
  const terminalState = await waitForRunStateOneOf(request, runId, [
    "USER_FIELD_REVIEW",
    "FAILED_FIXABLE",
  ]);
  await waitForWorkspaceState(page, terminalState);
});
