import { defineConfig, devices } from "@playwright/test";

// Screenshot-capture config. Reuses the same stub e2e server as the
// default config but only matches the help screenshot specs,
// which writes PNGs into `public/help-assets/<lang>/` for the
// HelpPage. Not part of CI — run manually via `npm run capture:help`
// when help-page screenshots need refreshing.
//
// Ports honour AUTOESSAY_E2E_API_PORT / AUTOESSAY_E2E_VITE_PORT so
// this can run alongside another local checkout.

const vitePort = process.env.AUTOESSAY_E2E_VITE_PORT ?? "5173";

export default defineConfig({
  testDir: "./e2e",
  testMatch: [
    "**/capture-screenshots.spec.ts",
    "**/help-screenshots-express.spec.ts",
  ],
  timeout: 180_000,
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
