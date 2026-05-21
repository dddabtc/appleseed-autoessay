// PR-C1.b: pure helpers for the research_role badge + dual-track
// partition. Vitest covers these directly; no React/jsdom needed.
//
// The four-tier taxonomy mirrors the backend (see
// `backend/src/autoessay/agents/research_role_classifier.py`):
//
//   primary_source           — evidentiary item
//   secondary_argument       — published scholarship arguing a position
//   theoretical_lens         — framework-level conceptual lens
//   methodological_reference — work cited only for a method

import type {
  DiscoverySource,
  DualTrackPayload,
  ResearchRole,
  SynthesisClaim,
} from "./api";

export const RESEARCH_ROLES: ResearchRole[] = [
  "primary_source",
  "secondary_argument",
  "theoretical_lens",
  "methodological_reference",
];

export function isResearchRole(value: unknown): value is ResearchRole {
  return (
    typeof value === "string" && (RESEARCH_ROLES as string[]).includes(value)
  );
}

export interface BadgeStyle {
  /** Tailwind background class. */
  bg: string;
  /** Tailwind text-color class. */
  text: string;
  /** Tailwind border class. */
  border: string;
  /** i18n key for the localized label rendered in the badge. */
  labelKey: string;
}

const _STYLES: Record<ResearchRole, BadgeStyle> = {
  primary_source: {
    bg: "bg-emerald-50",
    text: "text-emerald-900",
    border: "border-emerald-300",
    labelKey: "research_role.primary_source.label",
  },
  secondary_argument: {
    bg: "bg-slate-50",
    text: "text-slate-700",
    border: "border-slate-300",
    labelKey: "research_role.secondary_argument.label",
  },
  theoretical_lens: {
    bg: "bg-purple-50",
    text: "text-purple-900",
    border: "border-purple-300",
    labelKey: "research_role.theoretical_lens.label",
  },
  methodological_reference: {
    bg: "bg-amber-50",
    text: "text-amber-900",
    border: "border-amber-300",
    labelKey: "research_role.methodological_reference.label",
  },
};

const _DEFAULT_STYLE = _STYLES.secondary_argument;

export function badgeStyleFor(
  role: ResearchRole | undefined | null,
): BadgeStyle {
  if (!role || !isResearchRole(role)) return _DEFAULT_STYLE;
  return _STYLES[role];
}

/**
 * Resolve a source's role for rendering. Backfilled / missing fields
 * fall to ``secondary_argument`` (matches alembic 019 default).
 */
export function roleOf(source: DiscoverySource): ResearchRole {
  const r = source.research_role;
  return r && isResearchRole(r) ? r : "secondary_argument";
}

export interface DualTrackPartition {
  primary: SynthesisClaim[];
  secondary: SynthesisClaim[];
  lens: SynthesisClaim[];
  method: SynthesisClaim[];
  hasAny: boolean;
}

/**
 * Partition the dual-track payload into its 4 tracks. When the
 * payload is null (legacy run before C1.a), every track is empty.
 *
 * Returned arrays are clones — caller may sort / filter freely.
 */
export function partitionDualTrack(
  payload: DualTrackPayload | null | undefined,
): DualTrackPartition {
  if (!payload) {
    return {
      primary: [],
      secondary: [],
      lens: [],
      method: [],
      hasAny: false,
    };
  }
  const primary = [...(payload.primary_track ?? [])];
  const secondary = [...(payload.secondary_track ?? [])];
  const lens = [...(payload.theoretical_lens_track ?? [])];
  const method = [...(payload.methodological_track ?? [])];
  return {
    primary,
    secondary,
    lens,
    method,
    hasAny: primary.length + secondary.length + lens.length + method.length > 0,
  };
}

/**
 * Reason the evidence-ledger sub-tab should render an empty state.
 * Three distinguishable cases per codex C1.b round-1 amendment:
 *
 *   ``legacy``    — synthesizer ran before C1.a, no artifact ever
 *                    written. Copy: "运行早于 C1.a".
 *   ``no_primary`` — current run, synthesis ran, but no primary-track
 *                    evidence was extracted. Copy: "无一手证据".
 *   ``not_yet``   — synthesis hasn't run yet on this run. Copy:
 *                    normal pending state.
 *   ``ready``     — has entries; render the table.
 */
export function evidenceLedgerEmptyReason(
  artifactPresent: boolean,
  entryCount: number,
  synthesisRunCompleted: boolean,
): "legacy" | "no_primary" | "not_yet" | "ready" {
  if (entryCount > 0) return "ready";
  if (!artifactPresent) {
    return synthesisRunCompleted ? "legacy" : "not_yet";
  }
  return "no_primary";
}
