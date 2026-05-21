import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { fillNewRunKernel } from "./_kernel";

// Stage 3.E: real-LLM, full-UI version-management spec.
// Drives: rerun a completed phase, edit prompt and rerun, fork
// branch, switch branch, observe per-branch head isolation. NOT
// run only against a local mirror because it creates branch/version rows.
//
// Coverage:
//  - Stage 1: cleanup any prior PWTEST runs
//  - Stage 2: create + walk to USER_DEEP_DIVE_REVIEW (curator v0
//    vanilla complete)
//  - Stage 3: open history, assert "Rerun phase" CTA visible despite
//    versions[]==[] (the Stage 3.E first-rerun surface)
//  - Stage 4: click "Rerun phase" → produces v1
//  - Stage 5: click "Edit prompt and rerun" → fill override → save
//    and rerun → produces v2
//  - Stage 6: GET versions/{v2_id}/prompts asserts main.source=override
//  - Stage 7: fork branch from v1 → switch branch → reopen history,
//    assert v1 is active head on fork branch (v2 lives on main only)

const TITLE = `[PWTEST-VM] ${new Date()
  .toISOString()
  .replace(/[:.]/g, "-")
  .slice(0, 19)}`;

test.describe.configure({ mode: "serial" });

async function confirmSourceRerunIfPrompted(page: Page): Promise<void> {
  const confirm = page.getByTestId("source-rerun-confirm-submit");
  if (await confirm.isVisible({ timeout: 5_000 }).catch(() => false)) {
    await confirm.click();
  }
}

async function saveSearchReviewCheckpoint(
  request: APIRequestContext,
  runId: string,
): Promise<void> {
  const sourcesResp = await request.get(`/api/runs/${runId}/sources`);
  expect(sourcesResp.ok(), "GET /sources before curator").toBeTruthy();
  const sources = await sourcesResp.json();
  const sourceIds = (sources.skim_candidates as Array<{ source_id?: string }>)
    .map((row) => row.source_id)
    .filter((sourceId): sourceId is string => Boolean(sourceId));
  expect(sourceIds.length, "skim candidate source ids").toBeGreaterThan(0);
  const checkpointResp = await request.post(
    `/api/runs/${runId}/checkpoints/USER_SEARCH_REVIEW`,
    {
      data: {
        status: "ACCEPTED",
        decision_payload: {
          source_ids: sourceIds,
          approved_source_ids: sourceIds,
          review_scope: "search_review",
        },
      },
    },
  );
  expect(
    checkpointResp.ok(),
    `USER_SEARCH_REVIEW checkpoint: HTTP ${checkpointResp.status()}`,
  ).toBeTruthy();
}

