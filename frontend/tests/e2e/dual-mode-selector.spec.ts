import { expect, test } from "@playwright/test";

import { fillNewRunKernel } from "../../e2e/_kernel";

function title(prefix: string): string {
  return `${prefix} ${Date.now()}`;
}

async function createFromNewRun(page: import("@playwright/test").Page) {
  await page.locator('[data-testid="newrun-title"]').fill(title("Dual mode"));
  await fillNewRunKernel(page);
  await expect(page.locator('[data-testid="newrun-submit"]')).toBeEnabled({
    timeout: 20_000,
  });
  await page.locator('[data-testid="newrun-submit"]').click();
  await page.waitForURL(/\/runs\/run_/, { timeout: 30_000 });
  return page.url().split("/runs/")[1].split("?")[0];
}

test("mode selector defaults from server config and toggles cleanly", async ({
  page,
}) => {
  await page.goto("/runs/new");
  const selector = page.locator('[data-testid="mode-selector"]');
  await expect(selector).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('[data-testid="mode-option-express"]')).toHaveAttribute(
    "data-selected",
    "true",
  );
  await expect(page.locator('[data-testid="new-run-auto-advance"]')).toBeDisabled();

  await page.locator('[data-testid="mode-option-deep"]').click();
  await expect(page.locator('[data-testid="mode-option-deep"]')).toHaveAttribute(
    "data-selected",
    "true",
  );
  await expect(page.locator('[data-testid="new-run-auto-advance"]')).toBeEnabled();

  await page.locator('[data-testid="mode-option-express"]').click();
  await expect(page.locator('[data-testid="mode-option-express"]')).toHaveAttribute(
    "data-selected",
    "true",
  );
});

test("mode selector honors a deep server default", async ({ page }) => {
  await page.route("**/api/generation_modes", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        default_mode: "deep",
        modes: [
          { id: "express", label: "Express" },
          { id: "deep", label: "Deep" },
        ],
      }),
    }),
  );
  await page.goto("/runs/new");
  await expect(page.locator('[data-testid="mode-option-deep"]')).toHaveAttribute(
    "data-selected",
    "true",
    { timeout: 20_000 },
  );
});

test("express submit persists mode and run list shows mode badge", async ({
  page,
  request,
}) => {
  await page.goto("/runs/new");
  await expect(page.locator('[data-testid="mode-option-express"]')).toHaveAttribute(
    "data-selected",
    "true",
    { timeout: 20_000 },
  );
  const runId = await createFromNewRun(page);

  const runResp = await request.get(`/api/runs/${runId}`);
  expect(runResp.ok()).toBeTruthy();
  expect((await runResp.json()).mode).toBe("express");

  await page.goto("/");
  const card = page.locator(`[data-run-id="${runId}"]`);
  await expect(card.locator('[data-testid="run-mode-badge-express"]')).toBeVisible({
    timeout: 20_000,
  });
});
