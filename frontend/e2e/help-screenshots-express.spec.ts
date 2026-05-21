import { expect, test } from "@playwright/test";
import fs from "fs/promises";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type Lang = "en" | "zh" | "ja";

const LANGS: Lang[] = ["en", "zh", "ja"];
const ASSETS_ROOT = path.resolve(
  __dirname,
  "..",
  "public",
  "help-assets",
  "express",
);

const TITLE: Record<Lang, string> = {
  en: "Bretton Woods Gold Commitments After 1960",
  zh: "1960 年后布雷顿森林黄金承诺的实际约束",
  ja: "1960 年以後のブレトンウッズ金兌換約束",
};

const SAMPLE_MANUSCRIPT: Record<Lang, string> = {
  en: [
    "# Bretton Woods Gold Commitments After 1960",
    "",
    "## Abstract",
    "This express sample argues that the formal gold commitment survived longer than its effective policy constraint.",
    "",
    "## Evidence Window",
    "The preview compares reserve pressure, swap lines, and London Gold Pool records across the 1960-1971 period.",
    "",
    "## Conclusion",
    "Express mode produces a fast manuscript preview while preserving an audit summary, outline map, and downloadable manuscript artifacts.",
  ].join("\n"),
  zh: [
    "# 1960 年后布雷顿森林黄金承诺的实际约束",
    "",
    "## 摘要",
    "这份 Express 示例认为，黄金兑换承诺在制度文本中保留得更久，但它作为政策约束的实际强度已经提前下降。",
    "",
    "## 证据窗口",
    "预览把储备压力、互换安排与伦敦黄金池记录放在 1960-1971 年的时间线上比较。",
    "",
    "## 结论",
    "Express mode 可以快速给出可读稿件，同时保留审计摘要、提纲地图和可下载的稿件文件。",
  ].join("\n"),
  ja: [
    "# 1960 年以後のブレトンウッズ金兌換約束",
    "",
    "## 要旨",
    "この Express サンプルは、金兌換約束が制度文書では残りながら、政策上の拘束力を早く失ったことを示します。",
    "",
    "## 証拠範囲",
    "プレビューでは準備圧力、スワップ協定、ロンドン金プールの記録を 1960-1971 年の範囲で比較します。",
    "",
    "## 結論",
    "Express mode は短時間で原稿プレビューを作り、監査要約、アウトライン、ダウンロード可能な成果物も残します。",
  ].join("\n"),
};

async function ensureLangDir(lang: Lang): Promise<string> {
  const dir = path.join(ASSETS_ROOT, lang);
  await fs.mkdir(dir, { recursive: true });
  return dir;
}

