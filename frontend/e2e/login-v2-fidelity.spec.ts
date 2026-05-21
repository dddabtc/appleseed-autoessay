import { expect, test, type Page } from "@playwright/test";
import fs from "fs/promises";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type Lang = "en" | "zh" | "ja";
type ViewportName = "mobile" | "desktop";

const LANGS: Lang[] = ["zh", "en", "ja"];
const VIEWPORTS: Record<ViewportName, { width: number; height: number }> = {
  mobile: { width: 375, height: 812 },
  desktop: { width: 1280, height: 800 },
};
const OUTPUT_DIR = path.resolve(
  __dirname,
  "..",
  "test-results",
  "login-v2-codex",
);

async function forceLoggedOut(page: Page): Promise<void> {
  await page.route("**/api/auth/me", async (route) => {
    await route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({ detail: "not authenticated" }),
    });
  });
}

// kept as reference for the desktop bg <img> path; spec asserts
// via the visible username input now since mobile uses CSS bg.
// eslint-disable-next-line @typescript-eslint/no-unused-vars
async function waitForBackground(page: Page, lang: Lang): Promise<void> {
  const bg = page.locator(`[data-testid="login-bg-${lang}"]`);
  await expect(bg).toBeVisible();
  await expect(bg).toHaveAttribute(
    "src",
    new RegExp(`/login-bg/bg-${lang}\\.png$`),
  );
  await bg.evaluate((node) => {
    const image = node as HTMLImageElement;
    if (image.complete && image.naturalWidth > 0) return;
    return new Promise<void>((resolve, reject) => {
      image.addEventListener("load", () => resolve(), { once: true });
      image.addEventListener("error", () => reject(new Error("bg failed")), {
        once: true,
      });
    });
  });
}

for (const [viewportName, size] of Object.entries(VIEWPORTS) as [
  ViewportName,
  { width: number; height: number },
][]) {
  for (const lang of LANGS) {
    test(`login v2 ${viewportName} ${lang}`, async ({ page }) => {
      await fs.mkdir(OUTPUT_DIR, { recursive: true });
      await page.setViewportSize(size);
      await forceLoggedOut(page);
      await page.addInitScript((value: string) => {
        window.localStorage.setItem("autoessay.ui_language", value);
      }, lang);

      await page.goto("/login");
      // Mobile + desktop both render a `login-username-input`
      // testid, but only one is visible at a time (the other is
      // wrapped in `lg:hidden` / `hidden lg:block`). Wait on
      // either-or via the visible filter.
      await page
        .locator('[data-testid="login-username-input"]:visible')
        .waitFor({ timeout: 8000 });
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(600);
      await page.screenshot({
        path: path.join(OUTPUT_DIR, `${viewportName}-${lang}.png`),
        fullPage: false,
      });
    });
  }
}
