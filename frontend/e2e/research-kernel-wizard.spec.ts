import { expect, test, type Page } from "@playwright/test";

// PR-C0.b2.tests: kernel-intake wizard end-to-end. Exercises the
// NewRunPage research-kernel intake gate against the stub backend.
// Five scenarios:
//
//   1. disabled-then-enabled: fill the kernel fields incrementally
//      and assert submit goes from disabled → enabled with the
//      right reason text at each stage.
//   2. mode-selection: empirical (developer_preview) is now always
//      selectable per the PR-C0.b2.tests UX change; selecting it
//      flips the ack row visible, ticking ack + primary materials
//      enables submit, and the created run has paper_mode='empirical'.
//   3. degraded-banner: GET /api/paper_modes stubbed to 500;
//      degraded banner is visible AND submit still works because
//      FALLBACK_MODE_SPEC keeps the form submittable.
//   4. submit-creates-run: full happy path; assert via
//      /api/runs/{id} that the kernel payload + paper_mode landed.
//   5. partial-failure-deeplink: PUT /research_kernel stubbed to
//      500 once; spec asserts the workspace opens with the kernel
//      edit modal auto-opened (?repair=kernel deeplink consumed and
//      stripped via history.replaceState).
//
// Each test is independent (no serial mode) — codex round-2.b2.tests
// guidance: cleanup leaks active essay limits otherwise.

const TITLE_PREFIX = "[PWTEST-KW]";

function freshTitle(): string {
  return `${TITLE_PREFIX} ${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)} ${Math.floor(Math.random() * 1000)}`;
}

async function cleanupExistingRuns(page: Page): Promise<void> {
  await page.goto("/");
  await Promise.race([
    page.waitForSelector('[data-testid="run-card"]', { timeout: 8_000 }),
    page.waitForTimeout(2_000),
  ]);
  let rounds = 0;
  while (rounds < 30) {
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
    rounds++;
  }
}

const VALID_PUZZLE =
  "我观察到既有研究在断代上存在反复，需要重新检视一手材料以厘清边界关系。"; // 33 chars > 30
const VALID_QUESTION = "此组文献的断代依据如何被重新建立？";
const VALID_SCOPE = "19 世纪后期江南刊本，序跋与刻工题记。";

async function fillKernelFields(page: Page): Promise<void> {
  await page
    .locator('[data-testid="newrun-kernel-observed-puzzle"]')
    .fill(VALID_PUZZLE);
  await page
    .locator('[data-testid="newrun-kernel-tentative-question"]')
    .fill(VALID_QUESTION);
  await page.locator('[data-testid="newrun-kernel-scope"]').fill(VALID_SCOPE);
  await page.locator('[data-testid="newrun-kernel-primary-yes"]').click();
}

async function setTitle(page: Page, title: string): Promise<void> {
  await page.locator('[data-testid="newrun-title"]').fill(title);
}

test.beforeEach(async ({ page }) => {
  page.on("dialog", (d) => d.accept());
  await cleanupExistingRuns(page);
});

test("PR-C0.b2.tests wizard #1 — submit disabled until kernel valid", async ({
  page,
}) => {
  await page.goto("/runs/new");
  await setTitle(page, freshTitle());

  const submit = page.locator('[data-testid="newrun-submit"]');
  const reason = page.locator('[data-testid="newrun-kernel-disabled-reason"]');

  // Initial — empty puzzle. Submit disabled, reason mentions puzzle.
  await expect(submit).toBeDisabled();
  await expect(reason).toBeVisible();
  await expect(reason).toContainText(/疑点|puzzle/i);

  // Puzzle too short — still disabled, still puzzle reason.
  await page
    .locator('[data-testid="newrun-kernel-observed-puzzle"]')
    .fill("太短");
  await expect(submit).toBeDisabled();
  await expect(reason).toContainText(/30/);

  // Puzzle long enough — reason flips to question.
  await page
    .locator('[data-testid="newrun-kernel-observed-puzzle"]')
    .fill(VALID_PUZZLE);
  await expect(submit).toBeDisabled();
  await expect(reason).toContainText(/拟研究问题|research question/i);

  // Question filled — reason flips to scope.
  await page
    .locator('[data-testid="newrun-kernel-tentative-question"]')
    .fill(VALID_QUESTION);
  await expect(submit).toBeDisabled();
  await expect(reason).toContainText(/范围|scope/i);

  // Scope filled — submit enables (case_analysis default does not
  // require primary materials, so primary=none default is fine).
  await page.locator('[data-testid="newrun-kernel-scope"]').fill(VALID_SCOPE);
  await expect(submit).toBeEnabled();
  await expect(reason).toBeHidden();
});

