/**
 * PR-371 frontend wiring contract: ExportSubview must display the
 * title-slug-derived ``download_filename`` from the API and pass it
 * to the link's ``download`` attribute. The on-disk URL is unchanged.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKSPACE = readFileSync(
  join(__dirname, "WorkspacePage.tsx"),
  "utf-8",
);
const API = readFileSync(join(__dirname, "../lib/api.ts"), "utf-8");

describe("PR-371 ExportSubview wiring", () => {
  it("prefers download_filename over the disk filename", () => {
    // The view must read file.download_filename first and fall back
    // to file.filename only if the older backend omits the field.
    expect(WORKSPACE).toMatch(
      /file\.download_filename\s*\?\?\s*file\.filename/,
    );
  });

  it("sets the <a download> attribute to the resolved display name", () => {
    expect(WORKSPACE).toContain("download={displayName}");
  });

  it("each export file row exposes a data-testid", () => {
    // For Playwright assertions on the slug-derived filename in real
    // canaries (PR-371 follow-up).
    expect(WORKSPACE).toContain('data-testid={`export-file-${file.format}`}');
  });
});

describe("PR-371 ExportFileLink type contract", () => {
  it("declares optional download_filename on ExportFileLink", () => {
    expect(API).toMatch(/download_filename\?:\s*string/);
  });
});
