import { describe, expect, it } from "vitest";

import type { DiscoverySource, DualTrackPayload, SynthesisClaim } from "./api";
import {
  RESEARCH_ROLES,
  badgeStyleFor,
  evidenceLedgerEmptyReason,
  isResearchRole,
  partitionDualTrack,
  roleOf,
} from "./researchRole";

const _baseSource = (
  overrides: Partial<DiscoverySource> = {},
): DiscoverySource => ({
  source_id: "openalex_W1",
  title: "Some study",
  authors: [],
  year: null,
  venue: null,
  doi: null,
  url: null,
  pdf_url: null,
  abstract: null,
  source_client: "openalex",
  access_status: "open",
  license: null,
  rank_score: 0,
  risk_flags: [],
  ...overrides,
});

const _claim = (id: string, sourceId: string): SynthesisClaim => ({
  source_id: sourceId,
  claim_id: id,
  text: "claim",
  claim_type: "finding",
  n_sources_supporting: null,
  page_anchor: null,
});

describe("RESEARCH_ROLES", () => {
  it("contains the four canonical tiers in order", () => {
    expect(RESEARCH_ROLES).toEqual([
      "primary_source",
      "secondary_argument",
      "theoretical_lens",
      "methodological_reference",
    ]);
  });
});

describe("isResearchRole", () => {
  it("accepts the four tiers", () => {
    for (const r of RESEARCH_ROLES) {
      expect(isResearchRole(r)).toBe(true);
    }
  });
  it("rejects everything else", () => {
    expect(isResearchRole("primary")).toBe(false);
    expect(isResearchRole("")).toBe(false);
    expect(isResearchRole(null)).toBe(false);
    expect(isResearchRole(undefined)).toBe(false);
    expect(isResearchRole(42)).toBe(false);
  });
});

describe("badgeStyleFor", () => {
  it("returns a distinct style per tier", () => {
    const styles = RESEARCH_ROLES.map(badgeStyleFor);
    const labelKeys = new Set(styles.map((s) => s.labelKey));
    expect(labelKeys.size).toBe(4);
    for (const s of styles) {
      expect(s.bg).toBeTruthy();
      expect(s.text).toBeTruthy();
      expect(s.border).toBeTruthy();
    }
  });

  it("falls back to secondary_argument for unknown / missing role", () => {
    const fallback = badgeStyleFor(undefined);
    const sa = badgeStyleFor("secondary_argument");
    expect(fallback.labelKey).toBe(sa.labelKey);
  });
});

describe("roleOf", () => {
  it("returns the source's role when set", () => {
    expect(roleOf(_baseSource({ research_role: "primary_source" }))).toBe(
      "primary_source",
    );
    expect(roleOf(_baseSource({ research_role: "theoretical_lens" }))).toBe(
      "theoretical_lens",
    );
  });

  it("falls back to secondary_argument when missing", () => {
    expect(roleOf(_baseSource())).toBe("secondary_argument");
  });
});

describe("partitionDualTrack", () => {
  it("returns all-empty partition for null payload", () => {
    const p = partitionDualTrack(null);
    expect(p.primary).toEqual([]);
    expect(p.secondary).toEqual([]);
    expect(p.lens).toEqual([]);
    expect(p.method).toEqual([]);
    expect(p.hasAny).toBe(false);
  });

  it("partitions claims by track", () => {
    const payload: DualTrackPayload = {
      schema_version: 1,
      primary_track: [_claim("c1", "src_a")],
      secondary_track: [_claim("c2", "src_b"), _claim("c3", "src_c")],
      theoretical_lens_track: [_claim("c4", "src_d")],
      methodological_track: [],
      tension_summary_ref: null,
      framework_lens_summary_ref: null,
    };
    const p = partitionDualTrack(payload);
    expect(p.primary).toHaveLength(1);
    expect(p.secondary).toHaveLength(2);
    expect(p.lens).toHaveLength(1);
    expect(p.method).toHaveLength(0);
    expect(p.hasAny).toBe(true);
  });

  it("hasAny=false when all tracks empty", () => {
    const payload: DualTrackPayload = {
      schema_version: 1,
      primary_track: [],
      secondary_track: [],
      theoretical_lens_track: [],
      methodological_track: [],
      tension_summary_ref: null,
      framework_lens_summary_ref: null,
    };
    expect(partitionDualTrack(payload).hasAny).toBe(false);
  });
});

describe("evidenceLedgerEmptyReason", () => {
  it("'ready' when entries present", () => {
    expect(evidenceLedgerEmptyReason(true, 5, true)).toBe("ready");
    expect(evidenceLedgerEmptyReason(false, 1, false)).toBe("ready");
  });

  it("'legacy' when synthesis ran but no artifact", () => {
    expect(evidenceLedgerEmptyReason(false, 0, true)).toBe("legacy");
  });

  it("'not_yet' when synthesis pending", () => {
    expect(evidenceLedgerEmptyReason(false, 0, false)).toBe("not_yet");
  });

  it("'no_primary' when artifact present but no primary entries", () => {
    expect(evidenceLedgerEmptyReason(true, 0, true)).toBe("no_primary");
    expect(evidenceLedgerEmptyReason(true, 0, false)).toBe("no_primary");
  });
});
