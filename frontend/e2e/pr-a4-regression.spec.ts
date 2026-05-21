import { expect, test, type Page } from "@playwright/test";

import { fillNewRunKernel } from "./_kernel";

// PR-A4 series regression — exercises the per-phase version model
// shipped across PR-A4.1 → PR-A4.5 via the stub backend. Three
// concrete scenarios:
//
//   1. cascade-clears-downstream + activate-restores-downstream:
//      walk to USER_DEEP_DIVE_REVIEW (scout v1 + curator v1 done).
//      Rerun scout → cascade clears curator head; modal shows the
//      curator card in `ungenerated` state with no head pv visible.
//      Activate scout v1 → cascade restores curator v1 → curator
//      card is back to `generated`.
//
//   2. activate-lineage-match endpoint flow: in the
//      upstream_superseded state from (1), click the
//      [activate matching version] primary action. Backend finds
//      curator v1 (lineage = scout v1, current head) and activates
//      with cascade.
//
//   3. cancel-prompt-drafts: walk to USER_FIELD_REVIEW (synthesizer
//      v1 done). Open prompt modal, save a draft. Card flips to
//      `prompt_edited` with cancel + regenerate as primary actions.
//      Click cancel → backend DELETE /prompts/drafts → card flips
//      back to `generated`.
//
// Each test is self-contained (its own fresh run) to keep failures
// localized. Shared setup helpers below.

const TITLE_PREFIX = "[PWTEST-A4]";

test.describe.configure({ timeout: 360_000 });

function freshTitle(): string {
  return `${TITLE_PREFIX} ${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)} ${Math.floor(Math.random() * 1000)}`;
}

