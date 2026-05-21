import { expect, test, type Page } from "@playwright/test";

import { fillNewRunKernel } from "./_kernel";

// PR-C0.b2.tests: workspace KernelEditModal end-to-end. Five
// scenarios covering the read/edit/conflict/repair surfaces:
//
//   1. open-and-cancel: sidebar button opens modal with role=dialog
//      + aria-modal + Escape close + cancel close + reopen.
//   2. edit-pre-proposal: with no downstream phase completed, save
//      updates the current snapshot in-place; proposal_version
//      stays 0.
//   3. edit-after-pipeline-completion: walk past proposal accept
//      so scout v1 completes (USER_SEARCH_REVIEW); save now bumps
//      proposal_version because has_any_pipeline_completion=true,
//      and the paper-mode picker is read-only (mode-change guard).
//   4. stale-conflict: open modal, mutate the kernel via the
//      Playwright `request` fixture (impersonates a concurrent
//      client), save in modal → conflict panel shows server values
//      → "use server values" replaces form, panel disappears.
//   5. repair-deeplink-banner: navigate directly to
//      /runs/{id}?repair=kernel; modal auto-opens; closing reveals
//      the repair banner; clicking banner button re-opens modal.

const TITLE_PREFIX = "[PWTEST-KE]";

test.describe.configure({ timeout: 360_000 });

function freshTitle(): string {
  return `${TITLE_PREFIX} ${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)} ${Math.floor(Math.random() * 1000)}`;
}

async function cleanupExistingRuns(page: Page): Promise<void> {
  await page.goto("/");
  await Promise.race([
    page.waitForSelector('[data-testid="run-card"]', { timeout: 8_000 }),
    page.waitForTimeout(2_000),
  ]);
  let rounds = 0;
  while (rounds < 30) {
    const target = page
      .locator(
        '[data-testid="run-card"][data-project-deleted="false"]:not([data-run-state="EXPORTS_DONE"])',
      )
      .first();
    if ((await target.count()) === 0) break;
    const refetched = page.waitForResponse(
      (resp) =>
        resp.url().includes("/api/runs") &&
        resp.request().method() === "GET" &&
        resp.status() === 200,
      { timeout: 15_000 },
    );
    await target.locator('[data-testid="run-delete-button"]').click();
    await refetched;
    rounds++;
  }
}

async function createRun(page: Page, title: string): Promise<string> {
  await page.locator('[data-testid="runs-new-link"]').click();
  await page.locator('[data-testid="newrun-title"]').fill(title);
  await fillNewRunKernel(page);
  await page.getByTestId("mode-option-deep").click();
  await expect(page.locator('[data-testid="newrun-submit"]')).toBeEnabled({
    timeout: 30_000,
  });
  await page.locator('[data-testid="newrun-submit"]').click();
  await page.waitForURL(/\/runs\/run_/, { timeout: 30_000 });
  return page.url().split("/runs/")[1].split("?")[0];
}

async function waitForRunState(page: Page, state: string): Promise<void> {
  await page.waitForFunction(
    (s) =>
      document
        .querySelector('[data-testid="workspace-root"]')
        ?.getAttribute("data-run-state") === s,
    state,
    { timeout: 180_000 },
  );
}

async function openKernelEditModal(page: Page): Promise<void> {
  await page
    .locator('[data-testid="workspace-kernel-edit-button"]:visible')
    .first()
    .click();
  await expect(page.locator('[data-testid="kernel-edit-modal"]')).toBeVisible({
    timeout: 10_000,
  });
}

test.beforeEach(async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await cleanupExistingRuns(page);
});

test("PR-C0.b2.tests edit #1 — modal opens, Escape + cancel close", async ({
  page,
}) => {
  await createRun(page, freshTitle());

  await openKernelEditModal(page);

  // Modal a11y: role=dialog, aria-modal=true.
  const modal = page.locator('[data-testid="kernel-edit-modal"]');
  await expect(modal).toHaveAttribute("role", "dialog");
  await expect(modal).toHaveAttribute("aria-modal", "true");

  // Escape closes.
  await page.keyboard.press("Escape");
  await expect(modal).toBeHidden({ timeout: 5_000 });

  // Re-open via sidebar button.
  await openKernelEditModal(page);

  // Cancel closes.
  await page.locator('[data-testid="kernel-edit-cancel"]').click();
  await expect(modal).toBeHidden({ timeout: 5_000 });
});

test("PR-C0.b2.tests edit #2 — pre-proposal save updates current snapshot", async ({
  page,
  request,
}) => {
  const runId = await createRun(page, freshTitle());

  await openKernelEditModal(page);

  // Change tentative_question.
  const newQuestion = `[edited] ${Date.now()}`;
  await page
    .locator('[data-testid="kernel-edit-tentative-question"]')
    .fill(newQuestion);

  await page.locator('[data-testid="kernel-edit-save"]').click();
  await expect(page.locator('[data-testid="kernel-edit-modal"]')).toBeHidden({
    timeout: 10_000,
  });

  // Verify backend: question updated, proposal_version stays 0.
  const resp = await request.get(`/api/runs/${runId}`);
  const run = await resp.json();
  expect(run.research_kernel.tentative_question).toBe(newQuestion);
  expect(run.proposal_version || 0).toBe(0);
});

