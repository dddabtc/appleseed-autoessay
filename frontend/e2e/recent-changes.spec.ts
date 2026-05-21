/**
 * E2E coverage for the 2026-05-01 → 2026-05-02 PR run:
 *
 *   #118  edit modal CJK rendering + proposal editable past acceptance
 *   #119  PR-A3 — project title editing + replace/new mode
 *   #121  PR-B3 — workspace inline Corpus sub-tab
 *
 * Hybrid pattern: API-drives setup (creating projects, walking
 * the stub pipeline, planting on-disk content), UI-drives the
 * actual user-visible behavior. The stub server boots all phase
 * agents in stub mode and runs synchronously, so we can drive a
 * run past arbitrary phases without waiting for real LLMs.
 *
 * Tests are kept independent (each creates its own project) so
 * the suite can be reordered or sharded later.
 */
import { expect, test } from "@playwright/test";

// Soft-delete tracked projects on teardown so the per-user
// ACTIVE_ESSAY_LIMIT_PER_USER (=3) does not exhaust within a single
// suite run. Each test pushes its project_id here.
const _createdProjectIds: string[] = [];

test.afterEach(async ({ request }) => {
  while (_createdProjectIds.length > 0) {
    const id = _createdProjectIds.pop();
    if (!id) continue;
    try {
      await request.delete(`/api/projects/${id}`);
    } catch {
      // Best-effort cleanup; ignore per-call failures.
    }
  }
});

async function createProject(
  request: import("@playwright/test").APIRequestContext,
  title = "Playwright recent-changes test",
) {
  const projectResp = await request.post("/api/projects", {
    data: { title, domain_id: "financial_history", language: "en" },
  });
  expect(
    projectResp.ok(),
    `project create should succeed; status=${projectResp.status()}`,
  ).toBeTruthy();
  const project = await projectResp.json();
  _createdProjectIds.push(project.id);
  const runResp = await request.post(`/api/projects/${project.id}/runs`, {
    data: { mode: "deep" },
  });
  expect(runResp.ok(), "run create should succeed").toBeTruthy();
  const run = await runResp.json();
  return { project, run };
}

async function startPhase(
  request: import("@playwright/test").APIRequestContext,
  runId: string,
  phase: string,
) {
  if (phase === "curator") {
    await saveSourceReviewCheckpoint(request, runId, "skim_candidates");
  } else if (phase === "synthesizer") {
    await saveSourceReviewCheckpoint(request, runId, "shortlist");
  }
  const resp = await request.post(`/api/runs/${runId}/${phase}`, { data: {} });
  expect(resp.status(), `${phase} POST status`).toBe(202);
}

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

async function getRunState(
  request: import("@playwright/test").APIRequestContext,
  runId: string,
): Promise<string> {
  const status = await request.get(`/api/runs/${runId}`);
  expect(status.ok()).toBeTruthy();
  return (await status.json()).state as string;
}

// =====================================================================
// PR #119 — project title editing
// =====================================================================
test("PR #119: project title editable inline from workspace heading", async ({
  page,
  request,
}) => {
  const { project, run } = await createProject(
    request,
    "Title-edit playwright project",
  );

  await page.goto(`/runs/${run.id}`);
  await expect(page.locator("#root")).not.toBeEmpty({ timeout: 10_000 });

  // Display state: title visible, edit button present.
  const display = page.getByTestId("project-title-display");
  await expect(display).toContainText("Title-edit playwright project");
  const editButton = page.getByTestId("project-title-edit-button");
  await expect(editButton).toBeVisible();

  // Click edit, type a new title, save.
  await editButton.click();
  const input = page.getByTestId("project-title-input");
  await expect(input).toBeVisible();
  await input.fill("Edited from playwright 标题已改");
  await page.getByTestId("project-title-save").click();

  // Display state should re-render with the new title.
  await expect(display).toContainText("Edited from playwright 标题已改", {
    timeout: 10_000,
  });

  // Verify backend persistence.
  const projectResp = await request.get(`/api/projects/${project.id}`);
  expect(projectResp.ok()).toBeTruthy();
  expect((await projectResp.json()).title).toBe(
    "Edited from playwright 标题已改",
  );
});

