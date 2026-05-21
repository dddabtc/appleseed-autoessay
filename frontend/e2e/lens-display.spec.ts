import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

type RunResponse = {
  id: string;
  state: string;
};

type SourcesResponse = {
  shortlist: Array<{
    source_id?: string;
    title?: string;
  }>;
};

type LensBundle = {
  artifact_present: boolean;
  schema_version: number | null;
  synthesizer_input_ref: {
    synthesizer_pv_id: string | null;
    synthesizer_artifact_hash: string | null;
  } | null;
  signals: Array<{
    lens_name: string;
    key_concepts: string[];
    source_id: string;
    applicability_to_kernel: string;
  }>;
};

type SynthesisBundle = {
  dual_track: {
    framework_lens_summary_ref: string | null;
  } | null;
};

async function createRun(request: APIRequestContext): Promise<RunResponse> {
  const projectResp = await request.post("/api/projects", {
    data: {
      title: `Playwright lens-display ${Date.now()}`,
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
  return (await runResp.json()) as RunResponse;
}

async function startPhase(
  request: APIRequestContext,
  runId: string,
  phase: string,
  expectedState: string,
): Promise<void> {
  const resp = await request.post(`/api/runs/${runId}/${phase}`, {
    data: {},
  });
  expect(resp.status(), `${phase} POST status`).toBe(202);
  const status = await request.get(`/api/runs/${runId}`);
  expect(status.ok()).toBeTruthy();
  expect(((await status.json()) as RunResponse).state).toBe(expectedState);
}

async function saveSourceReviewCheckpoint(
  request: APIRequestContext,
  runId: string,
  sourceKey: "skim_candidates" | "shortlist",
): Promise<void> {
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

async function waitForRunState(page: Page, state: string): Promise<void> {
  await page.waitForFunction(
    (s) =>
      document
        .querySelector('[data-testid="workspace-root"]')
        ?.getAttribute("data-run-state") === s,
    state,
    { timeout: 60_000 },
  );
}

test("lens tab renders deterministic framework_lens artifact and read-only affordance", async ({
  page,
  request,
}) => {
  test.setTimeout(360_000);

  const run = await createRun(request);

  await startPhase(request, run.id, "proposal", "USER_PROPOSAL_REVIEW");
  await startPhase(request, run.id, "scout", "USER_SEARCH_REVIEW");
  await saveSourceReviewCheckpoint(request, run.id, "skim_candidates");
  await startPhase(request, run.id, "curator", "USER_DEEP_DIVE_REVIEW");

  const sourcesResp = await request.get(`/api/runs/${run.id}/sources`);
  expect(sourcesResp.ok()).toBeTruthy();
  const sources = (await sourcesResp.json()) as SourcesResponse;
  expect(sources.shortlist.length).toBeGreaterThan(0);
  const sourceId = sources.shortlist.find(
    (source) => source.source_id,
  )?.source_id;
  expect(sourceId).toBeTruthy();

  const roleResp = await request.put(
    `/api/runs/${run.id}/sources/${encodeURIComponent(sourceId!)}/research_role`,
    { data: { research_role: "theoretical_lens" } },
  );
  expect(roleResp.ok()).toBeTruthy();

  await saveSourceReviewCheckpoint(request, run.id, "shortlist");
  await startPhase(request, run.id, "synthesizer", "USER_FIELD_REVIEW");

  await page.goto(`/runs/${run.id}`);
  await expect(page.locator('[data-testid="workspace-root"]')).toHaveAttribute(
    "data-run-state",
    "USER_FIELD_REVIEW",
    { timeout: 30_000 },
  );

  const lensTab = page.locator('[data-testid="workspace-tab-lens"]');
  await expect(lensTab).toBeVisible();
  await lensTab.click();
  await expect(lensTab).toHaveAttribute("data-active", "true");
  await expect(page.locator('[data-testid="lens-subview"]')).toBeVisible();

  const editButton = page.locator('[data-testid="lens-edit-button"]');
  await expect(editButton).toBeVisible();
  await expect(editButton).toBeDisabled();
  await expect(editButton).toHaveAttribute("title", "PR-F1 待开发");

  // PR-249 normalized the sidebar key from "framework_lens" to
  // "framework-lens" so this testid now matches the sidebar button
  // AND every subview that renders a phase-action-framework-lens
  // (lens subview from PR-243 + synthesis subview from PR-244).
  // Use :visible to pick the rendered one (sidebar on lg viewport,
  // subview on mobile + the lens tab).
  const startButton = page
    .locator('[data-testid="phase-action-framework-lens"]:visible')
    .first();
  await expect(startButton).toBeEnabled({ timeout: 30_000 });
  await startButton.click();
  await waitForRunState(page, "USER_LENS_REVIEW");

  const lensResp = await request.get(`/api/runs/${run.id}/framework_lens`);
  expect(lensResp.ok()).toBeTruthy();
  const lens = (await lensResp.json()) as LensBundle;
  expect(lens.artifact_present).toBe(true);
  expect(lens.schema_version).toBe(2);
  expect(lens.synthesizer_input_ref?.synthesizer_pv_id).toBeTruthy();
  expect(
    lens.synthesizer_input_ref?.synthesizer_artifact_hash,
  ).toMatch(/^[a-f0-9]{64}$/);
  expect(lens.signals.length).toBeGreaterThanOrEqual(1);

  const synthesisResp = await request.get(`/api/runs/${run.id}/synthesis`);
  expect(synthesisResp.ok()).toBeTruthy();
  const synthesis = (await synthesisResp.json()) as SynthesisBundle;
  expect(synthesis.dual_track?.framework_lens_summary_ref).toBe(
    "synthesis/framework_lens.json",
  );

  const firstSignal = lens.signals[0];
  await expect(page.locator('[data-testid="lens-signals"]')).toBeVisible({
    timeout: 30_000,
  });
  const firstChip = page.locator('[data-testid="lens-signal-0"]');
  await expect(firstChip).toBeVisible();
  await expect(firstChip).toContainText(firstSignal.lens_name);
  for (const concept of firstSignal.key_concepts) {
    await expect(
      page.locator('[data-testid="lens-signal-0-concepts"]'),
    ).toContainText(concept);
  }

  await expect(firstChip).toHaveAttribute("aria-expanded", "false");
  await firstChip.click();
  await expect(firstChip).toHaveAttribute("aria-expanded", "true");
  await expect(
    page.locator('[data-testid="lens-signal-0-applicability"]'),
  ).toContainText(firstSignal.applicability_to_kernel);
  await expect(
    page.locator('[data-testid="lens-signal-0-details"]'),
  ).toContainText(firstSignal.source_id);
});
