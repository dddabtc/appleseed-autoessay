/**
 * PR-389 static-source contract: bulk hard-delete UI on the RunsPage.
 * The user reported that deleted essays should support batch select +
 * permanent removal. The flow:
 *
 * 1. ``show deleted`` toggle is already on (PR-388 / earlier).
 * 2. Each deleted card renders a checkbox bound to ``selectedRunIds``.
 * 3. A red action bar appears with "select all" / "clear" / count /
 *    "permanently delete" buttons.
 * 4. Confirm dialog (window.confirm) gates the API call.
 * 5. Helper dedupes by project: if a card is project-deleted, hit
 *    ``hardDeleteProject`` once instead of ``hardDeleteRun`` for every
 *    sibling card.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const RUNS_PAGE = readFileSync(join(__dirname, "RunsPage.tsx"), "utf-8");
const API = readFileSync(
  join(__dirname, "..", "lib", "api.ts"),
  "utf-8",
);
const I18N = readFileSync(
  join(__dirname, "..", "lib", "i18n.ts"),
  "utf-8",
);

describe("PR-389 bulk hard-delete UI on RunsPage", () => {
  it("imports both hardDeleteRun and hardDeleteProject from api", () => {
    expect(RUNS_PAGE).toContain("hardDeleteRun");
    expect(RUNS_PAGE).toContain("hardDeleteProject");
  });

  it("renders the bulk action bar only when show-deleted is on", () => {
    expect(RUNS_PAGE).toMatch(
      /showDeleted\s*&&\s*deletedCardsVisible\.length\s*>\s*0/,
    );
  });

  it("bulk bar has select-all, clear, count, and submit testids", () => {
    expect(RUNS_PAGE).toContain('data-testid="runs-bulk-hard-delete-bar"');
    expect(RUNS_PAGE).toContain('data-testid="runs-bulk-select-all"');
    expect(RUNS_PAGE).toContain('data-testid="runs-bulk-clear-selection"');
    expect(RUNS_PAGE).toContain('data-testid="runs-bulk-hard-delete-submit"');
  });

  it("deleted cards render a bulk-select checkbox", () => {
    expect(RUNS_PAGE).toContain('data-testid="run-bulk-select-checkbox"');
    // Checkbox is gated on isDeleted (run-level OR project-level).
    expect(RUNS_PAGE).toMatch(/isDeleted\s*\?\s*\(\s*<label/);
  });

  it("submit handler asks window.confirm before deleting", () => {
    expect(RUNS_PAGE).toMatch(/window\.confirm\(\s*t\("runs\.hard_delete_confirm"\)/);
  });

  it("submit handler dedupes by project-deleted via hardDeleteProject", () => {
    expect(RUNS_PAGE).toMatch(/projectsToHardDelete/);
    expect(RUNS_PAGE).toMatch(/hardDeleteProject\(/);
    expect(RUNS_PAGE).toMatch(/hardDeleteRun\(/);
  });

  it("api.ts exports hardDeleteRun and hardDeleteProject pointing to /hard", () => {
    expect(API).toMatch(/export async function hardDeleteRun/);
    expect(API).toMatch(/export async function hardDeleteProject/);
    expect(API).toContain("/api/runs/${runId}/hard");
    expect(API).toContain("/api/projects/${projectId}/hard");
  });

  it("api.ts surfaces 409 detail through ApiError so the user sees the reason", () => {
    // Constructor signature: ``new ApiError(status, body, message)``.
    // The hard-delete helpers throw ApiError to preserve the 409 detail
    // (e.g. "active phase lock" / "must be soft-deleted first").
    expect(API).toMatch(/hardDeleteRun[\s\S]{0,500}throw new ApiError/);
    expect(API).toMatch(/hardDeleteProject[\s\S]{0,500}throw new ApiError/);
  });

  it("i18n has runs.bulk_* and runs.hard_delete_* keys in all 3 languages", () => {
    for (const key of [
      "runs.bulk_select_all",
      "runs.bulk_clear_selection",
      "runs.bulk_selected_count",
      "runs.bulk_hard_delete_submit",
      "runs.hard_delete_confirm",
      "runs.hard_delete_failed",
    ]) {
      expect(I18N).toContain(`"${key}"`);
    }
    // Spot-check a translation
    expect(I18N).toMatch(/"runs\.bulk_hard_delete_submit"[\s\S]*?zh:\s*"永久删除"/);
  });
});
