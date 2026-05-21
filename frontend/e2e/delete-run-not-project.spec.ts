import { expect, test, type APIRequestContext } from "@playwright/test";

let createdProjectIds: string[] = [];

async function createProjectWithTwoRuns(request: APIRequestContext) {
  const projectResp = await request.post("/api/projects", {
    data: {
      title: `delete run scope ${Date.now()}`,
      domain_id: "financial_history",
      language: "en",
    },
  });
  expect(projectResp.ok(), `create project ${projectResp.status()}`).toBeTruthy();
  const project = await projectResp.json();
  createdProjectIds.push(project.id);

  const firstResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "deep" },
  });
  const secondResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "deep" },
  });
  expect(firstResp.ok(), `create first run ${firstResp.status()}`).toBeTruthy();
  expect(secondResp.ok(), `create second run ${secondResp.status()}`).toBeTruthy();

  return {
    project,
    firstRun: await firstResp.json(),
    secondRun: await secondResp.json(),
  };
}

test.afterEach(async ({ request }) => {
  for (const projectId of createdProjectIds) {
    try {
      await request.delete(`/api/projects/${projectId}`);
    } catch {
      // Keep cleanup best-effort; failed assertions should stay visible.
    }
  }
  createdProjectIds = [];
});

test("run-card delete soft-deletes one run without deleting sibling runs or the project", async ({
  page,
  request,
}) => {
  const { project, firstRun, secondRun } = await createProjectWithTwoRuns(request);

  await page.goto("/");
  await expect(page.locator("#root")).not.toBeEmpty({ timeout: 10_000 });

  const firstCard = page.locator(
    `[data-testid="run-card"][data-run-id="${firstRun.id}"]`,
  );
  const secondCard = page.locator(
    `[data-testid="run-card"][data-run-id="${secondRun.id}"]`,
  );
  await expect(firstCard).toBeVisible();
  await expect(secondCard).toBeVisible();

  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toBe("Delete this paper run?");
    await dialog.accept();
  });
  const refresh = page.waitForResponse(
    (response) =>
      response.url().includes("/api/runs") &&
      response.request().method() === "GET" &&
      response.status() === 200,
  );
  await firstCard.getByTestId("run-delete-button").click();
  await refresh;

  await expect(firstCard).toHaveCount(0);
  await expect(secondCard).toBeVisible();
  await expect(secondCard).toHaveAttribute("data-project-deleted", "false");
  await expect(secondCard).toHaveAttribute("data-run-deleted", "false");

  const projectStatus = await request.get(`/api/projects/${project.id}`);
  expect(projectStatus.ok(), `project status ${projectStatus.status()}`).toBeTruthy();
  expect((await projectStatus.json()).deleted_at).toBeNull();

  const deletedRunStatus = await request.get(`/api/runs/${firstRun.id}`);
  const siblingRunStatus = await request.get(`/api/runs/${secondRun.id}`);
  expect(deletedRunStatus.ok()).toBeTruthy();
  expect(siblingRunStatus.ok()).toBeTruthy();
  expect((await deletedRunStatus.json()).deleted_at).toBeTruthy();
  expect((await siblingRunStatus.json()).deleted_at).toBeNull();

  await page.getByTestId("runs-show-deleted-checkbox").check();
  const deletedCard = page.locator(
    `[data-testid="run-card"][data-run-id="${firstRun.id}"]`,
  );
  await expect(deletedCard).toBeVisible();
  await expect(deletedCard).toHaveAttribute("data-run-deleted", "true");
  await expect(deletedCard.getByTestId("run-restore-button")).toBeVisible();

  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("Restore this run");
    await dialog.accept();
  });
  const restoreRefresh = page.waitForResponse(
    (response) =>
      response.url().includes("/api/runs") &&
      response.request().method() === "GET" &&
      response.status() === 200,
  );
  await deletedCard.getByTestId("run-restore-button").click();
  await restoreRefresh;

  await page.getByTestId("runs-show-deleted-checkbox").uncheck();
  await expect(firstCard).toBeVisible();
  await expect(firstCard).toHaveAttribute("data-run-deleted", "false");
  await expect(firstCard.getByTestId("run-delete-button")).toBeVisible();

  const restoredRunStatus = await request.get(`/api/runs/${firstRun.id}`);
  expect(restoredRunStatus.ok()).toBeTruthy();
  expect((await restoredRunStatus.json()).deleted_at).toBeNull();
});