for (const lang of LANGS) {
  test.describe.serial(`express help screenshots [${lang}]`, () => {
    let langDir = "";

    test.beforeAll(async ({ request }) => {
      langDir = await ensureLangDir(lang);
      await cleanupScreenshotProjects(request);
    });

    test.beforeEach(async ({ context }) => {
      await context.addInitScript((value: string) => {
        try {
          window.localStorage.setItem("autoessay.ui_language", value);
        } catch {
          /* localStorage may be unavailable; not fatal */
        }
      }, lang);
    });

    async function shotPath(name: string): Promise<string> {
      return path.join(langDir, name);
    }

    async function waitForNewRun(page: import("@playwright/test").Page) {
      await page.goto("/runs/new");
      await expect(page.locator('[data-testid="newrun-form"]')).toBeVisible({
        timeout: 30_000,
      });
      await expect(page.locator('[data-testid="mode-selector"]')).toBeVisible();
      await expect(page.locator('[data-testid="newrun-domain"]')).toBeEnabled();
      await expect(
        page.locator('[data-testid="newrun-kernel-form"]'),
      ).toBeVisible();
      await page.waitForTimeout(400);
    }

    test("00-landing", async ({ page }) => {
      await waitForNewRun(page);
      await expect(
        page.locator('[data-testid="mode-option-express"]'),
      ).toHaveAttribute("data-selected", "true");
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({
        path: await shotPath("00-landing.png"),
        fullPage: false,
      });
    });

    test("01-mode-selector", async ({ page }) => {
      await waitForNewRun(page);
      await page.locator('[data-testid="mode-selector"]').screenshot({
        path: await shotPath("01-mode-selector.png"),
      });
    });

    test("02-kernel-form-filled", async ({ page }) => {
      await waitForNewRun(page);
      await page.locator('[data-testid="newrun-title"]').fill(TITLE[lang]);
      await page.locator('[data-testid="kernel-suggest-button"]').click();
      await expect(
        page.locator('[data-testid="newrun-kernel-observed-puzzle"]'),
      ).not.toHaveValue("", { timeout: 30_000 });
      await expect(
        page.locator('[data-testid="newrun-kernel-tentative-question"]'),
      ).not.toHaveValue("");
      await expect(page.locator('[data-testid="newrun-submit"]')).toBeEnabled({
        timeout: 10_000,
      });
      await page
        .locator('[data-testid="kernel-suggest-button"]')
        .scrollIntoViewIfNeeded();
      await page.screenshot({
        path: await shotPath("02-kernel-form-filled.png"),
        fullPage: false,
      });
    });

    test("03-express-running", async ({ page, request }) => {
      const { projectId, runId } = await createExpressRun(
        request,
        lang,
        "running",
      );
      await setRunState(request, runId, "EXPRESS_RUNNING", "express");
      await page.goto(`/runs/${runId}`);
      await expect(
        page.locator('[data-testid="workspace-root"]'),
      ).toHaveAttribute("data-run-state", "EXPRESS_RUNNING", {
        timeout: 15_000,
      });
      await page.waitForTimeout(600);
      await page.screenshot({
        path: await shotPath("03-express-running.png"),
        fullPage: false,
      });
      await deleteProject(request, projectId);
    });

    test("04-express-transparency", async ({ page, request }) => {
      const { projectId, runId } = await createExpressRun(
        request,
        lang,
        "done",
      );
      await completeExpressRun(request, runId, lang);
      await page.goto(`/runs/${runId}`);
      await expect(
        page.locator('[data-testid="workspace-root"]'),
      ).toHaveAttribute("data-run-state", "EXPRESS_DONE", { timeout: 15_000 });
      const panel = page.locator('[data-testid="express-transparency-panel"]');
      await expect(panel).toBeVisible();
      await expect(
        page.locator('[data-testid="express-audit-summary"]'),
      ).toBeVisible();
      await expect(
        page.locator('[data-testid="express-outline-map"]'),
      ).toBeVisible();
      await expect(
        page.locator('[data-testid="express-final-preview"]'),
      ).toBeVisible();
      await page.waitForTimeout(800);
      await panel.screenshot({
        path: await shotPath("04-express-transparency.png"),
      });
      await deleteProject(request, projectId);
    });
  });
}

async function createExpressRun(
  request: import("@playwright/test").APIRequestContext,
  lang: Lang,
  suffix: string,
): Promise<{ projectId: string; runId: string }> {
  const projectResp = await request.post("/api/projects", {
    data: {
      title: `Express screenshot ${lang} ${suffix}`,
      domain_id: "financial_history",
      language: lang,
    },
  });
  if (!projectResp.ok()) {
    const body = await projectResp.text().catch(() => "<no body>");
    throw new Error(`create project failed: ${projectResp.status()} ${body}`);
  }
  const project = (await projectResp.json()) as { id: string };

  const runResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "express" },
  });
  if (!runResp.ok()) {
    const body = await runResp.text().catch(() => "<no body>");
    throw new Error(`create express run failed: ${runResp.status()} ${body}`);
  }
  const run = (await runResp.json()) as { id: string };
  return { projectId: project.id, runId: run.id };
}

async function setRunState(
  request: import("@playwright/test").APIRequestContext,
  runId: string,
  state: string,
  activePhaseLock: string | null,
): Promise<void> {
  const resp = await request.post(`/api/test/runs/${runId}/state-lock`, {
    data: { state, active_phase_lock: activePhaseLock },
  });
  expect(resp.ok(), `set run ${runId} state ${state}`).toBeTruthy();
}

async function completeExpressRun(
  request: import("@playwright/test").APIRequestContext,
  runId: string,
  lang: Lang,
): Promise<void> {
  const resp = await request.post(`/api/test/runs/${runId}/express-complete`, {
    data: {
      manuscript: SAMPLE_MANUSCRIPT[lang],
      audit_status: "pass",
      total_tokens: 28640,
    },
  });
  expect(resp.ok(), `complete express run ${runId}`).toBeTruthy();
}

async function deleteProject(
  request: import("@playwright/test").APIRequestContext,
  projectId: string,
): Promise<void> {
  const resp = await request.delete(`/api/projects/${projectId}`);
  expect(
    resp.ok() || resp.status() === 204,
    `delete project ${projectId}`,
  ).toBeTruthy();
}

async function cleanupScreenshotProjects(
  request: import("@playwright/test").APIRequestContext,
): Promise<void> {
  const resp = await request.get("/api/projects", {
    params: { q: "Express screenshot", include_deleted: "1" },
  });
  expect(resp.ok(), "list express screenshot projects").toBeTruthy();
  const projects = (await resp.json()) as Array<{ id?: string }>;
  for (const project of projects) {
    if (project.id) {
      await deleteProject(request, project.id);
    }
  }
}
