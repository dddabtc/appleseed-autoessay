import { test, type APIRequestContext, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { fillNewRunKernel } from "./_kernel";

// Manual UI audit: walk the 11 pipeline states in stub mode for both
// zh and en, screenshotting the workspace at each state. Outputs
// land in /tmp/layout-audit/<lang>/<NN>_<state>.png so the human
// auditor can sweep them for layout / overflow / clipping bugs at
// PC viewport. Not part of CI — invoke manually with
//   AUTOESSAY_E2E_API_PORT=18017 AUTOESSAY_E2E_VITE_PORT=15173 \
//     npx playwright test e2e/capture-pc-layout.spec.ts
//
// PR-D2.2 mode (case_analysis) — real-LLM-grade audit not required;
// stub mode covers the layout surface comprehensively.

const VIEWPORT = { width: 1440, height: 900 };

// Match the IDs in WorkspacePage `workspaceTabs` definition. These
// are the data-testid suffixes (`workspace-tab-${id}`) — the spec
// previously used phase names (search/deep-dive/field/exports)
// which do not exist as tabs and produced empty captures.
const TABS_TO_CAPTURE = [
  "console",
  "proposal",
  "sources",
  "synthesis",
  "lens",
  "novelty",
  "draft",
  "style",
  "review",
  "integrity",
  "export",
  "corpus",
];

async function createRunWithLanguage(
  request: APIRequestContext,
  language: "en" | "zh",
): Promise<{ runId: string }> {
  const projectResp = await request.post("/api/projects", {
    data: {
      title: `Layout audit ${language} ${Date.now()}`,
      domain_id: "financial_history",
      language,
    },
  });
  if (!projectResp.ok()) {
    throw new Error(`project create failed: ${projectResp.status()}`);
  }
  const project = await projectResp.json();
  const runResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "deep" },
  });
  if (!runResp.ok()) {
    throw new Error(`run create failed: ${runResp.status()}`);
  }
  const run = await runResp.json();
  return { runId: run.id };
}

async function startPhase(
  request: APIRequestContext,
  runId: string,
  phase: string,
): Promise<void> {
  // POST /api/runs/{id}/exports is the GET listing endpoint; the
  // start-exports trigger lives at /api/runs/{id}/export (singular).
  const path = phase === "exports" ? "export" : phase;
  const resp = await request.post(`/api/runs/${runId}/${path}`, { data: {} });
  if (resp.status() !== 202 && resp.status() !== 200) {
    throw new Error(
      `startPhase(${phase}) → ${resp.status()}: ${await resp.text()}`,
    );
  }
}

async function recordCheckpoint(
  request: APIRequestContext,
  runId: string,
  type: string,
  body: Record<string, unknown>,
): Promise<void> {
  const resp = await request.post(`/api/runs/${runId}/checkpoints/${type}`, {
    data: body,
  });
  if (!resp.ok()) {
    throw new Error(
      `checkpoint(${type}) → ${resp.status()}: ${await resp.text()}`,
    );
  }
}

async function waitForRunState(page: Page, state: string): Promise<void> {
  await page.waitForFunction(
    (s) =>
      document
        .querySelector('[data-testid="workspace-root"]')
        ?.getAttribute("data-run-state") === s,
    state,
    { timeout: 30_000 },
  );
}

async function snap(page: Page, dir: string, label: string): Promise<void> {
  await page.screenshot({
    path: resolve(dir, `${label}.png`),
    fullPage: true,
  });
}

