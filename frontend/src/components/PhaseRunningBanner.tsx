import { useMemo } from "react";

import type { RunEvent } from "../lib/api";
import { describeEvent } from "../lib/eventDescription";
import { useT } from "../lib/i18n";

// All phases that can hit a *_RUNNING / PROPOSAL_DRAFTING state. Keeping
// this as a string-union forces a type-error at the call site if a new
// phase ships without an i18n entry, and matches the keys under
// ``workspace.running_banner.*``.
export type RunningPhaseId =
  | "proposal"
  | "scout"
  | "curator"
  | "synthesizer"
  | "tension_extraction"
  | "framework_lens"
  | "ideator"
  | "drafter"
  | "stylist"
  | "final_rewrite"
  | "critic"
  | "integrity"
  | "exports";

export interface PhaseRunningBannerProps {
  // Logical phase for i18n lookup + testid suffix. Distinct from the
  // raw run state so e.g. PROPOSAL_DRAFTING maps to ``proposal``.
  phase: RunningPhaseId;
  // Optional progress events for this phase (``section_progress`` /
  // ``source_progress``). When provided, the banner derives a
  // "completed N / total" line and a "currently working on item N+1"
  // hint without requiring a backend ``section_started`` event.
  progress?: RunEvent[];
  // Latest run event (any type) for this phase. When provided, the
  // banner appends a one-line ``describeEvent`` text so users see
  // human-readable activity rather than only a pulse dot.
  hintEvent?: RunEvent | null;
  // True once a ``phase_done`` event for this phase has landed.
  // drafter / stylist / critic / integrity / scout / curator /
  // synthesizer don't auto-transition the run state on completion —
  // they end at their *_RUNNING state waiting for the user to start
  // the next phase. Without this gate the banner keeps pulsing
  // "正在撰写草稿…" after the agent has actually finished, which is
  // the regression we're fixing here.
  completed?: boolean;
}

interface ProgressSummary {
  completedItems: number;
  totalItems: number;
  // Latest item title surfaced in the "已完成" line. Falls back to
  // section_id / source_id when no section_title is available.
  latestItemTitle: string;
}

function summarizeProgress(events: RunEvent[]): ProgressSummary | null {
  if (events.length === 0) return null;
  let maxCompleted = 0;
  let total = 0;
  let latestTitle = "";
  for (const event of events) {
    const payload = event.payload ?? {};
    const completed = Number(payload.completed ?? 0);
    const itemTotal = Number(payload.total ?? 0);
    if (Number.isFinite(completed) && completed > maxCompleted) {
      maxCompleted = completed;
      latestTitle =
        typeof payload.section_title === "string"
          ? payload.section_title
          : typeof payload.section_id === "string"
            ? payload.section_id
            : typeof payload.source_id === "string"
              ? payload.source_id
              : "";
    }
    if (Number.isFinite(itemTotal) && itemTotal > total) total = itemTotal;
  }
  if (total === 0 && maxCompleted === 0) return null;
  return {
    completedItems: maxCompleted,
    totalItems: total,
    latestItemTitle: latestTitle,
  };
}

export function PhaseRunningBanner({
  phase,
  progress = [],
  hintEvent = null,
  completed = false,
}: PhaseRunningBannerProps) {
  const t = useT();
  const summary = useMemo(() => summarizeProgress(progress), [progress]);
  if (completed) return null;

  const title = t(`workspace.running_banner.${phase}.title`);
  const noProgressHint = t("workspace.running_banner.starting");
  const finalizingHint = t("workspace.running_banner.finalizing");
  const inProgressTemplate = t("workspace.running_banner.in_progress_step");
  const completedTemplate = t("workspace.running_banner.completed_count");

  let stepLine: string | null = null;
  let countLine: string | null = null;
  if (summary) {
    if (summary.totalItems > 0) {
      countLine = completedTemplate
        .replace("{completed}", String(summary.completedItems))
        .replace("{total}", String(summary.totalItems));
    }
    if (
      summary.totalItems > 0 &&
      summary.completedItems >= summary.totalItems
    ) {
      stepLine = finalizingHint;
    } else {
      // ``completed + 1`` since the next item is the one being worked
      // on right now (``section_progress`` events fire on completion,
      // not on start). When ``total`` is unknown we still surface the
      // raw "currently on N" cue.
      const nextStep = summary.completedItems + 1;
      stepLine = inProgressTemplate
        .replace("{step}", String(nextStep))
        .replace(
          "{total}",
          summary.totalItems > 0 ? String(summary.totalItems) : "?",
        );
    }
  } else {
    stepLine = noProgressHint;
  }

  const hintText = hintEvent ? describeEvent(t, hintEvent) : "";

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid={`phase-running-banner-${phase}`}
      className="mb-4 flex items-start gap-3 rounded-[14px] border border-[#1d6f5a]/40 bg-[#e9f4ee] p-4 sm:p-5"
    >
      <span
        aria-hidden="true"
        className="mt-1 inline-block h-3 w-3 flex-none rounded-full bg-[#1d6f5a] motion-safe:animate-pulse"
      />
      <div className="flex min-w-0 flex-col gap-1">
        <p className="text-sm font-bold text-[#0f3a2c]">{title}</p>
        {stepLine ? (
          <p
            data-testid={`phase-running-banner-${phase}-step`}
            className="text-sm leading-6 text-[#1c4e3c]"
          >
            {stepLine}
          </p>
        ) : null}
        {countLine ? (
          <p className="text-xs text-[#1c4e3c]/80">{countLine}</p>
        ) : null}
        {hintText ? (
          <p className="text-xs text-[#1c4e3c]/70">{hintText}</p>
        ) : null}
      </div>
    </div>
  );
}
