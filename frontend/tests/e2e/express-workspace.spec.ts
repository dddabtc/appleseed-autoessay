import { expect, test } from "@playwright/test";

test("express workspace renders badge and transparency panel", async ({
  page,
  request,
}) => {
  const projectResp = await request.post("/api/projects", {
    data: {
      title: `AI时代的半导体存储泡沫分析 ${Date.now()}`,
      domain_id: "financial_history",
      language: "zh",
    },
  });
  expect(projectResp.ok()).toBeTruthy();
  const project = await projectResp.json();

  const runResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "express" },
  });
  expect(runResp.ok()).toBeTruthy();
  const run = await runResp.json();

  const injected = await request.post(`/api/test/runs/${run.id}/express-complete`, {
    data: {
      total_tokens: 30000,
      manuscript:
        "# Express transparency paper\n\n" +
        "## Abstract\nA compact express result for UI validation.\n\n" +
        "## Section map\nThe workspace displays parsed headings.\n\n" +
        "## Conclusion\nExpress remains auditable without phase previews.\n",
    },
  });
  expect(injected.ok()).toBeTruthy();

  await page.goto(`/runs/${run.id}`);
  await expect(page.locator('[data-testid="run-mode-badge-express"]')).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.locator('[data-testid="express-transparency-panel"]')).toBeVisible();
  await expect(page.locator('[data-testid="express-token-usage"]')).toContainText(
    "30,000",
  );
  // Provider/model + saved-prompt cards intentionally removed from
  // the transparency panel — internals, not user-facing.
  await expect(page.locator('[data-testid="express-provider-model"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="express-prompt-summary"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="express-audit-summary"]')).toContainText(
    "pass",
  );
  await expect(page.locator('[data-testid="express-outline-map"]')).toContainText(
    "Section map",
  );
  await expect(page.locator('[data-testid="express-final-preview"]')).toContainText(
    "Express transparency paper",
  );
  await expect(page.locator('[data-testid="express-regenerate-button"]')).toBeVisible();
  await expect(page.locator('[data-testid="express-start-deep-button"]')).toBeVisible();
  await expect(page.locator('[data-testid="workspace-tab-proposal"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="workspace-tab-review"]')).toHaveCount(0);

  await page.setViewportSize({ width: 390, height: 844 });
  const panel = page.locator('[data-testid="express-transparency-panel"]');
  await panel.scrollIntoViewIfNeeded();
  const box = await panel.boundingBox();
  const viewport = page.viewportSize();
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  if (!box || !viewport) {
    throw new Error("missing express transparency panel or viewport bounds");
  }
  expect(Math.floor(box.x)).toBeGreaterThanOrEqual(0);
  expect(Math.ceil(box.x + box.width)).toBeLessThanOrEqual(viewport.width);
  const documentScrollWidth = await page.evaluate(
    () => document.documentElement.scrollWidth,
  );
  expect(documentScrollWidth).toBeLessThanOrEqual(viewport.width);
});
