import { defineConfig, devices } from "@playwright/test";

// PR-J1 PC layout audit config. Reuses the stub e2e server but only
// matches `capture-pc-layout.spec.ts`, which walks the 11 phase
// states for both zh and en and writes screenshots to
// `/tmp/layout-audit/<lang>/`. Not part of CI — invoke manually
// via `npm run capture:layout` when auditing PC viewport layouts
// after a UI/CSS change.
//
// Ports honour AUTOESSAY_E2E_API_PORT / AUTOESSAY_E2E_VITE_PORT so
// this can run alongside another local checkout.

const vitePort = process.env.AUTOESSAY_E2E_VITE_PORT ?? "5173";

export default defineConfig({
  testDir: "./e2e",
  testMatch: ["**/capture-pc-layout.spec.ts"],
  timeout: 300_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${vitePort}`,
    trace: "off",
    headless: true,
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
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
