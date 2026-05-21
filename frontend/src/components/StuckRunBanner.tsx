import { useEffect, useMemo, useState } from "react";

import { recoverStuckPhase, type RunEvent } from "../lib/api";
import { useT } from "../lib/i18n";
import { RUNNING_STATE_TO_PHASE } from "../lib/runState";

// PR-I3: 15 min in seconds. Mirrors backend
// ``_ZOMBIE_PHASE_IDLE_SECONDS_DEFAULT`` so the UI threshold matches
// the recover-endpoint gate exactly — otherwise a banner that fires
// at minute 5 would just collect 409s for the next 10 min until the
// backend gate also agreed. If you change this, change
// ``backend/src/autoessay/main.py::_ZOMBIE_PHASE_IDLE_SECONDS_DEFAULT``
// in the same PR.
export const STUCK_RUN_IDLE_THRESHOLD_SECONDS = 15 * 60;

// PR-I3: how often to re-check the idle window. We don't subscribe
// to a timer per second — that wastes battery on a banner that only
// flips state once. 30s is enough resolution to render the banner
// within ~30s of the threshold being crossed even if no SSE event
// arrives (the SIGKILL case where the event stream goes silent is
// the whole point of this banner).
export const STUCK_RUN_RECHECK_INTERVAL_MS = 30_000;

export interface StuckRunBannerProps {
  runId: string;
  currentState: string;
  recentEvents: RunEvent[];
  activePhaseLockClaimedAt: string | null;
  runUpdatedAt: string;
  onRecovered: () => void;
}

export function StuckRunBanner({
  runId,
  currentState,
  recentEvents,
  activePhaseLockClaimedAt,
  runUpdatedAt,
  onRecovered,
}: StuckRunBannerProps) {
  const t = useT();
  const [now, setNow] = useState<number>(() => Date.now());
  const [isRecovering, setIsRecovering] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const id = window.setInterval(
      () => setNow(Date.now()),
      STUCK_RUN_RECHECK_INTERVAL_MS,
    );
    return () => window.clearInterval(id);
  }, []);

  const phase = RUNNING_STATE_TO_PHASE[currentState] ?? null;

  // Codex amendment#3: don't lean on a single global lastEvent. Walk
  // recent events newest-first and pick the first one whose payload
  // matches the current phase. Fall back to active_phase_lock claim
  // time, then run.updated_at — the same priority chain the backend
  // uses to compute "how long has this phase been silent".
  const lastPhaseEventIso = useMemo<string | null>(() => {
    if (phase) {
      for (const event of recentEvents) {
        const eventPhase = (event.payload as { phase?: unknown } | null)
          ?.phase;
        if (typeof eventPhase === "string" && eventPhase === phase) {
          return event.created_at;
        }
      }
    }
    return activePhaseLockClaimedAt ?? runUpdatedAt;
  }, [phase, recentEvents, activePhaseLockClaimedAt, runUpdatedAt]);

  if (phase === null || lastPhaseEventIso === null) {
    return null;
  }

  const lastEventMs = Date.parse(lastPhaseEventIso);
  if (Number.isNaN(lastEventMs)) {
    return null;
  }

  const idleSeconds = Math.max(0, Math.floor((now - lastEventMs) / 1000));
  if (idleSeconds < STUCK_RUN_IDLE_THRESHOLD_SECONDS) {
    return null;
  }

  const idleMinutes = Math.floor(idleSeconds / 60);

  async function handleRecover() {
    if (isRecovering || phase === null) return;
    setIsRecovering(true);
    setError(null);
    try {
      await recoverStuckPhase(runId, phase);
      onRecovered();
    } catch (caught) {
      // Backend returns 409 with {code: "recovery_gate_not_triggered"}
      // when the gate refuses (worker still alive, phase finished).
      // Surface a "refresh the page" hint instead of pretending the
      // recovery happened.
      const message =
        caught instanceof Error ? caught.message : String(caught);
      if (message.includes("recovery_gate_not_triggered")) {
        setError(t("workspace.stuck_banner.gate_refused"));
      } else {
        setError(
          t("workspace.stuck_banner.error_generic", { message }),
        );
      }
    } finally {
      setIsRecovering(false);
    }
  }

  return (
    <div
      className="mb-6 grid gap-3 rounded-lg border-2 border-amber-500 bg-amber-50 p-4 sm:p-5"
      data-testid="stuck-run-banner"
      data-stuck-phase={phase}
    >
      <p className="text-sm font-bold text-amber-900">
        {t("workspace.stuck_banner.title")}
      </p>
      <p className="text-sm leading-7 text-amber-900">
        {t("workspace.stuck_banner.body", {
          phase,
          minutes: String(idleMinutes),
        })}
      </p>
      {error ? (
        <p
          className="text-sm leading-7 text-red-700"
          data-testid="stuck-run-banner-error"
        >
          {error}
        </p>
      ) : null}
      <div>
        <button
          type="button"
          onClick={handleRecover}
          disabled={isRecovering}
          data-testid="stuck-run-banner-recover-button"
          className="inline-flex min-h-11 items-center justify-center rounded bg-amber-700 px-4 py-2 text-sm font-bold text-white transition hover:bg-amber-800 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {isRecovering
            ? t("workspace.stuck_banner.recovering")
            : t("workspace.stuck_banner.recover_button")}
        </button>
      </div>
    </div>
  );
}
