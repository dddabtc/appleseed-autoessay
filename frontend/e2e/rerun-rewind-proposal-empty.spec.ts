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
      title: `rerun-rewind proposal-less ${Date.now()}`,
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
  return (await response.json()).state;
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

async function openProposalTab(page: Page, runId: string): Promise<void> {
  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("workspace-root")).toBeVisible({
    timeout: 15_000,
  });
  const tab = page.getByTestId("workspace-tab-proposal");
  await tab.click();
  await expect(tab).toHaveAttribute("data-active", "true", {
    timeout: 5_000,
  });
}

test.afterEach(async ({ request }) => {
  for (const projectId of createdProjectIds) {
    try {
      await request.delete(`/api/projects/${projectId}`);
    } catch {
      // Keep cleanup best-effort so it does not hide the test failure.
    }
  }
  createdProjectIds = [];
});

test("proposal tab shows explicit empty state after proposal-less scout", async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const { runId } = await createProjectAndRun(request);

  const scoutResp = await request.post(`/api/runs/${runId}/scout`, {
    data: {},
  });
  expect(scoutResp.status(), "POST /scout").toBe(202);
  await waitForRunState(request, runId, "USER_SEARCH_REVIEW");

  const proposalResp = await request.get(`/api/runs/${runId}/proposal`);
  expect(proposalResp.status(), "proposal should not exist").toBe(404);

  await openProposalTab(page, runId);
  await expect(
    page.getByTestId("workspace-proposal-empty-state"),
  ).toBeVisible();
  await expect(
    page.getByTestId("workspace-proposal-empty-state-title"),
  ).toBeVisible();
  await expect(
    page.getByTestId("workspace-proposal-empty-state-body"),
  ).toBeVisible();
});
