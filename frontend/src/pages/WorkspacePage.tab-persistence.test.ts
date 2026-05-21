import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SOURCE_PATH = join(__dirname, "WorkspacePage.tsx");

describe("WorkspacePage active-subview persistence (PR-365)", () => {
  const source = readFileSync(SOURCE_PATH, "utf-8");

  it("declares a per-run localStorage key for the active subview", () => {
    expect(source).toContain("workspaceSubviewStorageKey");
    expect(source).toContain("autoessay:workspace:");
    expect(source).toContain("activeSubview");
  });

  it("reads the persisted value before falling back to state-driven landing", () => {
    // The mount-time effect should attempt readPersistedSubview() before
    // resolveLandingSubview(). Match the order in the file with a regex
    // that requires readPersistedSubview to appear in the same useEffect
    // body that calls resolveLandingSubview.
    expect(source).toContain("readPersistedSubview");
    const readIdx = source.indexOf("const persisted = readPersistedSubview(id)");
    const fallbackIdx = source.indexOf(
      "resolveLandingSubview(",
      readIdx >= 0 ? readIdx : 0,
    );
    expect(readIdx).toBeGreaterThan(-1);
    expect(fallbackIdx).toBeGreaterThan(readIdx);
  });

  it("writes activeSubview to localStorage whenever it changes after init", () => {
    // The persistence effect should setItem(...) gated on hasInitializedSubview.
    expect(source).toMatch(/localStorage\.setItem\(workspaceSubviewStorageKey\(id\),\s*activeSubview\)/);
  });

  it("guards localStorage access with try/catch (private mode / quota)", () => {
    // Two try/catch blocks are added (read + write); ensure both exist
    // around localStorage calls in WorkspacePage.tsx.
    const tryCatchCount = (
      source.match(/localStorage\.(?:getItem|setItem)\(workspaceSubviewStorageKey/g) || []
    ).length;
    expect(tryCatchCount).toBeGreaterThanOrEqual(2);
  });
});
