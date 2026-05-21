import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

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
      title: `sse blocker realtime ${Date.now()}`,
      domain_id: "financial_history",
      language: "en",
    },
  });
  expect(
    projectResp.ok(),
    `POST /projects: ${projectResp.status()}`,
  ).toBeTruthy();
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
  let lastSeen = "";
  while (Date.now() < deadline) {
    lastSeen = await fetchRunState(request, runId);
    if (lastSeen === expectedState) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `waitForRunState: expected ${expectedState}, last saw ${lastSeen}`,
  );
}

async function startPhase(
  request: APIRequestContext,
  runId: string,
  phase: string,
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
      // Best-effort cleanup so assertion failures stay visible.
    }
  }
  createdProjectIds = [];
});

test("SSE phase_failed refreshes run snapshot so force-approve appears without reload", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const { runId } = await createProjectAndRun(request);

  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  await page.goto(`/runs/${runId}`);
  await waitForWorkspaceState(page, "USER_SEARCH_REVIEW");

  const injectResp = await request.post(`/api/test/runs/${runId}/fail-phase`, {
    data: { phase: "scout" },
  });
  expect(
    injectResp.ok(),
    `inject scout failure: HTTP ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();

  await waitForRunState(request, runId, "FAILED_FIXABLE");
  await waitForWorkspaceState(page, "FAILED_FIXABLE");
  await expect(page.getByTestId("failure-resolution-banner")).toBeVisible();
  await expect(page.getByTestId("force-approve-open-modal")).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByTestId("force-approve-open-modal")).toBeEnabled();
});