for (const language of ["en", "zh"] as const) {
  test(`capture PC layout — ${language}`, async ({ page, request }) => {
    test.setTimeout(180_000);

    await page.setViewportSize(VIEWPORT);
    const outDir = `/tmp/layout-audit/${language}`;
    mkdirSync(outDir, { recursive: true });

    // Cleanup any prior runs to keep the runs page short. Same idea
    // as real-paper.spec.ts but no upper bound (stub flushes fast).
    await page.goto("/");
    let cleanupRounds = 0;
    while (cleanupRounds < 50) {
      const target = page
        .locator('[data-testid="run-card"][data-project-deleted="false"]')
        .first();
      if ((await target.count()) === 0) break;
      try {
        await target.locator('[data-testid="run-delete-button"]').click({
          timeout: 5_000,
        });
        await page.waitForResponse(
          (r) => r.url().includes("/api/runs") && r.status() < 400,
          { timeout: 5_000 },
        );
      } catch {
        break;
      }
      cleanupRounds++;
    }

    // Land on /runs (cleaned)
    await page.goto("/");
    await snap(page, outDir, "00_runs_list");

    // Create new project via UI to capture the kernel intake form
    await page.locator('[data-testid="runs-new-link"]').click();
    await page.waitForURL(/\/runs\/new$/, { timeout: 15_000 });
    await page.locator('[data-testid="newrun-title"]').fill("Layout audit run");
    await snap(page, outDir, "01_newrun_form_blank");
    await fillNewRunKernel(page);
    await snap(page, outDir, "02_newrun_form_filled");

    // Submit via API path so we have full state control afterwards.
    // Cancel the form navigation and create programmatically.
    const { runId } = await createRunWithLanguage(request, language);

    // Walk through every state we care about, snapping the workspace
    // at each pause point. Stub mode advances each phase to its
    // user-review or done state in milliseconds.
    await page.goto(`/runs/${runId}`);
    await waitForRunState(page, "DOMAIN_LOADED");
    await snap(page, outDir, "03_DOMAIN_LOADED");

    await startPhase(request, runId, "proposal");
    await waitForRunState(page, "USER_PROPOSAL_REVIEW");
    await snap(page, outDir, "04_USER_PROPOSAL_REVIEW");

    await startPhase(request, runId, "scout");
    await waitForRunState(page, "USER_SEARCH_REVIEW");
    await snap(page, outDir, "05_USER_SEARCH_REVIEW");

    await startPhase(request, runId, "curator");
    await waitForRunState(page, "USER_DEEP_DIVE_REVIEW");
    await snap(page, outDir, "06_USER_DEEP_DIVE_REVIEW");

    await startPhase(request, runId, "synthesizer");
    await waitForRunState(page, "USER_FIELD_REVIEW");
    await snap(page, outDir, "07_USER_FIELD_REVIEW");

    await startPhase(request, runId, "ideator");
    await waitForRunState(page, "USER_NOVELTY_REVIEW");
    await snap(page, outDir, "08_USER_NOVELTY_REVIEW");

    // After the novelty checkpoint, the run is DRAFTER_RUNNING; the
    // sync stub drafter completes immediately, so the next state we
    // snap is post-drafter (still DRAFTER_RUNNING until stylist starts).
    await recordCheckpoint(request, runId, "USER_NOVELTY_REVIEW", {
      selected_angle_id: "angle_001",
    });
    await page.waitForTimeout(500);
    await snap(page, outDir, "09_DRAFTER_RUNNING");

    await startPhase(request, runId, "stylist");
    await waitForRunState(page, "USER_REVISION_REVIEW");
    await snap(page, outDir, "10_USER_REVISION_REVIEW");

    await startPhase(request, runId, "critic");
    await waitForRunState(page, "USER_EXTERNAL_SCAN_APPROVAL");
    await snap(page, outDir, "11_USER_EXTERNAL_SCAN_APPROVAL");

    await recordCheckpoint(request, runId, "USER_EXTERNAL_SCAN_APPROVAL", {
      approve: true,
      scan_kinds: ["plagiarism", "ai_style"],
    });
    await startPhase(request, runId, "integrity");
    await waitForRunState(page, "USER_INTEGRITY_REVIEW");
    await snap(page, outDir, "12_USER_INTEGRITY_REVIEW");

    await recordCheckpoint(request, runId, "USER_INTEGRITY_REVIEW", {
      accept: true,
    });
    await waitForRunState(page, "USER_FINAL_ACCEPTANCE");
    await snap(page, outDir, "13_USER_FINAL_ACCEPTANCE");

    await startPhase(request, runId, "exports");
    await waitForRunState(page, "EXPORTS_DONE");
    await snap(page, outDir, "14_EXPORTS_DONE");

    // Finally, sweep all the workspace tabs at EXPORTS_DONE so the
    // auditor can see each tab's layout end-state.
    for (let i = 0; i < TABS_TO_CAPTURE.length; i++) {
      const tab = TABS_TO_CAPTURE[i];
      const locator = page.locator(`[data-testid="workspace-tab-${tab}"]`);
      if ((await locator.count()) === 0) continue;
      try {
        await locator.first().click({ timeout: 5_000 });
        await page.waitForTimeout(300);
        await snap(page, outDir, `15_tab_${String(i).padStart(2, "0")}_${tab}`);
      } catch {
        // tab not visible at this state; skip
      }
    }
  });
}
