/**
 * PR-377 static-source contract: workspace ``MarkdownView`` must parse
 * markdown tables, ``$$...$$`` display math, and ``![alt](url)``
 * images. Before this PR the hand-rolled renderer fell through to
 * raw paragraphs for all three, so users saw ``|---|---|`` literal
 * text in the workspace preview even after the export pipeline was
 * fixed.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKSPACE = readFileSync(join(__dirname, "WorkspacePage.tsx"), "utf-8");

describe("PR-377 MarkdownView table / image / math", () => {
  it("recognises markdown table separator rows", () => {
    expect(WORKSPACE).toContain("MD_TABLE_SEPARATOR_RE");
    expect(WORKSPACE).toContain("/^\\|[\\s:|-]+\\|$/");
  });

  it("renders tables inside <figure> with 表 N <figcaption>", () => {
    // The new code emits ``data-testid={`markdown-table-${tableCount}`}``
    // and the literal ``表 ${tableCount}`` text in the figcaption.
    expect(WORKSPACE).toContain('data-testid={`markdown-table-${tableCount}`}');
    expect(WORKSPACE).toMatch(/表\s*\{tableCount\}/);
  });

  it("renders standalone images inside <figure> with 图 N below", () => {
    expect(WORKSPACE).toContain('data-testid={`markdown-figure-${figureCount}`}');
    expect(WORKSPACE).toMatch(/图\s*\{figureCount\}/);
    expect(WORKSPACE).toContain("MD_IMAGE_RE");
  });

  it("handles $$ display math blocks across one or multiple lines", () => {
    expect(WORKSPACE).toContain('data-testid="markdown-math-block"');
    // Multi-line buffer flushes via flushMath.
    expect(WORKSPACE).toContain("function flushMath()");
    // Inline ``$$ ... $$`` on single line also handled.
    expect(WORKSPACE).toMatch(/trimmed\.startsWith\("\$\$"\)/);
  });

  it("does not regress the plain-paragraph path", () => {
    // The ``<p className="my-4 leading-7 text-slate-800"`` fallback
    // must still be emitted for non-table, non-image, non-list lines.
    expect(WORKSPACE).toMatch(/<p\s*\n\s*className="my-4 leading-7/);
  });

  it("keeps the - bullet list path", () => {
    // Lists continue to flush into <ul>.
    expect(WORKSPACE).toContain('list-disc space-y-2 pl-5');
    expect(WORKSPACE).toContain('line.startsWith("- ")');
  });
});