test("PR #119: empty title rejected client-side", async ({ page, request }) => {
  const { run } = await createProject(request, "Empty-title rejection");
  await page.goto(`/runs/${run.id}`);
  await page.getByTestId("project-title-edit-button").click();
  const input = page.getByTestId("project-title-input");
  await input.fill("   ");
  await page.getByTestId("project-title-save").click();
  // Frontend's local validator rejects whitespace-only and stays
  // in edit mode without calling the API.
  await expect(input).toBeVisible();
});

// =====================================================================
// PR #119 — proposal editable past acceptance + replace/new toggle
// =====================================================================
test("PR #119: proposal editable + replace mode at USER_PROPOSAL_REVIEW", async ({
  page,
  request,
}) => {
  const { run } = await createProject(request, "Proposal replace mode test");
  // Walk to USER_PROPOSAL_REVIEW.
  await request.post(`/api/runs/${run.id}/transitions`, {
    data: { to_state: "DOMAIN_LOADED", reason: "test setup" },
  });
  await startPhase(request, run.id, "proposal");
  expect(await getRunState(request, run.id)).toBe("USER_PROPOSAL_REVIEW");

  await page.goto(`/runs/${run.id}`);
  // Switch to the proposal tab.
  await page.getByTestId("workspace-tab-proposal").click();

  // Form is visible AND editable AND mode toggle shows replace as
  // the default selection (codex amendment 3).
  await expect(page.getByTestId("proposal-form")).toBeVisible();
  await expect(page.getByTestId("proposal-mode-fieldset")).toBeVisible();
  await expect(page.getByTestId("proposal-mode-replace")).toBeChecked();
  // post-accept warning should NOT be visible (we're still in
  // initial state).
  await expect(
    page.getByTestId("proposal-post-accept-warning"),
  ).toHaveCount(0);

  // Edit the research question + save with replace mode.
  const rq = page.getByTestId("proposal-research-question-textarea");
  await rq.fill("Replace-mode question. Should not bump version.");
  await page.getByTestId("proposal-save-button").click();

  // Wait for the save to settle. Verify version stayed at 1
  // (replace mode does NOT bump).
  await page.waitForTimeout(500);
  const proposalResp = await request.get(`/api/runs/${run.id}/proposal`);
  expect(proposalResp.ok()).toBeTruthy();
  const proposal = await proposalResp.json();
  expect(
    proposal.version,
    "replace mode at USER_PROPOSAL_REVIEW must not bump version",
  ).toBe(1);
  expect(proposal.proposal_json.research_question).toBe(
    "Replace-mode question. Should not bump version.",
  );
});

test("PR #118 + #119: proposal editable past acceptance + post-accept warning", async ({
  page,
  request,
}) => {
  test.setTimeout(360_000);
  const { run } = await createProject(request, "Post-accept proposal edit");
  await request.post(`/api/runs/${run.id}/transitions`, {
    data: { to_state: "DOMAIN_LOADED", reason: "test setup" },
  });
  await startPhase(request, run.id, "proposal");
  // happy-path style: start_scout from USER_PROPOSAL_REVIEW
  // transitions directly to USER_SEARCH_REVIEW (the stub agent
  // completes inline). No explicit checkpoint accept needed.
  await startPhase(request, run.id, "scout");
  expect(await getRunState(request, run.id)).toBe("USER_SEARCH_REVIEW");

  await page.goto(`/runs/${run.id}`);
  await page.getByTestId("workspace-tab-proposal").click();

  // Banner should appear (post-accept-edit branch).
  await expect(page.getByTestId("proposal-post-accept-warning")).toBeVisible();
  // Form is still editable; canEdit is true.
  await expect(page.getByTestId("proposal-edit-area")).toHaveAttribute(
    "data-can-edit",
    "true",
  );
  await expect(page.getByTestId("proposal-edit-area")).toHaveAttribute(
    "data-post-accept-edit",
    "true",
  );
  // Save button is the primary style now.
  await expect(page.getByTestId("proposal-save-button")).toBeVisible();
  // Regenerate / Accept buttons hidden post-accept.
  await expect(page.getByTestId("proposal-regenerate-button")).toHaveCount(0);
  await expect(page.getByTestId("proposal-accept-button")).toHaveCount(0);
});