test("kernel suggest button fills empty kernel fields from stubbed LLM", async ({
  page,
}) => {
  let requestPayload: Record<string, unknown> | null = null;
  await page.route("**/api/runs/kernel_suggest", async (route) => {
    requestPayload = JSON.parse(route.request().postData() || "{}");
    await new Promise((resolve) => setTimeout(resolve, 150));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        suggestion: {
          observed_puzzle:
            "既有研究把货币承诺视为单一政策选择，但材料显示国内政治与国际约束之间存在持续张力。",
          tentative_question: "战后货币承诺如何同时受国内政治与国际制度约束？",
          scope: "1944-1971 年布雷顿森林体系下的美国与西欧货币政策讨论。",
          method_preference: "制度史分析与政策文本细读",
          theory_preference: "历史制度主义",
        },
        model: "gpt-5.4",
        max_tokens: 900,
      }),
    });
  });

  await page.goto("/runs/new");
  const button = page.locator('[data-testid="kernel-suggest-button"]');

  await page.locator('[data-testid="newrun-title"]').fill("abc");
  await expect(button).toBeDisabled();

  const title = freshTitle();
  await setTitle(page, title);
  await page
    .locator('[data-testid="newrun-kernel-method-preference"]')
    .fill("保留手写方法");
  await expect(button).toBeEnabled({ timeout: 10_000 });

  const response = page.waitForResponse(
    (resp) =>
      resp.url().includes("/api/runs/kernel_suggest") &&
      resp.request().method() === "POST" &&
      resp.status() === 200,
  );
  await button.click();
  await expect(
    page.locator('[data-testid="kernel-suggest-loading"]'),
  ).toBeVisible();
  await response;

  expect(requestPayload?.title).toBe(title);
  expect(requestPayload?.domain_id).toBeTruthy();
  await expect(
    page.locator('[data-testid="newrun-kernel-observed-puzzle"]'),
  ).toHaveValue(
    "既有研究把货币承诺视为单一政策选择，但材料显示国内政治与国际约束之间存在持续张力。",
  );
  await expect(
    page.locator('[data-testid="newrun-kernel-tentative-question"]'),
  ).toHaveValue("战后货币承诺如何同时受国内政治与国际制度约束？");
  await expect(page.locator('[data-testid="newrun-kernel-scope"]')).toHaveValue(
    "1944-1971 年布雷顿森林体系下的美国与西欧货币政策讨论。",
  );
  await expect(
    page.locator('[data-testid="newrun-kernel-method-preference"]'),
  ).toHaveValue("保留手写方法");
  await expect(
    page.locator('[data-testid="newrun-kernel-theory-preference"]'),
  ).toHaveValue("历史制度主义");
  await expect(
    page.locator('[data-testid="kernel-suggest-error"]'),
  ).toBeHidden();
});

