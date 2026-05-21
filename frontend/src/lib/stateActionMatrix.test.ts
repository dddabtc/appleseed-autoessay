import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

interface MatrixAction {
  id: string;
  ui_keys?: string[];
  ui_key_pattern?: string;
  ui_testids?: string[];
  ui_testids_secondary?: string[];
  ui_testid_pattern?: string;
}

interface StateActionMatrix {
  schema_version: number;
  actions: MatrixAction[];
}

const matrix = JSON.parse(
  readFileSync(
    new URL("../../../docs/state_action_matrix.json", import.meta.url),
    "utf8",
  ),
) as StateActionMatrix;

const workspaceSource = readFileSync(
  new URL("../pages/WorkspacePage.tsx", import.meta.url),
  "utf8",
);

function phaseActionsBlock(): string {
  const start = workspaceSource.indexOf("const phaseActions = [");
  const end = workspaceSource.indexOf(
    "].filter((action): action is PhaseAction",
    start,
  );
  expect(start).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(start);
  return workspaceSource.slice(start, end);
}

function matrixActionKeys(): Set<string> {
  return new Set(matrix.actions.flatMap((action) => action.ui_keys ?? []));
}

function matrixTestIds(): Set<string> {
  return new Set(
    matrix.actions.flatMap((action) => [
      ...(action.ui_testids ?? []),
      ...(action.ui_testids_secondary ?? []),
    ]),
  );
}

describe("state action matrix", () => {
  it("covers every WorkspacePage phase action key", () => {
    const block = phaseActionsBlock();
    const keysInWorkspace = Array.from(
      block.matchAll(/key:\s*"([^"]+)"/g),
      (match) => match[1],
    );
    const keysInMatrix = matrixActionKeys();

    expect(keysInWorkspace.length).toBeGreaterThan(0);
    for (const key of keysInWorkspace) {
      expect(keysInMatrix.has(key)).toBe(true);
    }

    expect(workspaceSource.includes("key: `retry-${failedPhase}`")).toBe(true);
    expect(
      matrix.actions.some((action) => action.ui_key_pattern === "retry-{phase}"),
    ).toBe(true);
  });

  it("covers every static phase-action testid rendered by WorkspacePage", () => {
    const testIdsInWorkspace = Array.from(
      workspaceSource.matchAll(/data-testid="(phase-action-[^"]+)"/g),
      (match) => match[1],
    );
    const testIdsInMatrix = matrixTestIds();

    expect(testIdsInWorkspace.length).toBeGreaterThan(0);
    for (const testId of testIdsInWorkspace) {
      expect(testIdsInMatrix.has(testId)).toBe(true);
    }
  });

  it("declares stable testids for matrix-listed UI actions", () => {
    const hasDynamicPhaseActionTestId = workspaceSource.includes(
      "data-testid={`phase-action-${action.key}`}",
    );
    const keysInMatrix = matrixActionKeys();

    for (const testId of matrixTestIds()) {
      if (workspaceSource.includes(`data-testid="${testId}"`)) continue;
      if (
        hasDynamicPhaseActionTestId &&
        testId.startsWith("phase-action-") &&
        keysInMatrix.has(testId.replace(/^phase-action-/, ""))
      ) {
        continue;
      }
      throw new Error(`Matrix testid is not rendered by WorkspacePage: ${testId}`);
    }

    expect(
      matrix.actions.some(
        (action) => action.ui_testid_pattern === "phase-action-retry-{phase}",
      ),
    ).toBe(true);
  });
});
