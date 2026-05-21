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
      title: `source quality diagnostics ${Date.now()}`,
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

test("sources tab displays quality counts and weak-anchor badges", async ({
  page,
  request,
}) => {
  test.setTimeout(600_000);
  const { runId } = await createProjectAndRun(request);
  await startPhase(request, runId, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, runId, "scout", "USER_SEARCH_REVIEW");

  await page.route(`**/api/runs/${runId}/sources`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        run_id: runId,
        shortlist: [],
        fulltext_manifest: {},
        manual_upload_requests: [],
        curation_report: "",
        skim_candidates: [
          {
            source_id: "weak-anchor-source",
            title: "Weakly anchored source",
            authors: ["Author"],
            year: 2026,
            venue: "Journal",
            doi: null,
            url: null,
            pdf_url: null,
            abstract: null,
            source_client: "crossref",
            access_status: "metadata_only",
            license: null,
            rank_score: 1,
            risk_flags: ["weak_entity_anchor"],
          },
        ],
        source_quality_counts: {
          off_topic_dropped: 4,
          verification_rejected: 2,
          runner_up: 1,
          weak_anchor: 1,
        },
      }),
    });
  });

  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("workspace-sources-quality-counts")).toBeVisible({
    timeout: 10_000,
  });
  await expect(
    page.getByTestId("workspace-sources-quality-count-off_topic_dropped"),
  ).toContainText("4");
  await expect(page.getByTestId("source-row-weak-anchor-source-weak-anchor-badge")).toBeVisible();
});
