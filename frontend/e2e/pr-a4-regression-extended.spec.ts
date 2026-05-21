import { expect, test, type Page } from "@playwright/test";

import { fillNewRunKernel } from "./_kernel";

// Extended PR-A4 regression coverage — the scenarios that
// pr-a4-regression.spec.ts didn't cover. Two themes:
//
//   1. Delete-pv blocking: the [delete] button on a version row
//      is disabled when the pv is the active head, when it's the
//      lineage parent of another head pv, or when it's a fork
//      point. The button's ``disabled`` attribute reflects the
//      backend-supplied ``delete_blocked`` flag; the ``title`` is
//      the localized reason.
//
//   2. Modify upstream when downstream is not yet generated:
//      the user reruns scout while curator / downstream phases
//      have never run. There's no cascade work because no
//      downstream head exists; the rerun should land cleanly,
//      scout v2 visible, downstream cards stay ``ungenerated``.

const TITLE_PREFIX = "[PWTEST-A4X]";

test.describe.configure({ timeout: 900_000 });

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
      { timeout: 600_000 },
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
    { timeout: 600_000 },
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

async function getCardState(page: Page, phase: string): Promise<string | null> {
  return page
    .locator(`[data-testid="history-phase-${phase}"]`)
    .getAttribute("data-card-state");
}

async function rerunPhaseFromHistory(page: Page, phase: string): Promise<void> {
  const prevEventAt = await page
    .locator('[data-testid="workspace-root"]')
    .getAttribute("data-last-event-at");
  await page.locator(`[data-testid="history-rerun-phase-${phase}"]`).click();
  const sourceConfirm = page.locator('[data-testid="source-rerun-confirm-dialog"]');
  if (await sourceConfirm.isVisible({ timeout: 1000 }).catch(() => false)) {
    await expect(page.locator('[data-testid="source-rerun-confirm-body"]')).toBeVisible();
    await page.locator('[data-testid="source-rerun-confirm-submit"]').click();
  }
  await page.waitForFunction(
    ({ prev, phase }) => {
      const root = document.querySelector('[data-testid="workspace-root"]');
      const at = root?.getAttribute("data-last-event-at") ?? "";
      return (
        at !== "" &&
        at !== prev &&
        root?.getAttribute("data-last-event-type") === "phase_done" &&
        root.getAttribute("data-last-event-phase") === phase
      );
    },
    { prev: prevEventAt ?? "", phase },
    { timeout: 600_000 },
  );
}

test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await cleanupExistingRuns(page);
});

test("PR-A4: delete blocked on chain; activating older makes newer deletable", async ({
  page,
}) => {
  // Pins the PR-A4.3 multi-blocker logic on the same-phase chain:
  //   - active_head blocks delete
  //   - parent_pv_id reference blocks delete (a later same-phase
  //     version chains back via parent_pv_id, so the earlier
  //     version stays blocked even though it's not currently head)
  //
  // Initial: rerun → v2 head, v1 still v2's parent → BOTH disabled.
  // Activate v1 → cascade no-op (no downstream); v1 head, v2 non-
  // head. v3 doesn't exist, so nothing chains back to v2 → v2 is
  // now deletable. Click delete → row disappears.
  await createRun(page, freshTitle());
  await walkPhases(page, [
    { from: "DOMAIN_LOADED", click: "phase-action-proposal" },
    { from: "USER_PROPOSAL_REVIEW", click: "phase-action-proposal-accept" },
  ]);
  await waitForRunState(page, "USER_SEARCH_REVIEW");

  // Rerun scout: v1 → v2 (head); v1 is v2's parent_pv_id.
  await openHistoryModal(page);
  await rerunPhaseFromHistory(page, "scout");
  await expect(
    page.locator('[data-testid="history-version-scout-2"]'),
  ).toBeVisible({ timeout: 15_000 });

  // BOTH versions blocked: v2 = active_head, v1 = lineage_child.
  await expect(
    page.locator('[data-testid="history-version-scout-2-delete"]'),
  ).toBeDisabled();
  await expect(
    page.locator('[data-testid="history-version-scout-1-delete"]'),
  ).toBeDisabled();

  // Activate v1 → v1 head, v2 non-head and no longer the parent of
  // anything (v3 doesn't exist).
  await page
    .locator('[data-testid="history-version-scout-1-activate"]')
    .click();
  await expect(
    page.locator(
      '[data-testid="history-version-scout-1"][data-is-active="true"]',
    ),
  ).toBeVisible({ timeout: 15_000 });

  // v1 disabled (active_head); v2 has no blocker → deletable.
  await expect(
    page.locator('[data-testid="history-version-scout-1-delete"]'),
  ).toBeDisabled();
  const v2Delete = page.locator(
    '[data-testid="history-version-scout-2-delete"]',
  );
  await expect(v2Delete).toBeEnabled();

  // Click v2 delete → row disappears.
  await v2Delete.click();
  await expect(
    page.locator('[data-testid="history-version-scout-2"]'),
  ).toBeHidden({ timeout: 15_000 });
});