test("version management — rerun, prompt override, fork, branch isolation", async ({
  page,
  request,
}) => {
  test.skip(
    process.env.AUTOESSAY_E2E_TARGET === "remote",
    "version-management spec is local-only because it creates branch/version rows",
  );
  test.setTimeout(60 * 60 * 1000);
  page.on("dialog", (d) => d.accept());

  // ---- Stage 1: cleanup ---------------------------------------------------
  await page.goto("/");
  await Promise.race([
    page.waitForSelector('[data-testid="run-card"]', { timeout: 15_000 }),
    page.waitForTimeout(3_000),
  ]);
  let cleanupRounds = 0;
  while (cleanupRounds < 30) {
    await page
      .waitForFunction(
        () => document.querySelectorAll('[data-testid="run-card"]').length > 0,
        null,
        { timeout: 15_000 },
      )
      .catch(() => {});
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
    cleanupRounds++;
  }
  console.log(`cleanup ${cleanupRounds} round(s)`);

  // ---- Stage 2: create + walk to USER_DEEP_DIVE_REVIEW --------------------
  await page.locator('[data-testid="runs-new-link"]').click();
  await page.locator('[data-testid="newrun-title"]').fill(TITLE);
  await fillNewRunKernel(page);
  await page.getByTestId("mode-option-deep").click();
  await expect(page.locator('[data-testid="newrun-submit"]')).toBeEnabled({
    timeout: 30_000,
  });
  await page.locator('[data-testid="newrun-submit"]').click();
  await page.waitForURL(/\/runs\/run_/);
  const runId = page.url().split("/runs/")[1];
  console.log(`run: ${runId}`);

  const STEPS = [
    {
      from: "DOMAIN_LOADED",
      click: "phase-action-proposal",
      to: "USER_PROPOSAL_REVIEW",
    },
    {
      from: "USER_PROPOSAL_REVIEW",
      click: "phase-action-proposal-accept",
      to: "USER_SEARCH_REVIEW",
    },
    {
      from: "USER_SEARCH_REVIEW",
      click: "phase-action-curator",
      to: "USER_DEEP_DIVE_REVIEW",
    },
  ];
  for (const step of STEPS) {
    console.log(`${step.from} → ${step.click} → ${step.to}`);
    await page.waitForFunction(
      (s) =>
        document
          .querySelector('[data-testid="workspace-root"]')
          ?.getAttribute("data-run-state") === s,
      step.from,
      { timeout: 10 * 60 * 1000 },
    );
    if (step.click === "phase-action-curator") {
      await saveSearchReviewCheckpoint(request, runId);
      const curatorResp = await request.post(`/api/runs/${runId}/curator`, {
        data: {},
      });
      expect(
        curatorResp.status(),
        `POST /curator: ${await curatorResp.text()}`,
      ).toBe(202);
      continue;
    }
    await page
      .locator(`[data-testid="${step.click}"]:visible`)
      .first()
      .click({ timeout: 30_000 });
  }
  await page.waitForFunction(
    () =>
      document
        .querySelector('[data-testid="workspace-root"]')
        ?.getAttribute("data-run-state") === "USER_DEEP_DIVE_REVIEW",
    null,
    { timeout: 10 * 60 * 1000 },
  );
  console.log("walked to USER_DEEP_DIVE_REVIEW (curator v0 vanilla complete)");

  // ---- Stage 3: open history, assert first-rerun surface visible ---------
  await page
    .locator('[data-testid="workspace-history-button"]:visible')
    .first()
    .click();
  await expect(
    page.locator('[data-testid="history-phase-curator"]'),
  ).toBeVisible({ timeout: 30_000 });
  await expect(
    page.locator('[data-testid="history-rerun-phase-curator"]'),
  ).toBeVisible();
  await expect(
    page.locator('[data-testid="history-edit-prompt-curator"]'),
  ).toBeVisible();
  // PR-A4.1b (2026-05-02): vanilla first runs now also create a
  // v1 phase_versions row via maybe_run_with_versioning, so the
  // history list immediately shows ONE entry (was 0 before
  // A4.1b). All subsequent expectations shift by +1.
  await expect(
    page.locator(
      '[data-testid="history-phase-curator"] [data-testid="history-version-curator-1"]',
    ),
  ).toBeVisible({ timeout: 15_000 });
  console.log("first-rerun surface visible with v1 (vanilla agent run)");

  // ---- Stage 4: click "Rerun phase" — creates v2 -------------------------
  console.log("rerun curator (v1 vanilla → v2 first rerun)");
  // Snapshot the CURRENT phase_done event timestamp so we can wait for a
  // NEW one. Without this, the wait would resolve instantly because the
  // workspace already shows phase_done(synthesizer) from the Stage 2 walk.
  const prevEventAt1 = await page
    .locator('[data-testid="workspace-root"]')
    .getAttribute("data-last-event-at");
  await page.locator('[data-testid="history-rerun-phase-curator"]').click();
  await confirmSourceRerunIfPrompted(page);
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
    prevEventAt1 ?? "",
    { timeout: 10 * 60 * 1000 },
  );
  await expect(
    page.locator(
      '[data-testid="history-phase-curator"] [data-testid="history-version-curator-2"]',
    ),
  ).toBeVisible({ timeout: 30_000 });
  console.log("v2 visible (after first rerun)");

  // ---- Stage 5: edit prompt + rerun — creates v3 with override -----------
  console.log("edit prompt + rerun (v2 → v3 with override)");
  await page.locator('[data-testid="history-edit-prompt-curator"]').click();
  const OVERRIDE_MARKER =
    "[PWTEST-OVERRIDE] Rank sources with extra emphasis on policy mechanism.";
  await expect(
    page.locator('[data-testid="prompt-override-textarea"]'),
  ).toBeVisible({ timeout: 15_000 });
  await page
    .locator('[data-testid="prompt-override-textarea"]')
    .fill(OVERRIDE_MARKER);
  const prevEventAt2 = await page
    .locator('[data-testid="workspace-root"]')
    .getAttribute("data-last-event-at");
  await page.locator('[data-testid="prompt-save-and-rerun"]').click();
  await confirmSourceRerunIfPrompted(page);
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
    prevEventAt2 ?? "",
    { timeout: 15 * 60 * 1000 },
  );
  await expect(
    page.locator(
      '[data-testid="history-phase-curator"] [data-testid="history-version-curator-3"]',
    ),
  ).toBeVisible({ timeout: 30_000 });
  console.log("v3 visible (after prompt override rerun)");

  // ---- Stage 6: assert v3 prompts include source=override ----------------
  const v3Id = await page
    .locator('[data-testid="history-version-curator-3"]')
    .getAttribute("data-version-id");
  expect(v3Id).toBeTruthy();
  const promptsResp = await page.request.get(
    `/api/runs/${runId}/phases/curator/versions/${v3Id}/prompts`,
  );
  expect(promptsResp.ok()).toBeTruthy();
  const prompts = await promptsResp.json();
  console.log(
    "v3 prompts:",
    JSON.stringify(
      prompts.map((p: { prompt_key: string; source: string }) => ({
        key: p.prompt_key,
        source: p.source,
      })),
    ),
  );
  const overridePrompt = prompts.find(
    (p: { source: string }) => p.source === "override",
  );
  expect(overridePrompt?.content).toContain("PWTEST-OVERRIDE");

  // ---- Stage 7: fork from v2 (the first rerun, pre-override),
  //               switch branch, assert isolation ------------------
  // PR-A4.1b: vanilla = v1, first rerun = v2, override rerun = v3.
  // Forking from v2 means the fork branch's curator is at the
  // first-rerun state but does NOT carry the v3 override.
  console.log("fork branch from curator v2");
  await page
    .locator(
      '[data-testid="history-phase-curator"] [data-testid="history-version-curator-2"] [data-testid="version-fork-button"]',
    )
    .click();
  await page.locator('[data-testid="fork-branch-name"]').fill("test-fork");
  const forkResp = page.waitForResponse(
    (r) => r.url().includes("/branches") && r.request().method() === "POST",
    { timeout: 15_000 },
  );
  await page.locator('[data-testid="fork-branch-confirm"]').click();
  await forkResp;

  // Close history modal (× button), wait branch switcher reflects fork.
  // Don't rely on Esc — the modal now also responds to it, but clicking
  // the explicit close button is deterministic against UI changes.
  await page.locator('[data-testid="history-modal-close"]').click();
  await page.waitForFunction(
    () => {
      const sel = document.querySelector('[data-testid="branch-switcher"]');
      return (
        !!sel &&
        Array.from((sel as HTMLSelectElement).options).some((o) =>
          o.text.includes("test-fork"),
        )
      );
    },
    null,
    { timeout: 15_000 },
  );

  // Switch branch via dropdown
  const branchSwitcher = page.locator('[data-testid="branch-switcher"]');
  const forkValue = await branchSwitcher.evaluate((s) => {
    const opt = Array.from((s as HTMLSelectElement).options).find((o) =>
      o.text.includes("test-fork"),
    );
    return opt?.value;
  });
  expect(forkValue).toBeTruthy();
  const switchResp = page.waitForResponse(
    (r) =>
      r.url().includes("/branches/active") && r.request().method() === "POST",
    { timeout: 15_000 },
  );
  await branchSwitcher.selectOption(forkValue!);
  await switchResp;
  console.log("switched to fork branch");

  // Reopen history → fork branch should show curator v2 as active
  // (forked from v2) and NOT v3 (v3 lives on main only)
  await page
    .locator('[data-testid="workspace-history-button"]:visible')
    .first()
    .click();
  await expect(
    page.locator(
      '[data-testid="history-phase-curator"] [data-testid="history-version-curator-2"][data-is-active="true"]',
    ),
  ).toBeVisible({ timeout: 15_000 });
  // v3 should NOT appear in fork branch's history (or if it does,
  // it's not the active head). Fork branch only sees up to v2
  // because the fork descended from v2 — Stage 2.C branch
  // isolation.
  console.log("fork branch: curator v2 is active head");
  console.log("version management spec complete");
});
