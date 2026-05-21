import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";

import {
  deleteRun,
  hardDeleteProject,
  hardDeleteRun,
  listRuns,
  restoreProject,
  restoreRun,
  Run,
} from "../lib/api";
import { useT } from "../lib/i18n";
import { formatRunState } from "../lib/runState";

const LANGUAGE_BADGE: Record<"en" | "zh" | "ja", string> = {
  en: "EN",
  zh: "中",
  ja: "日",
};

const TERMINAL_STATES = new Set([
  "EXPORTS_DONE",
  "FAILED_VENDOR",
  "FAILED_FIXABLE",
  "FAILED_POLICY",
  "CANCELLED",
]);

const SEARCH_DEBOUNCE_MS = 250;

export default function RunsPage() {
  const t = useT();
  const [searchParams, setSearchParams] = useSearchParams();
  const urlQ = searchParams.get("q") ?? "";
  const [inputQ, setInputQ] = useState(urlQ);
  const [showDeleted, setShowDeleted] = useState(false);
  const [pendingRunId, setPendingRunId] = useState<string | null>(null);
  const [pendingProjectId, setPendingProjectId] = useState<string | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // PR-389: bulk hard-delete selection. Keyed by run id; project-
  // deleted entries deduped by project id when submitting.
  const [selectedRunIds, setSelectedRunIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [isBulkHardDeleting, setIsBulkHardDeleting] = useState(false);

  // Debounce: while the user is typing, sync URL ?q=… after a pause so
  // shareable URLs reflect the latest committed search but we don't
  // refetch on every keystroke.
  useEffect(() => {
    if (inputQ === urlQ) return;
    const handle = window.setTimeout(() => {
      const next = new URLSearchParams(searchParams);
      if (inputQ.trim()) {
        next.set("q", inputQ.trim());
      } else {
        next.delete("q");
      }
      setSearchParams(next, { replace: true });
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [inputQ, urlQ, searchParams, setSearchParams]);

  const load = useCallback(async (q: string, includeDeleted: boolean) => {
    setIsLoading(true);
    setError(null);
    try {
      const fetched = await listRuns({ q, includeDeleted });
      setRuns(fetched);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Failed to load runs",
      );
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(urlQ, showDeleted);
  }, [load, urlQ, showDeleted]);

  const hasActiveSearch = useMemo(() => urlQ.trim().length > 0, [urlQ]);

  async function handleDelete(runId: string) {
    if (!window.confirm(t("runs.delete_run_confirm"))) return;
    setPendingRunId(runId);
    try {
      await deleteRun(runId);
      await load(urlQ, showDeleted);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("runs.delete_run_failed")}: ${caught.message}`
          : t("runs.delete_run_failed"),
      );
    } finally {
      setPendingRunId(null);
    }
  }

  async function handleRestoreRun(runId: string) {
    if (!window.confirm(t("runs.restore_run_confirm"))) return;
    setPendingRunId(runId);
    try {
      await restoreRun(runId);
      await load(urlQ, showDeleted);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("runs.restore_run_failed")}: ${caught.message}`
          : t("runs.restore_run_failed"),
      );
    } finally {
      setPendingRunId(null);
    }
  }

  async function handleRestoreProject(projectId: string) {
    setPendingProjectId(projectId);
    try {
      await restoreProject(projectId);
      await load(urlQ, showDeleted);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("runs.restore_failed")}: ${caught.message}`
          : t("runs.restore_failed"),
      );
    } finally {
      setPendingProjectId(null);
    }
  }

  // PR-389: list of deleted run cards visible right now. Used to bound
  // the "select all" action so it doesn't latch onto cards that scroll
  // out of the filter (search query change, etc.).
  const deletedCardsVisible = runs.filter(
    (r) => !!r.deleted_at || !!r.project_deleted_at,
  );

  function toggleSelectForHardDelete(runId: string): void {
    setSelectedRunIds((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  }

  function selectAllVisibleDeleted(): void {
    setSelectedRunIds(new Set(deletedCardsVisible.map((r) => r.id)));
  }

  function clearSelection(): void {
    setSelectedRunIds(new Set());
  }

  async function handleBulkHardDelete(): Promise<void> {
    const ids = Array.from(selectedRunIds);
    if (ids.length === 0) return;
    if (!window.confirm(t("runs.hard_delete_confirm").replace("{n}", String(ids.length)))) return;
    setIsBulkHardDeleting(true);
    setError(null);
    // Dedupe: if a project is itself soft-deleted AND selected via any of
    // its run cards, drop runs that belong to that project and hard-delete
    // the project instead (cascades to all its runs server-side).
    const projectsToHardDelete = new Set<string>();
    const runsToHardDelete: string[] = [];
    for (const id of ids) {
      const run = runs.find((r) => r.id === id);
      if (!run) continue;
      if (run.project_deleted_at) {
        projectsToHardDelete.add(run.project_id);
      } else if (run.deleted_at) {
        runsToHardDelete.push(id);
      }
    }
    try {
      for (const projectId of projectsToHardDelete) {
        await hardDeleteProject(projectId);
      }
      for (const runId of runsToHardDelete) {
        await hardDeleteRun(runId);
      }
      clearSelection();
      await load(urlQ, showDeleted);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("runs.hard_delete_failed")}: ${caught.message}`
          : t("runs.hard_delete_failed"),
      );
    } finally {
      setIsBulkHardDeleting(false);
    }
  }

  return (
    <section className="grid min-w-0 max-w-full grid-cols-1 gap-5">
      {/* Top "My essays" panel — v2 design language: rounded card,
          serif headline, eyebrow tag, cream surface over the page bg. */}
      <div className="rounded-[22px] border border-[#e6e5e0]/80 bg-[rgba(255,255,253,0.93)] px-6 py-7 [box-shadow:0_20px_42px_rgba(27,42,34,0.10)] sm:px-8 sm:py-8">
        <p className="text-[0.72rem] font-semibold uppercase tracking-[0.18em] text-[#737572]">
          {t("runs.section_label")}
        </p>
        <h1 className="mt-2 font-serif text-[2rem] font-black leading-[1.05] text-[#101417] sm:text-[2.5rem]">
          {t("runs.heading")}
        </h1>
        <p className="mt-3 text-[0.95rem] leading-[1.5] text-[#777572]">
          {runs.length === 0 && !isLoading && !hasActiveSearch
            ? t("runs.empty_hint")
            : t("runs.list_hint")}
        </p>

        <div className="mt-5 flex flex-wrap items-center gap-2">
          <label className="relative flex min-h-[44px] flex-1 items-center rounded-[14px] border border-[#dfdfdc] bg-[rgba(255,255,255,0.72)] [box-shadow:inset_0_1px_2px_rgba(20,25,23,0.02)]">
            <SearchGlyph />
            <input
              type="search"
              data-testid="runs-search-input"
              value={inputQ}
              onChange={(event) => setInputQ(event.currentTarget.value)}
              placeholder={t("runs.search_placeholder")}
              maxLength={200}
              className="h-full min-w-0 flex-1 border-0 bg-transparent pl-12 pr-4 text-[0.95rem] text-[#222] outline-none placeholder:text-[#737572]"
            />
          </label>
          {inputQ ? (
            <button
              type="button"
              data-testid="runs-search-clear"
              onClick={() => setInputQ("")}
              className="inline-flex min-h-[40px] items-center rounded-full border border-[#dfdfdc] bg-white px-4 text-sm font-semibold text-[#245d49] transition hover:bg-[#f0ece1]"
            >
              {t("runs.search_clear")}
            </button>
          ) : null}
        </div>

        <label className="mt-4 inline-flex cursor-pointer items-center gap-2.5 text-sm text-[#1d2423]">
          <input
            type="checkbox"
            data-testid="runs-show-deleted-checkbox"
            checked={showDeleted}
            onChange={(event) => setShowDeleted(event.currentTarget.checked)}
            className="h-[15px] w-[15px] cursor-pointer appearance-none rounded border-2 border-[#737679] bg-white checked:border-[#245d49] checked:[background:linear-gradient(45deg,transparent_54%,#fff_55%_64%,transparent_65%),linear-gradient(-45deg,transparent_46%,#fff_47%_58%,transparent_59%),#245d49]"
          />
          <span>{t("runs.show_deleted")}</span>
        </label>

        {/* PR-389: bulk hard-delete action bar. Only visible when
            "show deleted" is on AND at least one deleted card is
            present in the current filter. */}
        {showDeleted && deletedCardsVisible.length > 0 ? (
          <div
            data-testid="runs-bulk-hard-delete-bar"
            className="mt-3 flex flex-wrap items-center gap-3 rounded-[12px] border border-[#f3d6d3] bg-[#fdecec] px-3 py-2 text-xs text-[#922323]"
          >
            <button
              type="button"
              data-testid="runs-bulk-select-all"
              onClick={selectAllVisibleDeleted}
              className="inline-flex items-center rounded border border-[#922323] bg-white px-2 py-1 font-bold transition hover:bg-[#fff5f5] disabled:opacity-60"
              disabled={isBulkHardDeleting}
            >
              {t("runs.bulk_select_all")}
            </button>
            <button
              type="button"
              data-testid="runs-bulk-clear-selection"
              onClick={clearSelection}
              disabled={selectedRunIds.size === 0 || isBulkHardDeleting}
              className="inline-flex items-center rounded border border-[#dfdfdc] bg-white px-2 py-1 font-bold text-[#737572] transition hover:bg-[#f8f8f5] disabled:opacity-60"
            >
              {t("runs.bulk_clear_selection")}
            </button>
            <span className="font-bold">
              {t("runs.bulk_selected_count").replace(
                "{n}",
                String(selectedRunIds.size),
              )}
            </span>
            <button
              type="button"
              data-testid="runs-bulk-hard-delete-submit"
              onClick={() => void handleBulkHardDelete()}
              disabled={selectedRunIds.size === 0 || isBulkHardDeleting}
              className="inline-flex items-center gap-1 rounded bg-[#b3322d] px-3 py-1 font-bold text-white transition hover:bg-[#922323] disabled:opacity-60"
            >
              {isBulkHardDeleting
                ? t("runs.bulk_hard_delete_in_flight")
                : t("runs.bulk_hard_delete_submit")}
            </button>
          </div>
        ) : null}

        <Link
          to="/runs/new"
          data-testid="runs-new-link"
          className="mt-5 inline-flex min-h-[46px] w-full items-center justify-center gap-2.5 rounded bg-[linear-gradient(180deg,#2e7659_0%,#28674f_100%)] px-5 py-2.5 text-[0.98rem] font-bold text-white no-underline transition [box-shadow:inset_0_-2px_0_rgba(12,41,31,0.18)] hover:brightness-105"
        >
          <SproutGlyph />
          {t("runs.cta_new_project")}
        </Link>
      </div>

      {error ? (
        <div className="rounded-[14px] border border-[#f0c0c0] bg-[#fdecec] px-4 py-3 text-sm text-[#922323]">
          {error}
        </div>
      ) : null}

      {isLoading ? (
        <div className="rounded-[18px] border border-[#e6e5e0]/80 bg-[rgba(255,255,253,0.93)] px-5 py-4 text-sm text-[#777572]">
          {t("runs.loading")}
        </div>
      ) : runs.length > 0 ? (
        <ul className="grid min-w-0 max-w-full grid-cols-1 list-none gap-3 p-0">
          {runs.map((run) => {
            const isProjectDeleted = !!run.project_deleted_at;
            const isRunDeleted = !!run.deleted_at;
            const isDeleted = isProjectDeleted || isRunDeleted;
            const isPending =
              pendingRunId === run.id || pendingProjectId === run.project_id;
            return (
              <li
                key={run.id}
                className="min-w-0 max-w-full"
                data-testid="run-card"
                data-run-id={run.id}
                data-run-state={run.state}
                data-project-id={run.project_id}
                data-run-deleted={isRunDeleted ? "true" : "false"}
                data-project-deleted={isProjectDeleted ? "true" : "false"}
              >
                <div
                  className={`grid min-w-0 max-w-full grid-cols-1 gap-3 overflow-hidden rounded-[18px] border bg-[rgba(255,255,253,0.95)] p-5 [box-shadow:0_10px_24px_rgba(27,42,34,0.06)] transition sm:p-6 ${
                    isDeleted
                      ? "border-[#dadad6] opacity-70"
                      : "border-[#e6e5e0] hover:border-[#245d49]/60 hover:[box-shadow:0_14px_30px_rgba(27,42,34,0.12)]"
                  }`}
                >
                  <div className="flex min-w-0 max-w-full flex-wrap items-start justify-between gap-3">
                    {/* PR-389: bulk hard-delete checkbox. Only
                        rendered on deleted cards (require soft-delete
                        first per the backend eligibility gate). */}
                    {isDeleted ? (
                      <label className="flex shrink-0 cursor-pointer items-center pt-1">
                        <input
                          type="checkbox"
                          data-testid="run-bulk-select-checkbox"
                          checked={selectedRunIds.has(run.id)}
                          onChange={() => toggleSelectForHardDelete(run.id)}
                          disabled={isBulkHardDeleting}
                          className="h-[16px] w-[16px] cursor-pointer appearance-none rounded border-2 border-[#922323] bg-white checked:border-[#922323] checked:[background:linear-gradient(45deg,transparent_54%,#fff_55%_64%,transparent_65%),linear-gradient(-45deg,transparent_46%,#fff_47%_58%,transparent_59%),#922323]"
                        />
                      </label>
                    ) : null}
                    <Link
                      to={`/runs/${run.id}`}
                      data-testid="run-card-link"
                      className="min-w-0 max-w-full flex-1 truncate font-serif text-[1.1rem] font-bold text-[#101417] no-underline hover:underline sm:text-[1.2rem]"
                    >
                      {run.project_title || run.project_id}
                    </Link>
                    <div className="flex max-w-full shrink-0 flex-wrap items-center gap-2">
                      <span
                        aria-label={`paper language ${run.project_language}`}
                        className="inline-flex h-7 min-w-[28px] items-center justify-center rounded border border-[#e6e5e0] bg-[rgba(255,255,255,0.7)] px-1.5 text-xs font-bold text-[#245d49]"
                      >
                        {LANGUAGE_BADGE[run.project_language] ??
                          run.project_language}
                      </span>
                      {isProjectDeleted ? (
                        <button
                          type="button"
                          data-testid="run-restore-button"
                          disabled={isPending}
                          onClick={() => void handleRestoreProject(run.project_id)}
                          className="inline-flex min-h-7 items-center rounded border border-[#e6e5e0] bg-white px-3 text-xs font-bold text-[#245d49] transition hover:bg-[#f0ece1] disabled:opacity-60"
                        >
                          {t("runs.restore_button")}
                        </button>
                      ) : isRunDeleted ? (
                        <button
                          type="button"
                          data-testid="run-restore-button"
                          disabled={isPending}
                          onClick={() => void handleRestoreRun(run.id)}
                          className="inline-flex min-h-7 items-center rounded border border-[#e6e5e0] bg-white px-3 text-xs font-bold text-[#245d49] transition hover:bg-[#f0ece1] disabled:opacity-60"
                        >
                          {t("runs.restore_run_button")}
                        </button>
                      ) : (
                        <button
                          type="button"
                          data-testid="run-delete-button"
                          disabled={isPending}
                          onClick={() => void handleDelete(run.id)}
                          className="inline-flex min-h-7 items-center gap-1.5 rounded border border-[#f3d6d3] bg-white px-3 text-xs font-bold text-[#b3322d] transition hover:bg-[#fdecec] disabled:opacity-60"
                        >
                          <TrashGlyph />
                          {t("runs.delete_run_button")}
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="flex min-w-0 max-w-full flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
                    {isDeleted ? (
                      <span className="inline-flex rounded-full bg-[#e4e2de] px-2.5 py-0.5 font-bold text-[#737572]">
                        {t("runs.deleted_badge")}
                      </span>
                    ) : null}
                    <RunModeBadge mode={run.mode} />
                    <StateBadge state={run.state} t={t} />
                    {run.auto_advance ? (
                      <span
                        data-testid="runs-card-auto-pilot-badge"
                        className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 font-bold text-emerald-800"
                        title={t("auto_pilot.tooltip")}
                      >
                        <span aria-hidden>🤖</span>
                        {t("auto_pilot.badge")}
                      </span>
                    ) : null}
                    <span className="min-w-0 max-w-full text-[#777572] [overflow-wrap:anywhere]">
                      {t("runs.domain_label")}:{" "}
                      <span className="text-[#1d2423]">
                        {run.domain_id || "—"}
                      </span>
                    </span>
                    <span className="text-[#777572]">•</span>
                    <span className="text-[#777572]">
                      {formatRelative(run.updated_at, t)}
                    </span>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      ) : hasActiveSearch ? (
        <div className="rounded-[18px] border border-[#e6e5e0]/80 bg-[rgba(255,255,253,0.93)] px-5 py-4 text-sm text-[#777572]">
          {t("runs.search_no_results")}
        </div>
      ) : null}
    </section>
  );
}

function RunModeBadge({ mode }: { mode: Run["mode"] }) {
  const normalized = mode === "deep" ? "deep" : "express";
  const tone =
    normalized === "express"
      ? "bg-[#e7f0ff] text-[#24456f]"
      : "bg-[#efe7d6] text-[#6a4f1d]";
  return (
    <span
      data-testid={`run-mode-badge-${normalized}`}
      className={`inline-flex rounded-full px-2.5 py-0.5 font-bold ${tone}`}
    >
      {normalized === "express" ? "Express" : "Deep"}
    </span>
  );
}

function StateBadge({
  state,
  t,
}: {
  state: string;
  t: (key: string) => string;
}) {
  const isDone = state === "EXPORTS_DONE";
  const isFailed = TERMINAL_STATES.has(state) && !isDone;
  const tone = isDone
    ? "bg-[#dceede] text-[#1c4e3c]"
    : isFailed
      ? "bg-[#fdecec] text-[#922323]"
      : "bg-[#f0ece1] text-[#5a6a5e]";
  return (
    <span
      className={`inline-flex rounded-full px-2.5 py-0.5 font-bold ${tone}`}
    >
      {formatRunState(t, state)}
    </span>
  );
}

function formatRelative(iso: string, t: (key: string) => string): string {
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diffMs = now - then;
  if (Number.isNaN(diffMs)) return iso;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return t("runs.time_just_now");
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

function SearchGlyph() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="absolute left-4 top-1/2 h-[18px] w-[18px] -translate-y-1/2 text-[#737572]"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  );
}

function TrashGlyph() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className="h-[14px] w-[14px]"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 6h18" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <path d="M10 11v6M14 11v6" />
    </svg>
  );
}

function SproutGlyph() {
  return (
    <svg
      aria-hidden
      viewBox="0 0 32 32"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M16 28V14" />
      <path d="M16 14c0-4 3-8 8-8 0 4-3 8-8 8z" />
      <path d="M16 14c0-4-3-8-8-8 0 4 3 8 8 8z" />
    </svg>
  );
}
