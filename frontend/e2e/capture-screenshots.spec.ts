import { expect, test } from "@playwright/test";
import path from "path";
import fs from "fs/promises";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Locale-aware help-page screenshot generator.
//
// For each UI language en / zh / ja, drives the stub pipeline far
// enough through state machine to take the screenshots referenced
// from `frontend/src/pages/HelpPage.tsx`, then writes them to
// `frontend/public/help-assets/<lang>/<filename>.png`.
//
// Run via `npm run capture:help` (or directly with
// `npx playwright test --config=playwright.screenshots.config.ts`).
// Not part of CI — runs only when help screenshots need refreshing.
//
// Each language is a separate `test.describe.serial` block so they
// run sequentially; the shared workers:1 setting means runs across
// languages don't collide on the stub backend's run-id space.

type Lang = "en" | "zh" | "ja";
const LANGS: Lang[] = ["en", "zh", "ja"];

const ASSETS_ROOT = path.resolve(__dirname, "..", "public", "help-assets");

async function ensureLangDir(lang: Lang): Promise<string> {
  const dir = path.join(ASSETS_ROOT, lang);
  await fs.mkdir(dir, { recursive: true });
  return dir;
}

for (const lang of LANGS) {
  test.describe.serial(`help screenshots [${lang}]`, () => {
    let langDir = "";

    test.beforeAll(async ({ request }) => {
      langDir = await ensureLangDir(lang);
      await cleanupScreenshotProjects(request);
    });

    // Inject UI language before any page script runs so the very
    // first render is in the target locale. The frontend reads
    // localStorage["autoessay.ui_language"] at boot.
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

    async function createProject(
      request: import("@playwright/test").APIRequestContext,
      titleSuffix: string,
    ): Promise<string> {
      const projectResp = await request.post("/api/projects", {
        data: {
          title: `Help screenshot ${lang} ${titleSuffix}`,
          domain_id: "financial_history",
          language: lang,
        },
      });
      if (!projectResp.ok()) {
        const body = await projectResp.text().catch(() => "<no body>");
        throw new Error(
          `create project (${titleSuffix}) failed: ${projectResp.status()} ${body}`,
        );
      }
      const project = await projectResp.json();
      return project.id as string;
    }

    async function createProjectAndRun(
      request: import("@playwright/test").APIRequestContext,
      titleSuffix: string,
    ): Promise<{ projectId: string; runId: string }> {
      const projectId = await createProject(request, titleSuffix);
      const runResp = await request.post(`/api/projects/${projectId}/runs`, {
        data: { mode: "deep" },
      });
      expect(runResp.ok(), `create run (${titleSuffix})`).toBeTruthy();
      const run = await runResp.json();
      return { projectId, runId: run.id as string };
    }

    async function deleteProject(
      request: import("@playwright/test").APIRequestContext,
      projectId: string,
    ): Promise<void> {
      // Soft-delete so the active-essay slot is freed for the next
      // test in the same describe.serial block. The default essay
      // limit is 3 per user, so without this the 4th test would
      // 409 with "essay_limit".
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
        params: { q: "Help screenshot", include_deleted: "1" },
      });
      expect(resp.ok(), "list screenshot projects").toBeTruthy();
      const projects = (await resp.json()) as Array<{ id?: string }>;
      for (const project of projects) {
        if (project.id) {
          await deleteProject(request, project.id);
        }
      }
    }

    async function startPhase(
      request: import("@playwright/test").APIRequestContext,
      runId: string,
      phase: string,
    ): Promise<void> {
      const resp = await request.post(`/api/runs/${runId}/${phase}`, {
        data: {},
      });
      expect(resp.status(), `${phase} start status`).toBe(202);
    }

    async function checkpoint(
      request: import("@playwright/test").APIRequestContext,
      runId: string,
      type: string,
      body: Record<string, unknown>,
    ): Promise<void> {
      const resp = await request.post(
        `/api/runs/${runId}/checkpoints/${type}`,
        { data: body },
      );
      expect(resp.ok(), `${type} checkpoint`).toBeTruthy();
    }

    async function sourceReview(
      request: import("@playwright/test").APIRequestContext,
      runId: string,
      sourceKey: "skim_candidates" | "shortlist",
    ): Promise<void> {
      const sourcesResp = await request.get(`/api/runs/${runId}/sources`);
      expect(
        sourcesResp.ok(),
        `GET /sources before ${sourceKey}`,
      ).toBeTruthy();
      const sources = await sourcesResp.json();
      const sourceIds = (sources[sourceKey] as Array<{ source_id?: string }>)
        .map((row) => row.source_id)
        .filter((sourceId): sourceId is string => Boolean(sourceId));
      expect(sourceIds.length, `${sourceKey} source ids`).toBeGreaterThan(0);
      const checkpointType =
        sourceKey === "skim_candidates"
          ? "USER_SEARCH_REVIEW"
          : "USER_DEEP_DIVE_REVIEW";
      const reviewScope =
        sourceKey === "skim_candidates" ? "search_review" : "deep_dive_review";
      await checkpoint(request, runId, checkpointType, {
        status: "ACCEPTED",
        decision_payload: {
          source_ids: sourceIds,
          approved_source_ids: sourceIds,
          rejected_source_ids: [],
          pinned_source_ids: [],
          review_scope: reviewScope,
          reviewed_at_client: new Date().toISOString(),
        },
      });
    }

    async function waitForWorkspace(
      page: import("@playwright/test").Page,
    ): Promise<void> {
      await expect(
        page.locator('[data-testid="workspace-root"]'),
      ).toBeVisible({ timeout: 15_000 });
      // Give phase-action buttons / banners a tick to settle so the
      // screenshot is not mid-transition.
      await page.waitForTimeout(800);
    }

    // Each test creates its own project, takes its screenshot, then
    // soft-deletes the project so the user's active-essay-slot count
    // (limited to 3) does not block subsequent tests.

    test("05-runs-list", async ({ page, request }) => {
      // Populate the list with three runs so the shot does not read
      // as an empty new account. The user's active-essay limit is 3,
      // so create exactly three; delete them all at the end of the
      // test so the next test in the serial block can still create
      // its own.
      const a = await createProjectAndRun(request, "list-a");
      const b = await createProjectAndRun(request, "list-b");
      const c = await createProjectAndRun(request, "list-c");
      // Push each run to a different state so the list visually
      // shows that runs progress through phases, not a row of
      // identical entries.
      await startPhase(request, a.runId, "proposal");
      await startPhase(request, b.runId, "proposal");
      await startPhase(request, b.runId, "scout");
      await startPhase(request, c.runId, "proposal");
      await startPhase(request, c.runId, "scout");
      await sourceReview(request, c.runId, "skim_candidates");
      await startPhase(request, c.runId, "curator");
      await page.goto("/");
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500);
      await page.screenshot({
        path: await shotPath("05-runs-list.png"),
        fullPage: false,
      });
      await deleteProject(request, a.projectId);
      await deleteProject(request, b.projectId);
      await deleteProject(request, c.projectId);
    });

    test("06-new-run-form", async ({ page }) => {
      await page.goto("/runs/new");
      await page.waitForLoadState("networkidle");
      await page.waitForTimeout(500);
      await page.screenshot({
        path: await shotPath("06-new-run-form.png"),
        fullPage: false,
      });
    });

    test("10-01-after-Generate-Initial-Proposal", async ({
      page,
      request,
    }) => {
      const { projectId, runId } = await createProjectAndRun(
        request,
        "proposal-review",
      );
      await startPhase(request, runId, "proposal");
      await page.goto(`/runs/${runId}`);
      await waitForWorkspace(page);
      await page.screenshot({
        path: await shotPath("10-01-after-Generate-Initial-Proposal.png"),
        fullPage: false,
      });
      await deleteProject(request, projectId);
    });

    test("08-workspace-loaded", async ({ page, request }) => {
      // Mid-flight: paused at synthesizer review (4 phases done).
      const { projectId, runId } = await createProjectAndRun(
        request,
        "mid-flight",
      );
      await startPhase(request, runId, "proposal");
      await startPhase(request, runId, "scout");
      await sourceReview(request, runId, "skim_candidates");
      await startPhase(request, runId, "curator");
      await sourceReview(request, runId, "shortlist");
      await startPhase(request, runId, "synthesizer");
      await page.goto(`/runs/${runId}`);
      await waitForWorkspace(page);
      await page.screenshot({
        path: await shotPath("08-workspace-loaded.png"),
        fullPage: false,
      });
      await deleteProject(request, projectId);
    });

    test("workspace-states (desktop status history)", async ({
      page,
      request,
    }) => {
      // Mid-flight run so the desktop workspace can show real status
      // and history content rather than an empty account state.
      const { projectId, runId } = await createProjectAndRun(
        request,
        "workspace-states-sidebar",
      );
      await startPhase(request, runId, "proposal");
      await startPhase(request, runId, "scout");
      await page.goto(`/runs/${runId}`);
      await waitForWorkspace(page);
      await page.locator('[data-testid="workspace-history-button"]').last().click();
      await expect(page.locator('[data-testid="phase-history-modal"]')).toBeVisible();
      await page.waitForTimeout(600);
      await page.screenshot({
        path: await shotPath("workspace-states.png"),
        fullPage: false,
      });
      await deleteProject(request, projectId);
    });

    test("10-10-after-Accept-integrity-findings", async ({
      page,
      request,
    }) => {
      const { projectId, runId } = await createProjectAndRun(
        request,
        "integrity-review",
      );
      await startPhase(request, runId, "proposal");
      await startPhase(request, runId, "scout");
      await sourceReview(request, runId, "skim_candidates");
      await startPhase(request, runId, "curator");
      await sourceReview(request, runId, "shortlist");
      await startPhase(request, runId, "synthesizer");
      await startPhase(request, runId, "ideator");
      await checkpoint(request, runId, "USER_NOVELTY_REVIEW", {
        selected_angle_id: "angle_001",
      });
      await startPhase(request, runId, "drafter");
      await startPhase(request, runId, "stylist");
      await startPhase(request, runId, "critic");
      await checkpoint(request, runId, "USER_EXTERNAL_SCAN_APPROVAL", {
        approve: true,
        scan_kinds: ["plagiarism", "ai_style"],
      });
      await startPhase(request, runId, "integrity");
      await page.goto(`/runs/${runId}`);
      await waitForWorkspace(page);
      await page.screenshot({
        path: await shotPath("10-10-after-Accept-integrity-findings.png"),
        fullPage: false,
      });
      await deleteProject(request, projectId);
    });

    test("11-exports-done-cta", async ({ page, request }) => {
      const { projectId, runId } = await createProjectAndRun(
        request,
        "exports-done",
      );
      await startPhase(request, runId, "proposal");
      await startPhase(request, runId, "scout");
      await sourceReview(request, runId, "skim_candidates");
      await startPhase(request, runId, "curator");
      await sourceReview(request, runId, "shortlist");
      await startPhase(request, runId, "synthesizer");
      await startPhase(request, runId, "ideator");
      await checkpoint(request, runId, "USER_NOVELTY_REVIEW", {
        selected_angle_id: "angle_001",
      });
      await startPhase(request, runId, "drafter");
      await startPhase(request, runId, "stylist");
      await startPhase(request, runId, "critic");
      await checkpoint(request, runId, "USER_EXTERNAL_SCAN_APPROVAL", {
        approve: true,
        scan_kinds: ["plagiarism", "ai_style"],
      });
      await startPhase(request, runId, "integrity");
      await checkpoint(request, runId, "USER_INTEGRITY_REVIEW", {
        accept: true,
      });
      await checkpoint(request, runId, "USER_FINAL_ACCEPTANCE", {
        accept: true,
      });
      await startPhase(request, runId, "export");
      await page.goto(`/runs/${runId}`);
      await waitForWorkspace(page);
      await page.screenshot({
        path: await shotPath("11-exports-done-cta.png"),
        fullPage: false,
      });
      // EXPORTS_DONE is already a terminal/limit-friendly state, but
      // delete to keep the runs list tidy for the next language run.
      await deleteProject(request, projectId);
    });
  });
}