test("PR-A4: delete blocked when downstream phase has lineage to this pv", async ({
  page,
}) => {
  // Pins the has_dependents (downstream lineage) blocker. Walk
  // through scout → curator so curator v1's
  // ``phase_version_inputs.upstream_pv_id`` references scout v1.
  // Then rerun scout → cascade clears curator's head row but the
  // PhaseVersion + lineage rows persist. After ``activate_version
  // (scout v1)`` (cascade restores curator v1 as head), then
  // ``rerun scout`` again → scout v2 head, cascade clears curator.
  // Now scout v1 is non-head AND has v2 chained off it AND has
  // curator v1 lineage downstream — multiple blockers stack.
  //
  // The cleanest demonstration is the simple post-walk state
  // before any rerun: scout v1 + curator v1, both heads, both
  // blocked.
  await createRun(page, freshTitle());
  await walkPhases(page, [
    { from: "DOMAIN_LOADED", click: "phase-action-proposal" },
    { from: "USER_PROPOSAL_REVIEW", click: "phase-action-proposal-accept" },
    { from: "USER_SEARCH_REVIEW", click: "phase-action-curator" },
  ]);
  await waitForRunState(page, "USER_DEEP_DIVE_REVIEW");

  await openHistoryModal(page);

  // Scout v1 head → blocked by active_head (NOTE: the same pv also
  // has curator v1 as a downstream lineage child; either reason
  // suffices to disable the button).
  await expect(
    page.locator('[data-testid="history-version-scout-1-delete"]'),
  ).toBeDisabled();
  await expect(
    page.locator('[data-testid="history-version-curator-1-delete"]'),
  ).toBeDisabled();

  // Now rerun scout → cascade clears curator (no curator candidate
  // matches scout v2). Now: scout v2 head (active_head); scout v1
  // not head but BOTH a parent of v2 AND a lineage parent of
  // curator v1's still-existing pv — multiple blockers stacked.
  await rerunPhaseFromHistory(page, "scout");
  await expect(
    page.locator('[data-testid="history-version-scout-2"]'),
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    page.locator('[data-testid="history-version-scout-1-delete"]'),
  ).toBeDisabled();

  // Verify curator card is now `ungenerated` (head cleared) but the
  // pv row is still there (lineage record persists).
  await expect(
    page.locator(
      '[data-testid="history-phase-curator"][data-card-state="ungenerated"]',
    ),
  ).toBeVisible({ timeout: 10_000 });
  await expect(
    page.locator('[data-testid="history-version-curator-1"]'),
  ).toBeVisible();
});

test("PR-A4: rerun upstream when downstream is not yet generated lands cleanly", async ({
  page,
}) => {
  await createRun(page, freshTitle());
  // Stop at USER_SEARCH_REVIEW: scout v1 only, curator never ran.
  await walkPhases(page, [
    { from: "DOMAIN_LOADED", click: "phase-action-proposal" },
    { from: "USER_PROPOSAL_REVIEW", click: "phase-action-proposal-accept" },
  ]);
  await waitForRunState(page, "USER_SEARCH_REVIEW");

  await openHistoryModal(page);
  // Pre-condition: scout is `generated`, curator (and below) are
  // `ungenerated` because no head exists.
  expect(await getCardState(page, "scout")).toBe("generated");
  expect(await getCardState(page, "curator")).toBe("ungenerated");
  expect(await getCardState(page, "synthesizer")).toBe("ungenerated");

  // Rerun scout → scout v2 created. No cascade work required since
  // no downstream phase has a head to clear. State should remain:
  // scout=generated, downstream=ungenerated, no errors.
  await rerunPhaseFromHistory(page, "scout");
  await expect(
    page.locator('[data-testid="history-version-scout-2"]'),
  ).toBeVisible({ timeout: 15_000 });

  // Cards should still report the same shape.
  expect(await getCardState(page, "scout")).toBe("generated");
  expect(await getCardState(page, "curator")).toBe("ungenerated");
  expect(await getCardState(page, "synthesizer")).toBe("ungenerated");

  // No phase-history-action-error rendered (modal-level error
  // banner from PhaseHistoryModal; absent on the happy path).
  await expect(
    page.locator('[data-testid="phase-history-action-error"]'),
  ).toHaveCount(0);
});
