/**
 * PR-388 static-source contract: the auto-pilot status badge must be
 * present on both the Workspace header and the RunsPage run-card meta
 * row, and must be gated on ``run.auto_advance``. Before PR-388 the
 * user had no way to see whether a run was in auto-pilot mode without
 * opening the Workspace style sidebar — they reported confusion when
 * the screen showed a manual-style "Generate proposal" button while
 * the toggle was actually on (separately fixed in PR-386).
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKSPACE = readFileSync(join(__dirname, "WorkspacePage.tsx"), "utf-8");
const RUNS_PAGE = readFileSync(join(__dirname, "RunsPage.tsx"), "utf-8");
const I18N = readFileSync(
  join(__dirname, "..", "lib", "i18n.ts"),
  "utf-8",
);

describe("PR-388 auto-pilot badge wiring", () => {
  it("Workspace header carries an auto-pilot badge with the right testid", () => {
    expect(WORKSPACE).toContain('data-testid="workspace-auto-pilot-badge"');
  });

  it("Workspace badge is gated on run.auto_advance", () => {
    // ``run?.auto_advance ? (<span ...badge.../>) : null``
    expect(WORKSPACE).toMatch(/run\?\.auto_advance\s*\?\s*\(/);
  });

  it("RunsPage card meta row carries an auto-pilot badge with the right testid", () => {
    expect(RUNS_PAGE).toContain('data-testid="runs-card-auto-pilot-badge"');
  });

  it("RunsPage badge is gated on run.auto_advance", () => {
    expect(RUNS_PAGE).toMatch(/run\.auto_advance\s*\?\s*\(/);
  });

  it("i18n has the shared auto_pilot.badge key in en/zh/ja", () => {
    expect(I18N).toContain('"auto_pilot.badge"');
    expect(I18N).toMatch(/"auto_pilot\.badge"[\s\S]*?en:\s*"Auto-pilot"/);
    expect(I18N).toMatch(/"auto_pilot\.badge"[\s\S]*?zh:\s*"全自动"/);
    expect(I18N).toMatch(/"auto_pilot\.badge"[\s\S]*?ja:\s*"自動運転"/);
  });

  it("i18n has the shared auto_pilot.tooltip key", () => {
    expect(I18N).toContain('"auto_pilot.tooltip"');
  });
});
