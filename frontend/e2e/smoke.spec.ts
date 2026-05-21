import { expect, test } from "@playwright/test";

// Smoke baseline: load /, no console errors, SPA root mounts. Anything
// past this requires real interaction with the workspace UI which is
// covered by happy-path.spec.ts.
test("workspace root loads without console errors", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (err) => consoleErrors.push(err.message));

  await page.goto("/");
  // Wait for the React tree to mount — the <html> root having #root
  // populated is enough; deeper assertions belong in happy-path.
  await expect(page.locator("#root")).not.toBeEmpty({ timeout: 10_000 });
  expect(consoleErrors).toEqual([]);
});
