import type { RunEvent } from "./api";

type Translator = (key: string) => string;

const PHASE_LABEL_KEYS: Record<string, string> = {
  scout: "phase.scout",
  curator: "phase.curator",
  synthesizer: "phase.synthesizer",
  tension_extraction: "phase.tension_extraction",
  framework_lens: "phase.framework_lens",
  ideator: "phase.ideator",
  drafter: "phase.drafter",
  stylist: "phase.stylist",
  final_rewrite: "phase.final_rewrite",
  critic: "phase.critic",
  integrity: "phase.integrity",
  exports: "phase.exports",
  proposal: "phase.proposal",
};

function phaseLabel(t: Translator, phase: unknown): string {
  if (typeof phase !== "string") return "";
  const key = PHASE_LABEL_KEYS[phase];
  if (!key) return phase;
  const translated = t(key);
  return translated === key ? phase : translated;
}

function asString(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  return String(value);
}

function fillTemplate(template: string, vars: Record<string, string>): string {
  return template.replace(/\{(\w+)\}/g, (_match, name: string) =>
    name in vars ? vars[name] : `{${name}}`,
  );
}

// Render one event as a single-line, user-readable status entry.
// Falls back to a generic "<event_type>" when an event is unknown so
// new event types still surface (raw JSON tab shows the full payload).
export function describeEvent(t: Translator, event: RunEvent): string {
  const payload = event.payload ?? {};
  const phase = phaseLabel(t, payload.phase);
  const reason = asString(payload.reason);
  const fromState = asString(payload.from);
  const toState = asString(payload.to);

  switch (event.event_type) {
    case "run_created":
      return t("console.timeline.run_created");
    case "run_cancelled":
      return t("console.timeline.run_cancelled");
    case "state_transition": {
      if (toState && fromState) {
        return fillTemplate(t("console.timeline.state_transition"), {
          from: fromState,
          to: toState,
        });
      }
      if (toState) {
        return fillTemplate(t("console.timeline.state_set"), { to: toState });
      }
      return t("console.timeline.state_changed");
    }
    case "phase_started":
      return fillTemplate(t("console.timeline.phase_started"), { phase });
    case "phase_done":
      return fillTemplate(t("console.timeline.phase_done"), { phase });
    case "phase_failed":
      if (reason) {
        return fillTemplate(t("console.timeline.phase_failed_with_reason"), {
          phase,
          reason,
        });
      }
      return fillTemplate(t("console.timeline.phase_failed"), { phase });
    case "phase_waiting":
      return fillTemplate(t("console.timeline.phase_waiting"), { phase });
    case "source_progress": {
      const sourceId = asString(payload.source_id);
      const status = asString(payload.status);
      return fillTemplate(t("console.timeline.source_progress"), {
        source: sourceId,
        status,
      });
    }
    case "section_progress": {
      const sectionId = asString(payload.section_id);
      const status = asString(payload.status);
      return fillTemplate(t("console.timeline.section_progress"), {
        section: sectionId,
        status,
      });
    }
    case "proposal_saved":
      return t("console.timeline.proposal_saved");
    case "source_uploaded":
      return t("console.timeline.source_uploaded");
    case "checkpoint_recorded":
      return t("console.timeline.checkpoint_recorded");
    case "force_approve":
      if (reason) {
        return fillTemplate(t("console.timeline.force_approve_with_reason"), {
          reason,
        });
      }
      return t("console.timeline.force_approve");
    case "phase_lock_force_cleared":
      return fillTemplate(t("console.timeline.phase_lock_force_cleared"), {
        phase,
      });
    case "scan_kinds_skipped": {
      const kinds = Array.isArray(payload.scan_kinds)
        ? (payload.scan_kinds as unknown[]).map(asString).join(", ")
        : asString(payload.scan_kinds);
      return fillTemplate(t("console.timeline.scan_kinds_skipped"), { kinds });
    }
    default:
      return event.event_type;
  }
}

// Format an event timestamp as a localized short time. The events list
// already shows newest-last so a wall-clock HH:MM:SS is enough — users
// can correlate with the precise ISO timestamp in the system-output tab.
export function formatEventTime(iso: string): string {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString();
  } catch {
    return iso;
  }
}