async function cleanupExistingRuns(page: Page): Promise<void> {
  await page.goto("/");
  await Promise.race([
    page.waitForSelector('[data-testid="run-card"]', { timeout: 10_000 }),
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
  return page.url().split("/runs/")[1];
}

type WalkStep = { from: string; click: string };

async function walkPhases(page: Page, steps: WalkStep[]): Promise<void> {
  for (const step of steps) {
    await page.waitForFunction(
      (s) =>
        document
          .querySelector('[data-testid="workspace-root"]')
          ?.getAttribute("data-run-state") === s,
      step.from,
      { timeout: 180_000 },
    );
    if (step.click === "phase-action-curator") {
      await page.getByTestId("workspace-tab-sources").click();
      const subview = page.getByTestId("workspace-subview-area");
      const approveButtons = subview.locator(
        '[data-testid^="source-row-"][data-testid$="-review-approved-button"]',
      );
      await approveButtons.first().click({ timeout: 30_000 });
      const approveCount = Math.min(await approveButtons.count(), 3);
      for (let index = 1; index < approveCount; index += 1) {
        await approveButtons.nth(index).click({ timeout: 30_000 });
      }
      await subview.getByTestId("phase-action-curator").click({ timeout: 30_000 });
      continue;
    }
    if (step.click === "phase-action-synthesizer") {
      await page.getByTestId("workspace-tab-sources").click();
      await page
        .getByTestId("workspace-subview-area")
        .getByTestId("phase-action-synthesizer")
        .click({ timeout: 30_000 });
      continue;
    }
    await page
      .locator(`[data-testid="${step.click}"]:visible`)
      .first()
      .click({ timeout: 30_000 });
  }
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

async function openHistoryModal(page: Page): Promise<void> {
  await page
    .locator('[data-testid="workspace-history-button"]:visible')
    .first()
    .click();
  await expect(page.locator('[data-testid="phase-history-modal"]')).toBeVisible(
    { timeout: 30_000 },
  );
}

async function closeHistoryModal(page: Page): Promise<void> {
  await page.locator('[data-testid="history-modal-close"]').click();
  await expect(page.locator('[data-testid="phase-history-modal"]')).toBeHidden({
    timeout: 10_000,
  });
}

async function getCardState(page: Page, phase: string): Promise<string | null> {
  return page
    .locator(`[data-testid="history-phase-${phase}"]`)
    .getAttribute("data-card-state");
}

async function clickHistoryRerun(page: Page, phase: string): Promise<void> {
  await page.locator(`[data-testid="history-rerun-phase-${phase}"]`).click();
  const sourceConfirm = page.locator('[data-testid="source-rerun-confirm-dialog"]');
  if (await sourceConfirm.isVisible({ timeout: 1000 }).catch(() => false)) {
    await expect(page.locator('[data-testid="source-rerun-confirm-body"]')).toBeVisible();
    await page.locator('[data-testid="source-rerun-confirm-submit"]').click();
  }
}

test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await cleanupExistingRuns(page);
});

test("PR-A4: rerun upstream cascades downstream to ungenerated, activate restores", async ({
  page,
}) => {
  await createRun(page, freshTitle());

  // Walk to scout v1 + curator v1 (USER_DEEP_DIVE_REVIEW).
  await walkPhases(page, [
    { from: "DOMAIN_LOADED", click: "phase-action-proposal" },
    { from: "USER_PROPOSAL_REVIEW", click: "phase-action-proposal-accept" },
    { from: "USER_SEARCH_REVIEW", click: "phase-action-curator" },
  ]);
  await waitForRunState(page, "USER_DEEP_DIVE_REVIEW");

  // Initial state: both scout + curator are `generated`.
  await openHistoryModal(page);
  await expect(
    page.locator('[data-testid="history-phase-scout"]'),
  ).toBeVisible();
  await expect(
    page.locator('[data-testid="history-phase-curator"]'),
  ).toBeVisible();
  expect(await getCardState(page, "scout")).toBe("generated");
  expect(await getCardState(page, "curator")).toBe("generated");

  // Rerun scout → produces scout v2; cascade should clear curator head
  // since curator v1's lineage points to scout v1, no longer the head.
  const prevEventAt = await page
    .locator('[data-testid="workspace-root"]')
    .getAttribute("data-last-event-at");
  await clickHistoryRerun(page, "scout");
  await page.waitForFunction(
    (prev) => {
      const root = document.querySelector('[data-testid="workspace-root"]');
      const at = root?.getAttribute("data-last-event-at") ?? "";
      return (
        at !== "" &&
        at !== prev &&
        root?.getAttribute("data-last-event-type") === "phase_done" &&
        root.getAttribute("data-last-event-phase") === "scout"
      );
    },
    prevEventAt ?? "",
    { timeout: 180_000 },
  );

  // Modal refetches on phase_done; curator card should now show
  // ungenerated (no curator candidate matches the new scout v2 head).
  await expect(
    page.locator(
      '[data-testid="history-phase-scout"] [data-testid="history-version-scout-2"]',
    ),
  ).toBeVisible({ timeout: 15_000 });
  await page.waitForFunction(
    () =>
      document
        .querySelector('[data-testid="history-phase-curator"]')
        ?.getAttribute("data-card-state") === "ungenerated",
    null,
    { timeout: 15_000 },
  );

  // Activate scout v1 → cascade should restore curator v1.
  await page
    .locator('[data-testid="history-version-scout-1"]')
    .locator('[data-testid="history-version-scout-1-activate"]')
    .click();
  await page.waitForFunction(
    () => {
      const cur = document.querySelector(
        '[data-testid="history-phase-curator"]',
      );
      const scout = document.querySelector(
        '[data-testid="history-phase-scout"]',
      );
      return (
        cur?.getAttribute("data-card-state") === "generated" &&
        scout?.getAttribute("data-card-state") === "generated"
      );
    },
    null,
    { timeout: 15_000 },
  );
  // Verify: curator v1 row is the active head.
  await expect(
    page.locator(
      '[data-testid="history-version-curator-1"][data-is-active="true"]',
    ),
  ).toBeVisible();
});

test("PR-A4: activate-lineage-match restores downstream from upstream_superseded", async ({
  page,
}) => {
  await createRun(page, freshTitle());

  // Same walk as test 1 to scout v1 + curator v1.
  await walkPhases(page, [
    { from: "DOMAIN_LOADED", click: "phase-action-proposal" },
    { from: "USER_PROPOSAL_REVIEW", click: "phase-action-proposal-accept" },
    { from: "USER_SEARCH_REVIEW", click: "phase-action-curator" },
  ]);
  await waitForRunState(page, "USER_DEEP_DIVE_REVIEW");

  // Rerun curator → curator v2 (lineage = scout v1).
  await openHistoryModal(page);
  let prevEventAt = await page
    .locator('[data-testid="workspace-root"]')
    .getAttribute("data-last-event-at");
  await clickHistoryRerun(page, "curator");
  await page.waitForFunction(
    (prev) => {
      const root = document.querySelector('[data-testid="workspace-root"]');
      const at = root?.getAttribute("data-last-event-at") ?? "";
      return (
        at !== "" &&
        at !== prev &&
        root?.getAttribute("data-last-event-type") === "phase_done" &&
        root.getAttribute("data-last-event-phase") === "curator"
      );
    },
    prevEventAt ?? "",
    { timeout: 180_000 },
  );
  await expect(
    page.locator('[data-testid="history-version-curator-2"]'),
  ).toBeVisible();

  // Activate curator v1 manually → curator now has a non-head v1 +
  // head v2; both lineage = scout v1. State should be `generated`.
  await page
    .locator('[data-testid="history-version-curator-1-activate"]')
    .click();
  await page.waitForFunction(
    () =>
      document.querySelector(
        '[data-testid="history-version-curator-1"][data-is-active="true"]',
      ) !== null,
    null,
    { timeout: 15_000 },
  );

  // Now rerun scout to produce scout v2 → cascade clears curator head
  // (no curator candidate has lineage = scout v2). Card → ungenerated.
  prevEventAt = await page
    .locator('[data-testid="workspace-root"]')
    .getAttribute("data-last-event-at");
  await clickHistoryRerun(page, "scout");
  await page.waitForFunction(
    (prev) => {
      const root = document.querySelector('[data-testid="workspace-root"]');
      const at = root?.getAttribute("data-last-event-at") ?? "";
      return (
        at !== "" &&
        at !== prev &&
        root?.getAttribute("data-last-event-type") === "phase_done" &&
        root.getAttribute("data-last-event-phase") === "scout"
      );
    },
    prevEventAt ?? "",
    { timeout: 180_000 },
  );
  await page.waitForFunction(
    () =>
      document
        .querySelector('[data-testid="history-phase-curator"]')
        ?.getAttribute("data-card-state") === "ungenerated",
    null,
    { timeout: 15_000 },
  );

  // Activate scout v1 → curator candidates with lineage scout v1 are
  // both v1 and v2; cascade picks the most recent (v2). Curator card
  // should now show v2 active and `generated` state.
  await page
    .locator('[data-testid="history-version-scout-1-activate"]')
    .click();
  await page.waitForFunction(
    () => {
      const cur = document.querySelector(
        '[data-testid="history-phase-curator"]',
      );
      return cur?.getAttribute("data-card-state") === "generated";
    },
    null,
    { timeout: 15_000 },
  );
  await expect(
    page.locator(
      '[data-testid="history-version-curator-2"][data-is-active="true"]',
    ),
  ).toBeVisible();
});

test("PR-A4: prompt-draft cancel reverts card from prompt_edited to generated", async ({
  page,
}) => {
  await createRun(page, freshTitle());

  // Walk to USER_DEEP_DIVE_REVIEW (curator v1 done). Curator is
  // prompt-editable and already has a generated head here, so this
  // covers prompt draft cancel without depending on fulltext fetches
  // sufficient for synthesizer.
  await walkPhases(page, [
    { from: "DOMAIN_LOADED", click: "phase-action-proposal" },
    { from: "USER_PROPOSAL_REVIEW", click: "phase-action-proposal-accept" },
    { from: "USER_SEARCH_REVIEW", click: "phase-action-curator" },
  ]);
  await waitForRunState(page, "USER_DEEP_DIVE_REVIEW");

  await openHistoryModal(page);
  expect(await getCardState(page, "curator")).toBe("generated");

  // Open the prompt edit modal, save a draft (don't rerun).
  await page.locator('[data-testid="history-edit-prompt-curator"]').click();
  await expect(
    page.locator('[data-testid="prompt-override-textarea"]'),
  ).toBeVisible({ timeout: 15_000 });
  await page
    .locator('[data-testid="prompt-override-textarea"]')
    .fill("[PWTEST-A4-DRAFT] testing prompt_edited state.");
  // Save the draft WITHOUT firing rerun; the modal exposes
  // `prompt-save` for this (separate from `prompt-save-and-rerun`).
  await page.locator('[data-testid="prompt-save"]').click();
  // Wait for the persist round-trip to finish before continuing.
  await page.waitForResponse(
    (resp) =>
      resp.url().includes("/prompt") &&
      resp.request().method() === "PUT" &&
      resp.status() < 400,
    { timeout: 15_000 },
  );

  // Save-only doesn't trigger the history modal to refetch, so close
  // both modals and reopen history to pick up the new prompt_dirty
  // state.
  await page.locator('[data-testid="prompt-close"]').click();
  await closeHistoryModal(page);
  await openHistoryModal(page);
  await page.waitForFunction(
    () =>
      document
        .querySelector('[data-testid="history-phase-curator"]')
        ?.getAttribute("data-card-state") === "prompt_edited",
    null,
    { timeout: 15_000 },
  );

  // The primary action set should be cancel + regenerate.
  await expect(
    page.locator('[data-testid="history-action-curator-cancel_prompt"]'),
  ).toBeVisible();
  await expect(
    page.locator('[data-testid="history-action-curator-regenerate"]'),
  ).toBeVisible();

  // Click cancel → DELETE /prompts/drafts → card flips back to
  // generated.
  await page
    .locator('[data-testid="history-action-curator-cancel_prompt"]')
    .click();
  await page.waitForFunction(
    () =>
      document
        .querySelector('[data-testid="history-phase-curator"]')
        ?.getAttribute("data-card-state") === "generated",
    null,
    { timeout: 15_000 },
  );
});
