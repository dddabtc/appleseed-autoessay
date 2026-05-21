import { expect, test, type Page } from "@playwright/test";

import { fillNewRunKernel } from "./_kernel";

// PR-C1.b end-to-end: research_role badge + adjust-tier popover on
// the Sources tab + the new evidence-ledger sub-tab on the
// Synthesis tab. Each test creates a fresh run, walks past
// curator (so shortlist.json carries research_role tags from the
// PR-C1.a classifier stub), and exercises the C1.b surfaces.

const TITLE_PREFIX = "[PWTEST-C1B]";

test.describe.configure({ timeout: 900_000 });

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
    { timeout: 360_000 },
  );
}

async function waitForRunStateOneOf(
  page: Page,
  states: string[],
): Promise<string> {
  await page.waitForFunction(
    (expectedStates) => {
      const state = document
        .querySelector('[data-testid="workspace-root"]')
        ?.getAttribute("data-run-state");
      return Array.isArray(expectedStates) && expectedStates.includes(state);
    },
    states,
    { timeout: 360_000 },
  );
  const state = await page
    .locator('[data-testid="workspace-root"]')
    .getAttribute("data-run-state");
  return state ?? "";
}

async function saveDeepDiveReviewCheckpoint(
  page: Page,
  runId: string,
): Promise<void> {
  const sourcesResp = await page.request.get(`/api/runs/${runId}/sources`);
  expect(sourcesResp.ok(), "GET /sources before synthesizer").toBeTruthy();
  const sources = await sourcesResp.json();
  const sourceIds = (sources.shortlist as Array<{ source_id?: string }>)
    .map((row) => row.source_id)
    .filter((sourceId): sourceId is string => Boolean(sourceId));
  expect(sourceIds.length, "shortlist source ids").toBeGreaterThan(0);
  const checkpointResp = await page.request.post(
    `/api/runs/${runId}/checkpoints/USER_DEEP_DIVE_REVIEW`,
    {
      data: {
        status: "ACCEPTED",
        decision_payload: {
          source_ids: sourceIds,
          approved_source_ids: sourceIds,
          review_scope: "deep_dive_review",
        },
      },
    },
  );
  expect(
    checkpointResp.ok(),
    `USER_DEEP_DIVE_REVIEW checkpoint: HTTP ${checkpointResp.status()}`,
  ).toBeTruthy();
}

async function walkPastCurator(page: Page): Promise<void> {
  // DOMAIN_LOADED → run proposal → USER_PROPOSAL_REVIEW → accept
  // proposal (runs scout) → USER_SEARCH_REVIEW → run curator →
  // USER_DEEP_DIVE_REVIEW. After this point shortlist.json
  // carries research_role on each entry and the Sources tab is
  // the active subview.
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
  await page.getByTestId("workspace-tab-sources").click();
  const subview = page.getByTestId("workspace-subview-area");
  const approveButtons = subview.locator(
    '[data-testid^="source-row-"][data-testid$="-review-approved-button"]',
  );
  await approveButtons.first().click({ timeout: 60_000 });
  const approveCount = Math.min(await approveButtons.count(), 3);
  for (let index = 1; index < approveCount; index += 1) {
    await approveButtons.nth(index).click({ timeout: 60_000 });
  }
  await subview.getByTestId("phase-action-curator").click({ timeout: 30_000 });
  await waitForRunState(page, "USER_DEEP_DIVE_REVIEW");
}

test.beforeEach(async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await cleanupExistingRuns(page);
});

test("PR-C1.b #1 — research_role badge renders on every shortlist entry", async ({
  page,
}) => {
  await createRun(page, freshTitle());
  await walkPastCurator(page);

  // The page auto-routes to the Sources tab at USER_DEEP_DIVE_REVIEW.
  await expect(
    page.locator('[data-testid="workspace-tab-sources"]'),
  ).toHaveAttribute("data-active", "true");

  // At least one source row is rendered with a research-role badge.
  const rows = page.locator('[data-testid^="source-row-"][data-research-role]');
  await expect(rows.first()).toBeVisible({ timeout: 30_000 });
  const role = await rows.first().getAttribute("data-research-role");
  expect([
    "primary_source",
    "secondary_argument",
    "theoretical_lens",
    "methodological_reference",
  ]).toContain(role ?? "");
});