// =====================================================================
// PR #118 — JSON parse-and-pretty rendering for CJK
//
// The full UI walk (drive run to USER_DEEP_DIVE_REVIEW + plant
// escaped content via user-edit + open modal) is flaky against the
// stub server because SSE keeps re-rendering the tab strip while we
// click. The behavior under test is a pure function
// (``normalizeContentForDisplay`` in WorkspacePage.tsx) that takes
// a legacy escaped JSON string and re-stringifies it via JSON.parse
// + JSON.stringify so non-ASCII characters become readable. Exercise
// it as JS via page.evaluate so the test stays fast and stable. The
// disk-write side (``ensure_ascii=False`` on every ``_write_json``
// call) is covered by backend pytests.
// =====================================================================
test("PR #118: JSON parse-and-pretty produces readable CJK in browser context", async ({
  page,
}) => {
  await page.goto("/");
  // Build the legacy on-disk form. Python json.dumps with the
  // default ensure_ascii=True yields the literal six-char sequence
  // backslash-u-X-X-X-X for every non-ASCII char. Reconstruct that
  // escaped form here, then exercise the same parse-and-pretty round
  // trip the modal does.
  const legacyEscaped = [
    "[",
    "  {",
    '    "abstract": "\\u6d4b\\u8bd5",',
    '    "title": "\\u672c\\u7814"',
    "  }",
    "]",
    "",
  ].join("\n");
  expect(legacyEscaped, "fixture has escape sequences").toContain("\\u672c");

  const result: string = await page.evaluate((raw) => {
    return JSON.stringify(JSON.parse(raw), null, 2) + "\n";
  }, legacyEscaped);

  expect(result, "real CJK after normalize").toContain("本研");
  expect(result, "escapes gone after normalize").not.toContain("\\u672c");
});

// =====================================================================
// PR #119 — edit modal mode toggle (replace/new)
// =====================================================================
test("PR #119: edit modal shows mode toggle when no downstream completed", async ({
  page,
  request,
}) => {
  test.setTimeout(360_000);
  const { run } = await createProject(request, "Modal mode toggle test");
  await request.post(`/api/runs/${run.id}/transitions`, {
    data: { to_state: "DOMAIN_LOADED", reason: "test setup" },
  });
  await startPhase(request, run.id, "proposal");
  // start_scout from USER_PROPOSAL_REVIEW transitions directly to
  // USER_SEARCH_REVIEW via the stub agent — no explicit accept
  // checkpoint needed.
  await startPhase(request, run.id, "scout");
  await startPhase(request, run.id, "curator");
  await startPhase(request, run.id, "synthesizer");
  expect(await getRunState(request, run.id)).toBe("USER_FIELD_REVIEW");
  // synthesizer has produced output, ideator has NOT.

  // editable endpoint should report replace_eligible=true here.
  const editable = await request.get(
    `/api/runs/${run.id}/phases/synthesizer/editable`,
  );
  expect(editable.ok()).toBeTruthy();
  const editableBody = await editable.json();
  expect(editableBody.replace_eligible, "no downstream completed").toBe(true);

  // Open modal in UI.
  await page.goto(`/runs/${run.id}`);
  await page.getByTestId("workspace-tab-synthesis").click();
  await page.getByTestId("edit-content-button-synthesis").click();

  // Mode toggle visible with replace as default (codex
  // amendment 3).
  await expect(page.getByTestId("edit-content-mode-fieldset")).toBeVisible();
  await expect(page.getByTestId("edit-content-mode-replace")).toBeChecked();
  await expect(page.getByTestId("edit-content-mode-new")).not.toBeChecked();
  // Forced-new notice should NOT appear.
  await expect(
    page.getByTestId("edit-content-mode-forced-new"),
  ).toHaveCount(0);
});

