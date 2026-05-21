import { expect, test, type APIRequestContext } from "@playwright/test";

let createdProjectIds: string[] = [];

async function createProjectAndRun(request: APIRequestContext) {
  const projectResp = await request.post("/api/projects", {
    data: {
      title: `blocked landing ${Date.now()}`,
      domain_id: "financial_history",
      language: "en",
    },
  });
  expect(projectResp.ok(), `create project ${projectResp.status()}`).toBeTruthy();
  const project = await projectResp.json();
  createdProjectIds.push(project.id);

  const runResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "deep" },
  });
  expect(runResp.ok(), `create run ${runResp.status()}`).toBeTruthy();
  const run = await runResp.json();
  return { projectId: project.id, runId: run.id };
}

test.afterEach(async ({ request }) => {
  for (const projectId of createdProjectIds) {
    try {
      await request.delete(`/api/projects/${projectId}`);
    } catch {
      // Best-effort cleanup so the real assertion remains visible.
    }
  }
  createdProjectIds = [];
});

test("FAILED_POLICY exports blocker lands directly on the export tab", async ({
  page,
  request,
}) => {
  const { runId } = await createProjectAndRun(request);
  const injectResp = await request.post(`/api/test/runs/${runId}/fail-phase`, {
    data: { phase: "exports", failure_state: "FAILED_POLICY" },
  });
  expect(
    injectResp.ok(),
    `inject exports FAILED_POLICY: ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();

  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("workspace-root")).toHaveAttribute(
    "data-run-state",
    "FAILED_POLICY",
    { timeout: 10_000 },
  );
  await expect(page.getByTestId("failure-resolution-banner")).toHaveAttribute(
    "data-failed-phase",
    "exports",
  );
  await expect(page.getByTestId("workspace-tab-export")).toHaveAttribute(
    "data-active",
    "true",
    { timeout: 10_000 },
  );
});

test("CRITIC_RUNNING with stale final_rewrite lock lands on review tab", async ({
  page,
  request,
}) => {
  const { runId } = await createProjectAndRun(request);
  const injectResp = await request.post(`/api/test/runs/${runId}/state-lock`, {
    data: { state: "CRITIC_RUNNING", active_phase_lock: "final_rewrite" },
  });
  expect(
    injectResp.ok(),
    `inject critic/final_rewrite mismatch: ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();

  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("workspace-root")).toHaveAttribute(
    "data-run-state",
    "CRITIC_RUNNING",
    { timeout: 10_000 },
  );
  await expect(page.getByTestId("workspace-tab-review")).toHaveAttribute(
    "data-active",
    "true",
    { timeout: 10_000 },
  );
  await expect(page.getByTestId("workspace-tab-style")).not.toHaveAttribute(
    "data-active",
    "true",
  );
});