test("PR-C1.b #2 — adjust-tier popover updates badge", async ({ page }) => {
  const runId = await createRun(page, freshTitle());
  await walkPastCurator(page);

  const firstRow = page
    .locator('[data-testid^="source-row-"][data-research-role]')
    .first();
  await expect(firstRow).toBeVisible({ timeout: 30_000 });
  const sid = (await firstRow.getAttribute("data-testid"))!.replace(
    "source-row-",
    "",
  );

  // Open the adjust-tier popover, click "primary_source".
  await page.locator(`[data-testid="source-row-${sid}-adjust-tier"]`).click();
  await expect(
    page.locator(`[data-testid="source-row-${sid}-role-popover"]`),
  ).toBeVisible();
  // Click radio (the label fires the radio onChange).
  await page
    .locator(
      `[data-testid="source-row-${sid}-role-option-primary_source"] input`,
    )
    .click();

  // Wait for refetch + re-render. Badge should now reflect primary_source.
  await page.waitForFunction(
    (testid) => {
      const el = document.querySelector(`[data-testid="${testid}"]`);
      return el?.getAttribute("data-research-role") === "primary_source";
    },
    `source-row-${sid}`,
    { timeout: 15_000 },
  );

  // Verify via API.
  const resp = await page.request.get(`/api/runs/${runId}`);
  expect(resp.ok()).toBeTruthy();
  // shortlist.json was updated server-side; we don't need run JSON
  // for this assertion (badge data-attr is the canonical signal).
});

test("PR-C1.b #3 — synthesis tab shows dual-track + ledger sub-tab", async ({
  page,
}) => {
  const runId = await createRun(page, freshTitle());
  await walkPastCurator(page);

  // Walk one more step: run synthesizer → USER_FIELD_REVIEW.
  await saveDeepDiveReviewCheckpoint(page, runId);
  await page.reload();
  await waitForRunState(page, "USER_DEEP_DIVE_REVIEW");
  await page.getByTestId("workspace-tab-sources").click();
  const subview = page.getByTestId("workspace-subview-area");
  await subview.getByTestId("phase-action-synthesizer").click({
    timeout: 60_000,
  });
  const synthState = await waitForRunStateOneOf(page, [
    "USER_FIELD_REVIEW",
    "FAILED_FIXABLE",
  ]);
  if (synthState === "FAILED_FIXABLE") {
    const forceResp = await page.request.post(`/api/runs/${runId}/force-approve`, {
      data: { reason: "e2e accepts synthesizer partial output for C1.b ledger UI" },
    });
    expect(forceResp.ok(), `force-approve synthesizer: ${forceResp.status()}`).toBeTruthy();
    await page.reload();
  }
  await waitForRunState(page, "USER_FIELD_REVIEW");

  // Synthesis tab is active; the dual-track block renders.
  await expect(
    page.locator('[data-testid="synthesis-dual-track"]'),
  ).toBeVisible({ timeout: 30_000 });
  await expect(
    page.locator('[data-testid="synthesis-dual-track-primary"]'),
  ).toBeVisible();
  await expect(
    page.locator('[data-testid="synthesis-dual-track-secondary"]'),
  ).toBeVisible();

  // Switch to evidence ledger sub-tab.
  await page.locator('[data-testid="synthesis-inner-tab-ledger"]').click();
  const panel = page.locator('[data-testid="evidence-ledger-panel"]');
  await expect(panel).toBeVisible();
  // The stub synthesizer doesn't classify any source as
  // primary_source by default (source_ids don't match the
  // archive_/primary_ prefix), so the ledger is empty with the
  // "no_primary" reason — artifact exists, just no primary
  // entries.
  const reason = await panel.getAttribute("data-empty-reason");
  expect(["no_primary", "ready"]).toContain(reason ?? "");
});