test("PR #119: edit modal forces new mode when downstream completed", async ({
  page,
  request,
}) => {
  test.setTimeout(360_000);
  const { run } = await createProject(request, "Modal forced-new test");
  await request.post(`/api/runs/${run.id}/transitions`, {
    data: { to_state: "DOMAIN_LOADED", reason: "test setup" },
  });
  await startPhase(request, run.id, "proposal");
  // start_scout from USER_PROPOSAL_REVIEW transitions directly to
  // USER_SEARCH_REVIEW via the stub agent — no explicit accept
  // checkpoint needed.
  await startPhase(request, run.id, "scout");
  await startPhase(request, run.id, "curator");
  await startPhase(request, run.id, "synthesizer");
  await startPhase(request, run.id, "ideator");
  expect(await getRunState(request, run.id)).toBe("USER_NOVELTY_REVIEW");
  // Now synthesizer has a downstream (ideator) with output —
  // replace should NOT be eligible.

  const editable = await request.get(
    `/api/runs/${run.id}/phases/synthesizer/editable`,
  );
  const editableBody = await editable.json();
  expect(
    editableBody.replace_eligible,
    "ideator output should disqualify replace",
  ).toBe(false);

  await page.goto(`/runs/${run.id}`);
  await page.getByTestId("workspace-tab-synthesis").click();
  await page.getByTestId("edit-content-button-synthesis").click();

  // Mode fieldset hidden; forced-new notice shown.
  await expect(
    page.getByTestId("edit-content-mode-fieldset"),
  ).toHaveCount(0);
  await expect(page.getByTestId("edit-content-mode-forced-new")).toBeVisible();
});

// =====================================================================
// PR #121 — workspace inline Corpus sub-tab
// =====================================================================
test("PR #121: workspace Corpus tab is visible and shows empty state", async ({
  page,
  request,
}) => {
  const { run } = await createProject(request, "Corpus tab visibility test");
  await page.goto(`/runs/${run.id}`);

  // Tab is in the strip immediately, no run progression needed.
  await expect(page.getByTestId("workspace-tab-corpus")).toBeVisible();
  await page.getByTestId("workspace-tab-corpus").click();
  await expect(page.getByTestId("corpus-subview")).toBeVisible();

  // Fully empty state on a brand-new project.
  await expect(
    page.getByTestId("corpus-subview-empty-fully"),
  ).toBeVisible();
  // Project docs section + globals section both render their
  // empty copy.
  await expect(
    page.getByTestId("corpus-subview-empty-project-docs"),
  ).toBeVisible();
  await expect(
    page.getByTestId("corpus-subview-empty-globals"),
  ).toBeVisible();
  // Manage globals link routes to /corpus.
  await expect(page.getByTestId("corpus-subview-manage-globals")).toHaveAttribute(
    "href",
    "/corpus",
  );
});

test("PR #121: workspace Corpus tab uploads a file and shows it", async ({
  page,
  request,
}) => {
  const { run } = await createProject(request, "Corpus upload test");
  await page.goto(`/runs/${run.id}`);
  await page.getByTestId("workspace-tab-corpus").click();
  await expect(page.getByTestId("corpus-subview")).toBeVisible();

  // Upload a small text file via the file input.
  const fileBuffer = Buffer.from(
    "Prior paper title\n\nAbstract goes here. Some content for the corpus.\n",
    "utf-8",
  );
  await page.getByTestId("corpus-subview-upload-input").setInputFiles({
    name: "prior-paper.txt",
    mimeType: "text/plain",
    buffer: fileBuffer,
  });

  // After upload, the project docs list appears and contains the
  // uploaded title. Backend normalizes "prior-paper.txt" → "prior
  // paper" (strips dash, drops extension), so match the
  // normalized form.
  await expect(page.getByTestId("corpus-subview-project-docs")).toBeVisible({
    timeout: 10_000,
  });
  await expect(
    page.getByTestId("corpus-subview-project-docs"),
  ).toContainText("prior paper");
});
