import { defineConfig, devices } from "@playwright/test";

// Ports default to the canonical 5173 used by CI; overrideable via
// AUTOESSAY_E2E_VITE_PORT / AUTOESSAY_E2E_API_PORT (read by
// scripts/run-e2e-server.sh and vite.config.ts) so the suite can
// run alongside another local checkout.
const vitePort = process.env.AUTOESSAY_E2E_VITE_PORT ?? "5173";

export default defineConfig({
  testDir: ".",
  testMatch: ["e2e/**/*.spec.ts", "tests/e2e/**/*.spec.ts"],
  // Stage 3.C base config ships smoke + happy-path stub tests in CI.
  // Screenshot and acceptance captures are manual workflows, not part
  // of the default local suite.
  testIgnore: [
    // Locale-aware help-page screenshot generator. Run manually
    // via `npm run capture:help` (uses playwright.screenshots.config.ts);
    // writes large PNG assets and is not part of CI.
    "**/capture-screenshots.spec.ts",
    // PR-J1 PC layout audit: walks 11 phase states for both zh+en
    // and writes screenshots to /tmp/layout-audit/. Run manually via
    // `npm run capture:layout` (uses playwright.layout.config.ts).
    "**/capture-pc-layout.spec.ts",
    // PR-C2c acceptance gate for the framework_lens milestone. It is
    // opt-in because it disables selected stubs and can call external
    // services when configured.
    // Run manually via
    //   ( cd frontend && AUTOESSAY_E2E_VITE_PORT=5174 AUTOESSAY_E2E_API_PORT=8018
    //     npx playwright test
    //     e2e/theory-article-lens-acceptance.spec.ts )
    "**/theory-article-lens-acceptance.spec.ts",
  ],
  timeout: 60_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${vitePort}`,
    trace: "on-first-retry",
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: "bash scripts/run-e2e-server.sh",
    url: `http://127.0.0.1:${vitePort}`,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