test("PR-C0.b2.tests wizard #2 — empirical preview ack flow", async ({
  page,
}) => {
  await page.goto("/runs/new");
  await setTitle(page, freshTitle());
  await fillKernelFields(page);

  // case_analysis is default; submit is enabled.
  const submit = page.locator('[data-testid="newrun-submit"]');
  await expect(submit).toBeEnabled();

  // Click empirical radio — now selectable per UX change.
  await page.locator('[data-testid="newrun-kernel-radio-empirical"]').click();

  // Ack row is now visible, submit disabled (no ack yet) AND
  // primary_materials="yes" was already set by fillKernelFields,
  // so the only blocker is the ack.
  await expect(
    page.locator('[data-testid="newrun-kernel-ack-row"]'),
  ).toBeVisible();
  await expect(submit).toBeDisabled();
  await expect(
    page.locator('[data-testid="newrun-kernel-disabled-reason"]'),
  ).toContainText(/预览|preview/i);

  // Tick ack — submit enables.
  await page.locator('[data-testid="newrun-kernel-ack-checkbox"]').check();
  await expect(submit).toBeEnabled();

  // Primary=none would re-block (empirical requires materials).
  await page.locator('[data-testid="newrun-kernel-primary-none"]').click();
  await expect(submit).toBeDisabled();
  await expect(
    page.locator('[data-testid="newrun-kernel-disabled-reason"]'),
  ).toContainText(/一手材料|primary materials/i);
  // Restore primary=yes.
  await page.locator('[data-testid="newrun-kernel-primary-yes"]').click();
  await expect(submit).toBeEnabled();
});

test("PR-C0.b2.tests wizard #3 — degraded banner when paper_modes 500", async ({
  page,
}) => {
  await page.route("**/api/paper_modes", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ detail: "registry unavailable" }),
    }),
  );

  await page.goto("/runs/new");
  await setTitle(page, freshTitle());

  // Degraded banner visible.
  await expect(
    page.locator('[data-testid="newrun-kernel-degraded-banner"]'),
  ).toBeVisible({ timeout: 10_000 });

  // FALLBACK_MODE_SPEC keeps form submittable; fill required fields.
  await fillKernelFields(page);
  await expect(page.locator('[data-testid="newrun-submit"]')).toBeEnabled();
});

test("PR-C0.b2.tests wizard #4 — submit creates run with correct kernel", async ({
  page,
  request,
}) => {
  await page.goto("/runs/new");
  await setTitle(page, freshTitle());
  await fillKernelFields(page);

  await page.locator('[data-testid="newrun-submit"]').click();
  await page.waitForURL(/\/runs\/run_/, { timeout: 30_000 });
  const runId = page.url().split("/runs/")[1].split("?")[0];

  const resp = await request.get(`/api/runs/${runId}`);
  expect(resp.ok()).toBeTruthy();
  const run = await resp.json();
  expect(run.paper_mode).toBe("case_analysis");
  expect(run.research_kernel.tentative_question).toBe(VALID_QUESTION);
  expect(run.research_kernel.scope).toBe(VALID_SCOPE);
  expect(run.research_kernel.primary_materials_status).toBe("yes");
});

test("PR-C0.b2.tests wizard #5 — partial-failure deeplinks to repair modal", async ({
  page,
}) => {
  // Stub PUT /api/runs/.../research_kernel to 500 once; subsequent
  // requests pass through (the modal save retry from inside the
  // workspace must succeed).
  let putAttempts = 0;
  await page.route("**/api/runs/**/research_kernel", async (route) => {
    if (route.request().method() === "PUT" && putAttempts === 0) {
      putAttempts++;
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "server error" }),
      });
      return;
    }
    await route.continue();
  });

  await page.goto("/runs/new");
  await setTitle(page, freshTitle());
  await fillKernelFields(page);

  await page.locator('[data-testid="newrun-submit"]').click();
  await page.waitForURL(/\/runs\/run_/, { timeout: 30_000 });

  // Workspace renders; KernelEditModal auto-opens via the
  // ?repair=kernel deeplink. WorkspacePage strips the query param
  // immediately via history.replaceState, so the URL must NOT
  // contain ?repair=kernel by the time the modal is visible.
  await expect(page.locator('[data-testid="kernel-edit-modal"]')).toBeVisible({
    timeout: 15_000,
  });
  expect(page.url()).not.toMatch(/[?&]repair=kernel/);
});
