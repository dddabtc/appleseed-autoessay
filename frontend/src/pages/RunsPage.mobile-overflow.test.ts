/**
 * PR-384 static-source contract: the runs-list page must not push
 * its grid tracks past the viewport on mobile. Before this PR a nested
 * ``grid gap-3/5`` chain without explicit ``grid-cols-1`` or ``min-w-0``
 * let intrinsic widths of long ASCII titles (timestamp tokens) and
 * ``shrink-0`` pill rows widen the card past 100vw on iPhone Safari,
 * with the right border + ellipsis falling outside the viewport.
 *
 * PR-380's attempted fix (``overflow-x-hidden`` on ``<main>``) only hid
 * the overflow, breaking pinch-zoom semantics on iOS. PR-383 reverted
 * that; PR-384 lands the real fix by constraining every grid/flex
 * ancestor in ``RunsPage.tsx`` with ``min-w-0 max-w-full grid-cols-1``
 * and adding ``overflow-wrap:anywhere`` to long crossref-style IDs.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const __dirname = dirname(fileURLToPath(import.meta.url));
const RUNS_PAGE = readFileSync(join(__dirname, "RunsPage.tsx"), "utf-8");
const APP = readFileSync(join(__dirname, "..", "App.tsx"), "utf-8");

describe("PR-384 mobile overflow constraints on RunsPage", () => {
  it("vertical section grid has grid-cols-1 + min-w-0", () => {
    // Outer ``<section>`` wrapping the runs list.
    expect(RUNS_PAGE).toMatch(/grid min-w-0 max-w-full grid-cols-1 gap-5/);
  });

  it("runs <ul> has grid-cols-1 + min-w-0 max-w-full", () => {
    expect(RUNS_PAGE).toMatch(
      /grid min-w-0 max-w-full grid-cols-1 list-none gap-3 p-0/,
    );
  });

  it("each run <li> has min-w-0 max-w-full", () => {
    // The list item carries data-testid="run-card" alongside the
    // width constraint; assert both appear on the same element.
    expect(RUNS_PAGE).toMatch(
      /className="min-w-0 max-w-full"\s+data-testid="run-card"/,
    );
  });

  it("card container has grid-cols-1 + min-w-0 max-w-full + overflow-hidden", () => {
    // Local overflow-hidden lives on the card, not on <main>.
    expect(RUNS_PAGE).toMatch(
      /grid min-w-0 max-w-full grid-cols-1 gap-3 overflow-hidden/,
    );
  });

  it("card header flex row carries min-w-0 max-w-full", () => {
    expect(RUNS_PAGE).toMatch(
      /flex min-w-0 max-w-full flex-wrap items-start justify-between gap-3/,
    );
  });

  it("title link keeps truncate + flex-1 with explicit max-w-full", () => {
    expect(RUNS_PAGE).toMatch(/min-w-0 max-w-full flex-1 truncate/);
  });

  it("right pill row allows flex-wrap with max-w-full instead of pure shrink-0", () => {
    expect(RUNS_PAGE).toMatch(
      /flex max-w-full shrink-0 flex-wrap items-center gap-2/,
    );
  });

  it("meta row carries min-w-0 max-w-full so timestamps wrap rather than push", () => {
    expect(RUNS_PAGE).toMatch(
      /flex min-w-0 max-w-full flex-wrap items-center gap-x-3 gap-y-1\.5 text-xs/,
    );
  });

  it("domain_id span uses overflow-wrap:anywhere for long crossref-style IDs", () => {
    expect(RUNS_PAGE).toContain("[overflow-wrap:anywhere]");
  });

  it("App.tsx does NOT reintroduce overflow-x-hidden on <main> (PR-383 revert held)", () => {
    // PR-380 added ``overflow-x-hidden`` and broke mobile; the real fix
    // lives in RunsPage.tsx, so ``<main>`` must stay unclipped to keep
    // pinch-zoom working on iOS Safari.
    expect(APP).not.toMatch(/<main[^>]*overflow-x-hidden/);
  });
});
