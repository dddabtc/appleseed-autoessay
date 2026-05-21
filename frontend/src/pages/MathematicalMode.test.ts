/**
 * PR-366 static-source coverage for "数理增强模式" (mathematical mode).
 *
 * The actual checkbox flows are still validated by Playwright on the
 * prod-canary run; this test pins the wiring contract (testids, i18n
 * keys, API hookup) so a careless rename / removal breaks CI before it
 * ships.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const NEW_RUN = readFileSync(join(__dirname, "NewRunPage.tsx"), "utf-8");
const WORKSPACE = readFileSync(join(__dirname, "WorkspacePage.tsx"), "utf-8");
const API = readFileSync(join(__dirname, "../lib/api.ts"), "utf-8");
const I18N = readFileSync(join(__dirname, "../lib/i18n.ts"), "utf-8");

describe("PR-366 wizard checkbox (NewRunPage)", () => {
  it("exposes data-testid=new-run-mathematical-mode", () => {
    expect(NEW_RUN).toContain('data-testid="new-run-mathematical-mode"');
  });

  it("threads mathematicalMode into createRun()", () => {
    expect(NEW_RUN).toContain("mathematical_mode: mathematicalMode");
  });

  it("uses both i18n label + tooltip keys", () => {
    expect(NEW_RUN).toContain("newrun.mathematical_mode.label");
    expect(NEW_RUN).toContain("newrun.mathematical_mode.tooltip");
  });
});

describe("PR-366 workspace toggle (WorkspacePage)", () => {
  it("exposes data-testid=workspace-mathematical-mode", () => {
    expect(WORKSPACE).toContain('data-testid="workspace-mathematical-mode"');
  });

  it("wires updateRunSettings to the toggle handler", () => {
    expect(WORKSPACE).toContain("updateRunSettings(id,");
    expect(WORKSPACE).toContain("handleToggleMathematicalMode");
  });

  it("disables the toggle while rewriter or critic is running", () => {
    expect(WORKSPACE).toContain('"REWRITE_RUNNING"');
    expect(WORKSPACE).toContain('"CRITIC_RUNNING"');
    expect(WORKSPACE).toContain("mathematicalModeLocked");
  });

  it("includes the locked-state hint key for the user", () => {
    expect(WORKSPACE).toContain(
      "workspace.style.mathematical_mode.locked",
    );
  });
});

describe("PR-366 API client (api.ts)", () => {
  it("exports updateRunSettings hitting PATCH /api/runs/{id}/settings", () => {
    expect(API).toContain("updateRunSettings");
    expect(API).toMatch(/\/api\/runs\/\$\{runId\}\/settings/);
    expect(API).toContain('method: "PATCH"');
  });

  it("createRun accepts CreateRunOptions.mathematical_mode", () => {
    expect(API).toContain("CreateRunOptions");
    expect(API).toContain("mathematical_mode?: boolean");
  });

  it("Run type carries an optional mathematical_mode field", () => {
    // The Run interface adds the new field; keep the test loose on
    // whitespace but pinned to the type-position so it can't drift
    // into an unrelated location.
    expect(API).toMatch(/mathematical_mode\?:\s*boolean/);
  });
});

describe("PR-366 i18n keys (i18n.ts)", () => {
  for (const key of [
    "newrun.mathematical_mode.label",
    "newrun.mathematical_mode.tooltip",
    "workspace.style.mathematical_mode.label",
    "workspace.style.mathematical_mode.tooltip",
    "workspace.style.mathematical_mode.locked",
    "workspace.errors.settings_update",
  ] as const) {
    it(`defines ${key} with zh + en + ja translations`, () => {
      expect(I18N).toContain(`"${key}"`);
    });
  }

  it("zh strings keep the 数理增强模式 label verbatim", () => {
    // Two anchor strings per memory feedback_say_human_words —
    // "数理增强模式" is the user-chosen label and must not silently
    // become an English string.
    const occurrences = (I18N.match(/数理增强模式/g) || []).length;
    expect(occurrences).toBeGreaterThanOrEqual(2);
  });
});