test("PR-C0.b2.tests edit #3 — after pipeline completion, save bumps proposal_version + mode is read-only", async ({
  page,
  request,
}) => {
  const runId = await createRun(page, freshTitle());

  // Walk: DOMAIN_LOADED → click "Start proposal" → USER_PROPOSAL_REVIEW
  // → click "Accept proposal" (runs scout) → USER_SEARCH_REVIEW.
  // At this point a pipeline phase (scout) has completed so
  // has_any_pipeline_completion=true.
  await page
    .locator('[data-testid="phase-action-proposal"]:visible')
    .first()
    .click({ timeout: 30_000 });
  await waitForRunState(page, "USER_PROPOSAL_REVIEW");
  await page
    .locator('[data-testid="phase-action-proposal-accept"]:visible')
    .first()
    .click({ timeout: 30_000 });
  await waitForRunState(page, "USER_SEARCH_REVIEW");

  // Capture proposal_version BEFORE the kernel edit.
  const before = await (await request.get(`/api/runs/${runId}`)).json();
  const versionBefore = before.proposal_version || 0;
  expect(versionBefore).toBeGreaterThanOrEqual(1);

  // Reload workspace so the React state picks up the freshest
  // run.proposal_version (without reload, the cached run may
  // still report 0 and the modal would render mode-picker
  // radios instead of the read-only pill).
  await page.reload();
  await waitForRunState(page, "USER_SEARCH_REVIEW");

  // Open kernel modal.
  await openKernelEditModal(page);

  // Mode picker is now a read-only pill (no radios).
  await expect(
    page.locator('[data-testid="kernel-edit-readonly-pill"]'),
  ).toBeVisible();
  await expect(
    page.locator('[data-testid="kernel-edit-radio-case_analysis"]'),
  ).toHaveCount(0);

  // Edit the scope and save.
  const newScope = `[scope-edited] ${Date.now()}`.padEnd(20, "x");
  await page.locator('[data-testid="kernel-edit-scope"]').fill(newScope);
  await page.locator('[data-testid="kernel-edit-save"]').click();
  await expect(page.locator('[data-testid="kernel-edit-modal"]')).toBeHidden({
    timeout: 15_000,
  });

  // proposal_version bumped (downstream-completed branch).
  const after = await (await request.get(`/api/runs/${runId}`)).json();
  expect(after.proposal_version).toBeGreaterThan(versionBefore);
  expect(after.research_kernel.scope).toBe(newScope);
});

test("PR-C0.b2.tests edit #4 — stale-conflict shows server panel + apply replaces form", async ({
  page,
  request,
}) => {
  const runId = await createRun(page, freshTitle());

  await openKernelEditModal(page);

  // Edit a field locally but don't save yet.
  await page
    .locator('[data-testid="kernel-edit-tentative-question"]')
    .fill("local edit");

  // Concurrent mutation via request fixture: impersonate another
  // client. Fetch current hash, send a PUT with a different scope.
  const beforeRun = await (await request.get(`/api/runs/${runId}`)).json();
  const concurrentScope = `[concurrent-${Date.now()}] xxxxxxxxxx`;
  const concurrent = await request.put(`/api/runs/${runId}/research_kernel`, {
    data: {
      paper_mode: beforeRun.paper_mode,
      kernel: {
        kernel_schema_version: 1,
        observed_puzzle: beforeRun.research_kernel.observed_puzzle,
        tentative_question: beforeRun.research_kernel.tentative_question,
        scope: concurrentScope,
        primary_materials_status:
          beforeRun.research_kernel.primary_materials_status,
      },
      base_proposal_version: beforeRun.proposal_version || 0,
      base_kernel_hash: beforeRun.research_kernel_hash || "",
      accept_developer_preview: false,
    },
  });
  expect(concurrent.ok()).toBeTruthy();

  // Now save in the modal — should hit a 409 stale-token conflict
  // because base_kernel_hash on the modal is the pre-mutation hash.
  await page.locator('[data-testid="kernel-edit-save"]').click();

  // Conflict panel renders with server snapshot.
  const panel = page.locator('[data-testid="kernel-edit-conflict-panel"]');
  await expect(panel).toBeVisible({ timeout: 10_000 });
  await expect(panel).toContainText(concurrentScope.slice(0, 30));

  // Apply server values: form fields update, panel disappears.
  await page
    .locator('[data-testid="kernel-edit-conflict-apply-server"]')
    .click();
  await expect(panel).toBeHidden({ timeout: 5_000 });
  await expect(page.locator('[data-testid="kernel-edit-scope"]')).toHaveValue(
    concurrentScope,
  );
});

test("PR-C0.b2.tests edit #5 — repair=kernel deeplink + banner reopen", async ({
  page,
}) => {
  const runId = await createRun(page, freshTitle());

  // Navigate directly to the repair deeplink. Workspace consumes
  // ?repair=kernel and auto-opens the modal, then strips the
  // query via history.replaceState.
  await page.goto(`/runs/${runId}?repair=kernel`);
  const modal = page.locator('[data-testid="kernel-edit-modal"]');
  await expect(modal).toBeVisible({ timeout: 15_000 });
  expect(page.url()).not.toMatch(/[?&]repair=kernel/);

  // Close the modal.
  await page.locator('[data-testid="kernel-edit-modal-close"]').click();
  await expect(modal).toBeHidden({ timeout: 5_000 });

  // Repair banner now visible (showKernelRepairBanner stayed true).
  const banner = page.locator('[data-testid="kernel-repair-banner"]');
  await expect(banner).toBeVisible({ timeout: 5_000 });

  // Click the banner's open button — modal re-opens.
  await page.locator('[data-testid="kernel-repair-banner-open"]').click();
  await expect(modal).toBeVisible({ timeout: 5_000 });
});
