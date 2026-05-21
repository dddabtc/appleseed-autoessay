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
      title: `restore consistency ${Date.now()}`,
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

test("restore shows recovery banner when a phase completed after cancel intent", async ({
  page,
  request,
}) => {
  const { runId } = await createProjectAndRun(request);

  const deleteResp = await request.delete(`/api/runs/${runId}`);
  expect(deleteResp.status(), `DELETE /runs/${runId}`).toBe(204);

  const injectResp = await request.post(
    `/api/test/runs/${runId}/late-phase-done-after-cancel`,
    { data: { phase: "scout" } },
  );
  expect(
    injectResp.ok(),
    `inject late phase_done: ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();

  const restoreResp = await request.post(`/api/runs/${runId}/restore`);
  expect(
    restoreResp.ok(),
    `restore run: ${restoreResp.status()} ${await restoreResp.text()}`,
  ).toBeTruthy();
  const restored = await restoreResp.json();
  expect(restored.last_event?.event_type).toBe("run_restore_recovery_warning");

  await page.goto(`/runs/${runId}`);
  const banner = page.getByTestId("workspace-restore-recovery-banner");
  await expect(banner).toBeVisible({ timeout: 10_000 });
  await expect(banner).toHaveAttribute("data-phase", "scout");
  await expect(
    page.getByTestId("workspace-restore-recovery-body"),
  ).toContainText("scout");
});

test("project restore preserves recovery banner when a phase completed after cancel intent", async ({
  page,
  request,
}) => {
  const { projectId, runId } = await createProjectAndRun(request);

  const deleteResp = await request.delete(`/api/projects/${projectId}`);
  expect(deleteResp.status(), `DELETE /projects/${projectId}`).toBe(204);

  const injectResp = await request.post(
    `/api/test/runs/${runId}/late-phase-done-after-cancel`,
    { data: { phase: "curator" } },
  );
  expect(
    injectResp.ok(),
    `inject late phase_done: ${injectResp.status()} ${await injectResp.text()}`,
  ).toBeTruthy();

  const restoreResp = await request.post(`/api/projects/${projectId}/restore`);
  expect(
    restoreResp.ok(),
    `restore project: ${restoreResp.status()} ${await restoreResp.text()}`,
  ).toBeTruthy();

  await page.goto(`/runs/${runId}`);
  const banner = page.getByTestId("workspace-restore-recovery-banner");
  await expect(banner).toBeVisible({ timeout: 10_000 });
  await expect(banner).toHaveAttribute("data-phase", "curator");
  await expect(
    page.getByTestId("workspace-restore-recovery-body"),
  ).toContainText("curator");
});
