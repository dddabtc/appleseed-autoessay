import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const workspaceSource = readFileSync(
  new URL("./WorkspacePage.tsx", import.meta.url),
  "utf8",
);

function blockBetween(startMarker: string, endMarker: string): string {
  const start = workspaceSource.indexOf(startMarker);
  const end = workspaceSource.indexOf(endMarker, start);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(start);
  return workspaceSource.slice(start, end);
}

describe("WorkspacePage express regenerate", () => {
  it("starts the new express run after copying the research kernel", () => {
    const block = blockBetween(
      "async function handleCreateModeRun",
      "async function handleRunCritic",
    );

    expect(block).toContain('if (mode === "express")');
    expect(block).toContain('await startProposal(created.id, "");');
    expect(block.indexOf("await editResearchKernel")).toBeLessThan(
      block.indexOf('await startProposal(created.id, "");'),
    );
  });

  it("uses the backend expected state for proposal-start optimistic UI", () => {
    const block = blockBetween(
      "async function handleRunProposal",
      "async function handleSaveProposal",
    );

    expect(block).toContain("const job = await startProposal(id, userDraft);");
    expect(block).toContain("state: job.expected_state");
  });
});
