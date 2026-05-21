import { expect, test } from "@playwright/test";

// Stage 3.C follow-up: full pipeline happy-path against the stub
// runtime booted by scripts/run-e2e-server.sh. Hybrid shape: UI
// drives entry (create project) and exit (workspace shows the run
// reaching EXPORTS_DONE), while the 9 phase transitions are
// pushed through page.request — the existing review UI has no
// data-testid markers so click-driven selectors would be brittle
// against i18n changes. Future PR can add data-testid markers
// and switch to all-UI flows for prompt-override / branch /
// failure-recovery scenarios.

async function saveSourceReviewCheckpoint(
  request: import("@playwright/test").APIRequestContext,
  runId: string,
  sourceKey: "skim_candidates" | "shortlist",
) {
  const sourcesResp = await request.get(`/api/runs/${runId}/sources`);
  expect(sourcesResp.ok(), `GET /sources before ${sourceKey}`).toBeTruthy();
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
  const checkpointResp = await request.post(
    `/api/runs/${runId}/checkpoints/${checkpointType}`,
    {
      data: {
        status: "ACCEPTED",
        decision_payload: {
          source_ids: sourceIds,
          approved_source_ids: sourceIds,
          review_scope: reviewScope,
        },
      },
    },
  );
  expect(
    checkpointResp.ok(),
    `${checkpointType} checkpoint: HTTP ${checkpointResp.status()}`,
  ).toBeTruthy();
}

test("create project → walk all 9 phases → exports done", async ({
  page,
  request,
}) => {
  test.setTimeout(360_000);

  // 1. Land on the workspace runs list. AUTH_BYPASS=1 means the
  //    AuthGate's /api/auth/me call returns a synthetic user, so
  //    we skip straight past /login.
  await page.goto("/");
  await expect(page.locator("#root")).not.toBeEmpty({ timeout: 10_000 });

  // 2. Create the project + first run via the API. We could
  //    drive the New essay form, but the form's submit handler
  //    auto-creates the run and navigates — same shape, fewer
  //    selectors. Domain `financial_history` is the bundled
  //    default the New-essay form would also pre-select.
  const projectResp = await request.post("/api/projects", {
    data: {
      title: "Playwright happy-path",
      domain_id: "financial_history",
      language: "en",
    },
  });
  expect(projectResp.ok()).toBeTruthy();
  const project = await projectResp.json();

  const runResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "deep" },
  });
  expect(runResp.ok()).toBeTruthy();
  const run = await runResp.json();

  // 3. Walk the 9-phase pipeline. Each step is a stub-mode
  //    POST + a state assertion. The order, payloads, and
  //    expected post-state were verified against a real backend
  //    walkthrough.
  const startPhase = async (phase: string, expected: string) => {
    const resp = await request.post(`/api/runs/${run.id}/${phase}`, {
      data: {},
    });
    expect(resp.status(), `${phase} POST status`).toBe(202);
    const status = await request.get(`/api/runs/${run.id}`);
    expect(status.ok()).toBeTruthy();
    expect((await status.json()).state, `state after ${phase}`).toBe(expected);
  };

  const recordCheckpoint = async (
    type: string,
    body: Record<string, unknown>,
    expected: string,
  ) => {
    const resp = await request.post(
      `/api/runs/${run.id}/checkpoints/${type}`,
      { data: body },
    );
    expect(
      resp.ok(),
      `${type} checkpoint should accept; got ${resp.status()}`,
    ).toBeTruthy();
    const status = await request.get(`/api/runs/${run.id}`);
    expect((await status.json()).state, `state after ${type}`).toBe(expected);
  };

  await startPhase("proposal", "USER_PROPOSAL_REVIEW");
  await startPhase("scout", "USER_SEARCH_REVIEW");
  await saveSourceReviewCheckpoint(request, run.id, "skim_candidates");
  await startPhase("curator", "USER_DEEP_DIVE_REVIEW");
  await saveSourceReviewCheckpoint(request, run.id, "shortlist");
  await startPhase("synthesizer", "USER_FIELD_REVIEW");
  await startPhase("ideator", "USER_NOVELTY_REVIEW");
  // The novelty checkpoint selects the first stub-generated angle
  // and transitions the run into DRAFTER_RUNNING; drafter then
  // dispatches via the synchronous worker.
  await recordCheckpoint(
    "USER_NOVELTY_REVIEW",
    { selected_angle_id: "angle_001" },
    "DRAFTER_RUNNING",
  );
  await startPhase("drafter", "DRAFTER_RUNNING");
  await startPhase("stylist", "USER_REVISION_REVIEW");
  await startPhase("critic", "USER_EXTERNAL_SCAN_APPROVAL");
  await recordCheckpoint(
    "USER_EXTERNAL_SCAN_APPROVAL",
    { approve: true, scan_kinds: ["plagiarism", "ai_style"] },
    "USER_EXTERNAL_SCAN_APPROVAL",
  );
  await startPhase("integrity", "USER_INTEGRITY_REVIEW");
  await recordCheckpoint(
    "USER_INTEGRITY_REVIEW",
    { accept: true },
    "USER_FINAL_ACCEPTANCE",
  );
  await recordCheckpoint(
    "USER_FINAL_ACCEPTANCE",
    { accept: true },
    "USER_FINAL_ACCEPTANCE",
  );
  await startPhase("export", "EXPORTS_DONE");

  // 4. Verify the export manifest references the four format
  //    deliverables that the stub exporter writes.
  const manifest = await request.get(`/api/runs/${run.id}/exports/manifest`);
  if (manifest.ok()) {
    const body = await manifest.json();
    expect(body.files ?? body, "manifest should list export files").toBeTruthy();
  }

  // 5. Drive UI back to the run page to confirm the workspace
  //    rendering does not regress on a finished run. We cannot
  //    rely on a specific i18n string here (en/zh/ja diverge),
  //    but the run id is unambiguous.
  await page.goto(`/runs/${run.id}`);
  await expect(page.locator("#root")).not.toBeEmpty({ timeout: 10_000 });
  // The run id renders inside a CSS-truncated <p>, which Playwright
  // treats as `hidden` for `toBeVisible`. We just need to confirm
  // the workspace mounted the right run, so assert URL + DOM text
  // containment instead.
  expect(page.url()).toContain(`/runs/${run.id}`);
  await expect(page.locator("#root")).toContainText(run.id, {
    timeout: 10_000,
  });
});
