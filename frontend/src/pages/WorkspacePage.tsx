import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  type ReactElement,
} from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useRunSSE } from "../hooks/useRunSSE";
import { describeEvent, formatEventTime } from "../lib/eventDescription";
import { useT } from "../lib/i18n";
import {
  type PhaseCardState,
  type PrimaryActionKey,
  deriveCardState,
  derivePrimaryActions,
  describeDeleteBlock,
  describeVersionSource,
  isActivateDisabled,
  isDeleteDisabled,
} from "../lib/phaseHistoryState";
import {
  RUNNING_STATE_TO_PHASE,
  formatRunState,
  isRunningState,
} from "../lib/runState";
import { smartRetry } from "../lib/retryStrategy";
import {
  buildSourceUploadFormData,
  suggestedPdfFilename,
  type SourceUploadTarget,
} from "../lib/sourceUploadForm";
import { PhaseRunningBanner } from "../components/PhaseRunningBanner";
import { StuckRunBanner } from "../components/StuckRunBanner";
import {
  ApiError,
  acceptProposal,
  acceptFinalDraft,
  acceptIntegrity,
  approveExternalScan,
  createRun,
  discussNovelty,
  editResearchKernel,
  getDiscovery,
  getCritic,
  getDraft,
  getDrafts,
  getExports,
  getIntegrity,
  getNovelty,
  getNoveltyDiscussion,
  getProposal,
  getRun,
  getSources,
  getStyle,
  getSynthesis,
  getFrameworkLens,
  requestIntegrityRevision,
  selectNoveltyAngle,
  skipExternalScan,
  saveProposal,
  saveSourceReviewCheckpoint,
  startProposal,
  startCurator,
  startCritic,
  startDrafter,
  startExports,
  startIdeator,
  startFrameworkLens,
  patchProject,
  startIntegrity,
  startScout,
  startStylist,
  startSynthesizer,
  rerunPhase,
  transitionRun,
  forceApproveRun,
  uploadSourcePdf,
  activatePhaseVersion,
  getPhasePrompt,
  upsertPhasePrompt,
  listBranches,
  createBranch,
  switchActiveBranch,
  listEditableArtifacts,
  editPhaseArtifacts,
  getProjectCorpus,
  setProjectCorpusSelection,
  uploadProjectCorpusDocument,
  getPhaseHistory,
  activateLineageMatch,
  cancelPhasePromptDrafts,
  deletePhaseVersion,
  updateResearchRole,
  getEvidenceLedger,
  getExpressTransparency,
  appendEvidenceLedgerOverride,
  updateRunSettings,
} from "../lib/api";
import type {
  ActivePhaseLock,
  AngleOutline,
  Discovery,
  DiscoverySource,
  CriticBundle,
  ForceApproveHint,
  DraftBundle,
  DraftClaim,
  DraftListBundle,
  ExportsBundle,
  IntegrityBundle,
  ManualUploadRequest,
  MaterialDiagnostic,
  NoveltyBundle,
  NoveltyDiscussionMessage,
  BranchEntry,
  BranchListResponse,
  PhaseHistoryEntry,
  PhaseHistoryResponse,
  PhaseHistoryVersionEntry,
  PhasePromptResponse,
  PhaseEditableEntry,
  ProjectCorpusResponse,
  ProjectLanguage,
  ProposalBundle,
  ProposalContent,
  Run,
  RunEvent,
  SourcesBundle,
  SourceReviewCheckpointPayload,
  StyleBundle,
  SynthesisBundle,
  SynthesisClaim,
  FrameworkLensBundle,
  EvidenceLedgerEntry,
  EvidenceLedgerResponse,
  ExpressTransparency,
  GenerationMode,
  ResearchRole,
  ResearchKernel,
} from "../lib/api";
import {
  RESEARCH_ROLES,
  badgeStyleFor,
  evidenceLedgerEmptyReason,
  partitionDualTrack,
  roleOf,
} from "../lib/researchRole";

// Phases that have at least one editable prompt surface in the
// backend prompt registry. Adding a phase here lights up the
// "Edit prompt" button in the stale banner. After Stage 3.A.4 the
// backend's GET endpoint runs a discovery fallback when the caller
// omits prompt_key, so curator (which only has the ``ranking`` key
// — no ``main``) is included.
const PROMPT_EDITABLE_PHASES = new Set<string>([
  "synthesizer",
  "ideator",
  "critic",
  "drafter",
  "stylist",
  "curator",
]);

const SOURCE_RERUN_PHASES = new Set<string>(["scout", "curator"]);

const PHASE_ORDER = [
  "scout",
  "curator",
  "synthesizer",
  "tension_extraction",
  "framework_lens",
  "ideator",
  "drafter",
  "stylist",
  "critic",
  "integrity",
  "exports",
] as const;

// PR-A4.4 codex round 2 amendment 1: phase → URL path for
// "run now" (the start_<phase> endpoint). Most phases match
// /api/runs/{id}/<phase>, but exports is singular: backend
// route is /api/runs/{id}/export. Keep this co-located with
// the other phase tables so future renames stay in sync.
const PHASE_RUN_NOW_PATHS: Record<string, string> = {
  exports: "export",
};

const SOURCE_PAGE_SIZE = 20;
const CLAIM_PAGE_SIZE = 25;

type SourceRerunImpact = {
  phase: string;
  skimCandidates: number;
  shortlist: number;
  manualRequests: number;
  userUploads: number;
  downstreamGenerated: number;
};

// Stage 3.E follow-up (codex AGREE): all terminal failure states the
// FailureResolutionBanner can render. FAILED_FIXABLE → rerun the
// failed phase. FAILED_VENDOR → retry external scan or skip integrity.
// FAILED_NEEDS_USER / FAILED_POLICY / CANCELLED → message-only.
type FailureState =
  | "FAILED_FIXABLE"
  | "FAILED_VENDOR"
  | "FAILED_NEEDS_USER"
  | "FAILED_POLICY"
  | "CANCELLED";

const FAILURE_STATES = new Set<string>([
  "FAILED_FIXABLE",
  "FAILED_VENDOR",
  "FAILED_NEEDS_USER",
  "FAILED_POLICY",
  "CANCELLED",
]);

/**
 * `FailureResolutionBanner` "重试该步骤" handler. Dispatches to the
 * matching `start_<phase>` endpoint instead of `rerun_phase` so that
 * a first-attempt failure (no completed phase output yet) can recover
 * — `rerun_phase` rejects those cases. The backend
 * `_recover_failed_fixable_for_phase` helper rewinds run.state from
 * FAILED_FIXABLE back to the phase's input state and clears the
 * stale phase-lock, so each `start_<phase>` accepts the request.
 */
/**
 * Maps the workspace subview tab id (the URL-ish frontend label —
 * "synthesis", "novelty", "draft", etc.) to the backend phase
 * name used by `phase_user_edit` and the `phases/{phase}/edit`
 * endpoint. Returns ``null`` for tabs that are not user-editable
 * through the generic mechanism: ``proposal`` has its own dedicated
 * save endpoint (PUT /api/runs/{id}/proposal); ``export`` is the
 * terminal output and never user-edited; ``console`` aggregates
 * scout streaming output and is read-only — scout edits go through
 * the history modal's per-version flow, not the generic
 * edit-content button.
 */
function subviewToEditablePhase(subview: string): string | null {
  switch (subview) {
    case "sources":
      return "curator";
    case "synthesis":
      return "synthesizer";
    case "novelty":
      return "ideator";
    case "draft":
      return "drafter";
    case "style":
      return "stylist";
    case "review":
      return "critic";
    case "integrity":
      return "integrity";
    default:
      return null;
  }
}

async function retryFailedPhase(
  runId: string,
  phase: string,
): Promise<unknown> {
  switch (phase) {
    case "proposal":
      return startProposal(runId);
    case "scout":
      return startScout(runId);
    case "curator":
      return startCurator(runId);
    case "synthesizer":
      return startSynthesizer(runId);
    // PR-I3 amendment#2: ``framework_lens`` was missing from this
    // switch, so a stuck-lens recovery (which lands in
    // FAILED_FIXABLE with payload.phase=framework_lens) would fall
    // through to ``rerunPhase`` and 409 (no completed lens output
    // exists yet on a first-attempt failure). ``tension_extraction``
    // ships its retry mapping with PR-C3.b.2.
    case "framework_lens":
      return startFrameworkLens(runId);
    case "ideator":
      return startIdeator(runId);
    case "drafter":
      return startDrafter(runId);
    case "stylist":
      return startStylist(runId);
    case "critic":
      return startCritic(runId);
    case "integrity":
      return startIntegrity(runId);
    case "exports":
      return startExports(runId);
    default:
      // Fallback: phases not in the canonical list still use
      // rerun_phase. Today every failable phase is in the switch
      // above, so this branch is unreachable at runtime.
      return rerunPhase(runId, phase);
  }
}

const PROPOSAL_VISIBLE_STATES = new Set([
  "DOMAIN_LOADED",
  "PROPOSAL_DRAFTING",
  "USER_PROPOSAL_REVIEW",
  "SCOUT_RUNNING",
  "USER_SEARCH_REVIEW",
  "CURATOR_RUNNING",
  "USER_DEEP_DIVE_REVIEW",
  "SYNTHESIZER_RUNNING",
  "USER_FIELD_REVIEW",
  // PR-C3.a: tension_extraction is an optional sub-phase between
  // synthesizer and lens. The states are gated by
  // ``Settings.tension_taxonomy_enabled`` (default OFF in prod).
  "TENSION_EXTRACTION_RUNNING",
  "USER_TENSION_REVIEW",
  // PR-C2.a: optional lens phase. UI exposure (dedicated tab,
  // RUNNING indicator) lands in C2.b — for now treat both states
  // as workspace-visible so the existing tab strip stays alive.
  "FRAMEWORK_LENS_RUNNING",
  "USER_LENS_REVIEW",
  "IDEATOR_RUNNING",
  "USER_NOVELTY_REVIEW",
  "DRAFTER_RUNNING",
  "STYLIST_RUNNING",
  "USER_REVISION_REVIEW",
  // Slice E final_rewrite phase, opt-in via
  // ``AUTOESSAY_FINAL_REWRITE_ENABLED`` (default ON). When ON the
  // run sits in REWRITE_RUNNING between stylist and critic.
  "REWRITE_RUNNING",
  "CRITIC_RUNNING",
  "USER_EXTERNAL_SCAN_APPROVAL",
  "INTEGRITY_RUNNING",
  "USER_INTEGRITY_REVIEW",
  "USER_FINAL_ACCEPTANCE",
  "EXPORTS_RUNNING",
  "EXPORTS_DONE",
]);

type WorkspaceSubview =
  | "console"
  | "corpus"
  | "proposal"
  | "sources"
  | "synthesis"
  | "lens"
  | "novelty"
  | "draft"
  | "style"
  | "review"
  | "integrity"
  | "export";

const WORKSPACE_SUBVIEW_VALUES: ReadonlySet<WorkspaceSubview> = new Set([
  "console",
  "corpus",
  "proposal",
  "sources",
  "synthesis",
  "lens",
  "novelty",
  "draft",
  "style",
  "review",
  "integrity",
  "export",
]);

// 2026-05-12 PR-365: persist the user's last-clicked workspace tab in
// localStorage (per run id), so a page refresh restores the tab they
// were on instead of bouncing them back to whatever ``resolveLandingSubview``
// picks for the current state. If no saved value, fall back to the
// state-driven landing logic.
function workspaceSubviewStorageKey(runId: string): string {
  return `autoessay:workspace:${runId}:activeSubview`;
}

function readPersistedSubview(runId: string | undefined): WorkspaceSubview | null {
  if (!runId || typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(workspaceSubviewStorageKey(runId));
    if (raw && WORKSPACE_SUBVIEW_VALUES.has(raw as WorkspaceSubview)) {
      return raw as WorkspaceSubview;
    }
  } catch {
    // localStorage can throw in private-mode / quota-exceeded; ignore.
  }
  return null;
}

// Stage 3.E follow-up: when a run first loads, default the active
// subview to whichever tab corresponds to the current run state, so
// the user lands on something useful instead of the empty Console
// tab. (After the user clicks a tab, this is not re-applied — see
// ``hasInitializedSubview``.)
function defaultSubviewForState(
  state: string | undefined | null,
): WorkspaceSubview {
  if (!state) return "console";
  switch (state) {
    case "TOPIC_ENTERED":
    case "DOMAIN_LOADED":
    case "PROPOSAL_DRAFTING":
    case "USER_PROPOSAL_REVIEW":
      return "proposal";
    case "SCOUT_RUNNING":
    case "USER_SEARCH_REVIEW":
    case "CURATOR_RUNNING":
    case "USER_DEEP_DIVE_REVIEW":
      return "sources";
    case "SYNTHESIZER_RUNNING":
    case "USER_FIELD_REVIEW":
    case "TENSION_EXTRACTION_RUNNING":
    case "USER_TENSION_REVIEW":
      return "synthesis";
    // PR-C2.b audit fix (round 1 #5): lens states route to a
    // dedicated lens subview, not novelty. The novelty subview's
    // visibility gate omits lens states, so routing there
    // produced an empty page.
    case "FRAMEWORK_LENS_RUNNING":
    case "USER_LENS_REVIEW":
      return "lens";
    case "IDEATOR_RUNNING":
    case "USER_NOVELTY_REVIEW":
      return "novelty";
    case "DRAFTER_RUNNING":
      return "draft";
    case "STYLIST_RUNNING":
    case "USER_REVISION_REVIEW":
    case "REWRITE_RUNNING":
      return "style";
    case "CRITIC_RUNNING":
    case "USER_EXTERNAL_SCAN_APPROVAL":
      return "review";
    case "INTEGRITY_RUNNING":
    case "USER_INTEGRITY_REVIEW":
      return "integrity";
    case "USER_FINAL_ACCEPTANCE":
    case "EXPORTS_RUNNING":
    case "EXPORTS_DONE":
      return "export";
    default:
      return "console";
  }
}

// Stage 3.E follow-up: map a phase name to the subview that shows
// its artifacts. Used by FailureResolutionBanner so clicking the
// error message takes the user to the offending phase's tab.
//
// P0 fix #3 (codex state-machine audit §1.1 C): added
// ``framework_lens`` and ``tension_extraction`` cases. Without
// these the FAILED_FIXABLE → tab routing path treated lens/tension
// failures as ``null`` (no auto-route), so users opening the URL
// of a lens-failed run landed on the default sources tab instead
// of the lens tab where the failure guidance lives.
function phaseToSubview(phase: string): WorkspaceSubview | null {
  switch (phase) {
    case "proposal":
      return "proposal";
    case "scout":
    case "curator":
      return "sources";
    case "synthesizer":
    case "tension_extraction":
      return "synthesis";
    case "framework_lens":
      return "lens";
    case "ideator":
      return "novelty";
    case "drafter":
      return "draft";
    case "stylist":
    case "final_rewrite":
      return "style";
    case "critic":
      return "review";
    case "integrity":
      return "integrity";
    case "exports":
      return "export";
    default:
      return null;
  }
}

function sourceRerunNeedsConfirm(phase: string, actionKey?: string): boolean {
  if (!SOURCE_RERUN_PHASES.has(phase)) return false;
  return (
    actionKey === undefined ||
    actionKey === "rerun" ||
    actionKey === "rerun_for_new_match" ||
    actionKey === "regenerate"
  );
}

function buildSourceRerunImpact(
  phase: string,
  sources: SourcesBundle | null,
  phases: PhaseHistoryEntry[] = [],
): SourceRerunImpact {
  const phaseIndex = PHASE_ORDER.indexOf(phase as (typeof PHASE_ORDER)[number]);
  const downstreamGenerated =
    phaseIndex < 0
      ? 0
      : phases.filter((entry) => {
          const entryIndex = PHASE_ORDER.indexOf(
            entry.phase as (typeof PHASE_ORDER)[number],
          );
          return entryIndex > phaseIndex && entry.head_version_no !== null;
        }).length;
  return {
    phase,
    skimCandidates: sources?.skim_candidates.length ?? 0,
    shortlist: sources?.shortlist.length ?? 0,
    manualRequests: sources?.manual_upload_requests.length ?? 0,
    userUploads: countUserUploads(sources),
    downstreamGenerated,
  };
}

function countUserUploads(sources: SourcesBundle | null): number {
  if (!sources) return 0;
  const ids = new Set<string>();
  for (const source of sources.shortlist) {
    if (source.source_client === "user_upload") ids.add(source.source_id);
  }
  for (const [sourceId, entry] of Object.entries(sources.fulltext_manifest)) {
    if (entry.pdf_path.startsWith("sources/uploads/")) ids.add(sourceId);
  }
  return ids.size;
}

// P0 fix #2 (codex state-machine audit §1.1 D): when a run loads
// with ``active_phase_lock`` claimed for a known phase, the lock is
// usually the strongest signal of which tab the user should land on,
// even when the run state has been rewound by a rerun cascade.
//
// PR-bug-A: final_rewrite -> critic can briefly expose
// state=CRITIC_RUNNING while an older final_rewrite lock is still
// visible. If the current running state maps to a different phase than
// the lock, prefer the state because it is the transition target and
// the lock may be a stale handoff artifact.
function resolveLandingSubview(
  state: string | undefined | null,
  activePhaseLock: ActivePhaseLock | null | undefined,
  blockedPhase: string | null | undefined = null,
): WorkspaceSubview {
  if (activePhaseLock !== null && activePhaseLock !== undefined) {
    const lockPhase = activePhaseLock.phase;
    if (typeof lockPhase === "string" && lockPhase.length > 0) {
      const statePhase =
        state !== undefined && state !== null ? RUNNING_STATE_TO_PHASE[state] : undefined;
      if (statePhase !== undefined && statePhase !== lockPhase) {
        return defaultSubviewForState(state);
      }
      const lockSubview = phaseToSubview(lockPhase);
      if (lockSubview !== null) return lockSubview;
    }
  }
  if (shouldLandOnBlockedPhase(state, blockedPhase)) {
    const blockedSubview = phaseToSubview(blockedPhase);
    if (blockedSubview !== null) return blockedSubview;
  }
  return defaultSubviewForState(state);
}

function shouldLandOnBlockedPhase(
  state: string | undefined | null,
  blockedPhase: string | null | undefined,
): blockedPhase is string {
  if (!state || !blockedPhase) return false;
  if (
    state === "FAILED_FIXABLE" ||
    state === "FAILED_NEEDS_USER" ||
    state === "FAILED_VENDOR" ||
    state === "FAILED_POLICY" ||
    state === "CANCELLED"
  ) {
    return true;
  }
  return state.startsWith("USER_") && state.endsWith("_REVIEW");
}

type PhaseAction = {
  key: string;
  label: string;
  disabled: boolean;
  disabledReason?: string;
  onClick: () => Promise<void>;
};

type SourceTabId = "shortlist" | "manual" | "skimmed";
type SourceReviewDecision = "approved" | "rejected" | "pinned";
type SourceReviewScope = "search_review" | "deep_dive_review";

type SourceReviewStats = {
  total: number;
  approved: number;
  rejected: number;
  pinned: number;
  pending: number;
  selected: number;
};

const SOURCE_REVIEW_DECISIONS: SourceReviewDecision[] = [
  "approved",
  "rejected",
  "pinned",
];

type CuratorReadiness = {
  canRun: boolean;
  reasonKey: string | null;
  reasonValues?: Record<string, string>;
};

function resolveDefaultSourceTab(
  currentState: string | undefined,
  shortlistCount: number,
  skimmedCount: number,
): SourceTabId {
  if (
    currentState === "USER_SEARCH_REVIEW" &&
    shortlistCount === 0 &&
    skimmedCount > 0
  ) {
    return "skimmed";
  }
  return "shortlist";
}

function resolveCuratorReadiness({
  currentState,
  scoutCompleted,
  blockedPhase,
}: {
  currentState: string | undefined;
  scoutCompleted: boolean;
  blockedPhase: string | null | undefined;
}): CuratorReadiness {
  if (currentState && FAILURE_STATES.has(currentState)) {
    return blockedPhase
      ? {
          canRun: false,
          reasonKey: "workspace.sources.disabled.blocked_phase",
          reasonValues: { phase: blockedPhase },
        }
      : {
          canRun: false,
          reasonKey: "workspace.sources.disabled.blocked_unknown",
        };
  }
  if (currentState === "CURATOR_RUNNING") {
    return { canRun: false, reasonKey: "workspace.sources.disabled.running" };
  }
  if (currentState === "USER_SEARCH_REVIEW") {
    return scoutCompleted
      ? { canRun: true, reasonKey: null }
      : {
          canRun: false,
          reasonKey: "workspace.sources.disabled.waiting_scout",
        };
  }
  if (
    currentState === "TOPIC_ENTERED" ||
    currentState === "DOMAIN_LOADED" ||
    currentState === "PROPOSAL_DRAFTING" ||
    currentState === "USER_PROPOSAL_REVIEW" ||
    currentState === "SCOUT_RUNNING"
  ) {
    return {
      canRun: false,
      reasonKey: "workspace.sources.disabled.upstream_pending",
    };
  }
  return { canRun: false, reasonKey: "workspace.sources.disabled.already_done" };
}

function reviewDecisionFor(
  decisions: Record<string, SourceReviewDecision>,
  sourceId: string,
  defaultDecision: SourceReviewDecision | null,
): SourceReviewDecision | null {
  return decisions[sourceId] ?? defaultDecision;
}

function reviewStatsFor(
  sourceIds: string[],
  decisions: Record<string, SourceReviewDecision>,
  defaultDecision: SourceReviewDecision | null,
): SourceReviewStats {
  let approved = 0;
  let rejected = 0;
  let pinned = 0;
  let pending = 0;
  for (const sourceId of sourceIds) {
    const decision = reviewDecisionFor(decisions, sourceId, defaultDecision);
    if (decision === "approved") approved += 1;
    else if (decision === "rejected") rejected += 1;
    else if (decision === "pinned") pinned += 1;
    else pending += 1;
  }
  return {
    total: sourceIds.length,
    approved,
    rejected,
    pinned,
    pending,
    selected: approved + pinned,
  };
}

function buildSourceReviewPayload(
  sourceIds: string[],
  decisions: Record<string, SourceReviewDecision>,
  defaultDecision: SourceReviewDecision | null,
  reviewScope: SourceReviewScope,
): SourceReviewCheckpointPayload {
  const approvedSourceIds: string[] = [];
  const rejectedSourceIds: string[] = [];
  const pinnedSourceIds: string[] = [];
  for (const sourceId of sourceIds) {
    const decision = reviewDecisionFor(decisions, sourceId, defaultDecision);
    if (decision === "approved") {
      approvedSourceIds.push(sourceId);
    } else if (decision === "pinned") {
      pinnedSourceIds.push(sourceId);
      approvedSourceIds.push(sourceId);
    } else if (decision === "rejected") {
      rejectedSourceIds.push(sourceId);
    }
  }
  return {
    source_ids: approvedSourceIds,
    approved_source_ids: approvedSourceIds,
    rejected_source_ids: rejectedSourceIds,
    pinned_source_ids: pinnedSourceIds,
    review_scope: reviewScope,
    reviewed_at_client: new Date().toISOString(),
  };
}

function pruneSourceReviewDecisions(
  decisions: Record<string, SourceReviewDecision>,
  sourceIds: Set<string>,
): Record<string, SourceReviewDecision> {
  let changed = false;
  const next: Record<string, SourceReviewDecision> = {};
  for (const [sourceId, decision] of Object.entries(decisions)) {
    if (sourceIds.has(sourceId)) {
      next[sourceId] = decision;
    } else {
      changed = true;
    }
  }
  return changed ? next : decisions;
}

function withDefaultSourceReviewDecisions(
  decisions: Record<string, SourceReviewDecision>,
  sourceIds: string[],
  defaultDecision: SourceReviewDecision,
): Record<string, SourceReviewDecision> {
  let changed = false;
  const next: Record<string, SourceReviewDecision> = {};
  const sourceIdSet = new Set(sourceIds);
  for (const sourceId of sourceIds) {
    const decision = decisions[sourceId] ?? defaultDecision;
    next[sourceId] = decision;
    if (decisions[sourceId] !== decision) changed = true;
  }
  for (const sourceId of Object.keys(decisions)) {
    if (!sourceIdSet.has(sourceId)) changed = true;
  }
  return changed ? next : decisions;
}

// PR-A v2 style port: switched the primary slate-blue ``#114b5f``
// to the v2 deep-green ``#245d49`` (paper #f7f4ee + serif headings
// + larger rounded radius + softer shadows) so the workspace
// matches the login + dashboard look. All subviews that read these
// constants pick up the new style automatically.
const primaryButtonClasses =
  "inline-flex min-h-11 w-full items-center justify-center rounded bg-[linear-gradient(180deg,#2e7659_0%,#28674f_100%)] px-4 py-2 text-sm font-bold text-white transition [box-shadow:inset_0_-2px_0_rgba(12,41,31,0.18)] hover:brightness-105 disabled:cursor-default disabled:opacity-65 sm:w-auto";

const secondaryButtonClasses =
  "inline-flex min-h-11 w-full items-center justify-center rounded border border-[#e6e5e0] bg-white px-4 py-2 text-sm font-bold text-[#245d49] transition hover:bg-[#f0ece1] disabled:cursor-default disabled:opacity-65 sm:w-auto";

const inputClasses =
  "min-h-11 w-full rounded-[14px] border border-[#dfdfdc] bg-[rgba(255,255,255,0.72)] px-3 py-2 text-sm text-[#101417] outline-none transition focus:border-[#245d49] focus:ring-2 focus:ring-[#245d49]/20";

const selectClasses =
  "min-h-11 w-full rounded-[14px] border border-[#dfdfdc] bg-[rgba(255,255,255,0.72)] px-3 py-2 text-sm text-[#101417] outline-none transition focus:border-[#245d49] focus:ring-2 focus:ring-[#245d49]/20";

const sectionClasses = "mt-6";
const sectionHeadingClasses =
  "grid gap-3 sm:flex sm:items-center sm:justify-between";
const inlineActionsClasses = "grid gap-2 sm:flex sm:flex-wrap sm:justify-end";
const eyebrowClasses =
  "mb-2 text-[0.72rem] font-semibold uppercase tracking-[0.18em] text-[#737572]";
const h1Classes =
  "font-serif text-[1.65rem] font-black leading-[1.1] text-[#101417] sm:text-[2rem]";
const h2Classes =
  "mt-6 font-serif text-[1.25rem] font-black text-[#101417] first:mt-0";
const reportPreClasses =
  "overflow-auto whitespace-pre-wrap rounded-[14px] border border-[#e6e5e0] bg-[rgba(255,255,253,0.7)] p-4 text-sm leading-6 text-[#1d2423]";
const cardListClasses = "grid gap-3 p-0";
const infoCardClasses =
  "grid list-none gap-3 rounded-[18px] border border-[#e6e5e0] bg-[rgba(255,255,253,0.95)] p-3 [box-shadow:0_8px_18px_rgba(27,42,34,0.04)] sm:p-4";
const linkClasses =
  "font-bold text-[#245d49] underline-offset-2 hover:underline";
const noticeClasses = "leading-7 text-[#1c4e3c]";

function tabButtonClasses(isActive: boolean): string {
  return [
    "min-h-11 shrink-0 snap-start rounded px-4 py-2 text-sm font-bold transition",
    isActive
      ? "bg-[linear-gradient(180deg,#2e7659_0%,#28674f_100%)] text-white [box-shadow:inset_0_-2px_0_rgba(12,41,31,0.18)]"
      : "border border-[#e6e5e0] bg-white text-[#245d49] hover:bg-[#f0ece1] md:border-transparent md:bg-transparent md:text-[#1d2423]",
  ].join(" ");
}

export default function WorkspacePage() {
  const t = useT();
  const { id } = useParams();
  const navigate = useNavigate();
  const [run, setRun] = useState<Run | null>(null);
  const [proposalBundle, setProposalBundle] = useState<ProposalBundle | null>(
    null,
  );
  const [proposalMissing, setProposalMissing] = useState(false);
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [sourcesBundle, setSourcesBundle] = useState<SourcesBundle | null>(
    null,
  );
  const [synthesisBundle, setSynthesisBundle] =
    useState<SynthesisBundle | null>(null);
  const [frameworkLensBundle, setFrameworkLensBundle] =
    useState<FrameworkLensBundle | null>(null);
  const [noveltyBundle, setNoveltyBundle] = useState<NoveltyBundle | null>(
    null,
  );
  const [noveltyDiscussion, setNoveltyDiscussion] = useState<
    NoveltyDiscussionMessage[]
  >([]);
  const [draftList, setDraftList] = useState<DraftListBundle | null>(null);
  const [activeDraft, setActiveDraft] = useState<DraftBundle | null>(null);
  const [styleBundle, setStyleBundle] = useState<StyleBundle | null>(null);
  const [criticBundle, setCriticBundle] = useState<CriticBundle | null>(null);
  const [integrityBundle, setIntegrityBundle] =
    useState<IntegrityBundle | null>(null);
  const [exportsBundle, setExportsBundle] = useState<ExportsBundle | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isStartingProposal, setIsStartingProposal] = useState(false);
  const [isSavingProposal, setIsSavingProposal] = useState(false);
  const [isAcceptingProposal, setIsAcceptingProposal] = useState(false);
  const [isStartingCurator, setIsStartingCurator] = useState(false);
  const [isStartingSynthesizer, setIsStartingSynthesizer] = useState(false);
  const [isStartingFrameworkLens, setIsStartingFrameworkLens] = useState(false);
  const [isStartingIdeator, setIsStartingIdeator] = useState(false);
  const [isStartingDrafter, setIsStartingDrafter] = useState(false);
  const [isStartingStylist, setIsStartingStylist] = useState(false);
  const [isStartingCritic, setIsStartingCritic] = useState(false);
  const [isStartingIntegrity, setIsStartingIntegrity] = useState(false);
  const [isStartingExports, setIsStartingExports] = useState(false);
  const [isCreatingModeRun, setIsCreatingModeRun] =
    useState<GenerationMode | null>(null);
  const [isSelectingAngle, setIsSelectingAngle] = useState(false);
  const [isDiscussingNovelty, setIsDiscussingNovelty] = useState(false);
  const [isUploadingPdf, setIsUploadingPdf] = useState(false);
  const [activeSubview, setActiveSubview] =
    useState<WorkspaceSubview>("console");
  const [consoleSubtab, setConsoleSubtab] = useState<"timeline" | "system">(
    "timeline",
  );
  // Stage 3.E follow-up: when the workspace first loads a run, jump
  // to whichever subview matches the run's current phase rather than
  // dropping the user on the empty Console tab. ``hasInitializedSubview``
  // ensures we only do this once per mount — once the user has clicked
  // a tab, subsequent state advances do NOT yank them back.
  const [hasInitializedSubview, setHasInitializedSubview] = useState(false);
  const [isWorkspaceSidebarOpen, setIsWorkspaceSidebarOpen] = useState(false);
  const [isHistoryModalOpen, setIsHistoryModalOpen] = useState(false);
  const [isKernelEditModalOpen, setIsKernelEditModalOpen] = useState(false);
  // PR-C0.b2.ui: ?repair=kernel query param signals NewRunPage's
  // partial-failure path (project+run created but kernel PUT
  // failed). Auto-open the kernel-edit modal on mount; clear
  // the param via history.replaceState so refreshing the page
  // doesn't re-open. Banner stays until user saves or dismisses.
  const [showKernelRepairBanner, setShowKernelRepairBanner] = useState<boolean>(
    () => {
      if (typeof window === "undefined") return false;
      return (
        new URLSearchParams(window.location.search).get("repair") === "kernel"
      );
    },
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("repair") === "kernel") {
      setIsKernelEditModalOpen(true);
      params.delete("repair");
      const qs = params.toString();
      const next = window.location.pathname + (qs ? `?${qs}` : "");
      window.history.replaceState({}, "", next);
    }
  }, []);
  const [isEditContentModalOpen, setIsEditContentModalOpen] = useState(false);
  const [bundleRefreshTick, setBundleRefreshTick] = useState(0);
  const [branchList, setBranchList] = useState<BranchListResponse | null>(null);
  const { events, error: streamError } = useRunSSE(id);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    listBranches(id)
      .then((response) => {
        if (!cancelled) setBranchList(response);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [id, bundleRefreshTick]);

  async function handleSwitchBranch(branchId: string) {
    if (!id) return;
    // PR-I4.b A7: backend POST /branches/active 409s if any phase is
    // running; pre-fix the frontend would call into the catch arm
    // and silently swallow the 409 (no UI signal). Refuse up front
    // when isRunningState so the user sees the modal still works
    // but the action is gated rather than silently failing.
    if (isRunningState(currentState)) return;
    try {
      const updated = await switchActiveBranch(id, branchId);
      setBranchList(updated);
      // Refetch run + bundle data — every phase view now points at a
      // different branch's heads, so legacy paths must be reloaded.
      const refreshed = await getRun(id);
      setRun(refreshed);
      setBundleRefreshTick((tick) => tick + 1);
    } catch {
      // Surface in the modal instead of breaking the workspace.
    }
  }

  useEffect(() => {
    let isCancelled = false;
    setRun(null);
    setError(null);
    if (!id) {
      return;
    }

    setIsLoading(true);
    getRun(id)
      .then((nextRun) => {
        if (!isCancelled) {
          setRun(nextRun);
        }
      })
      .catch((caught) => {
        if (!isCancelled) {
          setError(
            caught instanceof Error
              ? caught.message
              : t("workspace.errors.run_fetch"),
          );
        }
      })
      .finally(() => {
        if (!isCancelled) {
          setIsLoading(false);
        }
      });

    return () => {
      isCancelled = true;
    };
  }, [id, t]);

  const recentEvents = useMemo(() => {
    if (events.length > 0) {
      return [...events].reverse();
    }
    return run?.last_event ? [run.last_event] : [];
  }, [events, run]);
  const lastEvent = recentEvents[0] ?? null;
  const realtimeRunRefreshEvent = recentEvents.find((event) =>
    ["state_transition", "phase_failed"].includes(event.event_type),
  );
  const realtimeRunRefreshEventId = realtimeRunRefreshEvent?.id ?? null;
  useEffect(() => {
    if (!id || !realtimeRunRefreshEventId) return;
    let isCancelled = false;
    getRun(id)
      .then((nextRun) => {
        if (!isCancelled) {
          setRun(nextRun);
        }
      })
      .catch(() => undefined);
    return () => {
      isCancelled = true;
    };
  }, [id, realtimeRunRefreshEventId]);
  const stateEvent = recentEvents.find(
    (event) => typeof event.payload.to_state === "string",
  );
  // Stage 3.E follow-up: prefer the run's authoritative ``state``
  // unless we have a state-transition event that is *newer* than
  // the run snapshot. During SSE replay on page refresh, older
  // ``state_transition`` events stream in one at a time; without
  // this timestamp anchor, the derived ``currentState`` briefly
  // flashed through past states (e.g. FAILED_POLICY) even when
  // the run's true state had already moved on (e.g.
  // USER_FINAL_ACCEPTANCE), producing a visible banner flicker.
  const stateFromEvent = stateEvent?.payload.to_state;
  const eventIsNewerThanRun =
    !!stateEvent &&
    !!run?.updated_at &&
    Date.parse(stateEvent.created_at) >= Date.parse(run.updated_at);
  const currentState =
    typeof stateFromEvent === "string" && eventIsNewerThanRun
      ? stateFromEvent
      : (run?.state ??
        (typeof stateFromEvent === "string" ? stateFromEvent : undefined));
  const restoreRecoveryEvent = recentEvents.find(
    (event) => event.event_type === "run_restore_recovery_warning",
  );
  const restoreRecoveryPhase =
    typeof restoreRecoveryEvent?.payload.phase === "string" &&
    restoreRecoveryEvent.payload.phase.length > 0
      ? restoreRecoveryEvent.payload.phase
      : t("workspace.restore_recovery.phase_unknown");

  // Stage 3.E follow-up: in FAILED_* states the original gating
  // hides every workspace tab, so clicking the banner's
  // "open the X tab to inspect / edit" link silently does nothing.
  // We compute an ``effectiveStateForViews`` that, during a failure,
  // pretends the run is at the USER_*_REVIEW level corresponding to
  // the most recently failed phase. Every ``show*`` predicate below
  // uses this so the tabs the user reached remain reachable while
  // the run is in a failed state.
  const _failedPhaseFromEvents = (() => {
    for (const event of recentEvents) {
      if (event.event_type !== "phase_failed") continue;
      const phase = (event.payload as { phase?: string } | undefined)?.phase;
      if (typeof phase === "string" && phase.length > 0) return phase;
    }
    return null;
  })();
  const FAILED_PHASE_VIEW_STATE: Record<string, string> = {
    proposal: "USER_PROPOSAL_REVIEW",
    scout: "USER_SEARCH_REVIEW",
    curator: "USER_DEEP_DIVE_REVIEW",
    synthesizer: "USER_FIELD_REVIEW",
    // PR-I4.d: tension_extraction + framework_lens both come AFTER
    // synthesizer's USER_FIELD_REVIEW exit. When either of them
    // fails, fall back to that "post-synthesizer" view so the user
    // still sees the proposal / sources / synthesis / lens tabs
    // they need to consult per the agent's guidance (e.g., the
    // theory_article + 0 lens_source case asks the user to go to
    // the Sources tab and re-tag a source). Pre-fix the missing
    // entry caused effectiveStateForViews to fall through to
    // FAILED_FIXABLE, which is in none of the show* allowlists, so
    // the workspace collapsed to console + corpus + lens only —
    // hiding the very tabs the failure guidance pointed to.
    tension_extraction: "USER_FIELD_REVIEW",
    framework_lens: "USER_FIELD_REVIEW",
    ideator: "USER_NOVELTY_REVIEW",
    drafter: "USER_REVISION_REVIEW",
    stylist: "USER_REVISION_REVIEW",
    critic: "USER_EXTERNAL_SCAN_APPROVAL",
    integrity: "USER_FINAL_ACCEPTANCE",
    exports: "USER_FINAL_ACCEPTANCE",
  };
  const FAILURE_STATE_NAMES = new Set([
    "FAILED_FIXABLE",
    "FAILED_NEEDS_USER",
    "FAILED_VENDOR",
    "FAILED_POLICY",
    "CANCELLED",
  ]);
  const effectiveStateForViews =
    typeof currentState === "string" &&
    FAILURE_STATE_NAMES.has(currentState) &&
    _failedPhaseFromEvents
      ? (FAILED_PHASE_VIEW_STATE[_failedPhaseFromEvents] ?? currentState)
      : currentState;

  // Stage 3.E follow-up: one-shot landing-tab selection. When the run
  // first loads, we know the state and can pick a useful tab (e.g.
  // "export" when the run is at USER_FINAL_ACCEPTANCE / FAILED_POLICY)
  // instead of dropping the user on Console. ``hasInitializedSubview``
  // guards against re-applying every time SSE bumps state — once user
  // navigates, their choice sticks.
  //
  // P0 fix #2 (codex state-machine audit §1.1 D): use
  // ``resolveLandingSubview`` so when ``active_phase_lock`` is held,
  // the running phase's tab wins over the (possibly rewound) state.
  // Fixes the rerun-cascade case where rerunning synthesizer rewinds
  // state to USER_DEEP_DIVE_REVIEW and the workspace lands the user
  // on "sources" instead of staying on "synthesis" where the user
  // was already observing the rerun.
  //
  // PR-322 follow-up: gate on `run?.state` (the authoritative run
  // snapshot) rather than `effectiveStateForViews`. The latter falls
  // through to `stateFromEvent` while `run` is still loading, and the
  // first SSE event replayed on page refresh is usually `run_created`
  // / `DOMAIN_LOADED`, which routed the user to the proposal tab and
  // pinned `hasInitializedSubview=true` before the real state landed.
  // Symptom: opening a STYLIST_RUNNING run URL "总是自动定位到提案".
  //
  // 2026-05-12 PR-365: prefer the user's previously-selected tab from
  // localStorage (per run id) if it exists. Falls back to state-driven
  // ``resolveLandingSubview`` when no persisted value is present.
  useEffect(() => {
    if (hasInitializedSubview) return;
    if (!run?.state) return;
    const persisted = readPersistedSubview(id);
    let target =
      persisted ??
      resolveLandingSubview(
        run.state,
        run.active_phase_lock ?? null,
        _failedPhaseFromEvents,
      );
    // Express runs have no proposal/sources/synthesis/etc. phases —
    // their workspace is the transparency panel + the always-visible
    // console / corpus tabs. ``defaultSubviewForState`` is deep-mode
    // aware and would otherwise route DOMAIN_LOADED to "proposal",
    // which renders the empty "提案产物待生成" placeholder under an
    // express run. Pin express to "console".
    if (run.mode === "express") {
      target = "console";
    }
    if (target !== activeSubview) {
      setActiveSubview(target);
    }
    setHasInitializedSubview(true);
  }, [
    run?.state,
    run?.mode,
    hasInitializedSubview,
    activeSubview,
    run?.active_phase_lock,
    _failedPhaseFromEvents,
    id,
  ]);

  // 2026-05-12 PR-365: persist activeSubview to localStorage whenever
  // the user navigates. Gate on ``hasInitializedSubview`` so the initial
  // restore-from-localStorage step does not immediately rewrite the
  // same value back. Run id is part of the key so different runs do
  // not stomp each other's saved tab.
  useEffect(() => {
    if (!hasInitializedSubview) return;
    if (!id || typeof window === "undefined") return;
    try {
      window.localStorage.setItem(workspaceSubviewStorageKey(id), activeSubview);
    } catch {
      // private mode / quota — silently drop persistence.
    }
  }, [activeSubview, hasInitializedSubview, id]);

  // On mobile the workspace tab strip is a horizontal scroller; tabs
  // past the viewport (e.g. Export when default-landing on a finished
  // run) are visually hidden until the user scrolls. Bring the active
  // tab into view whenever it changes so users don't have to.
  useEffect(() => {
    const el = document.querySelector(
      `[data-testid="workspace-tab-${activeSubview}"]`,
    );
    if (el && typeof (el as HTMLElement).scrollIntoView === "function") {
      (el as HTMLElement).scrollIntoView({
        block: "nearest",
        inline: "center",
        behavior: "smooth",
      });
    }
  }, [activeSubview]);

  const isExpressRun = run?.mode === "express";
  const showProposal =
    !isExpressRun &&
    typeof effectiveStateForViews === "string" &&
    PROPOSAL_VISIBLE_STATES.has(effectiveStateForViews);
  const showDiscovery =
    !isExpressRun &&
    (effectiveStateForViews === "SCOUT_RUNNING" ||
      effectiveStateForViews === "USER_SEARCH_REVIEW");
  const showSources =
    !isExpressRun &&
    (effectiveStateForViews === "SCOUT_RUNNING" ||
      effectiveStateForViews === "USER_SEARCH_REVIEW" ||
      effectiveStateForViews === "CURATOR_RUNNING" ||
      effectiveStateForViews === "USER_DEEP_DIVE_REVIEW" ||
      effectiveStateForViews === "SYNTHESIZER_RUNNING" ||
      effectiveStateForViews === "USER_FIELD_REVIEW" ||
      effectiveStateForViews === "TENSION_EXTRACTION_RUNNING" ||
      effectiveStateForViews === "USER_TENSION_REVIEW" ||
      effectiveStateForViews === "IDEATOR_RUNNING" ||
      effectiveStateForViews === "USER_NOVELTY_REVIEW" ||
      effectiveStateForViews === "DRAFTER_RUNNING" ||
      effectiveStateForViews === "STYLIST_RUNNING" ||
      effectiveStateForViews === "USER_REVISION_REVIEW" ||
      effectiveStateForViews === "REWRITE_RUNNING" ||
      effectiveStateForViews === "CRITIC_RUNNING" ||
      effectiveStateForViews === "USER_EXTERNAL_SCAN_APPROVAL" ||
      effectiveStateForViews === "INTEGRITY_RUNNING" ||
      effectiveStateForViews === "USER_INTEGRITY_REVIEW" ||
      effectiveStateForViews === "USER_FINAL_ACCEPTANCE" ||
      effectiveStateForViews === "EXPORTS_RUNNING" ||
      effectiveStateForViews === "EXPORTS_DONE");
  const showStyle =
    !isExpressRun &&
    (effectiveStateForViews === "DRAFTER_RUNNING" ||
      effectiveStateForViews === "STYLIST_RUNNING" ||
      effectiveStateForViews === "USER_REVISION_REVIEW" ||
      effectiveStateForViews === "REWRITE_RUNNING" ||
      effectiveStateForViews === "CRITIC_RUNNING" ||
      effectiveStateForViews === "USER_EXTERNAL_SCAN_APPROVAL" ||
      effectiveStateForViews === "INTEGRITY_RUNNING" ||
      effectiveStateForViews === "USER_INTEGRITY_REVIEW" ||
      effectiveStateForViews === "USER_FINAL_ACCEPTANCE" ||
      effectiveStateForViews === "EXPORTS_RUNNING" ||
      effectiveStateForViews === "EXPORTS_DONE");
  const showSynthesis =
    !isExpressRun &&
    (effectiveStateForViews === "USER_DEEP_DIVE_REVIEW" ||
      effectiveStateForViews === "SYNTHESIZER_RUNNING" ||
      effectiveStateForViews === "USER_FIELD_REVIEW" ||
      // tension_extraction is a synthesizer→lens sub-phase that
      // belongs to the synthesis tab (phaseToSubview agrees).
      effectiveStateForViews === "TENSION_EXTRACTION_RUNNING" ||
      effectiveStateForViews === "USER_TENSION_REVIEW" ||
      effectiveStateForViews === "IDEATOR_RUNNING" ||
      effectiveStateForViews === "USER_NOVELTY_REVIEW" ||
      effectiveStateForViews === "DRAFTER_RUNNING");
  const showNovelty =
    !isExpressRun &&
    (effectiveStateForViews === "USER_FIELD_REVIEW" ||
      effectiveStateForViews === "TENSION_EXTRACTION_RUNNING" ||
      effectiveStateForViews === "USER_TENSION_REVIEW" ||
      // PR-real-paper-fix: USER_LENS_REVIEW (post-lens path) was
      // missing — without it the novelty tab disappears the moment
      // lens completes, while the lens tab shows "confirm to advance"
      // amber with no button. User stuck. Add to gate.
      effectiveStateForViews === "USER_LENS_REVIEW" ||
      effectiveStateForViews === "IDEATOR_RUNNING" ||
      effectiveStateForViews === "USER_NOVELTY_REVIEW" ||
      effectiveStateForViews === "DRAFTER_RUNNING" ||
      effectiveStateForViews === "STYLIST_RUNNING" ||
      effectiveStateForViews === "USER_REVISION_REVIEW" ||
      effectiveStateForViews === "REWRITE_RUNNING" ||
      effectiveStateForViews === "CRITIC_RUNNING" ||
      effectiveStateForViews === "USER_EXTERNAL_SCAN_APPROVAL" ||
      effectiveStateForViews === "INTEGRITY_RUNNING" ||
      effectiveStateForViews === "USER_INTEGRITY_REVIEW" ||
      effectiveStateForViews === "USER_FINAL_ACCEPTANCE" ||
      effectiveStateForViews === "EXPORTS_RUNNING" ||
      effectiveStateForViews === "EXPORTS_DONE");
  const showDraft =
    !isExpressRun &&
    (effectiveStateForViews === "DRAFTER_RUNNING" ||
      effectiveStateForViews === "STYLIST_RUNNING" ||
      effectiveStateForViews === "USER_REVISION_REVIEW" ||
      effectiveStateForViews === "REWRITE_RUNNING" ||
      effectiveStateForViews === "CRITIC_RUNNING" ||
      effectiveStateForViews === "USER_EXTERNAL_SCAN_APPROVAL" ||
      effectiveStateForViews === "INTEGRITY_RUNNING" ||
      effectiveStateForViews === "USER_INTEGRITY_REVIEW" ||
      effectiveStateForViews === "USER_FINAL_ACCEPTANCE" ||
      effectiveStateForViews === "EXPORTS_RUNNING" ||
      effectiveStateForViews === "EXPORTS_DONE");
  const showReview =
    !isExpressRun &&
    (effectiveStateForViews === "CRITIC_RUNNING" ||
      effectiveStateForViews === "USER_EXTERNAL_SCAN_APPROVAL" ||
      effectiveStateForViews === "INTEGRITY_RUNNING" ||
      effectiveStateForViews === "USER_INTEGRITY_REVIEW" ||
      effectiveStateForViews === "USER_FINAL_ACCEPTANCE" ||
      effectiveStateForViews === "EXPORTS_RUNNING" ||
      effectiveStateForViews === "EXPORTS_DONE");
  const showIntegrity =
    !isExpressRun &&
    (effectiveStateForViews === "INTEGRITY_RUNNING" ||
      effectiveStateForViews === "USER_INTEGRITY_REVIEW" ||
      effectiveStateForViews === "USER_FINAL_ACCEPTANCE" ||
      effectiveStateForViews === "EXPORTS_RUNNING" ||
      effectiveStateForViews === "EXPORTS_DONE");
  const showExport =
    !isExpressRun &&
    (effectiveStateForViews === "USER_FINAL_ACCEPTANCE" ||
      effectiveStateForViews === "EXPORTS_RUNNING" ||
      effectiveStateForViews === "EXPORTS_DONE");
  const sourceProgress = useMemo(
    () =>
      events
        .filter(
          (event) =>
            event.event_type === "source_progress" &&
            event.payload.phase === "scout",
        )
        .reverse(),
    [events],
  );
  const proposalProgress = useMemo(
    () =>
      events.filter((event) => event.payload.phase === "proposal").reverse(),
    [events],
  );
  const curatorProgress = useMemo(
    () =>
      events
        .filter(
          (event) =>
            event.event_type === "source_progress" &&
            event.payload.phase === "curator",
        )
        .reverse(),
    [events],
  );
  const synthesizerProgress = useMemo(
    () =>
      events
        .filter(
          (event) =>
            event.event_type === "source_progress" &&
            event.payload.phase === "synthesizer",
        )
        .reverse(),
    [events],
  );
  const drafterProgress = useMemo(
    () =>
      events
        .filter(
          (event) =>
            event.event_type === "section_progress" &&
            event.payload.phase === "drafter",
        )
        .reverse(),
    [events],
  );
  const stylistProgress = useMemo(
    () =>
      events
        .filter(
          (event) =>
            event.event_type === "section_progress" &&
            event.payload.phase === "stylist",
        )
        .reverse(),
    [events],
  );
  const skimCandidates =
    sourcesBundle?.skim_candidates ?? discovery?.skim_candidates ?? [];

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showProposal) {
      setProposalBundle(null);
      setProposalMissing(false);
      return;
    }
    setProposalMissing(false);
    getProposal(id)
      .then((nextProposal) => {
        if (!isCancelled) {
          setProposalBundle(nextProposal);
          setProposalMissing(false);
        }
      })
      .catch((caught) => {
        if (!isCancelled) {
          setProposalBundle(null);
          setProposalMissing(
            caught instanceof ApiError && caught.status === 404,
          );
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showProposal,
    events.length,
    currentState,
    activeSubview,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showDiscovery) {
      setDiscovery(null);
      return;
    }
    getDiscovery(id)
      .then((nextDiscovery) => {
        if (!isCancelled) {
          setDiscovery(nextDiscovery);
        }
      })
      .catch((caught) => {
        if (!isCancelled) {
          setError(
            caught instanceof Error
              ? caught.message
              : t("workspace.errors.discovery_fetch"),
          );
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showDiscovery,
    events.length,
    currentState,
    activeSubview,
    t,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showSources) {
      setSourcesBundle(null);
      return;
    }
    getSources(id)
      .then((nextSources) => {
        if (!isCancelled) {
          setSourcesBundle(nextSources);
        }
      })
      .catch((caught) => {
        if (!isCancelled) {
          setError(
            caught instanceof Error
              ? caught.message
              : t("workspace.errors.sources_fetch"),
          );
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showSources,
    events.length,
    currentState,
    activeSubview,
    t,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showSynthesis) {
      setSynthesisBundle(null);
      return;
    }
    getSynthesis(id)
      .then((nextSynthesis) => {
        if (!isCancelled) {
          setSynthesisBundle(nextSynthesis);
        }
      })
      .catch((caught) => {
        if (!isCancelled) {
          setError(
            caught instanceof Error
              ? caught.message
              : t("workspace.errors.synthesis_fetch"),
          );
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showSynthesis,
    events.length,
    currentState,
    activeSubview,
    t,
    bundleRefreshTick,
  ]);

  // PR-C2.b Tier 4: fetch framework_lens artifact for the Lens tab.
  // Trigger on the same conditions as synthesis (any state where the
  // lens tab content might be shown, plus refreshes after lens runs).
  useEffect(() => {
    let isCancelled = false;
    if (!id) {
      setFrameworkLensBundle(null);
      return;
    }
    getFrameworkLens(id)
      .then((next) => {
        if (!isCancelled) {
          setFrameworkLensBundle(next);
        }
      })
      .catch(() => {
        // Soft-fail: lens artifact is optional. Errors don't surface
        // to the global error banner — the subview itself shows
        // "no lens output yet" when bundle is null/empty.
        if (!isCancelled) {
          setFrameworkLensBundle(null);
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [id, events.length, currentState, bundleRefreshTick]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showNovelty) {
      setNoveltyBundle(null);
      setNoveltyDiscussion([]);
      return;
    }
    getNovelty(id)
      .then((nextNovelty) => {
        if (!isCancelled) {
          setNoveltyBundle(nextNovelty);
        }
      })
      .catch((caught) => {
        if (!isCancelled) {
          setError(
            caught instanceof Error
              ? caught.message
              : t("workspace.errors.novelty_fetch"),
          );
        }
      });
    return () => {
      isCancelled = true;
    };
    // Refetch when state transitions (so we pick up angle_cards.json once
    // Ideator writes it) and when the user explicitly opens the Novelty
    // tab. events.length on its own can race the file-write since SSE
    // delivery is not guaranteed in lockstep with state transitions.
  }, [
    id,
    showNovelty,
    events.length,
    currentState,
    activeSubview,
    t,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showNovelty) {
      setNoveltyDiscussion([]);
      return;
    }
    getNoveltyDiscussion(id)
      .then((messages) => {
        if (!isCancelled) {
          setNoveltyDiscussion(messages);
        }
      })
      .catch(() => {
        if (!isCancelled) {
          setNoveltyDiscussion([]);
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showNovelty,
    events.length,
    currentState,
    activeSubview,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showDraft) {
      setDraftList(null);
      setActiveDraft(null);
      return;
    }
    getDrafts(id)
      .then(async (nextDrafts) => {
        if (isCancelled) {
          return;
        }
        setDraftList(nextDrafts);
        const latest = nextDrafts.drafts.at(-1);
        if (latest?.version) {
          const draft = await getDraft(id, latest.version);
          if (!isCancelled) {
            setActiveDraft(draft);
          }
        }
      })
      .catch((caught) => {
        if (!isCancelled) {
          setError(
            caught instanceof Error
              ? caught.message
              : t("workspace.errors.draft_fetch"),
          );
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showDraft,
    events.length,
    currentState,
    activeSubview,
    t,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showStyle) {
      setStyleBundle(null);
      return;
    }
    getStyle(id)
      .then((nextStyle) => {
        if (!isCancelled) {
          setStyleBundle(nextStyle);
        }
      })
      .catch(() => {
        if (!isCancelled && currentState === "USER_REVISION_REVIEW") {
          setStyleBundle(null);
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showStyle,
    currentState,
    events.length,
    activeSubview,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showReview) {
      setCriticBundle(null);
      return;
    }
    getCritic(id)
      .then((nextCritic) => {
        if (!isCancelled) {
          setCriticBundle(nextCritic);
        }
      })
      .catch(() => {
        if (!isCancelled) {
          setCriticBundle(null);
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showReview,
    events.length,
    currentState,
    activeSubview,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showIntegrity) {
      setIntegrityBundle(null);
      return;
    }
    getIntegrity(id)
      .then((nextIntegrity) => {
        if (!isCancelled) {
          setIntegrityBundle(nextIntegrity);
        }
      })
      .catch(() => {
        if (!isCancelled) {
          setIntegrityBundle(null);
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showIntegrity,
    events.length,
    currentState,
    activeSubview,
    bundleRefreshTick,
  ]);

  useEffect(() => {
    let isCancelled = false;
    if (!id || !showExport) {
      setExportsBundle(null);
      return;
    }
    getExports(id)
      .then((nextExports) => {
        if (!isCancelled) {
          setExportsBundle(nextExports);
        }
      })
      .catch(() => {
        if (!isCancelled) {
          setExportsBundle(null);
        }
      });
    return () => {
      isCancelled = true;
    };
  }, [
    id,
    showExport,
    events.length,
    currentState,
    activeSubview,
    bundleRefreshTick,
  ]);

  async function handleRunProposal(userDraft: string) {
    if (!id) {
      return;
    }
    setIsStartingProposal(true);
    setError(null);
    try {
      const job = await startProposal(id, userDraft);
      setRun((current) =>
        current ? { ...current, state: job.expected_state } : current,
      );
      setActiveSubview("proposal");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.proposal_start"),
      );
    } finally {
      setIsStartingProposal(false);
    }
  }

  async function handleSaveProposal(
    proposal: ProposalContent,
    options: { mode?: "new" | "replace"; base_version?: number } = {},
  ) {
    if (!id) {
      return;
    }
    setIsSavingProposal(true);
    setError(null);
    try {
      const saved = await saveProposal(id, proposal, options);
      setProposalBundle(saved);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.proposal_save"),
      );
    } finally {
      setIsSavingProposal(false);
    }
  }

  async function handleAcceptProposal() {
    if (!id) {
      return;
    }
    setIsAcceptingProposal(true);
    setError(null);
    try {
      await acceptProposal(id);
      setRun((current) =>
        current ? { ...current, state: "SCOUT_RUNNING" } : current,
      );
      setActiveSubview("console");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.proposal_accept"),
      );
    } finally {
      setIsAcceptingProposal(false);
    }
  }

  async function handleRunCurator() {
    if (!id) {
      return;
    }
    setIsStartingCurator(true);
    setError(null);
    try {
      await startCurator(id);
      setRun((current) =>
        current ? { ...current, state: "CURATOR_RUNNING" } : current,
      );
      setActiveSubview("sources");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.curator_start"),
      );
    } finally {
      setIsStartingCurator(false);
    }
  }

  async function handleRunSynthesizer() {
    if (!id) {
      return;
    }
    setIsStartingSynthesizer(true);
    setError(null);
    try {
      await startSynthesizer(id);
      setRun((current) =>
        current ? { ...current, state: "SYNTHESIZER_RUNNING" } : current,
      );
      setActiveSubview("synthesis");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.synthesizer_start"),
      );
    } finally {
      setIsStartingSynthesizer(false);
    }
  }

  async function handleRunIdeator() {
    if (!id) {
      return;
    }
    setIsStartingIdeator(true);
    setError(null);
    try {
      await startIdeator(id);
      setRun((current) =>
        current ? { ...current, state: "IDEATOR_RUNNING" } : current,
      );
      setActiveSubview("novelty");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.ideator_start"),
      );
    } finally {
      setIsStartingIdeator(false);
    }
  }

  async function handleRunFrameworkLens() {
    if (!id) {
      return;
    }
    setIsStartingFrameworkLens(true);
    setError(null);
    try {
      await startFrameworkLens(id);
      setRun((current) =>
        current ? { ...current, state: "FRAMEWORK_LENS_RUNNING" } : current,
      );
      setActiveSubview("lens");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.framework_lens_start"),
      );
    } finally {
      setIsStartingFrameworkLens(false);
    }
  }

  async function handleRunDrafter() {
    if (!id) {
      return;
    }
    setIsStartingDrafter(true);
    setError(null);
    try {
      await startDrafter(id);
      setRun((current) =>
        current ? { ...current, state: "DRAFTER_RUNNING" } : current,
      );
      setActiveSubview("draft");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.drafter_start"),
      );
    } finally {
      setIsStartingDrafter(false);
    }
  }

  async function handleRunStylist() {
    if (!id) {
      return;
    }
    setIsStartingStylist(true);
    setError(null);
    try {
      await startStylist(id);
      setRun((current) =>
        current ? { ...current, state: "STYLIST_RUNNING" } : current,
      );
      setActiveSubview("style");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.stylist_start"),
      );
    } finally {
      setIsStartingStylist(false);
    }
  }

  // PR-366: toggle "数理增强模式" mid-run. Refused by the backend with
  // 409 while rewriter/critic is in flight; we surface the message.
  async function handleToggleMathematicalMode(next: boolean) {
    if (!id) {
      return;
    }
    setError(null);
    try {
      const updated = await updateRunSettings(id, {
        mathematical_mode: next,
      });
      setRun(updated);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.settings_update"),
      );
    }
  }

  // PR-382: toggle 一键全自动 mid-run. Flipping ON fires the
  // backend coordinator immediately, so the run advances from the
  // current USER_*_REVIEW state on the same response.
  async function handleToggleAutoAdvance(next: boolean) {
    if (!id) {
      return;
    }
    setError(null);
    try {
      const updated = await updateRunSettings(id, {
        auto_advance: next,
      });
      setRun(updated);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.settings_update"),
      );
    }
  }

  async function handleCreateModeRun(mode: GenerationMode) {
    if (!run) {
      return;
    }
    setIsCreatingModeRun(mode);
    setError(null);
    try {
      const created = await createRun(run.project_id, {
        mode,
        mathematical_mode: Boolean(run.mathematical_mode),
        auto_advance: mode === "deep" ? Boolean(run.auto_advance) : false,
      });
      try {
        await editResearchKernel(created.id, {
          paper_mode: run.paper_mode || "case_analysis",
          kernel: (run.research_kernel ?? {
            kernel_schema_version: 1,
          }) as unknown as ResearchKernel,
          base_proposal_version: 0,
          base_kernel_hash: created.research_kernel_hash || "",
          accept_developer_preview: true,
        });
      } catch (kernelErr) {
        navigate(`/runs/${created.id}?repair=kernel`);
        console.warn("kernel PUT failed for mode rerun", kernelErr);
        return;
      }
      if (mode === "express") {
        await startProposal(created.id, "");
      }
      navigate(`/runs/${created.id}`);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.mode_run_create"),
      );
    } finally {
      setIsCreatingModeRun(null);
    }
  }

  async function handleRunCritic() {
    if (!id) {
      return;
    }
    setIsStartingCritic(true);
    setError(null);
    try {
      await startCritic(id);
      setRun((current) =>
        current ? { ...current, state: "CRITIC_RUNNING" } : current,
      );
      setActiveSubview("review");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.critic_start"),
      );
    } finally {
      setIsStartingCritic(false);
    }
  }

  async function handleApproveExternalScan(
    scanKinds: Array<"plagiarism" | "ai_style">,
  ) {
    if (!id) {
      return;
    }
    setError(null);
    try {
      await approveExternalScan(id, scanKinds);
      await handleRunIntegrity();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.external_scan_approval"),
      );
    }
  }

  async function handleSkipExternalScan(skipReason: string) {
    if (!id) {
      return;
    }
    setError(null);
    try {
      await skipExternalScan(id, skipReason);
      setRun((current) =>
        current ? { ...current, state: "USER_FINAL_ACCEPTANCE" } : current,
      );
      setActiveSubview("export");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.external_scan_skip"),
      );
    }
  }

  async function handleRunIntegrity() {
    if (!id) {
      return;
    }
    setIsStartingIntegrity(true);
    setError(null);
    try {
      await startIntegrity(id);
      setRun((current) =>
        current ? { ...current, state: "INTEGRITY_RUNNING" } : current,
      );
      setActiveSubview("integrity");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.integrity_start"),
      );
    } finally {
      setIsStartingIntegrity(false);
    }
  }

  async function handleAcceptIntegrity(
    spanDecisions: Array<Record<string, unknown>>,
  ) {
    if (!id) {
      return;
    }
    setError(null);
    try {
      await acceptIntegrity(id, spanDecisions);
      setRun((current) =>
        current ? { ...current, state: "USER_FINAL_ACCEPTANCE" } : current,
      );
      setActiveSubview("export");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.integrity_accept"),
      );
    }
  }

  async function handleRequestIntegrityRevision(
    spanDecisions: Array<Record<string, unknown>>,
    nextRevisionDimension: string,
  ) {
    if (!id) {
      return;
    }
    setError(null);
    try {
      await requestIntegrityRevision(id, spanDecisions, nextRevisionDimension);
      setRun((current) =>
        current ? { ...current, state: "DRAFTER_RUNNING" } : current,
      );
      setActiveSubview("draft");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.integrity_revision"),
      );
    }
  }

  async function handleAcceptFinalDraft(exportFormats: string[]) {
    if (!id) {
      return;
    }
    setError(null);
    try {
      await acceptFinalDraft(id, exportFormats);
      await handleRunExports();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.final_acceptance"),
      );
    }
  }

  async function handleRunExports() {
    if (!id) {
      return;
    }
    setIsStartingExports(true);
    setError(null);
    try {
      await startExports(id);
      setRun((current) =>
        current ? { ...current, state: "EXPORTS_RUNNING" } : current,
      );
      setActiveSubview("export");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.export_start"),
      );
    } finally {
      setIsStartingExports(false);
    }
  }

  async function handleSelectAngle(angleId: string) {
    if (!id) {
      return;
    }
    setIsSelectingAngle(true);
    setError(null);
    try {
      await selectNoveltyAngle(id, angleId);
      setRun((current) =>
        current ? { ...current, state: "DRAFTER_RUNNING" } : current,
      );
      setActiveSubview("draft");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.angle_select"),
      );
    } finally {
      setIsSelectingAngle(false);
    }
  }

  async function handleDiscussNovelty(userMessage: string) {
    if (!id) {
      return;
    }
    setIsDiscussingNovelty(true);
    setError(null);
    try {
      const response = await discussNovelty(id, userMessage);
      setNoveltyBundle((current) =>
        current ? { ...current, angle_cards: response.angle_cards } : current,
      );
      const messages = await getNoveltyDiscussion(id);
      setNoveltyDiscussion(messages);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.novelty_discussion"),
      );
    } finally {
      setIsDiscussingNovelty(false);
    }
  }

  async function handleUploadPdf(formData: FormData) {
    if (!id) {
      return;
    }
    // PR-I4.b A7: backend POST /sources/upload 409s when any phase
    // is in *_RUNNING. Refuse up front so the user gets a clear
    // error instead of the spinner-then-409 sequence.
    if (isRunningState(currentState)) {
      setError(t("workspace.errors.pdf_upload_running"));
      return;
    }
    setIsUploadingPdf(true);
    setError(null);
    try {
      await uploadSourcePdf(id, formData);
      const nextSources = await getSources(id);
      setSourcesBundle(nextSources);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.pdf_upload"),
      );
    } finally {
      setIsUploadingPdf(false);
    }
  }

  // Stylist gating: codex preferred "any recent phase_done(drafter)"
  // over a strict last-event match — intermediate events (e.g.
  // unrelated source_progress) may land between drafter completion
  // and the user's stylist click.
  const drafterCompleted = recentEvents.some(
    (event) =>
      event.event_type === "phase_done" &&
      (event.payload as { phase?: string } | undefined)?.phase === "drafter",
  );
  // PR-C2.b lens phase reachability: framework_lens is gated on
  // synthesizer completion. Mirror drafterCompleted's pattern.
  const synthesizerCompleted = recentEvents.some(
    (event) =>
      event.event_type === "phase_done" &&
      (event.payload as { phase?: string } | undefined)?.phase ===
        "synthesizer",
  );
  const stylistCompleted = recentEvents.some(
    (event) =>
      event.event_type === "phase_done" &&
      (event.payload as { phase?: string } | undefined)?.phase === "stylist",
  );
  const criticCompleted = recentEvents.some(
    (event) =>
      event.event_type === "phase_done" &&
      (event.payload as { phase?: string } | undefined)?.phase === "critic",
  );
  const integrityCompleted = recentEvents.some(
    (event) =>
      event.event_type === "phase_done" &&
      (event.payload as { phase?: string } | undefined)?.phase === "integrity",
  );
  const scoutCompleted = recentEvents.some(
    (event) =>
      event.event_type === "phase_done" &&
      (event.payload as { phase?: string } | undefined)?.phase === "scout",
  );
  const curatorCompleted = recentEvents.some(
    (event) =>
      event.event_type === "phase_done" &&
      (event.payload as { phase?: string } | undefined)?.phase === "curator",
  );

  // Stage 3.E follow-up: most-recently-failed phase, used to keep the
  // phase-action menu populated during FAILED_* states (so the user
  // can retry directly instead of hunting for the banner button).
  // PR-I4.a: also surface the failure_class from the same event so
  // smartRetry can pick start vs rerun. Walk events newest-first
  // for the most-recent phase_failed; capture both phase and class
  // in a single pass.
  const { failedPhase, failedPhaseFailureClass } = (() => {
    for (const event of recentEvents) {
      if (event.event_type !== "phase_failed") continue;
      const payload = event.payload as
        | { phase?: string; failure_class?: string }
        | undefined;
      const phase = payload?.phase;
      if (typeof phase === "string" && phase.length > 0) {
        return {
          failedPhase: phase,
          failedPhaseFailureClass:
            typeof payload?.failure_class === "string"
              ? payload.failure_class
              : null,
        };
      }
    }
    return {
      failedPhase: null as string | null,
      failedPhaseFailureClass: null,
    };
  })();
  const [isRetryingFailedPhase, setIsRetryingFailedPhase] = useState(false);
  async function handleRetryFailedPhase() {
    if (!id || !failedPhase || isRetryingFailedPhase) return;
    if (currentState === "FAILED_POLICY") return;
    setIsRetryingFailedPhase(true);
    try {
      // PR-I4.a: route via smartRetry instead of always rerun_phase.
      // start_<phase> + rerun_phase split-brain killed first-attempt
      // recovery on graceful failures whose sentinel was written
      // (synthesizer 0-of-6) and zombie recoveries that satisfied
      // ``has_completed_output``. smartRetry picks based on
      // failure_class and falls back on 409 once.
      await smartRetry({
        runId: id,
        phase: failedPhase,
        failureClass: failedPhaseFailureClass,
        startCaller: retryFailedPhase,
      });
      const refreshed = await getRun(id);
      setRun(refreshed);
      setBundleRefreshTick((tick) => tick + 1);
    } catch (caught) {
      window.alert(
        caught instanceof Error
          ? `${t("workspace.rerun_failed")}: ${caught.message}`
          : t("workspace.rerun_failed"),
      );
    } finally {
      setIsRetryingFailedPhase(false);
    }
  }
  const failedPolicyRetryDisabled = currentState === "FAILED_POLICY";
  const failedPhaseAction: PhaseAction | null = failedPhase
    ? {
        key: `retry-${failedPhase}`,
        label: isRetryingFailedPhase
          ? t("workspace.rerun_running")
          : t("workspace.failed_retry_button").replace(
              /\{phase\}/g,
              failedPhase,
            ),
        disabled: isRetryingFailedPhase || failedPolicyRetryDisabled,
        disabledReason: failedPolicyRetryDisabled
          ? t("workspace.failed_policy_retry_disabled")
          : undefined,
        onClick: handleRetryFailedPhase,
      }
    : null;
  const curatorReadiness = resolveCuratorReadiness({
    currentState,
    scoutCompleted,
    blockedPhase: failedPhase,
  });
  const curatorDisabledReason = curatorReadiness.reasonKey
    ? t(curatorReadiness.reasonKey, curatorReadiness.reasonValues)
    : undefined;

  const phaseActions = [
    currentState === "DOMAIN_LOADED" && run?.mode === "express"
      ? {
          key: "express-start",
          label: isStartingProposal
            ? t("phase.express.starting")
            : t("phase.express.start"),
          disabled: isStartingProposal,
          onClick: () => handleRunProposal(""),
        }
      : null,
    currentState === "EXPRESS_FAILED" && run?.mode === "express"
      ? {
          key: "express-regenerate",
          label: isStartingProposal
            ? t("phase.express.starting")
            : t("phase.express.regenerate"),
          disabled: isStartingProposal,
          onClick: () => handleRunProposal(""),
        }
      : null,
    currentState === "DOMAIN_LOADED" && run?.mode !== "express"
      ? {
          key: "proposal",
          label: isStartingProposal
            ? t("phase.proposal.starting")
            : t("phase.proposal.start"),
          disabled: isStartingProposal,
          onClick: () => handleRunProposal(""),
        }
      : null,
    currentState === "USER_PROPOSAL_REVIEW"
      ? {
          key: "proposal-accept",
          label: isAcceptingProposal
            ? t("phase.scout.starting")
            : t("phase.proposal.accept"),
          disabled: isAcceptingProposal,
          onClick: handleAcceptProposal,
        }
      : null,
    currentState === "USER_SEARCH_REVIEW"
      ? {
          key: "curator",
          label: t("workspace.sources.review.open_search_review"),
          disabled: false,
          disabledReason: !curatorReadiness.canRun
            ? curatorDisabledReason
            : undefined,
          onClick: async () => setActiveSubview("sources"),
        }
      : null,
    currentState === "USER_DEEP_DIVE_REVIEW"
      ? {
          key: "synthesizer",
          label: t("workspace.sources.review.open_deep_review"),
          disabled: false,
          onClick: async () => setActiveSubview("sources"),
        }
      : null,
    // PR-C2.b audit (round 1 #2): framework_lens entry from
    // sidebar. Optional for non-theory papers (skip-direct ideator
    // is fine when no lens inputs); mandatory for theory_article.
    currentState === "USER_FIELD_REVIEW"
      ? {
          // PR-249: normalize sidebar key to hyphen form so the
          // testid (``phase-action-framework-lens``) matches the
          // subview button PR-244 added. Before this PR the
          // sidebar emitted ``phase-action-framework_lens`` (with
          // an underscore) and e2e specs looking for the lens
          // step would fail to find a unified selector.
          key: "framework-lens",
          label: isStartingFrameworkLens
            ? t("phase.framework_lens.starting")
            : t("phase.framework_lens.start"),
          disabled: isStartingFrameworkLens,
          onClick: handleRunFrameworkLens,
        }
      : null,
    // PR-C2.b audit (round 1 #3, #4): ideator visible at both
    // USER_FIELD_REVIEW (lens-skipped path) and USER_LENS_REVIEW
    // (post-lens path). Backend accepts both via
    // IDEATOR_VALID_INPUT_STATES.
    // Codex round-4 #1 (2026-05-03): for paper_mode=theory_article
    // the lens phase is mandatory — hide the skip-direct ideator
    // option at USER_FIELD_REVIEW. Backend start_ideator rejects
    // this case with 409 so visibility must mirror.
    (currentState === "USER_FIELD_REVIEW" &&
      run?.paper_mode !== "theory_article") ||
    currentState === "USER_LENS_REVIEW"
      ? {
          key: "ideator",
          label: isStartingIdeator
            ? t("phase.ideator.starting")
            : t("phase.ideator.start"),
          disabled: isStartingIdeator,
          onClick: handleRunIdeator,
        }
      : null,
    currentState === "USER_NOVELTY_REVIEW"
      ? {
          key: "drafter",
          label: isStartingDrafter
            ? t("phase.drafter.starting")
            : t("phase.drafter.start"),
          // Drafter requires a selected angle. Without one, the
          // backend would FAIL_FIXABLE 11ms after kickoff (guidance:
          // "No selected novelty angle is available for drafting").
          // Disable the button until the user picks an angle card.
          disabled:
            isStartingDrafter || !noveltyBundle?.selected_thesis?.angle_id,
          disabledReason: !noveltyBundle?.selected_thesis?.angle_id
            ? t("phase.drafter.needs_angle")
            : undefined,
          onClick: handleRunDrafter,
        }
      : null,
    currentState === "DRAFTER_RUNNING"
      ? {
          key: "stylist",
          label: isStartingStylist
            ? t("phase.stylist.starting")
            : t("phase.stylist.start"),
          // DRAFTER_RUNNING begins immediately on novelty angle-select
          // but drafter takes 5-10min to write manuscript.md. Without
          // this gate the user can click stylist mid-flight, the
          // backend would FAIL_FIXABLE on missing artifacts (mirrored
          // by start_stylist 409). Require having observed a
          // phase_done(drafter) event in the recent stream — codex
          // preferred this over a strict `lastEvent` match because
          // intermediate events (e.g. unrelated source_progress) may
          // arrive between drafter completion and the click.
          disabled: isStartingStylist || !drafterCompleted,
          disabledReason: !drafterCompleted
            ? t("phase.stylist.needs_drafter_done")
            : undefined,
          onClick: handleRunStylist,
        }
      : null,
    currentState === "USER_REVISION_REVIEW"
      ? {
          key: "critic",
          label: isStartingCritic
            ? t("phase.critic.starting")
            : t("phase.critic.start"),
          disabled: isStartingCritic,
          onClick: handleRunCritic,
        }
      : null,
    // Stage 3.E follow-up: when a phase has failed, the workspace
    // must not hide the actions menu — the user expects to retry
    // directly. We surface a "Retry {phase}" entry that calls
    // rerunPhase on the most recently failed phase. The
    // FailureResolutionBanner (red/amber box) remains as the
    // contextual heavy-info display + alternate trigger; this is
    // the always-visible workspace control.
    currentState === "FAILED_FIXABLE" ||
    currentState === "FAILED_POLICY" ||
    currentState === "FAILED_NEEDS_USER"
      ? failedPhaseAction
      : null,
  ].filter((action): action is PhaseAction => action !== null);

  const workspaceTabs: Array<{
    id: WorkspaceSubview;
    label: string;
    isVisible: boolean;
  }> = [
    { id: "console", label: t("workspace.tab.console"), isVisible: true },
    { id: "corpus", label: t("workspace.tab.corpus"), isVisible: true },
    {
      id: "proposal",
      label: t("workspace.tab.proposal"),
      isVisible: showProposal,
    },
    {
      id: "sources",
      label: t("workspace.tab.sources"),
      isVisible: showSources,
    },
    {
      id: "synthesis",
      label: t("workspace.tab.synthesis"),
      isVisible: showSynthesis,
    },
    // PR-C2.b audit (round 2): always render the lens tab so users
    // discover the framework-lens phase exists. Disabled / pending
    // content lives inside the subview.
    {
      id: "lens",
      label: t("workspace.tab.lens"),
      isVisible: !isExpressRun,
    },
    {
      id: "novelty",
      label: t("workspace.tab.novelty"),
      isVisible: showNovelty,
    },
    { id: "draft", label: t("workspace.tab.draft"), isVisible: showDraft },
    { id: "style", label: t("workspace.tab.style"), isVisible: showStyle },
    { id: "review", label: t("workspace.tab.review"), isVisible: showReview },
    {
      id: "integrity",
      label: t("workspace.tab.integrity"),
      isVisible: showIntegrity,
    },
    { id: "export", label: t("workspace.tab.export"), isVisible: showExport },
  ];

  return (
    <section>
      <div
        className={`fixed inset-y-0 left-0 z-40 w-[min(20rem,calc(100vw-2rem))] bg-white shadow-xl transition duration-200 lg:hidden ${
          isWorkspaceSidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <WorkspaceStatusPanel
          runId={id}
          currentState={currentState}
          lastEvent={lastEvent}
          isLoading={isLoading}
          phaseActions={phaseActions}
          onOpenHistory={() => setIsHistoryModalOpen(true)}
          onOpenKernelEdit={() => setIsKernelEditModalOpen(true)}
          className="h-full overflow-y-auto p-5"
          onClose={() => setIsWorkspaceSidebarOpen(false)}
        />
      </div>
      {isWorkspaceSidebarOpen ? (
        <button
          type="button"
          data-testid="workspace-sidebar-close-overlay"
          className="fixed inset-0 z-30 cursor-default bg-slate-950/25 lg:hidden"
          aria-label={t("workspace.close_details")}
          onClick={() => setIsWorkspaceSidebarOpen(false)}
        />
      ) : null}
      <div
        className="grid gap-6 lg:grid-cols-[18rem_minmax(0,1fr)]"
        data-testid="workspace-root"
        data-run-id={id ?? ""}
        data-run-state={currentState ?? ""}
        data-last-event-type={lastEvent?.event_type ?? ""}
        data-last-event-phase={
          (lastEvent?.payload as { phase?: string } | undefined)?.phase ?? ""
        }
        data-last-event-at={lastEvent?.created_at ?? ""}
      >
        <WorkspaceStatusPanel
          runId={id}
          currentState={currentState}
          lastEvent={lastEvent}
          isLoading={isLoading}
          phaseActions={phaseActions}
          onOpenHistory={() => setIsHistoryModalOpen(true)}
          onOpenKernelEdit={() => setIsKernelEditModalOpen(true)}
          className="hidden rounded-[22px] border border-[#e6e5e0]/80 bg-[rgba(255,255,253,0.93)] p-5 [box-shadow:0_20px_42px_rgba(27,42,34,0.10)] lg:sticky lg:top-24 lg:block"
        />
        <div
          data-testid="workspace-subview-area"
          className="min-w-0 rounded-[22px] border border-[#e6e5e0]/80 bg-[rgba(255,255,253,0.93)] p-5 [box-shadow:0_20px_42px_rgba(27,42,34,0.10)] sm:p-7"
        >
          <div className="mb-6 grid gap-4 sm:flex sm:items-start sm:justify-between">
            <div className="min-w-0">
              <p className={eyebrowClasses}>{t("workspace.eyebrow")}</p>
              {run ? (
                <ProjectTitleEditor
                  run={run}
                  onUpdated={(updated) => setRun(updated)}
                />
              ) : (
                <h1 className={h1Classes}>{t("workspace.heading_default")}</h1>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-600">
                {run ? (
                  <PaperLanguageEditor
                    run={run}
                    onUpdated={(updated) => setRun(updated)}
                  />
                ) : null}
                {run ? <GenerationModeBadge mode={run.mode} /> : null}
                {run ? (
                  <span className="text-xs text-slate-500">
                    {run.domain_id || ""}
                  </span>
                ) : null}
                {/* PR-388: surface ``auto_advance`` in the header so
                    the user can see at a glance whether the run is
                    in auto-pilot mode, without opening the style
                    sidebar. Hidden when the toggle is off. */}
                {run?.auto_advance ? (
                  <span
                    data-testid="workspace-auto-pilot-badge"
                    className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-bold text-emerald-800"
                    title={t("auto_pilot.tooltip")}
                  >
                    <span aria-hidden>🤖</span>
                    {t("auto_pilot.badge")}
                  </span>
                ) : null}
                <span className="text-xs text-slate-400">
                  {id ?? t("workspace.unknown")}
                </span>
                {/* PR-376: hide the branch dropdown when only the
                    auto-created "main" branch exists — a 1-option
                    selector is UI noise. The ``ForkBranchButton`` (in
                    the phase-version sidebar) is the only entry point
                    that creates additional branches, so the dropdown
                    starts being useful at length >= 2. */}
                {branchList && branchList.branches.length > 1 ? (
                  <BranchSwitcher
                    list={branchList}
                    onSwitch={(branchId) => void handleSwitchBranch(branchId)}
                  />
                ) : null}
              </div>
            </div>
            <button
              type="button"
              data-testid="workspace-sidebar-toggle"
              className={primaryButtonClasses + " lg:hidden"}
              onClick={() => setIsWorkspaceSidebarOpen(true)}
            >
              {t("workspace.state_and_checkpoints")}
            </button>
          </div>
          {restoreRecoveryEvent ? (
            <div
              data-testid="workspace-restore-recovery-banner"
              data-phase={restoreRecoveryPhase}
              className="mb-6 grid gap-2 rounded-[18px] border border-amber-300 bg-amber-50 p-5 text-amber-950"
            >
              <p
                data-testid="workspace-restore-recovery-title"
                className="text-sm font-bold"
              >
                {t("workspace.restore_recovery.title")}
              </p>
              <p
                data-testid="workspace-restore-recovery-body"
                className="text-sm leading-7"
              >
                {t("workspace.restore_recovery.body").replace(
                  "{phase}",
                  restoreRecoveryPhase,
                )}
              </p>
            </div>
          ) : null}
          {run?.stale_from_phase ? (
            <StaleBanner
              runId={id ?? ""}
              stalePhase={run.stale_from_phase}
              runState={currentState}
              activePhaseLock={run.active_phase_lock ?? null}
              onRefreshed={() => {
                if (id) {
                  void getRun(id)
                    .then(setRun)
                    .catch(() => undefined);
                }
              }}
            />
          ) : null}
          {currentState && id && FAILURE_STATES.has(currentState) ? (
            <FailureResolutionBanner
              runId={id}
              state={currentState as FailureState}
              lastEvent={lastEvent}
              forceApproveHint={run?.force_approve ?? null}
              onNavigateToPhase={(phase) => {
                const subview = phaseToSubview(phase);
                if (subview) setActiveSubview(subview);
              }}
              onRefreshed={() => {
                if (id) {
                  void getRun(id)
                    .then(setRun)
                    .catch(() => undefined);
                  setBundleRefreshTick((tick) => tick + 1);
                }
              }}
            />
          ) : null}
          {currentState && id && isRunningState(currentState) && run ? (
            <StuckRunBanner
              runId={id}
              currentState={currentState}
              recentEvents={recentEvents}
              activePhaseLockClaimedAt={
                run.active_phase_lock?.claimed_at ?? null
              }
              runUpdatedAt={run.updated_at}
              onRecovered={() => {
                void getRun(id)
                  .then(setRun)
                  .catch(() => undefined);
                setBundleRefreshTick((tick) => tick + 1);
              }}
            />
          ) : null}
          {(() => {
            const sev = (
              lastEvent?.payload as { severity?: string } | undefined
            )?.severity;
            if (
              currentState !== "FAILED_FIXABLE" &&
              (sev === "amber_minor" || sev === "amber_major")
            ) {
              return (
                <DegradedDraftBanner
                  severity={sev}
                  payload={
                    lastEvent?.payload as Record<string, unknown> | undefined
                  }
                />
              );
            }
            return null;
          })()}
          {currentState === "EXPORTS_DONE" ? (
            <div className="mb-6 grid gap-3 rounded-[18px] border border-[#245d49]/40 bg-[#dceede]/60 p-5 sm:p-6">
              <p className="text-sm font-bold text-[#1c4e3c]">
                {t("workspace.exports_done_banner")}
              </p>
              <div className="flex flex-wrap gap-2">
                <Link
                  to="/"
                  data-testid="workspace-back-to-runs-link"
                  className="inline-flex min-h-11 items-center justify-center rounded bg-[linear-gradient(180deg,#2e7659_0%,#28674f_100%)] px-4 py-2 text-sm font-bold text-white no-underline transition [box-shadow:inset_0_-2px_0_rgba(12,41,31,0.18)] hover:brightness-105"
                >
                  {t("workspace.back_to_runs")}
                </Link>
                <Link
                  to="/runs/new"
                  data-testid="workspace-start-new-project-link"
                  className="inline-flex min-h-11 items-center justify-center rounded border border-[#e6e5e0] bg-white px-4 py-2 text-sm font-bold text-[#245d49] no-underline transition hover:bg-[#f0ece1]"
                >
                  {t("workspace.start_new_project")}
                </Link>
              </div>
            </div>
          ) : null}
          {error ? <p className="leading-7 text-red-700">{error}</p> : null}
          {streamError && currentState !== "EXPORTS_DONE" ? (
            <p className="leading-7 text-red-700">
              {t("workspace.stream_disconnected")}
            </p>
          ) : null}
          {run?.mode === "express" ? (
            <ExpressTransparencyPanel
              run={run}
              refreshKey={`${events.length}:${currentState ?? ""}:${bundleRefreshTick}`}
              onRegenerateExpress={() => handleCreateModeRun("express")}
              onStartDeep={() => handleCreateModeRun("deep")}
              isRegenerating={isCreatingModeRun === "express"}
              isStartingDeep={isCreatingModeRun === "deep"}
            />
          ) : null}
          {/* Tab strip is always rendered: ``console`` + ``corpus``
              (PR-B3) are always-visible, so we no longer gate on
              the phase-specific show* flags. */}
          <div
            className="my-6 flex snap-x gap-2 overflow-x-auto pb-2 md:max-w-full md:flex-wrap md:overflow-x-visible md:rounded-[14px] md:border md:border-[#e6e5e0] md:bg-[rgba(255,255,253,0.7)] md:p-1.5 md:pb-1.5"
            role="tablist"
            aria-label={t("workspace.tablist_label")}
          >
            {workspaceTabs
              .filter((tab) => tab.isVisible)
              .map((tab) => (
                <button
                  type="button"
                  data-testid={`workspace-tab-${tab.id}`}
                  data-active={activeSubview === tab.id ? "true" : "false"}
                  className={tabButtonClasses(activeSubview === tab.id)}
                  onClick={() => setActiveSubview(tab.id)}
                  key={tab.id}
                >
                  {tab.label}
                </button>
              ))}
          </div>
          {id &&
          subviewToEditablePhase(activeSubview) &&
          !isRunningState(currentState) ? (
            <div className="mb-4">
              <button
                type="button"
                data-testid={`edit-content-button-${activeSubview}`}
                className={secondaryButtonClasses + " sm:w-auto"}
                onClick={() => setIsEditContentModalOpen(true)}
              >
                {t("workspace.edit_content.button")}
              </button>
            </div>
          ) : null}
          {activeSubview === "corpus" ? (
            run?.project_id ? (
              <CorpusSubview
                projectId={run.project_id}
                hasDraftRun={Boolean(run?.stale_from_phase) || showDraft}
              />
            ) : null
          ) : null}
          {activeSubview === "console" ? (
            <>
              {showDiscovery ? (
                <section className={sectionClasses}>
                  <h2 className={h2Classes}>
                    {t("workspace.console.scout_progress")}
                  </h2>
                  {sourceProgress.length > 0 ? (
                    <ul className={cardListClasses}>
                      {sourceProgress.map((event) => (
                        <li
                          className={
                            infoCardClasses + " sm:flex sm:justify-between"
                          }
                          key={event.id}
                        >
                          <strong>
                            {String(
                              event.payload.source_id ??
                                t("workspace.common.source_default"),
                            )}
                          </strong>
                          <span>
                            {String(
                              event.payload.status ??
                                t("workspace.common.status_pending"),
                            )}
                          </span>
                          <span>
                            {Number(event.payload.count ?? 0)}{" "}
                            {t("workspace.common.results_suffix")}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="leading-7 text-slate-700">
                      {t("workspace.console.no_source_progress")}
                    </p>
                  )}
                  <h2 className={h2Classes}>
                    {t("workspace.console.scout_report")}
                  </h2>
                  <pre className={reportPreClasses}>
                    {discovery?.scout_report ||
                      t("workspace.console.report_pending")}
                  </pre>
                </section>
              ) : null}
              {currentState === "CURATOR_RUNNING" ? (
                <section className={sectionClasses}>
                  <h2 className={h2Classes}>
                    {t("workspace.console.curator_progress")}
                  </h2>
                  {curatorProgress.length > 0 ? (
                    <ul className={cardListClasses}>
                      {curatorProgress.map((event) => (
                        <li
                          className={
                            infoCardClasses + " sm:flex sm:justify-between"
                          }
                          key={event.id}
                        >
                          <strong>
                            {String(
                              event.payload.source_id ??
                                t("workspace.common.source_default"),
                            )}
                          </strong>
                          <span>
                            {String(
                              event.payload.status ??
                                t("workspace.common.status_pending"),
                            )}
                          </span>
                          <span>
                            {Number(event.payload.completed ?? 0)} /{" "}
                            {Number(event.payload.total ?? 0)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="leading-7 text-slate-700">
                      {t("workspace.console.no_curator_progress")}
                    </p>
                  )}
                </section>
              ) : null}
              <h2 className={h2Classes}>{t("workspace.recent_events")}</h2>
              <div
                className="my-3 inline-flex gap-2 rounded-lg border border-slate-200 bg-slate-50 p-1"
                role="tablist"
                aria-label={t("workspace.recent_events")}
              >
                <button
                  type="button"
                  data-testid="console-subtab-timeline"
                  data-active={consoleSubtab === "timeline" ? "true" : "false"}
                  className={tabButtonClasses(consoleSubtab === "timeline")}
                  onClick={() => setConsoleSubtab("timeline")}
                  role="tab"
                  aria-selected={consoleSubtab === "timeline"}
                >
                  {t("workspace.console.subtab.timeline")}
                </button>
                <button
                  type="button"
                  data-testid="console-subtab-system"
                  data-active={consoleSubtab === "system" ? "true" : "false"}
                  className={tabButtonClasses(consoleSubtab === "system")}
                  onClick={() => setConsoleSubtab("system")}
                  role="tab"
                  aria-selected={consoleSubtab === "system"}
                >
                  {t("workspace.console.subtab.system_output")}
                </button>
              </div>
              {recentEvents.length === 0 ? (
                <p className="leading-7 text-slate-700">
                  {t("workspace.console.no_events")}
                </p>
              ) : consoleSubtab === "timeline" ? (
                <ul
                  className={cardListClasses}
                  data-testid="console-timeline-list"
                >
                  {recentEvents.map((event) => (
                    <li
                      className={
                        infoCardClasses + " sm:flex sm:justify-between sm:gap-3"
                      }
                      key={event.id}
                    >
                      <span className="leading-7 text-slate-800">
                        {describeEvent(t, event)}
                      </span>
                      <time
                        className="font-mono text-xs text-slate-500"
                        dateTime={event.created_at}
                      >
                        {formatEventTime(event.created_at)}
                      </time>
                    </li>
                  ))}
                </ul>
              ) : (
                <ul
                  className={cardListClasses}
                  data-testid="console-system-output-list"
                >
                  {recentEvents.map((event) => (
                    <li className={infoCardClasses} key={event.id}>
                      <strong>{event.event_type}</strong>
                      <pre className={reportPreClasses}>
                        {JSON.stringify(event.payload, null, 2)}
                      </pre>
                    </li>
                  ))}
                </ul>
              )}
            </>
          ) : null}
          {activeSubview === "proposal" && !isExpressRun ? (
            <ProposalSubview
              currentState={currentState}
              proposalBundle={proposalBundle}
              proposalMissing={proposalMissing}
              progress={proposalProgress}
              isStartingProposal={isStartingProposal}
              isSavingProposal={isSavingProposal}
              isAcceptingProposal={isAcceptingProposal}
              onGenerate={handleRunProposal}
              onRegenerate={handleRunProposal}
              onSave={handleSaveProposal}
              onAccept={handleAcceptProposal}
            />
          ) : null}
          {activeSubview === "sources" ? (
            <SourcesSubview
              runId={id}
              currentState={currentState}
              skimCandidates={skimCandidates}
              shortlist={sourcesBundle?.shortlist ?? []}
              manifest={sourcesBundle?.fulltext_manifest ?? {}}
              manualRequests={sourcesBundle?.manual_upload_requests ?? []}
              curationReport={sourcesBundle?.curation_report ?? ""}
              sourceQualityCounts={sourcesBundle?.source_quality_counts ?? {}}
              isStartingCurator={isStartingCurator}
              isStartingSynthesizer={isStartingSynthesizer}
              isUploadingPdf={isUploadingPdf}
              blockedPhase={failedPhase}
              onRunCurator={handleRunCurator}
              onRunSynthesizer={handleRunSynthesizer}
              onUploadPdf={handleUploadPdf}
              onRefresh={() => setBundleRefreshTick((tick) => tick + 1)}
              synthesisArtifactPresent={Boolean(synthesisBundle?.dual_track)}
              scoutCompleted={scoutCompleted}
              curatorCompleted={curatorCompleted}
              scoutProgress={sourceProgress}
              curatorProgress={curatorProgress}
            />
          ) : null}
          {activeSubview === "synthesis" ? (
            <SynthesisSubview
              runId={id}
              currentState={currentState}
              paperMode={run?.paper_mode}
              synthesisBundle={synthesisBundle}
              progress={synthesizerProgress}
              isStartingSynthesizer={isStartingSynthesizer}
              isStartingFrameworkLens={isStartingFrameworkLens}
              isStartingIdeator={isStartingIdeator}
              onRunSynthesizer={handleRunSynthesizer}
              onRunFrameworkLens={handleRunFrameworkLens}
              onRunIdeator={handleRunIdeator}
              curatorCompleted={curatorCompleted}
              synthesizerCompleted={synthesizerCompleted}
            />
          ) : null}
          {activeSubview === "lens" ? (
            <FrameworkLensSubview
              runId={id}
              currentState={currentState}
              paperMode={run?.paper_mode}
              frameworkLensBundle={frameworkLensBundle}
              isStartingFrameworkLens={isStartingFrameworkLens}
              isStartingIdeator={isStartingIdeator}
              onRunFrameworkLens={handleRunFrameworkLens}
              onRunIdeator={handleRunIdeator}
              synthesizerCompleted={synthesizerCompleted}
            />
          ) : null}
          {activeSubview === "novelty" ? (
            <NoveltySubview
              currentState={currentState}
              paperMode={run?.paper_mode}
              noveltyBundle={noveltyBundle}
              discussion={noveltyDiscussion}
              isStartingIdeator={isStartingIdeator}
              isSelectingAngle={isSelectingAngle}
              isDiscussing={isDiscussingNovelty}
              onRunIdeator={handleRunIdeator}
              onSelectAngle={handleSelectAngle}
              onDiscuss={handleDiscussNovelty}
            />
          ) : null}
          {activeSubview === "draft" ? (
            <DraftSubview
              currentState={currentState}
              draftList={draftList}
              activeDraft={activeDraft}
              progress={drafterProgress}
              drafterCompleted={drafterCompleted}
            />
          ) : null}
          {activeSubview === "style" ? (
            <StyleSubview
              currentState={currentState}
              styleBundle={styleBundle}
              progress={stylistProgress}
              isStartingStylist={isStartingStylist}
              isStartingCritic={isStartingCritic}
              onRunStylist={handleRunStylist}
              onRunCritic={handleRunCritic}
              drafterCompleted={drafterCompleted}
              stylistCompleted={stylistCompleted}
              mathematicalMode={Boolean(run?.mathematical_mode)}
              onToggleMathematicalMode={handleToggleMathematicalMode}
              autoAdvance={Boolean(run?.auto_advance)}
              onToggleAutoAdvance={handleToggleAutoAdvance}
            />
          ) : null}
          {activeSubview === "review" ? (
            <ReviewSubview
              currentState={currentState}
              criticBundle={criticBundle}
              isStartingCritic={isStartingCritic}
              isStartingIntegrity={isStartingIntegrity}
              onRunCritic={handleRunCritic}
              onApproveExternalScan={handleApproveExternalScan}
              onSkipExternalScan={handleSkipExternalScan}
              stylistCompleted={stylistCompleted}
              criticCompleted={criticCompleted}
            />
          ) : null}
          {activeSubview === "integrity" ? (
            <IntegritySubview
              currentState={currentState}
              integrityBundle={integrityBundle}
              integrityCompleted={integrityCompleted}
              manuscript={
                styleBundle?.paper_styled ?? activeDraft?.manuscript ?? ""
              }
              onAcceptIntegrity={handleAcceptIntegrity}
              onRequestRevision={handleRequestIntegrityRevision}
            />
          ) : null}
          {activeSubview === "export" ? (
            <ExportSubview
              currentState={currentState}
              exportsBundle={exportsBundle}
              isStartingExports={isStartingExports}
              onAcceptFinalDraft={handleAcceptFinalDraft}
              onRunExports={handleRunExports}
              criticCompleted={criticCompleted}
              integrityCompleted={integrityCompleted}
            />
          ) : null}
        </div>
      </div>
      {isHistoryModalOpen && id ? (
        <PhaseHistoryModal
          runId={id}
          lastEvent={lastEvent}
          runState={currentState}
          onClose={() => setIsHistoryModalOpen(false)}
          onActivated={() => {
            if (id) {
              void getRun(id)
                .then(setRun)
                .catch(() => undefined);
            }
            // Bump the bundle-refresh tick so every phase view reloads
            // its data from the freshly-restored legacy paths.
            setBundleRefreshTick((tick) => tick + 1);
          }}
          onForked={() => {
            // Forking creates a new branch but does NOT switch to it
            // (the user's intent is "fork from this version"; switching
            // is a separate action). Just refresh the branch list so the
            // dropdown shows the new branch, and let the user pick.
            setBundleRefreshTick((tick) => tick + 1);
          }}
        />
      ) : null}
      {isKernelEditModalOpen && id && run ? (
        <KernelEditModal
          run={run}
          onClose={() => setIsKernelEditModalOpen(false)}
          onSaved={(refreshed) => {
            setRun(refreshed);
            setShowKernelRepairBanner(false);
            setBundleRefreshTick((tick) => tick + 1);
            setIsKernelEditModalOpen(false);
          }}
        />
      ) : null}
      {showKernelRepairBanner && !isKernelEditModalOpen ? (
        <div
          data-testid="kernel-repair-banner"
          className="fixed inset-x-0 top-0 z-30 border-b border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 shadow-sm"
        >
          <p>{t("workspace.kernel.repair_banner")}</p>
          <button
            type="button"
            data-testid="kernel-repair-banner-open"
            className={primaryButtonClasses + " mt-2"}
            onClick={() => setIsKernelEditModalOpen(true)}
          >
            {t("workspace.kernel.edit_button")}
          </button>
          <button
            type="button"
            data-testid="kernel-repair-banner-dismiss"
            className={secondaryButtonClasses + " mt-2 ml-2"}
            onClick={() => setShowKernelRepairBanner(false)}
          >
            {t("workspace.kernel.repair_banner_dismiss")}
          </button>
        </div>
      ) : null}
      {isEditContentModalOpen && id && subviewToEditablePhase(activeSubview) ? (
        <EditPhaseContentModal
          runId={id}
          phase={subviewToEditablePhase(activeSubview)!}
          onClose={() => setIsEditContentModalOpen(false)}
          onSaved={() => {
            setIsEditContentModalOpen(false);
            setBundleRefreshTick((tick) => tick + 1);
            if (id) {
              void getRun(id)
                .then(setRun)
                .catch(() => undefined);
            }
          }}
        />
      ) : null}
    </section>
  );
}

/**
 * Generic per-phase artifact editor modal. Loads the editable
 * artifacts via `GET /api/runs/{id}/phases/{phase}/editable`,
 * shows one textarea per artifact, and submits via
 * `PUT /api/runs/{id}/phases/{phase}/edit` with the
 * server-provided `base_version_id` for optimistic concurrency.
 *
 * Per codex amendment 7, drafter's `manuscript.md` and
 * `claim_map.jsonl` cross-reference each other. The modal does not
 * partial-validate this client-side (the backend rejects with 400
 * if only one is sent), but the registry's `required_with`
 * annotation is surfaced as a hint under the textarea so the user
 * sees the dependency before they save.
 */
/**
 * Normalize on-disk content for in-modal display so non-ASCII
 * (esp. CJK) shows up as readable characters instead of
 * ``\uXXXX`` escapes. Old prod runs were written when the
 * backend's ``json.dumps`` defaulted to ``ensure_ascii=True``,
 * so their JSON files store CJK as escapes — codex amendment 4
 * (2026-05-01) requires we normalize at display time and DIFF
 * against the displayed baseline, not the raw on-disk text, so
 * just opening + saving an old file does not create a false
 * "user edited" version.
 */
function normalizeContentForDisplay(path: string, raw: string): string {
  if (path.endsWith(".json")) {
    try {
      // ``JSON.stringify`` does NOT escape non-ASCII by default,
      // so re-stringifying after parse yields the same structure
      // with real characters in place of ``\uXXXX``.
      return JSON.stringify(JSON.parse(raw), null, 2) + "\n";
    } catch {
      return raw;
    }
  }
  if (path.endsWith(".jsonl")) {
    // JSONL must stay one-record-per-line (codex amendment 5);
    // pretty-printing would break downstream readers.
    const lines = raw.split("\n");
    const out: string[] = [];
    let allParsed = true;
    for (const line of lines) {
      if (line === "") {
        out.push(line);
        continue;
      }
      try {
        out.push(JSON.stringify(JSON.parse(line)));
      } catch {
        allParsed = false;
        break;
      }
    }
    return allParsed ? out.join("\n") : raw;
  }
  return raw;
}

/**
 * Validate that a JSON / JSONL textarea draft still parses before
 * we PUT it. Returns null if valid, or a localized error string.
 */
function validateContentForSave(path: string, value: string): string | null {
  if (path.endsWith(".json")) {
    try {
      JSON.parse(value);
      return null;
    } catch (caught) {
      return `${path}: ${caught instanceof Error ? caught.message : String(caught)}`;
    }
  }
  if (path.endsWith(".jsonl")) {
    const lines = value.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line === "") continue;
      try {
        JSON.parse(line);
      } catch (caught) {
        return `${path}:${i + 1}: ${caught instanceof Error ? caught.message : String(caught)}`;
      }
    }
    return null;
  }
  return null;
}

function EditPhaseContentModal({
  runId,
  phase,
  onClose,
  onSaved,
}: {
  runId: string;
  phase: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT();
  const [entries, setEntries] = useState<PhaseEditableEntry[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  // The "displayed baseline" — what the user sees in the textarea
  // when they open the modal. We diff drafts against THIS, not
  // ``entry.current_content``, so re-saving an unedited modal
  // doesn't churn an old escaped file into a fake user-edit.
  const [displayedBaseline, setDisplayedBaseline] = useState<
    Record<string, string>
  >({});
  const [baseVersionId, setBaseVersionId] = useState<string | null>(null);
  const [replaceEligible, setReplaceEligible] = useState(false);
  // PR-A3: when replaceEligible is true, default to "replace" per
  // codex AGREE 2026-05-01 question 3. The user can switch to
  // "new" via radio. When replaceEligible is false, stuck on "new".
  const [saveMode, setSaveMode] = useState<"replace" | "new">("new");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    listEditableArtifacts(runId, phase)
      .then((response) => {
        if (cancelled) return;
        setEntries(response.entries);
        setBaseVersionId(response.base_version_id);
        const eligible = response.replace_eligible === true;
        setReplaceEligible(eligible);
        setSaveMode(eligible ? "replace" : "new");
        const initial: Record<string, string> = {};
        const baseline: Record<string, string> = {};
        for (const entry of response.entries) {
          const display = normalizeContentForDisplay(
            entry.path,
            entry.current_content,
          );
          initial[entry.path] = display;
          baseline[entry.path] = display;
        }
        setDrafts(initial);
        setDisplayedBaseline(baseline);
      })
      .catch((caught) => {
        if (cancelled) return;
        setError(caught instanceof Error ? caught.message : String(caught));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, phase]);

  async function handleSave() {
    if (!entries || entries.length === 0) return;
    setIsSaving(true);
    setError(null);
    // Only send files whose content has actually changed —
    // diffing against the DISPLAYED baseline, not the raw
    // backend ``current_content``. Without this, a CJK-bearing
    // file that was originally written with ``ensure_ascii=True``
    // would appear "changed" the moment we display the readable
    // form, even when the user typed nothing.
    const changed: Record<string, string> = {};
    for (const entry of entries) {
      if (drafts[entry.path] !== displayedBaseline[entry.path]) {
        const validationError = validateContentForSave(
          entry.path,
          drafts[entry.path],
        );
        if (validationError !== null) {
          setError(validationError);
          setIsSaving(false);
          return;
        }
        changed[entry.path] = drafts[entry.path];
      }
    }
    if (Object.keys(changed).length === 0) {
      onClose();
      return;
    }
    // Codex audit (2026-05-01): the drafter backend rule requires
    // ``manuscript.md`` and ``claim_map.jsonl`` to move together.
    // If the user only edited one side, auto-bundle the unchanged
    // partner so the backend's pair check passes — without this a
    // typo-only manuscript edit 400s with "claim_map must move with
    // manuscript". Use the displayed baseline (post CJK
    // normalization) so the bundled-along payload also stays in
    // its readable, written-out form.
    const byPath = new Map(entries.map((e) => [e.path, e]));
    for (const path of Object.keys(changed)) {
      const partner = byPath.get(path)?.required_with;
      if (partner && !(partner in changed)) {
        if (partner in drafts) {
          changed[partner] =
            drafts[partner] ?? displayedBaseline[partner] ?? "";
        }
      }
    }
    try {
      await editPhaseArtifacts(runId, phase, {
        base_version_id: baseVersionId,
        files: changed,
        mode: saveMode,
      });
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4"
      data-testid="edit-content-modal"
      data-phase={phase}
    >
      <section className="grid max-h-[90vh] w-full max-w-3xl grid-rows-[auto_1fr_auto] rounded-lg bg-white shadow-xl">
        <div className="border-b border-slate-200 p-5">
          <h2 className="text-xl font-bold text-slate-950">
            {t("workspace.edit_content.title")}
          </h2>
          <p className="mt-2 text-sm leading-6 text-slate-700">
            {t("workspace.edit_content.description")}
          </p>
        </div>
        <div className="overflow-y-auto p-5">
          {isLoading ? (
            <p className="leading-7 text-slate-700">{t("workspace.loading")}</p>
          ) : error ? (
            <p className="rounded-md bg-red-50 px-4 py-3 text-red-700">
              {error}
            </p>
          ) : entries && entries.length > 0 ? (
            <div className="grid gap-5">
              {/* PR-A3 replace/new toggle. Shown when the server
                  reports replace_eligible; otherwise rendered as a
                  one-line "downstream forces new version" notice. */}
              {replaceEligible ? (
                <fieldset
                  className="rounded-md border border-slate-200 p-3"
                  data-testid="edit-content-mode-fieldset"
                >
                  <legend className="px-1 text-xs font-bold text-slate-700">
                    {t("workspace.edit_content.mode_heading")}
                  </legend>
                  <label className="flex items-start gap-2 py-1">
                    <input
                      type="radio"
                      data-testid="edit-content-mode-replace"
                      name="edit-content-save-mode"
                      checked={saveMode === "replace"}
                      onChange={() => setSaveMode("replace")}
                      disabled={isSaving}
                      className="mt-1"
                    />
                    <span>
                      <span className="text-sm font-semibold text-slate-950">
                        {t("workspace.edit_content.mode.replace")}
                      </span>
                      <span className="ml-2 text-xs text-slate-600">
                        {t("workspace.edit_content.mode.replace_hint")}
                      </span>
                    </span>
                  </label>
                  <label className="flex items-start gap-2 py-1">
                    <input
                      type="radio"
                      data-testid="edit-content-mode-new"
                      name="edit-content-save-mode"
                      checked={saveMode === "new"}
                      onChange={() => setSaveMode("new")}
                      disabled={isSaving}
                      className="mt-1"
                    />
                    <span>
                      <span className="text-sm font-semibold text-slate-950">
                        {t("workspace.edit_content.mode.new")}
                      </span>
                      <span className="ml-2 text-xs text-slate-600">
                        {t("workspace.edit_content.mode.new_hint")}
                      </span>
                    </span>
                  </label>
                </fieldset>
              ) : (
                <p
                  className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-700"
                  data-testid="edit-content-mode-forced-new"
                >
                  {t("workspace.edit_content.mode.new_forced")}
                </p>
              )}
              {entries.map((entry) => (
                <div key={entry.path} className="grid gap-1">
                  <label className="text-sm font-bold text-slate-900">
                    <code className="font-mono">{entry.path}</code>
                    <span className="ml-2 text-xs font-normal text-slate-500">
                      ({entry.kind})
                    </span>
                  </label>
                  {entry.required_with ? (
                    <p className="text-xs text-amber-700">
                      {t("workspace.edit_content.required_with_hint").replace(
                        /\{path\}/g,
                        entry.required_with,
                      )}
                    </p>
                  ) : null}
                  <textarea
                    data-testid={`edit-content-textarea-${entry.path}`}
                    className="min-h-48 w-full rounded-md border border-slate-300 p-3 font-mono text-sm text-slate-900 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20"
                    value={drafts[entry.path] ?? ""}
                    onChange={(event) =>
                      setDrafts((current) => ({
                        ...current,
                        [entry.path]: event.target.value,
                      }))
                    }
                  />
                </div>
              ))}
            </div>
          ) : (
            <p className="leading-7 text-slate-700">
              {t("workspace.edit_content.empty")}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-slate-200 p-4">
          <button
            type="button"
            data-testid="edit-content-cancel"
            className={secondaryButtonClasses}
            onClick={onClose}
            disabled={isSaving}
          >
            {t("workspace.edit_content.cancel")}
          </button>
          <button
            type="button"
            data-testid="edit-content-save"
            className={primaryButtonClasses}
            onClick={() => void handleSave()}
            disabled={isSaving || isLoading || !entries || entries.length === 0}
          >
            {isSaving
              ? t("workspace.edit_content.saving")
              : t("workspace.edit_content.save")}
          </button>
        </div>
      </section>
    </div>
  );
}

function SourceRerunConfirmDialog({
  impact,
  onCancel,
  onConfirm,
}: {
  impact: SourceRerunImpact;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useT();
  const body = t("workspace.source_rerun_confirm.body")
    .replace(/\{phase\}/g, t(`phase.${impact.phase}`))
    .replace(/\{skim\}/g, String(impact.skimCandidates))
    .replace(/\{shortlist\}/g, String(impact.shortlist))
    .replace(/\{manual\}/g, String(impact.manualRequests))
    .replace(/\{downstream\}/g, String(impact.downstreamGenerated))
    .replace(/\{uploads\}/g, String(impact.userUploads));
  return (
    <div
      className="fixed inset-0 z-[60] grid place-items-center bg-slate-950/45 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="source-rerun-confirm-title"
      data-testid="source-rerun-confirm-dialog"
    >
      <section className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl">
        <h2
          id="source-rerun-confirm-title"
          className="text-lg font-bold text-slate-950"
          data-testid="source-rerun-confirm-title"
        >
          {t("workspace.source_rerun_confirm.title").replace(
            /\{phase\}/g,
            t(`phase.${impact.phase}`),
          )}
        </h2>
        <p
          className="mt-3 whitespace-pre-line text-sm leading-6 text-slate-700"
          data-testid="source-rerun-confirm-body"
        >
          {body}
        </p>
        <p
          className="mt-3 text-sm font-semibold text-emerald-800"
          data-testid="source-rerun-confirm-uploads-note"
        >
          {t("workspace.source_rerun_confirm.uploads_retained").replace(
            /\{uploads\}/g,
            String(impact.userUploads),
          )}
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            className={secondaryButtonClasses}
            data-testid="source-rerun-confirm-cancel"
            onClick={onCancel}
          >
            {t("workspace.source_rerun_confirm.cancel")}
          </button>
          <button
            type="button"
            className={primaryButtonClasses}
            data-testid="source-rerun-confirm-submit"
            onClick={onConfirm}
          >
            {t("workspace.source_rerun_confirm.submit")}
          </button>
        </div>
      </section>
    </div>
  );
}

function StaleBanner({
  runId,
  stalePhase,
  runState,
  activePhaseLock,
  onRefreshed,
}: {
  runId: string;
  stalePhase: string;
  // PR-I4.b A9: passed through to PhasePromptModal so the prompt
  // sub-modal can disable Save / Save-and-rerun while a phase is
  // running.
  runState: string | undefined;
  // P0 fix (codex state-machine audit §1.1 A): when the stale
  // phase is currently being refreshed (active_phase_lock claimed
  // for the same phase), the "请刷新" wording is misleading —
  // user already triggered the refresh. Switch the banner copy to
  // "正在刷新中" and disable the rerun button instead of letting
  // the user click and hit ``409 another phase is currently
  // running``.
  activePhaseLock: ActivePhaseLock | null;
  onRefreshed: () => void;
}) {
  const t = useT();
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isPromptModalOpen, setIsPromptModalOpen] = useState(false);
  const [pendingSourceRerun, setPendingSourceRerun] =
    useState<SourceRerunImpact | null>(null);
  const promptEditable = PROMPT_EDITABLE_PHASES.has(stalePhase);
  // P0: detect "the stale phase IS the phase currently running".
  const lockBlocksRerun =
    activePhaseLock !== null &&
    typeof activePhaseLock.phase === "string" &&
    activePhaseLock.phase === stalePhase;

  async function handleRerun() {
    if (!runId || isRunning) return;
    if (sourceRerunNeedsConfirm(stalePhase)) {
      try {
        const sources = await getSources(runId);
        setPendingSourceRerun(buildSourceRerunImpact(stalePhase, sources));
      } catch {
        setPendingSourceRerun(buildSourceRerunImpact(stalePhase, null));
      }
      return;
    }
    await executeRerun();
  }

  async function executeRerun() {
    if (!runId || isRunning) return;
    setIsRunning(true);
    setError(null);
    try {
      await rerunPhase(runId, stalePhase);
      onRefreshed();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("workspace.rerun_failed")}: ${caught.message}`
          : t("workspace.rerun_failed"),
      );
    } finally {
      setIsRunning(false);
    }
  }

  return (
    <div className="mb-6 grid gap-3 rounded-lg border-2 border-amber-400 bg-amber-50 p-4 sm:p-5">
      {pendingSourceRerun ? (
        <SourceRerunConfirmDialog
          impact={pendingSourceRerun}
          onCancel={() => setPendingSourceRerun(null)}
          onConfirm={() => {
            setPendingSourceRerun(null);
            void executeRerun();
          }}
        />
      ) : null}
      <p className="text-sm font-bold text-amber-900">
        {lockBlocksRerun
          ? t("workspace.stale_banner_running_title")
          : t("workspace.stale_banner_title")}
      </p>
      <p className="text-sm leading-6 text-amber-900">
        {lockBlocksRerun
          ? t("workspace.stale_banner_running_body").replace(
              /\{phase\}/g,
              stalePhase,
            )
          : t("workspace.stale_banner_body").replace(/\{phase\}/g, stalePhase)}
      </p>
      <div
        className="flex flex-wrap items-center gap-2"
        data-testid="stale-banner"
        data-stale-phase={stalePhase}
        data-lock-blocks-rerun={lockBlocksRerun ? "true" : "false"}
      >
        <button
          type="button"
          data-testid="stale-rerun"
          disabled={isRunning || lockBlocksRerun}
          onClick={() => void handleRerun()}
          className="inline-flex min-h-10 items-center rounded bg-[#114b5f] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#0d3d4d] disabled:opacity-60"
        >
          {lockBlocksRerun
            ? t("workspace.rerun_running")
            : isRunning
              ? t("workspace.rerun_running")
              : t("workspace.rerun_phase").replace(/\{phase\}/g, stalePhase)}
        </button>
        {promptEditable ? (
          <button
            type="button"
            data-testid="stale-edit-prompt"
            onClick={() => setIsPromptModalOpen(true)}
            className={secondaryButtonClasses}
          >
            {t("workspace.prompt.edit_button")}
          </button>
        ) : null}
      </div>
      {isPromptModalOpen ? (
        <PhasePromptModal
          runId={runId}
          phase={stalePhase}
          runState={runState}
          onClose={() => setIsPromptModalOpen(false)}
          onRerunCompleted={() => {
            setIsPromptModalOpen(false);
            onRefreshed();
          }}
        />
      ) : null}
      {error ? <p className="text-sm leading-6 text-red-700">{error}</p> : null}
    </div>
  );
}

function FailureResolutionBanner({
  runId,
  state,
  lastEvent,
  forceApproveHint,
  onNavigateToPhase,
  onRefreshed,
}: {
  runId: string;
  state: FailureState;
  lastEvent: RunEvent | null;
  forceApproveHint: ForceApproveHint | null;
  onNavigateToPhase: (phase: string) => void;
  onRefreshed: () => void;
}) {
  const t = useT();
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isForceModalOpen, setIsForceModalOpen] = useState(false);

  const payload = (lastEvent?.payload ?? {}) as Record<string, unknown>;
  const phase =
    typeof payload.phase === "string" ? (payload.phase as string) : null;
  // PR-I4.a: failure_class drives smartRetry's start-vs-rerun decision.
  // Same payload key the backend's PR-I3.b _PARTIAL_FAILURE_CLASSES
  // check reads, so frontend and backend stay in sync.
  const failureClass =
    typeof payload.failure_class === "string"
      ? (payload.failure_class as string)
      : null;
  const guidance =
    typeof payload.guidance === "string" ? (payload.guidance as string) : null;
  // PR-J5: synthesizer FAILED_FIXABLE now exposes per_source_warnings
  // (each = {source_id, failure_class, message}) so the banner can
  // tell users WHICH sources failed and WHY (no PDF + no abstract /
  // PoorExtraction / LLM parse fail), not just the generic
  // "upload more PDFs / broaden / refine" wording.
  const perSourceWarningsRaw = payload.per_source_warnings;
  const perSourceWarnings: Array<{
    source_id: string;
    failure_class: string;
    message: string;
  }> = Array.isArray(perSourceWarningsRaw)
    ? perSourceWarningsRaw
        .filter(
          (item): item is Record<string, unknown> =>
            typeof item === "object" && item !== null,
        )
        .map((item) => ({
          source_id: String(item.source_id ?? ""),
          failure_class: String(item.failure_class ?? ""),
          message: String(item.message ?? ""),
        }))
    : [];
  const perSourceWarningTotal =
    typeof payload.per_source_warning_total === "number"
      ? (payload.per_source_warning_total as number)
      : perSourceWarnings.length;

  async function withRunningState(action: () => Promise<unknown>) {
    if (isRunning) return;
    setIsRunning(true);
    setError(null);
    try {
      await action();
      onRefreshed();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("workspace.rerun_failed")}: ${caught.message}`
          : t("workspace.rerun_failed"),
      );
    } finally {
      setIsRunning(false);
    }
  }

  // Variant rendering decisions (codex AGREE-with-amendments,
  // system-wide audit P1c).
  const isCancelledOrPolicyOrNeedsUser =
    state === "CANCELLED" ||
    state === "FAILED_POLICY" ||
    state === "FAILED_NEEDS_USER";

  // CANCELLED / FAILED_POLICY / FAILED_NEEDS_USER are treated as
  // amber-message-only by default — no generic "retry" affordance,
  // since codex flagged them as either user-input-required or
  // policy-terminal where blanket rerun would be misleading.
  const isAmberOnly = isCancelledOrPolicyOrNeedsUser;
  const borderClass = isAmberOnly ? "border-amber-400" : "border-red-400";
  const bgClass = isAmberOnly ? "bg-amber-50" : "bg-red-50";
  const headlineClass = isAmberOnly ? "text-amber-900" : "text-red-900";
  const bodyClass = isAmberOnly ? "text-amber-900" : "text-red-900";

  let titleKey: string;
  let bodyKey: string;
  if (state === "FAILED_FIXABLE") {
    titleKey = phase
      ? "workspace.failed_banner_title"
      : "workspace.failed_banner_title_generic";
    bodyKey = "workspace.failed_banner_body_fallback";
  } else if (state === "FAILED_VENDOR") {
    titleKey = "workspace.failed_vendor_banner_title";
    bodyKey = "workspace.failed_vendor_banner_body";
  } else if (state === "FAILED_NEEDS_USER") {
    titleKey = "workspace.failed_needs_user_banner_title";
    bodyKey = "workspace.failed_needs_user_banner_body";
  } else if (state === "FAILED_POLICY") {
    titleKey = "workspace.failed_policy_banner_title";
    bodyKey = "workspace.failed_policy_banner_body";
  } else {
    titleKey = "workspace.cancelled_banner_title";
    bodyKey = "workspace.cancelled_banner_body";
  }

  const title = phase ? t(titleKey).replace(/\{phase\}/g, phase) : t(titleKey);

  const navigateLabel = phase
    ? t("workspace.failed_navigate_link").replace(/\{phase\}/g, phase)
    : null;

  return (
    <div
      className={`mb-6 grid gap-3 rounded-lg border-2 ${borderClass} ${bgClass} p-4 sm:p-5`}
      data-testid="failure-resolution-banner"
      data-failure-state={state}
      data-failed-phase={phase ?? ""}
    >
      <p className={`text-sm font-bold ${headlineClass}`}>{title}</p>
      <p className={`text-sm leading-6 ${bodyClass}`}>
        {guidance ?? t(bodyKey)}
      </p>

      {perSourceWarnings.length > 0 ? (
        <details
          className={`text-sm ${bodyClass}`}
          data-testid="failure-banner-per-source-warnings"
          data-warning-count={perSourceWarnings.length}
        >
          <summary className="cursor-pointer font-semibold">
            {t("workspace.failure_per_source_summary")
              .replace(/\{visible\}/g, String(perSourceWarnings.length))
              .replace(/\{total\}/g, String(perSourceWarningTotal))}
          </summary>
          <ul className="mt-2 grid gap-1.5">
            {perSourceWarnings.map((warning, idx) => (
              <li
                key={`${warning.source_id || "unknown"}-${idx}`}
                data-testid={`failure-banner-warning-${idx}`}
                data-failure-class={warning.failure_class}
                className="rounded-md bg-white/60 px-2 py-1.5 text-xs leading-5"
              >
                <span className="font-mono break-all">
                  {warning.source_id || "—"}
                </span>
                {warning.failure_class ? (
                  <span className="ml-2 inline-block rounded-full bg-slate-200 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-700">
                    {warning.failure_class}
                  </span>
                ) : null}
                {warning.message ? (
                  <p className="mt-0.5 text-slate-700">{warning.message}</p>
                ) : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {phase && navigateLabel ? (
        <button
          type="button"
          data-testid="failure-banner-navigate"
          onClick={() => onNavigateToPhase(phase)}
          className={`inline-flex w-fit items-center rounded border border-current px-3 py-1.5 text-xs font-semibold ${headlineClass} transition hover:bg-white/40`}
        >
          → {navigateLabel}
        </button>
      ) : null}

      {state === "FAILED_FIXABLE" ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="failed-retry-button"
            disabled={isRunning || !phase}
            onClick={() =>
              phase &&
              void withRunningState(() =>
                // PR-I4.a: route through smartRetry so graceful
                // failed_fixable + sentinel goes via rerun_phase
                // and zombie/runtime_error goes via start_<phase>.
                // Pre-PR-I4.a always called retryFailedPhase →
                // start_<phase> path → 409 on graceful classes.
                smartRetry({
                  runId,
                  phase,
                  failureClass,
                  startCaller: retryFailedPhase,
                }),
              )
            }
            className="inline-flex min-h-10 items-center rounded bg-[#114b5f] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#0d3d4d] disabled:opacity-60"
          >
            {isRunning
              ? t("workspace.rerun_running")
              : phase
                ? t("workspace.failed_retry_button").replace(
                    /\{phase\}/g,
                    phase,
                  )
                : t("workspace.failed_retry_button_generic")}
          </button>
        </div>
      ) : null}

      {state === "FAILED_VENDOR" ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="failed-vendor-retry-button"
            disabled={isRunning}
            onClick={() => void withRunningState(() => startIntegrity(runId))}
            className="inline-flex min-h-10 items-center rounded bg-[#114b5f] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#0d3d4d] disabled:opacity-60"
          >
            {isRunning
              ? t("workspace.rerun_running")
              : t("workspace.failed_vendor_retry_button")}
          </button>
          <button
            type="button"
            data-testid="failed-vendor-skip-button"
            disabled={isRunning}
            onClick={() =>
              void withRunningState(() =>
                transitionRun(
                  runId,
                  "USER_FINAL_ACCEPTANCE",
                  "user skipped integrity after FAILED_VENDOR",
                ),
              )
            }
            className={secondaryButtonClasses}
          >
            {t("workspace.failed_vendor_skip_button")}
          </button>
        </div>
      ) : null}

      {forceApproveHint?.applicable ? (
        <div
          data-testid="force-approve-row"
          className="flex flex-wrap items-center gap-2 border-t border-current pt-3"
        >
          {/* PR-249 follow-up (user request 2026-05-06): elevate the
              force-approve affordance from secondary to primary so
              the user notices it on a FAILED_FIXABLE banner where
              "retry the same phase" isn't going to help (e.g.
              synthesizer min-sources gate when curator selected
              metadata-only sources — common LLM-quality scenario).
              Phase-aware label makes it explicit which phase will be
              force-approved. */}
          <button
            type="button"
            data-testid="force-approve-open-modal"
            disabled={isRunning}
            onClick={() => setIsForceModalOpen(true)}
            className="inline-flex min-h-10 items-center rounded bg-amber-600 px-4 py-2 text-sm font-bold text-white transition hover:bg-amber-700 disabled:opacity-60"
          >
            {phase
              ? t("workspace.force_approve_phase_button").replace(
                  /\{phase\}/g,
                  phase,
                )
              : t("workspace.force_approve_button")}
          </button>
          {forceApproveHint.consequence ? (
            <p className={`text-xs leading-5 ${bodyClass}`}>
              {forceApproveHint.consequence}
            </p>
          ) : null}
        </div>
      ) : null}

      {error ? <p className="text-sm leading-6 text-red-700">{error}</p> : null}

      {isForceModalOpen && forceApproveHint?.applicable ? (
        <ForceApproveModal
          consequence={forceApproveHint.consequence}
          onCancel={() => setIsForceModalOpen(false)}
          onConfirm={async (reason) => {
            setIsForceModalOpen(false);
            await withRunningState(() => forceApproveRun(runId, reason));
          }}
        />
      ) : null}
    </div>
  );
}

function ForceApproveModal({
  consequence,
  onCancel,
  onConfirm,
}: {
  consequence: string | null;
  onCancel: () => void;
  onConfirm: (reason: string) => Promise<void>;
}) {
  const t = useT();
  const [reason, setReason] = useState("");
  const [isPending, setIsPending] = useState(false);
  const trimmedLength = reason.trim().length;
  const canConfirm = trimmedLength >= 5 && !isPending;

  useEffect(() => {
    function onKeyDown(ev: globalThis.KeyboardEvent) {
      if (ev.key === "Escape") onCancel();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onCancel]);

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4"
      data-testid="force-approve-modal"
    >
      <section className="grid w-full max-w-lg gap-3 rounded-lg bg-white p-5 shadow-xl">
        <h2 className="text-base font-bold text-slate-950">
          {t("workspace.force_approve_modal_title")}
        </h2>
        {consequence ? (
          <p className="text-sm leading-6 text-slate-700">{consequence}</p>
        ) : null}
        <label
          htmlFor="force-approve-reason"
          className="text-xs font-semibold uppercase tracking-wide text-slate-500"
        >
          {t("workspace.force_approve_reason_label")}
        </label>
        <textarea
          id="force-approve-reason"
          data-testid="force-approve-reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          maxLength={1000}
          rows={4}
          className="rounded-md border border-slate-300 p-2 text-sm leading-6"
          placeholder={t("workspace.force_approve_reason_placeholder")}
        />
        <p className="text-xs text-slate-500">
          {t("workspace.force_approve_reason_count").replace(
            /\{count\}/g,
            String(trimmedLength),
          )}
        </p>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            data-testid="force-approve-cancel"
            disabled={isPending}
            onClick={onCancel}
            className={secondaryButtonClasses}
          >
            {t("workspace.cancel")}
          </button>
          <button
            type="button"
            data-testid="force-approve-confirm"
            disabled={!canConfirm}
            onClick={async () => {
              setIsPending(true);
              try {
                await onConfirm(reason.trim());
              } finally {
                setIsPending(false);
              }
            }}
            className="inline-flex min-h-10 items-center rounded bg-red-700 px-4 py-2 text-sm font-bold text-white transition hover:bg-red-800 disabled:opacity-60"
          >
            {isPending
              ? t("workspace.rerun_running")
              : t("workspace.force_approve_confirm")}
          </button>
        </div>
      </section>
    </div>
  );
}

function DegradedDraftBanner({
  severity,
  payload,
}: {
  severity: "amber_minor" | "amber_major";
  payload: Record<string, unknown> | undefined;
}) {
  const t = useT();
  const stubbed =
    typeof payload?.stubbed_sections === "number"
      ? (payload.stubbed_sections as number)
      : 0;
  const total =
    typeof payload?.sections === "number" ? (payload.sections as number) : 0;
  const ids = Array.isArray(payload?.stubbed_section_ids)
    ? (payload.stubbed_section_ids as unknown[]).filter(
        (item): item is string => typeof item === "string",
      )
    : [];
  const headlineKey =
    severity === "amber_major"
      ? "workspace.draft_degraded_major_title"
      : "workspace.draft_degraded_minor_title";
  return (
    <div
      className="mb-6 grid gap-2 rounded-lg border-2 border-amber-400 bg-amber-50 p-4 sm:p-5"
      data-testid="draft-degraded-banner"
      data-severity={severity}
    >
      <p className="text-sm font-bold text-amber-900">{t(headlineKey)}</p>
      <p className="text-sm leading-6 text-amber-900">
        {t("workspace.draft_degraded_body")
          .replace(/\{stubbed\}/g, String(stubbed))
          .replace(/\{total\}/g, String(total))}
      </p>
      {ids.length > 0 ? (
        <p className="text-xs leading-5 text-amber-900">
          {t("workspace.draft_degraded_section_ids")}: {ids.join(", ")}
        </p>
      ) : null}
    </div>
  );
}

function eventLabel(event: RunEvent): string {
  return `${event.event_type} at ${event.created_at}`;
}

function ProjectTitleEditor({
  run,
  onUpdated,
}: {
  run: Run;
  onUpdated: (updated: Run) => void;
}) {
  const t = useT();
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(run.project_title || "");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isEditing) {
      setDraft(run.project_title || "");
      setError(null);
    }
  }, [run.project_title, isEditing]);

  async function handleSave() {
    const trimmed = draft.trim();
    if (!trimmed) {
      setError(t("workspace.title.error_required"));
      return;
    }
    if (trimmed === run.project_title) {
      setIsEditing(false);
      return;
    }
    setIsSaving(true);
    setError(null);
    try {
      const updated = await patchProject(run.project_id, { title: trimmed });
      onUpdated({
        ...run,
        project_title: updated.title,
        project_language: updated.language,
      });
      setIsEditing(false);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.title.error_failed"),
      );
    } finally {
      setIsSaving(false);
    }
  }

  if (isEditing) {
    return (
      <div className="grid gap-2">
        <input
          type="text"
          data-testid="project-title-input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled={isSaving}
          maxLength={500}
          className={`${h1Classes} w-full rounded-md border border-slate-300 px-3 py-1.5 text-2xl outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20`}
          autoFocus
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void handleSave();
            }
            if (event.key === "Escape") {
              setIsEditing(false);
            }
          }}
        />
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <button
            type="button"
            data-testid="project-title-save"
            className={primaryButtonClasses}
            onClick={() => void handleSave()}
            disabled={isSaving}
          >
            {isSaving
              ? t("workspace.proposal.saving")
              : t("workspace.title.save")}
          </button>
          <button
            type="button"
            data-testid="project-title-cancel"
            className={secondaryButtonClasses}
            onClick={() => setIsEditing(false)}
            disabled={isSaving}
          >
            {t("workspace.title.cancel")}
          </button>
          {error ? <span className="text-xs text-red-700">{error}</span> : null}
        </div>
      </div>
    );
  }

  // 2026-05-02 prod feedback: keeping the edit button inside the
  // ``<h1>`` (`inline-flex items-center`) wraps the button to the
  // next line on narrow viewports where it visually attaches to
  // the branch dropdown. Render the title as a clean block element
  // and place the edit affordance on its own line directly below,
  // so it always reads as "edit the title".
  return (
    <div className="grid gap-1" data-testid="project-title-display">
      <h1 className={h1Classes}>
        {run.project_title || t("workspace.heading_default")}
      </h1>
      <div>
        <button
          type="button"
          data-testid="project-title-edit-button"
          className="text-xs font-bold text-[#114b5f] underline opacity-70 transition hover:opacity-100"
          onClick={() => setIsEditing(true)}
          aria-label={t("workspace.title.edit_button")}
        >
          {t("workspace.title.edit_button")}
        </button>
      </div>
    </div>
  );
}

function PaperLanguageEditor({
  run,
  onUpdated,
}: {
  run: Run;
  onUpdated: (updated: Run) => void;
}) {
  const t = useT();
  const [isEditing, setIsEditing] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const displayName: Record<ProjectLanguage, string> = {
    en: "English",
    zh: "中文",
    ja: "日本語",
  };

  async function pick(value: ProjectLanguage) {
    if (value === run.project_language) {
      setIsEditing(false);
      return;
    }
    setIsSaving(true);
    setError(null);
    try {
      const updated = await patchProject(run.project_id, { language: value });
      onUpdated({
        ...run,
        project_language: updated.language,
        project_title: updated.title,
      });
      setIsEditing(false);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : t("workspace.errors.language_update_failed"),
      );
    } finally {
      setIsSaving(false);
    }
  }

  if (isEditing) {
    const codes: ProjectLanguage[] = ["en", "zh", "ja"];
    return (
      <span className="inline-flex flex-wrap items-center gap-1">
        {codes.map((code) => (
          <button
            type="button"
            key={code}
            onClick={() => void pick(code)}
            disabled={isSaving}
            className={`min-h-7 rounded-full border px-2.5 py-0.5 text-xs font-bold transition ${
              run.project_language === code
                ? "border-[#114b5f] bg-[#114b5f] text-white"
                : "border-slate-300 bg-white text-[#114b5f] hover:bg-slate-50"
            }`}
          >
            {displayName[code]}
          </button>
        ))}
        <button
          type="button"
          onClick={() => {
            setIsEditing(false);
            setError(null);
          }}
          className="text-xs text-slate-500 underline"
        >
          {t("workspace.cancel")}
        </button>
        {error ? (
          <span className="ml-1 text-xs text-red-700">{error}</span>
        ) : null}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setIsEditing(true)}
      className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-bold text-[#114b5f] transition hover:bg-slate-200"
      title={t("workspace.paper_language_edit_hint")}
    >
      {displayName[run.project_language]}
      <span className="text-slate-400">▾</span>
    </button>
  );
}

function GenerationModeBadge({ mode }: { mode: GenerationMode }) {
  const normalized = mode === "deep" ? "deep" : "express";
  const tone =
    normalized === "express"
      ? "bg-[#e7f0ff] text-[#24456f]"
      : "bg-[#efe7d6] text-[#6a4f1d]";
  return (
    <span
      data-testid={`run-mode-badge-${normalized}`}
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-bold ${tone}`}
      title={
        normalized === "express"
          ? "Express generation architecture"
          : "Deep generation architecture"
      }
    >
      {normalized === "express" ? "Express" : "Deep"}
    </span>
  );
}

function ExpressTransparencyPanel({
  run,
  refreshKey,
  onRegenerateExpress,
  onStartDeep,
  isRegenerating,
  isStartingDeep,
}: {
  run: Run;
  refreshKey: string;
  onRegenerateExpress: () => Promise<void>;
  onStartDeep: () => Promise<void>;
  isRegenerating: boolean;
  isStartingDeep: boolean;
}) {
  const t = useT();
  const [bundle, setBundle] = useState<ExpressTransparency | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getExpressTransparency(run.id)
      .then((next) => {
        if (!cancelled) {
          setBundle(next);
          setError(null);
        }
      })
      .catch((caught) => {
        if (!cancelled) {
          setBundle(null);
          setError(
            caught instanceof Error
              ? caught.message
              : t("workspace.express.load_failed"),
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [run.id, refreshKey, t]);

  const totalTokens = Number(bundle?.token_usage.total_tokens ?? 0);
  const cap = Number(bundle?.token_cap ?? 0);
  const audit = bundle?.audit_summary ?? {};
  const isRunning = run.state === "EXPRESS_RUNNING";

  return (
    <section
      data-testid="express-transparency-panel"
      className="mb-6 grid min-w-0 gap-5 overflow-hidden rounded-[18px] border border-[#c8d8e8] bg-[#f7fbff] p-5 text-slate-900 sm:p-6"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className={eyebrowClasses}>{t("workspace.express.eyebrow")}</p>
          <h2 className={h2Classes}>{t("workspace.express.heading")}</h2>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-600">
            {t("workspace.express.summary")}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            data-testid="express-regenerate-button"
            className={secondaryButtonClasses + " sm:w-auto"}
            onClick={() => void onRegenerateExpress()}
            disabled={isRunning || isRegenerating || isStartingDeep}
          >
            {isRegenerating
              ? t("workspace.express.creating")
              : t("workspace.express.regenerate")}
          </button>
          <button
            type="button"
            data-testid="express-start-deep-button"
            className={primaryButtonClasses + " sm:w-auto"}
            onClick={() => void onStartDeep()}
            disabled={isRunning || isRegenerating || isStartingDeep}
          >
            {isStartingDeep
              ? t("workspace.express.creating")
              : t("workspace.express.start_deep")}
          </button>
        </div>
      </div>

      {error ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          {error}
        </p>
      ) : null}

      <div className="grid min-w-0 gap-3 md:grid-cols-2">
        <div
          data-testid="express-token-usage"
          className="min-w-0 rounded-md border border-slate-200 bg-white p-3"
        >
          <dt className="text-xs font-bold uppercase text-slate-500">
            {t("workspace.express.tokens")}
          </dt>
          <dd className="mt-1 text-sm font-semibold text-slate-950">
            {totalTokens > 0 ? totalTokens.toLocaleString() : "—"} /{" "}
            {cap > 0 ? cap.toLocaleString() : "—"}
          </dd>
        </div>
        {/* provider/model card intentionally removed — internal */}
        <div
          data-testid="express-audit-summary"
          className="min-w-0 rounded-md border border-slate-200 bg-white p-3"
        >
          <dt className="text-xs font-bold uppercase text-slate-500">
            {t("workspace.express.audit")}
          </dt>
          <dd className="mt-1 text-sm font-semibold text-slate-950">
            {String(audit.status ?? "—")}
          </dd>
          {audit.summary ? (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-slate-600">
              {typeof audit.summary === "string"
                ? audit.summary
                : JSON.stringify(audit.summary, null, 2)}
            </p>
          ) : null}
        </div>
      </div>

      {/* Saved-prompt, prompt-excerpt and provider/model cards
          intentionally removed — we do not expose internal system
          prompt, path metadata, or provider identity in the
          user-facing transparency panel. */}

      <div
        data-testid="express-outline-map"
        className="min-w-0 rounded-md border border-slate-200 bg-white p-3"
      >
        <h3 className="text-sm font-bold text-slate-950">
          {t("workspace.express.outline")}
        </h3>
        {bundle?.outline.length ? (
          <ol className="mt-2 grid gap-1 text-sm text-slate-700">
            {bundle.outline.map((item, index) => (
              <li key={`${item.line ?? index}-${item.title ?? ""}`}>
                <span className="font-mono text-xs text-slate-400">
                  h{item.level ?? "?"}
                </span>{" "}
                {item.title ?? "Untitled"}
              </li>
            ))}
          </ol>
        ) : (
          <p className="mt-2 text-sm text-slate-600">
            {t("workspace.express.outline_pending")}
          </p>
        )}
      </div>

      <div
        data-testid="express-final-preview"
        className="min-w-0 rounded-md border border-slate-200 bg-white p-3"
      >
        <h3 className="mb-3 text-sm font-bold text-slate-950">
          {t("workspace.express.preview")}
        </h3>
        {bundle?.manuscript_preview ? (
          <MarkdownView markdown={bundle.manuscript_preview} />
        ) : (
          <p className="text-sm text-slate-600">
            {t("workspace.express.preview_pending")}
          </p>
        )}
      </div>
    </section>
  );
}

function WorkspaceStatusPanel({
  runId,
  currentState,
  lastEvent,
  isLoading,
  phaseActions,
  onOpenHistory,
  onOpenKernelEdit,
  className,
  onClose,
}: {
  runId: string | undefined;
  currentState: string | undefined;
  lastEvent: RunEvent | null;
  isLoading: boolean;
  phaseActions: PhaseAction[];
  onOpenHistory?: () => void;
  /** PR-C0.b2.ui: open the research-kernel edit modal. Lives in
   * the status panel action stack (codex round-1.b2.ui amendment 3:
   * matches where history affordance already lives). */
  onOpenKernelEdit?: () => void;
  className: string;
  onClose?: () => void;
}) {
  const t = useT();
  return (
    <aside className={className}>
      <div className="mb-5 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className={eyebrowClasses}>{t("workspace.checkpoint_eyebrow")}</p>
          <h2 className="text-lg font-bold text-slate-950">
            {t("workspace.run_state")}
          </h2>
          <p className="mt-1 truncate text-sm text-slate-600">
            {runId ?? t("workspace.unknown")}
          </p>
        </div>
        {onClose ? (
          <button
            type="button"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded bg-slate-100 text-xl font-bold text-[#114b5f] transition hover:bg-slate-200"
            aria-label={t("workspace.close_details")}
            onClick={onClose}
          >
            ×
          </button>
        ) : null}
      </div>
      {isLoading ? (
        <p className="mb-4 leading-7 text-slate-700">
          {t("workspace.loading")}
        </p>
      ) : null}
      <dl className="grid gap-3 text-sm">
        <div className="rounded-lg border border-slate-200 p-3">
          <dt className="mb-1 font-bold text-slate-500">
            {t("workspace.state_label")}
          </dt>
          <dd className="break-words font-semibold text-slate-950">
            {currentState
              ? formatRunState(t, currentState)
              : t("workspace.unknown")}
          </dd>
        </div>
        <div className="rounded-lg border border-slate-200 p-3">
          <dt className="mb-1 font-bold text-slate-500">
            {t("workspace.last_event_label")}
          </dt>
          <dd className="break-words text-slate-700">
            {lastEvent ? eventLabel(lastEvent) : t("workspace.none")}
          </dd>
        </div>
      </dl>
      {phaseActions.length > 0 ? (
        <div className="mt-5 grid gap-2">
          {phaseActions.map((action) => (
            <div key={action.key} className="grid gap-1">
              <button
                type="button"
                data-testid={`phase-action-${action.key}`}
                className={primaryButtonClasses + " lg:w-full"}
                onClick={action.onClick}
                disabled={action.disabled}
                title={action.disabledReason}
              >
                {action.label}
              </button>
              {action.disabled && action.disabledReason ? (
                <p
                  data-testid={`phase-action-${action.key}-reason`}
                  className="text-xs leading-5 text-slate-600"
                >
                  {action.disabledReason}
                </p>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {onOpenHistory ? (
        <button
          type="button"
          data-testid="workspace-history-button"
          className={primaryButtonClasses + " mt-3 lg:w-full"}
          onClick={onOpenHistory}
        >
          {t("workspace.history.button")}
        </button>
      ) : null}
      {onOpenKernelEdit ? (
        <button
          type="button"
          data-testid="workspace-kernel-edit-button"
          className={secondaryButtonClasses + " mt-2 lg:w-full"}
          onClick={onOpenKernelEdit}
        >
          {t("workspace.kernel.edit_button")}
        </button>
      ) : null}
    </aside>
  );
}

function PhaseHistoryModal({
  runId,
  lastEvent,
  runState,
  onClose,
  onActivated,
  onForked,
}: {
  runId: string;
  lastEvent: RunEvent | null;
  /** PR-C2.b follow-up: when the run is mid-flight (any
   * RUNNING_STATES), every per-phase action (rerun / activate /
   * delete / edit_prompt) would 409 backend-side. Gate the
   * affordances so the user doesn't get teach-then-reject UX. */
  runState: string | undefined;
  onClose: () => void;
  onActivated: () => void;
  onForked: () => void;
}) {
  // PR-A4.4 (codex AGREE 2026-05-02): rewritten to consume
  // GET /api/runs/{id}/phase-history (single batched call) +
  // render per-phase cards with state pill + decided actions
  // + collapsed all-versions list with [activate]+[delete]
  // per row. Existing testids preserved per codex amendment 9.
  const t = useT();
  const [data, setData] = useState<PhaseHistoryResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [inFlight, setInFlight] = useState<string | null>(null);
  const [sourceImpactBundle, setSourceImpactBundle] =
    useState<SourcesBundle | null>(null);
  const [pendingSourceRerun, setPendingSourceRerun] = useState<{
    actionKey: PrimaryActionKey;
    entry: PhaseHistoryEntry;
    impact: SourceRerunImpact;
  } | null>(null);
  const [expandedVersions, setExpandedVersions] = useState<Set<string>>(
    new Set(),
  );
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  // Codex amendment 2: aria-modal + initial focus + Escape close.
  useEffect(() => {
    const onKeyDown = (ev: globalThis.KeyboardEvent) => {
      if (ev.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    closeButtonRef.current?.focus();
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const fetchHistory = useCallback(
    async (signal?: AbortSignal): Promise<void> => {
      setLoadError(null);
      try {
        const response = await getPhaseHistory(runId, { signal });
        setData(response);
        // Default-expand any phase that has versions so the
        // existing version-management.spec.ts e2e (which selects
        // history-version-{phase}-{N} directly without toggling)
        // keeps working. The user can still collapse manually.
        setExpandedVersions((prev) => {
          if (prev.size > 0) return prev;
          const next = new Set<string>();
          for (const entry of response.phases) {
            if (entry.versions.length > 0) next.add(entry.phase);
          }
          return next;
        });
      } catch (caught) {
        if (signal?.aborted) return;
        setLoadError(
          caught instanceof Error
            ? `${t("workspace.history.error.load")} ${caught.message}`
            : t("workspace.history.error.load"),
        );
      }
    },
    [runId, t],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    void fetchHistory(ctrl.signal);
    return () => ctrl.abort();
  }, [fetchHistory]);

  useEffect(() => {
    let cancelled = false;
    getSources(runId)
      .then((bundle) => {
        if (!cancelled) setSourceImpactBundle(bundle);
      })
      .catch(() => {
        if (!cancelled) setSourceImpactBundle(null);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // Codex amendment 8: refetch full payload on phase_done /
  // phase_failed events.
  const lastEventAt = lastEvent?.created_at ?? null;
  const lastEventType = lastEvent?.event_type ?? null;
  useEffect(() => {
    if (lastEventType !== "phase_done" && lastEventType !== "phase_failed") {
      return;
    }
    const ctrl = new AbortController();
    void fetchHistory(ctrl.signal);
    return () => ctrl.abort();
  }, [fetchHistory, lastEventType, lastEventAt]);

  const runAction = useCallback(
    async (
      actionId: string,
      body: () => Promise<void>,
      options: { notifyParent?: boolean } = {},
    ): Promise<void> => {
      if (inFlight !== null) return;
      setInFlight(actionId);
      setActionError(null);
      try {
        await body();
        await fetchHistory();
        if (options.notifyParent) onActivated();
      } catch (caught) {
        setActionError(
          t("workspace.history.error.action").replace(
            /\{message\}/g,
            caught instanceof Error ? caught.message : String(caught),
          ),
        );
      } finally {
        setInFlight(null);
      }
    },
    [inFlight, fetchHistory, onActivated, t],
  );

  const phases = useMemo(() => data?.phases ?? [], [data?.phases]);
  const isLoading = data === null && loadError === null;

  const requestSourceRerunConfirm = useCallback(
    (entry: PhaseHistoryEntry, actionKey: PrimaryActionKey) => {
      setPendingSourceRerun({
        actionKey,
        entry,
        impact: buildSourceRerunImpact(entry.phase, sourceImpactBundle, phases),
      });
    },
    [phases, sourceImpactBundle],
  );

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="phase-history-modal-title"
      data-testid="phase-history-modal"
    >
      {pendingSourceRerun ? (
        <SourceRerunConfirmDialog
          impact={pendingSourceRerun.impact}
          onCancel={() => setPendingSourceRerun(null)}
          onConfirm={() => {
            const pending = pendingSourceRerun;
            setPendingSourceRerun(null);
            void handlePrimaryAction(
              pending.actionKey,
              pending.entry,
              runId,
              runAction,
              () => undefined,
              () => undefined,
            );
          }}
        />
      ) : null}
      <section className="grid max-h-[85vh] w-full max-w-2xl grid-rows-[auto_1fr_auto] rounded-lg bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-5">
          <div className="min-w-0">
            <p className={eyebrowClasses}>{t("workspace.history.button")}</p>
            <h2
              id="phase-history-modal-title"
              className="text-xl font-bold text-slate-950"
            >
              {t("workspace.history.title")}
            </h2>
            <p className="mt-2 text-sm leading-6 text-slate-700">
              {t("workspace.history.body")}
            </p>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            data-testid="history-modal-close"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded bg-slate-100 text-xl font-bold text-[#114b5f] transition hover:bg-slate-200"
            aria-label={t("workspace.close_details")}
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <div className="overflow-y-auto p-5">
          {/* PR-I4.b B2: top-of-modal running banner — codex
              audit C1 invariant + user feedback "如果已经重新开始
              某阶段，当前阶段应该是正在跑的阶段". Tells the user up
              front which phase is running so the disabled mutation
              buttons + 409s make sense, instead of leaving them to
              parse "某节点正在运行" hint at the bottom of each card. */}
          {isRunningState(runState) ? (
            <div
              className="mb-4 rounded-md border-2 border-amber-500 bg-amber-50 p-3 text-sm font-bold text-amber-900"
              data-testid="phase-history-running-banner"
            >
              {t("workspace.history.running_banner").replace(
                /\{phase\}/g,
                runState
                  ? t(
                      `phase.${(RUNNING_STATE_TO_PHASE as Record<string, string>)[runState] ?? "unknown"}`,
                    )
                  : "",
              )}
            </div>
          ) : null}
          {loadError ? (
            <p className="mb-4 text-sm leading-6 text-red-700">{loadError}</p>
          ) : null}
          {actionError ? (
            <p
              className="mb-4 text-sm leading-6 text-red-700"
              data-testid="phase-history-action-error"
            >
              {actionError}
            </p>
          ) : null}
          {isLoading ? (
            <p className="text-sm leading-6 text-slate-700">
              {t("workspace.loading")}
            </p>
          ) : null}
          {!isLoading && phases.length === 0 ? (
            <p className="text-sm leading-6 text-slate-700">
              {t("workspace.history.no_versions")}
            </p>
          ) : null}
          {phases.map((entry) => (
            <PhaseHistoryCard
              key={entry.phase}
              runId={runId}
              entry={entry}
              inFlight={inFlight}
              runState={runState}
              isExpanded={expandedVersions.has(entry.phase)}
              onToggleExpanded={() =>
                setExpandedVersions((prev) => {
                  const next = new Set(prev);
                  if (next.has(entry.phase)) next.delete(entry.phase);
                  else next.add(entry.phase);
                  return next;
                })
              }
              onAction={runAction}
              onForked={onForked}
              onRequestSourceRerunConfirm={requestSourceRerunConfirm}
            />
          ))}
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-200 p-4">
          <button
            type="button"
            className={secondaryButtonClasses}
            onClick={onClose}
          >
            {t("workspace.close_details")}
          </button>
        </div>
      </section>
    </div>
  );
}

function PhaseHistoryCard({
  runId,
  entry,
  inFlight,
  runState,
  isExpanded,
  onToggleExpanded,
  onAction,
  onForked,
  onRequestSourceRerunConfirm,
}: {
  runId: string;
  entry: PhaseHistoryEntry;
  inFlight: string | null;
  runState: string | undefined;
  isExpanded: boolean;
  onToggleExpanded: () => void;
  onAction: (
    actionId: string,
    body: () => Promise<void>,
    options?: { notifyParent?: boolean },
  ) => Promise<void>;
  onForked: () => void;
  onRequestSourceRerunConfirm: (
    entry: PhaseHistoryEntry,
    actionKey: PrimaryActionKey,
  ) => void;
}) {
  const t = useT();
  const cardState = deriveCardState(entry);
  const actions = derivePrimaryActions(entry);
  const [isPromptModalOpen, setIsPromptModalOpen] = useState(false);
  // Disable all actions while either a HTTP round-trip is in flight
  // OR an agent is currently writing artifacts. The latter mirrors
  // the backend 409 path on every mutate endpoint (rerun/edit/
  // activate/delete) and avoids teach-then-reject affordances.
  const allDisabled = inFlight !== null || isRunningState(runState);

  return (
    <section
      data-testid={`history-phase-${entry.phase}`}
      data-card-state={cardState}
      data-runnable-now={entry.runnable_now ? "true" : "false"}
      className="mb-6 rounded-md border border-slate-200 bg-white p-4 shadow-sm last:mb-0"
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-base font-bold text-slate-950">
            {t(`phase.${entry.phase}`)}
            {entry.head_version_no !== null ? (
              <span className="ml-2 text-sm font-semibold text-slate-600">
                v{String(entry.head_version_no).padStart(3, "0")}
              </span>
            ) : null}
          </h3>
        </div>
        <span
          data-testid={`history-phase-${entry.phase}-state-pill`}
          data-state={cardState}
          className={
            "rounded-full px-3 py-1 text-xs font-bold " +
            cardStatePillClasses(cardState)
          }
        >
          {t(`workspace.history.state.${cardState}.title`)}
        </span>
      </header>
      <p className="mt-2 text-sm leading-6 text-slate-700">
        {t(`workspace.history.state.${cardState}.reason`)}
      </p>
      {/* Codex round 1 amendment 5 + round 2 amendment 3: when
          prompt_dirty AND lineage_dirty coexist, primary actions
          remain cancel/regenerate, but the user needs to know
          upstream also changed AND that snapping to a matching
          version requires cancelling the prompt edit first
          (since activate_lineage_match is not surfaced while
          prompt edits take priority). */}
      {cardState === "prompt_edited" && entry.state_flags.lineage_dirty ? (
        <p
          className="mt-1 text-xs text-amber-700"
          data-testid={`history-phase-${entry.phase}-prompt-upstream-advisory`}
        >
          {t("workspace.history.state.prompt_edited_with_upstream.advisory")}
        </p>
      ) : null}
      {entry.upstream_summary.length > 0 ? (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span className="font-semibold uppercase tracking-wide text-slate-500">
            {t("workspace.history.upstream_label")}
          </span>
          {entry.upstream_summary.map((up) => (
            <span
              key={up.upstream_phase}
              data-testid={`history-phase-${entry.phase}-upstream-${up.upstream_phase}`}
              className={
                "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-semibold " +
                (up.matches_my_lineage
                  ? "border-emerald-300 bg-emerald-50 text-emerald-900"
                  : "border-amber-300 bg-amber-50 text-amber-900")
              }
            >
              {t(`phase.${up.upstream_phase}`)}{" "}
              {up.head_version_no !== null
                ? `v${String(up.head_version_no).padStart(3, "0")}`
                : "—"}
              {!up.matches_my_lineage ? (
                <span className="ml-1">
                  {t("workspace.history.upstream_mismatch_hint")}
                </span>
              ) : null}
            </span>
          ))}
        </div>
      ) : null}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {actions.map((actionKey) => {
          const testid = primaryActionTestid(entry.phase, actionKey);
          const label = t(`workspace.history.action.${actionKey}`);
          const isThisInFlight = inFlight === testid;
          const className =
            actionKey === "regenerate" ||
            actionKey === "activate_lineage_match" ||
            actionKey === "run_now"
              ? primaryButtonClasses
              : secondaryButtonClasses;
          // Round-2 audit: run_now is always emitted for ungenerated
          // cards; disable it when the backend says it isn't runnable
          // yet (upstream not done) so the affordance is visible but
          // not actionable. Reason is rendered inline below.
          const runNowBlocked = actionKey === "run_now" && !entry.runnable_now;
          const buttonDisabled = allDisabled || runNowBlocked;
          const buttonTitle = runNowBlocked
            ? t("workspace.history.action.run_now_blocked")
            : allDisabled && isRunningState(runState)
              ? t("workspace.history.action.disabled_running")
              : undefined;
          return (
            <button
              key={actionKey}
              type="button"
              data-testid={testid}
              className={className}
              disabled={buttonDisabled}
              title={buttonTitle}
              aria-describedby={
                runNowBlocked
                  ? `history-phase-${entry.phase}-run-now-reason`
                  : undefined
              }
              onClick={() => {
                if (sourceRerunNeedsConfirm(entry.phase, actionKey)) {
                  onRequestSourceRerunConfirm(entry, actionKey);
                  return;
                }
                void handlePrimaryAction(
                  actionKey,
                  entry,
                  runId,
                  onAction,
                  () => setIsPromptModalOpen(true),
                  onToggleExpanded,
                );
              }}
            >
              {isThisInFlight ? `${label}…` : label}
            </button>
          );
        })}
        {cardState === "ungenerated" && !entry.runnable_now ? (
          <p
            id={`history-phase-${entry.phase}-run-now-reason`}
            data-testid={`history-phase-${entry.phase}-run-now-reason`}
            role="status"
            className="text-xs italic text-slate-600"
          >
            {t("workspace.history.action.run_now_blocked")}
          </p>
        ) : null}
        {isRunningState(runState) && actions.length > 0 ? (
          <p
            data-testid={`history-phase-${entry.phase}-disabled-running-reason`}
            role="status"
            className="text-xs italic text-slate-600"
          >
            {t("workspace.history.action.disabled_running")}
          </p>
        ) : null}
      </div>
      <button
        type="button"
        data-testid={`history-phase-${entry.phase}-toggle-versions`}
        className="mt-4 text-xs font-semibold text-[#114b5f] underline hover:no-underline"
        onClick={onToggleExpanded}
      >
        {(isExpanded ? "▴ " : "▾ ") +
          t("workspace.history.versions_section_label").replace(
            /\{n\}/g,
            String(entry.versions.length),
          )}
      </button>
      {isExpanded ? (
        <ul
          data-testid={`history-phase-${entry.phase}-versions`}
          className="mt-2 grid gap-2"
        >
          {entry.versions.length === 0 ? (
            <li className="text-sm text-slate-600">
              {t("workspace.history.versions_section_empty")}
            </li>
          ) : null}
          {(() => {
            // PR-I4.c: while a version is mid-flight (status=running),
            // the user expects THAT version to carry the "当前生效"
            // badge — once they trigger a rerun, all downstream stale
            // markers and active-pv decisions flip to it. The backend
            // doesn't move the head pointer until the run finishes,
            // so we treat status=running as the visual "active" for
            // display purposes; the true backend head gets a passive
            // "上一版" / "previous head" tag while the new attempt is
            // running. If two versions are simultaneously running
            // (race), the newest version_no wins.
            const runningVersion = [...entry.versions]
              .filter((v) => v.status === "running")
              .sort((a, b) => b.version_no - a.version_no)[0];
            const displayActivePvId = runningVersion
              ? runningVersion.pv_id
              : (entry.versions.find((v) => v.is_head)?.pv_id ?? null);
            const realHeadPvId =
              entry.versions.find((v) => v.is_head)?.pv_id ?? null;
            return entry.versions.map((version) => (
              <PhaseHistoryVersionRow
                key={version.pv_id}
                runId={runId}
                phase={entry.phase}
                version={version}
                inFlight={inFlight}
                allDisabled={allDisabled}
                isDisplayActive={version.pv_id === displayActivePvId}
                isPreviousHead={
                  version.pv_id === realHeadPvId &&
                  realHeadPvId !== displayActivePvId
                }
                onAction={onAction}
                onForked={onForked}
              />
            ));
          })()}
        </ul>
      ) : null}
      {isPromptModalOpen ? (
        <PhasePromptModal
          runId={runId}
          phase={entry.phase}
          runState={runState}
          onClose={() => setIsPromptModalOpen(false)}
          onRerunCompleted={() => {
            setIsPromptModalOpen(false);
            void onAction(
              `history-phase-${entry.phase}-prompt-rerun-finished`,
              async () => {
                /* parent refresh handled by fetchHistory in onAction */
              },
              { notifyParent: true },
            );
          }}
        />
      ) : null}
    </section>
  );
}

function PhaseHistoryVersionRow({
  runId,
  phase,
  version,
  inFlight,
  allDisabled,
  isDisplayActive,
  isPreviousHead,
  onAction,
  onForked,
}: {
  runId: string;
  phase: string;
  version: PhaseHistoryVersionEntry;
  inFlight: string | null;
  allDisabled: boolean;
  // PR-I4.c: parent computes "which version should look active to
  // the user" — flips from backend `is_head` to the running version
  // while a rerun is mid-flight. The actual `version.is_head`
  // remains the backend head pointer (unchanged); these props are
  // visual only.
  isDisplayActive: boolean;
  isPreviousHead: boolean;
  onAction: (
    actionId: string,
    body: () => Promise<void>,
    options?: { notifyParent?: boolean },
  ) => Promise<void>;
  onForked: () => void;
}) {
  const t = useT();
  const versionLabel = `v${String(version.version_no).padStart(3, "0")}`;
  const sourceTag = describeVersionSource(version.source);
  const activateDisabled = isActivateDisabled(version) || allDisabled;
  const deleteDisabled = isDeleteDisabled(version) || allDisabled;
  const deleteBlock = describeDeleteBlock(version);
  const activateTestid = `history-version-${phase}-${version.version_no}-activate`;
  const deleteTestid = `history-version-${phase}-${version.version_no}-delete`;
  const deleteTooltip = deleteBlock
    ? t(`workspace.history.delete_block.${deleteBlock.key}`)
        .replace(/\{branch\}/g, deleteBlock.interpolation.branch ?? "")
        .replace(/\{name\}/g, deleteBlock.interpolation.name ?? "")
    : "";
  return (
    <li
      data-testid={`history-version-${phase}-${version.version_no}`}
      data-version-id={version.pv_id}
      data-version-no={version.version_no}
      data-is-active={isDisplayActive ? "true" : "false"}
      data-is-backend-head={version.is_head ? "true" : "false"}
      data-is-previous-head={isPreviousHead ? "true" : "false"}
      data-status={version.status}
      data-source={version.source}
      className={
        "grid gap-2 rounded-md border p-3 sm:grid-cols-[1fr_auto] sm:items-center " +
        (isDisplayActive
          ? "border-[#236b45] bg-[#236b45]/5"
          : "border-slate-200 bg-white")
      }
    >
      <div className="min-w-0">
        <p className="text-sm font-bold text-slate-950">
          {versionLabel}
          <span className="ml-2 text-xs font-semibold text-slate-600">
            {t(`workspace.history.status.${version.status}`)}
          </span>
          {isDisplayActive ? (
            <span
              data-testid={`history-version-${phase}-${version.version_no}-active-badge`}
              className="ml-2 inline-block rounded bg-[#236b45] px-2 py-0.5 text-xs font-bold text-white"
            >
              {t("workspace.history.is_active")}
            </span>
          ) : null}
          {/* PR-I4.c: when a rerun is mid-flight, the backend head
              loses the green badge but we still mark it so the user
              knows that's the version current downstream phases
              were last lineage-anchored to. */}
          {isPreviousHead ? (
            <span
              data-testid={`history-version-${phase}-${version.version_no}-previous-head-badge`}
              className="ml-2 inline-block rounded border border-slate-300 px-2 py-0.5 text-xs font-semibold text-slate-600"
            >
              {t("workspace.history.previous_head")}
            </span>
          ) : null}
          {sourceTag === "user_edit" ? (
            <span className="ml-2 inline-block rounded border border-[#114b5f] px-2 py-0.5 text-xs font-bold text-[#114b5f]">
              {t("workspace.history.source.user_edit")}
            </span>
          ) : null}
        </p>
        <p className="mt-1 text-xs text-slate-600">
          {formatTimestamp(version.created_at)}
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          data-testid={activateTestid}
          className={primaryButtonClasses}
          disabled={activateDisabled}
          onClick={() =>
            void onAction(
              activateTestid,
              async () => {
                await activatePhaseVersion(runId, phase, version.pv_id);
              },
              { notifyParent: true },
            )
          }
        >
          {inFlight === activateTestid
            ? t("workspace.history.activating")
            : t("workspace.history.activate")}
        </button>
        <button
          type="button"
          data-testid={deleteTestid}
          className={secondaryButtonClasses}
          disabled={deleteDisabled}
          title={deleteTooltip || undefined}
          onClick={() =>
            void onAction(
              deleteTestid,
              () => deletePhaseVersion(runId, phase, version.pv_id),
              { notifyParent: true },
            )
          }
        >
          {t("workspace.history.action.delete")}
        </button>
        <ForkBranchButton
          runId={runId}
          pvId={version.pv_id}
          versionLabel={versionLabel}
          onForked={onForked}
          disabled={allDisabled || version.status !== "done"}
        />
      </div>
      {/* Round-2 audit (always-render + disabled + hint): when the
          delete button is blocked, surface the reason as visible text
          beneath the row, not just as a tooltip. Title-only reasons
          were invisible on touch devices and to keyboard-only users. */}
      {deleteTooltip ? (
        <p
          data-testid={`history-version-${phase}-${version.version_no}-delete-reason`}
          role="status"
          className="text-xs italic text-slate-600 sm:col-span-2"
        >
          {deleteTooltip}
        </p>
      ) : null}
    </li>
  );
}

function cardStatePillClasses(state: PhaseCardState): string {
  switch (state) {
    case "generated":
      return "border border-emerald-300 bg-emerald-50 text-emerald-900";
    case "prompt_edited":
      return "border border-amber-300 bg-amber-50 text-amber-900";
    case "upstream_superseded":
      return "border border-amber-300 bg-amber-50 text-amber-900";
    case "ungenerated":
      return "border border-slate-300 bg-slate-100 text-slate-700";
  }
}

function primaryActionTestid(
  phase: string,
  actionKey: PrimaryActionKey,
): string {
  // Codex amendment 9: preserve existing testids the
  // version-management.spec.ts e2e relies on. Map state-decided
  // action keys back to those names for the two actions that
  // pre-existed; the rest use new namespaced testids.
  switch (actionKey) {
    case "rerun":
      return `history-rerun-phase-${phase}`;
    case "edit_prompt":
      return `history-edit-prompt-${phase}`;
    default:
      return `history-action-${phase}-${actionKey}`;
  }
}

async function handlePrimaryAction(
  actionKey: PrimaryActionKey,
  entry: PhaseHistoryEntry,
  runId: string,
  onAction: (
    actionId: string,
    body: () => Promise<void>,
    options?: { notifyParent?: boolean },
  ) => Promise<void>,
  openPromptModal: () => void,
  toggleVersions: () => void,
): Promise<void> {
  const testid = primaryActionTestid(entry.phase, actionKey);
  switch (actionKey) {
    case "rerun":
    case "rerun_for_new_match":
      await onAction(
        testid,
        async () => {
          await rerunPhase(runId, entry.phase);
        },
        { notifyParent: true },
      );
      return;
    case "edit_prompt":
      openPromptModal();
      return;
    case "cancel_prompt":
      await onAction(
        testid,
        () => cancelPhasePromptDrafts(runId, entry.phase),
        { notifyParent: true },
      );
      return;
    case "regenerate":
      await onAction(
        testid,
        async () => {
          await rerunPhase(runId, entry.phase);
        },
        { notifyParent: true },
      );
      return;
    case "activate_lineage_match":
      await onAction(
        testid,
        async () => {
          await activateLineageMatch(runId, entry.phase);
        },
        { notifyParent: true },
      );
      return;
    case "run_now":
      // Codex amendment 4: ``run_now`` only surfaces when
      // entry.runnable_now is true, which means start_<phase>
      // is the legal endpoint from the current run state.
      // Most phases POST /api/runs/{id}/{phase}, but exports
      // is singular (POST /api/runs/{id}/export — see
      // start_exports in lib/api.ts), so we maintain a path
      // map. Codex round 2 amendment 1: exports MUST hit
      // /export not /exports or it 404s.
      await onAction(
        testid,
        async () => {
          const path = PHASE_RUN_NOW_PATHS[entry.phase] ?? entry.phase;
          const response = await fetch(
            `/api/runs/${encodeURIComponent(runId)}/${path}`,
            { method: "POST", credentials: "include" },
          );
          if (!response.ok) {
            const text = await response.text();
            throw new Error(`${response.status} ${text}`.slice(0, 200));
          }
        },
        { notifyParent: true },
      );
      return;
    case "scroll_to_versions":
      toggleVersions();
      return;
  }
}

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function BranchSwitcher({
  list,
  onSwitch,
}: {
  list: BranchListResponse;
  onSwitch: (branchId: string) => void;
}) {
  const t = useT();
  return (
    <label className="flex items-center gap-1 text-xs text-slate-600">
      <span className="font-semibold uppercase tracking-wide text-slate-500">
        {t("workspace.branch.label")}
      </span>
      <select
        data-testid="branch-switcher"
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs"
        value={list.active_branch_id ?? ""}
        onChange={(event) => onSwitch(event.target.value)}
      >
        {list.branches.map((b: BranchEntry) => (
          <option key={b.id} value={b.id}>
            {b.name}
            {b.stale_from_phase ? " ●" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

function ForkBranchButton({
  runId,
  pvId,
  versionLabel,
  onForked,
  disabled = false,
}: {
  runId: string;
  pvId: string;
  versionLabel: string;
  onForked: () => void;
  disabled?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleCreate() {
    const cleaned = name.trim();
    if (!cleaned) {
      setError(t("workspace.branch.name_required"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await createBranch(runId, { name: cleaned, base_pv_id: pvId });
      setOpen(false);
      setName("");
      onForked();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("workspace.branch.fork_failed")}: ${caught.message}`
          : t("workspace.branch.fork_failed"),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        data-testid="version-fork-button"
        className="text-xs font-semibold text-[#114b5f] underline hover:no-underline disabled:cursor-not-allowed disabled:text-slate-400 disabled:no-underline"
        onClick={() => setOpen(true)}
        disabled={disabled}
      >
        {t("workspace.branch.fork_button")}
      </button>
      {open ? (
        <div className="fixed inset-0 z-[55] grid place-items-center bg-slate-950/40 p-4">
          <section className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl">
            <p className={eyebrowClasses}>
              {t("workspace.branch.fork_eyebrow").replace(
                /\{label\}/g,
                versionLabel,
              )}
            </p>
            <h3 className="mb-3 text-lg font-bold text-slate-950">
              {t("workspace.branch.fork_title")}
            </h3>
            <p className="mb-3 text-sm leading-6 text-slate-700">
              {t("workspace.branch.fork_body")}
            </p>
            <input
              type="text"
              data-testid="fork-branch-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={t("workspace.branch.name_placeholder")}
              className="mb-2 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
              maxLength={120}
              autoFocus
            />
            {error ? (
              <p className="mb-2 text-sm text-red-700">{error}</p>
            ) : null}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className={secondaryButtonClasses}
                onClick={() => setOpen(false)}
                disabled={busy}
              >
                {t("workspace.close_details")}
              </button>
              <button
                type="button"
                data-testid="fork-branch-confirm"
                className={primaryButtonClasses}
                onClick={() => void handleCreate()}
                disabled={busy}
              >
                {busy
                  ? t("workspace.branch.forking")
                  : t("workspace.branch.fork_confirm")}
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </>
  );
}

function PhasePromptModal({
  runId,
  phase,
  runState,
  onClose,
  onRerunCompleted,
}: {
  runId: string;
  phase: string;
  // PR-I4.b A9: parent passes the run's current state so the modal
  // can disable mutation buttons when a phase is mid-flight.
  // Pre-fix the modal accepted Save / Save-and-rerun even after the
  // user kicked off another phase in a different tab — backend
  // 409s either way (Save trips the prompt-mutation RUNNING_STATES
  // guard; Save-and-rerun trips it twice). Disable up front.
  runState: string | undefined;
  onClose: () => void;
  onRerunCompleted: () => void;
}) {
  const t = useT();
  const [prompt, setPrompt] = useState<PhasePromptResponse | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isRerunning, setIsRerunning] = useState(false);
  // Stage 3.A.4: which prompt_key is currently displayed/edited.
  // ``undefined`` on the very first fetch so the backend runs its
  // discovery fallback (e.g. curator → ``ranking``). Subsequent
  // fetches pass the resolved or user-selected key explicitly.
  const [selectedKey, setSelectedKey] = useState<string | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    getPhasePrompt(runId, phase, selectedKey)
      .then((response) => {
        if (cancelled) return;
        setPrompt(response);
        setDraft(response.override_content ?? "");
        // Reflect the resolved key in local state so subsequent
        // saves PUT against the same surface. If the user has not
        // touched the dropdown yet (selectedKey === undefined),
        // adopt whatever the backend resolved to.
        if (selectedKey === undefined) {
          setSelectedKey(response.prompt_key);
        }
      })
      .catch((caught) => {
        if (cancelled) return;
        setError(
          caught instanceof Error
            ? `${t("workspace.prompt.load_failed")}: ${caught.message}`
            : t("workspace.prompt.load_failed"),
        );
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, phase, selectedKey, t]);

  const isDirty = (prompt?.override_content ?? "") !== draft;

  function handleKeyChange(nextKey: string) {
    if (nextKey === selectedKey) return;
    if (
      isDirty &&
      !window.confirm(t("workspace.prompt.discard_unsaved_confirm"))
    ) {
      return;
    }
    setSelectedKey(nextKey);
  }

  async function persist(
    content: string | null,
  ): Promise<PhasePromptResponse | null> {
    if (!prompt) return null;
    setIsSaving(true);
    setError(null);
    try {
      // Always save against the currently displayed key, NOT the
      // hardcoded "main". Without this the modal would PUT to main
      // even when the user is editing e.g. drafter's "introduction".
      const response = await upsertPhasePrompt(
        runId,
        phase,
        content,
        prompt.prompt_key,
      );
      setPrompt(response);
      setDraft(response.override_content ?? "");
      return response;
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("workspace.prompt.save_failed")}: ${caught.message}`
          : t("workspace.prompt.save_failed"),
      );
      return null;
    } finally {
      setIsSaving(false);
    }
  }

  async function handleSave() {
    await persist(draft.trim() ? draft : null);
  }

  async function handleDiscard() {
    await persist(null);
  }

  async function handleSaveAndRerun() {
    if (!prompt) return;
    const cleaned = draft.trim() ? draft : null;
    const saved = await persist(cleaned);
    // persist() returns null on failure — gate the rerun on that
    // explicit return, NOT on the React `error` state, which is
    // stale inside this closure.
    if (saved === null) return;
    setIsRerunning(true);
    setError(null);
    try {
      // Always send draft_hash (including null when there's no
      // override). The backend treats absence and null differently:
      // null means "I expect NO override to be active" and is still
      // checked, so a concurrent tab writing an override after the
      // user clicked Save-and-Rerun will be caught. Since Stage
      // 3.A.2's hash check is prompt-key-aware, also send the
      // resolved prompt_key.
      await rerunPhase(runId, phase, {
        draft_hash: saved.draft_hash,
        prompt_key: prompt.prompt_key,
      });
      onRerunCompleted();
    } catch (caught) {
      setError(
        caught instanceof Error
          ? `${t("workspace.rerun_failed")}: ${caught.message}`
          : t("workspace.rerun_failed"),
      );
    } finally {
      setIsRerunning(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4">
      <section className="grid max-h-[90vh] w-full max-w-3xl grid-rows-[auto_1fr_auto] rounded-lg bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-5">
          <div className="min-w-0">
            <p className={eyebrowClasses}>
              {t("workspace.prompt.eyebrow").replace(/\{phase\}/g, phase)}
            </p>
            <h2 className="text-xl font-bold text-slate-950">
              {prompt?.label ?? t("workspace.prompt.title")}
            </h2>
            <p className="mt-2 text-sm leading-6 text-slate-700">
              {t("workspace.prompt.body")}
            </p>
          </div>
          <button
            type="button"
            data-testid="prompt-modal-close"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded bg-slate-100 text-xl font-bold text-[#114b5f] transition hover:bg-slate-200"
            aria-label={t("workspace.close_details")}
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <div className="grid gap-4 overflow-y-auto p-5">
          {error ? (
            <p className="text-sm leading-6 text-red-700">{error}</p>
          ) : null}
          {isLoading ? (
            <p className="text-sm leading-6 text-slate-700">
              {t("workspace.prompt.loading")}
            </p>
          ) : prompt ? (
            <>
              {prompt.supported_keys.length > 1 ? (
                <div>
                  <label
                    className="mb-1 block text-xs font-bold uppercase tracking-wide text-slate-500"
                    htmlFor="prompt-key-select"
                  >
                    {t("workspace.prompt.key_label")}
                  </label>
                  <select
                    id="prompt-key-select"
                    data-testid="prompt-key-select"
                    className="w-full rounded-md border border-slate-300 bg-white p-2 text-sm text-slate-900"
                    value={prompt.prompt_key}
                    disabled={isSaving || isRerunning}
                    onChange={(event) => handleKeyChange(event.target.value)}
                  >
                    {prompt.supported_keys.map((key) => (
                      <option key={key} value={key}>
                        {key}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}
              <div>
                <p className="mb-1 text-xs font-bold uppercase tracking-wide text-slate-500">
                  {t("workspace.prompt.default_label")}
                </p>
                <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 p-3 text-xs leading-6 text-slate-800">
                  {prompt.default_content}
                </pre>
              </div>
              <div>
                <label
                  className="mb-1 block text-xs font-bold uppercase tracking-wide text-slate-500"
                  htmlFor="prompt-override-textarea"
                >
                  {t("workspace.prompt.override_label")}
                </label>
                <textarea
                  id="prompt-override-textarea"
                  data-testid="prompt-override-textarea"
                  className="min-h-[200px] w-full rounded-md border border-slate-300 bg-white p-3 font-mono text-sm leading-6 text-slate-900"
                  value={draft}
                  placeholder={t("workspace.prompt.override_placeholder")}
                  onChange={(event) => setDraft(event.target.value)}
                />
                <p className="mt-1 text-xs text-slate-600">
                  {t("workspace.prompt.dynamic_context_note")}
                </p>
              </div>
            </>
          ) : null}
        </div>
        {/* PR-I4.b A9: surface a "locked" hint inline with the
            buttons so a user who opened this modal before kicking
            off another phase can see why Save / Save-and-rerun are
            disabled. The buttons themselves get the `runIsBusy`
            disabled bit below. */}
        {isRunningState(runState) ? (
          <p
            className="border-t border-slate-200 px-5 pt-3 text-sm leading-6 text-amber-800"
            data-testid="prompt-locked-running"
          >
            {t("workspace.prompt.locked_running")}
          </p>
        ) : null}
        <div className="flex flex-wrap justify-end gap-2 border-t border-slate-200 p-4">
          <button
            type="button"
            data-testid="prompt-close"
            className={secondaryButtonClasses}
            onClick={onClose}
          >
            {t("workspace.close_details")}
          </button>
          <button
            type="button"
            data-testid="prompt-discard"
            className={secondaryButtonClasses}
            disabled={
              isSaving ||
              isRerunning ||
              isLoading ||
              !prompt?.override_content ||
              isRunningState(runState)
            }
            onClick={() => void handleDiscard()}
          >
            {t("workspace.prompt.discard")}
          </button>
          <button
            type="button"
            data-testid="prompt-save"
            className={secondaryButtonClasses}
            disabled={
              isSaving ||
              isRerunning ||
              isLoading ||
              !isDirty ||
              isRunningState(runState)
            }
            onClick={() => void handleSave()}
          >
            {isSaving
              ? t("workspace.prompt.saving")
              : t("workspace.prompt.save")}
          </button>
          <button
            type="button"
            data-testid="prompt-save-and-rerun"
            className={primaryButtonClasses}
            disabled={
              isSaving ||
              isRerunning ||
              isLoading ||
              !prompt ||
              isRunningState(runState)
            }
            onClick={() => void handleSaveAndRerun()}
          >
            {isRerunning
              ? t("workspace.rerun_running")
              : t("workspace.prompt.save_and_rerun")}
          </button>
        </div>
      </section>
    </div>
  );
}
export function ProposalSubview({
  currentState,
  proposalBundle,
  proposalMissing,
  progress,
  isStartingProposal,
  isSavingProposal,
  isAcceptingProposal,
  onGenerate,
  onRegenerate,
  onSave,
  onAccept,
}: {
  currentState: string | undefined;
  proposalBundle: ProposalBundle | null;
  proposalMissing: boolean;
  progress: RunEvent[];
  isStartingProposal: boolean;
  isSavingProposal: boolean;
  isAcceptingProposal: boolean;
  onGenerate: (userDraft: string) => Promise<void>;
  onRegenerate: (userDraft: string) => Promise<void>;
  onSave: (
    proposal: ProposalContent,
    options?: { mode?: "new" | "replace"; base_version?: number },
  ) => Promise<void>;
  onAccept: () => Promise<void>;
}) {
  const t = useT();
  const [userDraft, setUserDraft] = useState("");
  const [proposal, setProposal] = useState<ProposalContent>(emptyProposal());
  const [keywordInput, setKeywordInput] = useState("");
  const hasProposal = Boolean(proposalBundle?.proposal_json);
  // Codex amendment 2 (2026-05-01): allow proposal edits in any
  // quiescent state once a proposal exists. RUNNING states
  // (PROPOSAL_DRAFTING / SCOUT_RUNNING / ...) keep the form
  // disabled because an agent is consuming the current proposal as
  // input. EXPORTS_DONE is terminal and out of scope.
  const isRunningState =
    typeof currentState === "string" && currentState.endsWith("_RUNNING");
  const canEdit =
    hasProposal &&
    !isRunningState &&
    currentState !== "PROPOSAL_DRAFTING" &&
    currentState !== "EXPORTS_DONE";
  const isPostAcceptEdit = canEdit && currentState !== "USER_PROPOSAL_REVIEW";
  // PR-A3 replace-vs-new: in USER_PROPOSAL_REVIEW, by definition no
  // pipeline phase has produced output yet, so replace is always
  // eligible. In post-accept states, eligibility depends on whether
  // any phase has produced output on the active branch — we can't
  // know that purely client-side, so the server enforces it via 409
  // and we offer the toggle anyway. The user sees a meaningful
  // choice when no downstream actually completed.
  const replaceEligible = canEdit;
  // Codex AGREE 2026-05-01 question 3: default to "replace" when
  // available because typical intent is a typo fix. Force-reset to
  // "new" only AFTER the proposal bundle has loaded — otherwise the
  // initial pre-load render fires the reset (replaceEligible=false
  // because hasProposal=false), and saveMode sticks at "new" even
  // after the bundle arrives and replace becomes eligible.
  const [saveMode, setSaveMode] = useState<"replace" | "new">("replace");
  useEffect(() => {
    if (hasProposal && !replaceEligible) {
      setSaveMode("new");
    }
  }, [hasProposal, replaceEligible]);
  const previewMarkdown = proposalToMarkdown(proposal, t);

  useEffect(() => {
    if (proposalBundle?.proposal_json) {
      setProposal(proposalBundle.proposal_json);
    }
  }, [proposalBundle]);

  function updateField(
    field: Exclude<keyof ProposalContent, "preliminary_keywords">,
    value: string,
  ) {
    setProposal((current) => ({ ...current, [field]: value }));
  }

  function addKeyword(rawKeyword: string) {
    const keyword = rawKeyword.trim();
    if (!keyword) {
      return;
    }
    setProposal((current) => {
      if (
        current.preliminary_keywords.some(
          (item) => item.toLowerCase() === keyword.toLowerCase(),
        )
      ) {
        return current;
      }
      return {
        ...current,
        preliminary_keywords: [...current.preliminary_keywords, keyword],
      };
    });
    setKeywordInput("");
  }

  function removeKeyword(keyword: string) {
    setProposal((current) => ({
      ...current,
      preliminary_keywords: current.preliminary_keywords.filter(
        (item) => item !== keyword,
      ),
    }));
  }

  function handleKeywordKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      addKeyword(keywordInput);
    }
    if (
      event.key === "Backspace" &&
      !keywordInput &&
      proposal.preliminary_keywords.length > 0
    ) {
      setProposal((current) => ({
        ...current,
        preliminary_keywords: current.preliminary_keywords.slice(0, -1),
      }));
    }
  }

  return (
    <section className={sectionClasses}>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.proposal.heading")}</h2>
        {proposalBundle ? (
          <span className="rounded-full bg-slate-100 px-3 py-1 text-sm font-semibold text-slate-700">
            v{String(proposalBundle.version).padStart(3, "0")}
          </span>
        ) : null}
      </div>
      {currentState === "DOMAIN_LOADED" ? (
        <div className="mt-4 grid gap-3">
          <label className="grid gap-2 text-sm font-semibold text-slate-800">
            {t("workspace.proposal.draft_notes")}
            <textarea
              className="min-h-36 w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20"
              value={userDraft}
              onChange={(event) => setUserDraft(event.target.value)}
            />
          </label>
          <button
            type="button"
            className={primaryButtonClasses}
            onClick={() => onGenerate(userDraft)}
            disabled={isStartingProposal}
          >
            {isStartingProposal
              ? t("workspace.proposal.drafting_button")
              : t("workspace.proposal.generate_initial")}
          </button>
        </div>
      ) : null}
      {currentState === "PROPOSAL_DRAFTING" ? (
        <div className="mt-4">
          <PhaseRunningBanner
            phase="proposal"
            hintEvent={progress[0] ?? null}
          />
          {progress.length > 0 ? (
            <ul className={cardListClasses}>
              {progress.map((event) => (
                <li className={infoCardClasses} key={event.id}>
                  {describeEvent(t, event)}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      {hasProposal && isPostAcceptEdit ? (
        <p
          className="mt-5 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900"
          data-testid="proposal-post-accept-warning"
        >
          {t("workspace.proposal.post_accept_warning")}
        </p>
      ) : null}
      {!hasProposal &&
      proposalMissing &&
      currentState !== "DOMAIN_LOADED" &&
      currentState !== "PROPOSAL_DRAFTING" ? (
        <div
          className="mt-5 rounded-md border border-slate-200 bg-slate-50 px-4 py-3"
          data-testid="workspace-proposal-empty-state"
        >
          <h3
            className="text-sm font-bold text-slate-900"
            data-testid="workspace-proposal-empty-state-title"
          >
            {t("workspace.proposal.no_proposal_yet")}
          </h3>
          <p
            className="mt-2 leading-7 text-slate-700"
            data-testid="workspace-proposal-empty-state-body"
          >
            {t("workspace.proposal.no_proposal_yet_body")}
          </p>
        </div>
      ) : null}
      {hasProposal ? (
        <div
          className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(18rem,0.9fr)] lg:items-start"
          data-testid="proposal-edit-area"
          data-can-edit={canEdit ? "true" : "false"}
          data-post-accept-edit={isPostAcceptEdit ? "true" : "false"}
        >
          <form
            className="grid gap-4"
            data-testid="proposal-form"
            data-save-mode={saveMode}
            onSubmit={(event) => {
              event.preventDefault();
              void onSave(cleanProposal(proposal), {
                mode: saveMode,
                base_version: proposalBundle?.version,
              });
            }}
          >
            <ProposalTextarea
              label={t("workspace.proposal.research_question")}
              value={proposal.research_question}
              disabled={!canEdit || isSavingProposal}
              onChange={(value) => updateField("research_question", value)}
              testId="proposal-research-question-textarea"
            />
            <ProposalTextarea
              label={t("workspace.proposal.significance")}
              value={proposal.significance}
              disabled={!canEdit || isSavingProposal}
              onChange={(value) => updateField("significance", value)}
              testId="proposal-significance-textarea"
            />
            <ProposalTextarea
              label={t("workspace.proposal.preliminary_approach")}
              value={proposal.preliminary_approach}
              disabled={!canEdit || isSavingProposal}
              onChange={(value) => updateField("preliminary_approach", value)}
              testId="proposal-approach-textarea"
            />
            <ProposalTextarea
              label={t("workspace.proposal.expected_contribution")}
              value={proposal.expected_contribution}
              disabled={!canEdit || isSavingProposal}
              onChange={(value) => updateField("expected_contribution", value)}
              testId="proposal-contribution-textarea"
            />
            <ProposalTextarea
              label={t("workspace.proposal.scope")}
              value={proposal.scope}
              disabled={!canEdit || isSavingProposal}
              onChange={(value) => updateField("scope", value)}
              testId="proposal-scope-textarea"
            />
            <label className="grid gap-2 text-sm font-semibold text-slate-800">
              {t("workspace.proposal.preliminary_keywords")}
              <div className="flex min-h-11 flex-wrap items-center gap-2 rounded-md border border-slate-300 px-2 py-2 focus-within:border-[#114b5f] focus-within:ring-2 focus-within:ring-[#114b5f]/20">
                {proposal.preliminary_keywords.map((keyword) => (
                  <span
                    className="inline-flex min-h-8 items-center gap-2 rounded-md bg-slate-100 px-2 text-sm font-semibold text-slate-700"
                    key={keyword}
                  >
                    {keyword}
                    {canEdit ? (
                      <button
                        type="button"
                        className="text-[#114b5f]"
                        aria-label={t(
                          "workspace.proposal.remove_keyword_aria",
                          { keyword },
                        )}
                        onClick={() => removeKeyword(keyword)}
                      >
                        x
                      </button>
                    ) : null}
                  </span>
                ))}
                <input
                  className="min-h-8 min-w-32 flex-1 border-0 bg-transparent px-1 text-sm text-slate-950 outline-none"
                  value={keywordInput}
                  onChange={(event) => setKeywordInput(event.target.value)}
                  onBlur={() => addKeyword(keywordInput)}
                  onKeyDown={handleKeywordKeyDown}
                  disabled={!canEdit || isSavingProposal}
                />
              </div>
            </label>
            {canEdit && !isPostAcceptEdit ? (
              <label className="grid gap-2 text-sm font-semibold text-slate-800">
                {t("workspace.proposal.draft_notes")}
                <textarea
                  data-testid="proposal-draft-notes-textarea"
                  className="min-h-28 w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20"
                  value={userDraft}
                  onChange={(event) => setUserDraft(event.target.value)}
                />
              </label>
            ) : null}
            {canEdit && replaceEligible ? (
              <fieldset
                className="rounded-md border border-slate-200 p-3"
                data-testid="proposal-mode-fieldset"
              >
                <legend className="px-1 text-xs font-bold text-slate-700">
                  {t("workspace.edit_content.mode_heading")}
                </legend>
                <label className="flex items-start gap-2 py-1">
                  <input
                    type="radio"
                    data-testid="proposal-mode-replace"
                    name="proposal-save-mode"
                    checked={saveMode === "replace"}
                    onChange={() => setSaveMode("replace")}
                    disabled={isSavingProposal}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-sm font-semibold text-slate-950">
                      {t("workspace.proposal.mode.replace")}
                    </span>
                    <span className="ml-2 text-xs text-slate-600">
                      {t("workspace.proposal.mode.replace_hint").replace(
                        /\{version\}/g,
                        proposalBundle
                          ? String(proposalBundle.version).padStart(3, "0")
                          : "",
                      )}
                    </span>
                  </span>
                </label>
                <label className="flex items-start gap-2 py-1">
                  <input
                    type="radio"
                    data-testid="proposal-mode-new"
                    name="proposal-save-mode"
                    checked={saveMode === "new"}
                    onChange={() => setSaveMode("new")}
                    disabled={isSavingProposal}
                    className="mt-1"
                  />
                  <span>
                    <span className="text-sm font-semibold text-slate-950">
                      {t("workspace.proposal.mode.new")}
                    </span>
                    <span className="ml-2 text-xs text-slate-600">
                      {t("workspace.proposal.mode.new_hint").replace(
                        /\{next_version\}/g,
                        proposalBundle
                          ? String(proposalBundle.version + 1).padStart(3, "0")
                          : "",
                      )}
                    </span>
                  </span>
                </label>
              </fieldset>
            ) : null}
            {canEdit ? (
              <div className={inlineActionsClasses}>
                {/* Regenerate / Accept-and-search make sense only
                    in USER_PROPOSAL_REVIEW; post-accept the user
                    can only re-save (which marks downstream stale). */}
                {!isPostAcceptEdit ? (
                  <button
                    type="button"
                    data-testid="proposal-regenerate-button"
                    className={secondaryButtonClasses}
                    onClick={() => onRegenerate(userDraft)}
                    disabled={isStartingProposal}
                  >
                    {isStartingProposal
                      ? t("workspace.proposal.regenerating")
                      : t("workspace.proposal.regenerate")}
                  </button>
                ) : null}
                <button
                  type="submit"
                  data-testid="proposal-save-button"
                  className={
                    isPostAcceptEdit
                      ? primaryButtonClasses
                      : secondaryButtonClasses
                  }
                  disabled={isSavingProposal}
                >
                  {isSavingProposal
                    ? t("workspace.proposal.saving")
                    : t("workspace.proposal.save_edits")}
                </button>
                {!isPostAcceptEdit ? (
                  <button
                    type="button"
                    data-testid="proposal-accept-button"
                    className={primaryButtonClasses}
                    onClick={onAccept}
                    disabled={isAcceptingProposal}
                  >
                    {isAcceptingProposal
                      ? t("workspace.proposal.starting_search")
                      : t("workspace.proposal.accept_and_search")}
                  </button>
                ) : null}
              </div>
            ) : null}
          </form>
          <article className="min-w-0">
            <MarkdownView markdown={previewMarkdown} />
          </article>
        </div>
      ) : !proposalMissing &&
        currentState !== "DOMAIN_LOADED" &&
        currentState !== "PROPOSAL_DRAFTING" ? (
        <p className="leading-7 text-slate-700">
          {t("workspace.proposal.artifact_pending")}
        </p>
      ) : null}
    </section>
  );
}

function ProposalTextarea({
  label,
  value,
  disabled,
  onChange,
  testId,
}: {
  label: string;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
  testId?: string;
}) {
  return (
    <label className="grid gap-2 text-sm font-semibold text-slate-800">
      {label}
      <textarea
        data-testid={testId}
        className="min-h-28 w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20 disabled:bg-slate-50 disabled:text-slate-700"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function emptyProposal(): ProposalContent {
  return {
    research_question: "",
    significance: "",
    preliminary_approach: "",
    expected_contribution: "",
    scope: "",
    preliminary_keywords: [],
  };
}

function cleanProposal(proposal: ProposalContent): ProposalContent {
  return {
    research_question: proposal.research_question.trim(),
    significance: proposal.significance.trim(),
    preliminary_approach: proposal.preliminary_approach.trim(),
    expected_contribution: proposal.expected_contribution.trim(),
    scope: proposal.scope.trim(),
    preliminary_keywords: proposal.preliminary_keywords
      .map((keyword) => keyword.trim())
      .filter(Boolean),
  };
}

function proposalToMarkdown(
  proposal: ProposalContent,
  t: (key: string, vars?: Record<string, string | number>) => string,
): string {
  const cleaned = cleanProposal(proposal);
  const pending = t("workspace.proposal.md_pending");
  return [
    `# ${t("workspace.proposal.md_initial_proposal")}`,
    "",
    `## ${t("workspace.proposal.md_research_question")}`,
    "",
    cleaned.research_question || pending,
    "",
    `## ${t("workspace.proposal.md_significance")}`,
    "",
    cleaned.significance || pending,
    "",
    `## ${t("workspace.proposal.md_preliminary_approach")}`,
    "",
    cleaned.preliminary_approach || pending,
    "",
    `## ${t("workspace.proposal.md_expected_contribution")}`,
    "",
    cleaned.expected_contribution || pending,
    "",
    `## ${t("workspace.proposal.md_scope")}`,
    "",
    cleaned.scope || pending,
    "",
    `## ${t("workspace.proposal.md_preliminary_keywords")}`,
    "",
    cleaned.preliminary_keywords.join(", ") || t("workspace.common.none_lower"),
  ].join("\n");
}

// PR-B3: per-project corpus management surfaced inline in the
// workspace, so users no longer have to leave the workspace and
// navigate to /corpus to upload prior papers or change which
// global corpora this project uses. Reads via
// ``GET /api/projects/{id}/corpus`` (PR #112), writes via
// ``PUT .../corpus/selection`` and ``POST .../corpus/upload``.
//
// Codex AGREE-with-amendments 2026-05-01 verdict applied:
// - tab always visible (project always has a corpus)
// - selection PUT serialized: disable toggles while one is in
//   flight, refetch the canonical state on response
// - upload pre-checks size > 30 MB AND empty file before POST
// - empty state CTA covers no-project-docs / no-globals /
//   nothing-at-all
// - amber warning shown when ``hasDraftRun`` because changing
//   the selection after drafter ran requires a drafter rerun
//   for full dedup effect (backend stale propagation is a
//   separate follow-up).
function CorpusSubview({
  projectId,
  hasDraftRun,
}: {
  projectId: string;
  hasDraftRun: boolean;
}) {
  const t = useT();
  const [data, setData] = useState<ProjectCorpusResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // ``savingId`` is the corpus_id whose toggle is currently in
  // flight (codex amendment 3 — serialize PUTs, no concurrent
  // checkbox toggles).
  const [savingId, setSavingId] = useState<string | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  async function refetch() {
    setIsLoading(true);
    setLoadError(null);
    try {
      const response = await getProjectCorpus(projectId);
      setData(response);
    } catch (caught) {
      setLoadError(
        caught instanceof Error
          ? caught.message
          : t("workspace.corpus.error.load"),
      );
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    void refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function handleToggle(corpusId: string, nextSelected: boolean) {
    if (!data) return;
    if (savingId !== null) return;
    setSavingId(corpusId);
    setSelectionError(null);
    const currentlySelected = data.global_corpora
      .filter((entry) => entry.is_selected)
      .map((entry) => entry.id);
    const nextIds = nextSelected
      ? Array.from(new Set([...currentlySelected, corpusId]))
      : currentlySelected.filter((id) => id !== corpusId);
    try {
      await setProjectCorpusSelection(projectId, nextIds);
      // Refetch the canonical state instead of merging
      // optimistically — codex amendment 3.
      await refetch();
    } catch (caught) {
      setSelectionError(
        caught instanceof Error
          ? caught.message
          : t("workspace.corpus.error.selection"),
      );
    } finally {
      setSavingId(null);
    }
  }

  async function handleUpload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setUploadError(null);
    if (file.size === 0) {
      setUploadError(t("workspace.corpus.upload.error.empty"));
      return;
    }
    if (file.size > 30 * 1024 * 1024) {
      setUploadError(t("workspace.corpus.upload.error.too_large"));
      return;
    }
    setIsUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      await uploadProjectCorpusDocument(projectId, form);
      await refetch();
    } catch (caught) {
      setUploadError(
        caught instanceof Error
          ? caught.message
          : t("workspace.corpus.error.upload"),
      );
    } finally {
      setIsUploading(false);
    }
  }

  if (isLoading) {
    return (
      <section className={sectionClasses} data-testid="corpus-subview-loading">
        <p className="leading-7 text-slate-700">{t("workspace.loading")}</p>
      </section>
    );
  }

  if (loadError !== null || data === null) {
    return (
      <section className={sectionClasses} data-testid="corpus-subview-error">
        <p className="rounded-md bg-red-50 px-4 py-3 text-red-700">
          {loadError ?? t("workspace.corpus.error.load")}
        </p>
      </section>
    );
  }

  const fullyEmpty =
    data.project_documents.length === 0 && data.global_corpora.length === 0;
  const noGlobals = data.global_corpora.length === 0;

  return (
    <section className={sectionClasses} data-testid="corpus-subview">
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.corpus.heading")}</h2>
      </div>
      <p className="mt-2 text-sm leading-6 text-slate-600">
        {t("workspace.corpus.intro")}
      </p>

      {hasDraftRun ? (
        <p
          className="mt-4 rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm leading-6 text-amber-900"
          data-testid="corpus-subview-stale-warning"
        >
          {t("workspace.corpus.warn.stale_after_draft")}
        </p>
      ) : null}

      {fullyEmpty ? (
        <p
          className="mt-4 rounded-md border border-slate-200 bg-slate-50 px-4 py-3 text-sm leading-6 text-slate-700"
          data-testid="corpus-subview-empty-fully"
        >
          {t("workspace.corpus.empty.fully_empty")}
        </p>
      ) : null}

      {/* Upload: always visible so the user can grow the
          project's prior-papers list. */}
      <fieldset
        className="mt-5 grid gap-2 rounded-md border border-slate-200 p-4"
        data-testid="corpus-subview-upload"
      >
        <legend className="px-1 text-sm font-bold text-slate-900">
          {t("workspace.corpus.upload.label")}
        </legend>
        <p className="text-xs text-slate-600">
          {t("workspace.corpus.upload.hint")}
        </p>
        <label className="inline-flex w-fit items-center">
          <input
            type="file"
            data-testid="corpus-subview-upload-input"
            accept=".pdf,.docx,.md,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/markdown,text/plain"
            disabled={isUploading}
            onChange={(event) => void handleUpload(event)}
            className="text-sm"
          />
        </label>
        {isUploading ? (
          <p
            className="text-xs text-slate-700"
            data-testid="corpus-subview-upload-status"
          >
            {t("workspace.corpus.upload.uploading")}
          </p>
        ) : null}
        {uploadError ? (
          <p
            className="text-xs text-red-700"
            data-testid="corpus-subview-upload-error"
          >
            {uploadError}
          </p>
        ) : null}
      </fieldset>

      {/* Project documents — always show the heading, list rows
          when present, empty-state copy otherwise. */}
      <section className="mt-6" data-testid="corpus-subview-project-section">
        <h3 className="text-base font-bold text-slate-900">
          {t("workspace.corpus.section.project")}
        </h3>
        {data.project_documents.length === 0 ? (
          <p
            className="mt-2 text-sm text-slate-700"
            data-testid="corpus-subview-empty-project-docs"
          >
            {t("workspace.corpus.empty.no_project_docs")}
          </p>
        ) : (
          <ul
            className="mt-2 grid gap-2"
            data-testid="corpus-subview-project-docs"
          >
            {data.project_documents.map((doc) => (
              <li
                key={doc.id}
                data-testid={`corpus-subview-project-doc-${doc.id}`}
                className="grid gap-1 rounded-md border border-slate-200 px-3 py-2 sm:flex sm:items-center sm:justify-between"
              >
                <span className="text-sm font-semibold text-slate-900">
                  {doc.title}
                </span>
                <span className="flex flex-wrap items-center gap-3 text-xs text-slate-600">
                  <span>
                    {t("workspace.corpus.column.status")}: {doc.ingest_status}
                  </span>
                  {doc.original_size_bytes != null ? (
                    <span>
                      {t("workspace.corpus.column.size")}:{" "}
                      {Math.round(doc.original_size_bytes / 1024)} KB
                    </span>
                  ) : null}
                  <span>
                    {t("workspace.corpus.column.uploaded")}:{" "}
                    {new Date(doc.created_at).toLocaleDateString()}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Global selection — shown whenever the user has any
          globals; ``noGlobals`` shows an empty-state hint. */}
      <section className="mt-6" data-testid="corpus-subview-globals-section">
        <h3 className="text-base font-bold text-slate-900">
          {t("workspace.corpus.section.globals")}
        </h3>
        {noGlobals ? (
          <p
            className="mt-2 text-sm text-slate-700"
            data-testid="corpus-subview-empty-globals"
          >
            {t("workspace.corpus.empty.no_globals")}
          </p>
        ) : (
          <ul className="mt-2 grid gap-2" data-testid="corpus-subview-globals">
            {data.global_corpora.map((entry) => (
              <li
                key={entry.id}
                data-testid={`corpus-subview-global-${entry.id}`}
                className="grid gap-1 rounded-md border border-slate-200 px-3 py-2 sm:flex sm:items-center sm:justify-between"
              >
                <label className="flex items-center gap-2 text-sm font-semibold text-slate-900">
                  <input
                    type="checkbox"
                    data-testid={`corpus-subview-global-toggle-${entry.id}`}
                    checked={entry.is_selected}
                    disabled={savingId !== null}
                    onChange={(event) =>
                      void handleToggle(entry.id, event.target.checked)
                    }
                  />
                  {entry.name}
                </label>
                <span className="text-xs text-slate-600">
                  {t("workspace.corpus.column.docs")}: {entry.document_count}
                </span>
              </li>
            ))}
          </ul>
        )}
        {savingId !== null ? (
          <p
            className="mt-2 text-xs text-slate-600"
            data-testid="corpus-subview-saving"
          >
            {t("workspace.corpus.saving")}
          </p>
        ) : null}
        {selectionError ? (
          <p
            className="mt-2 text-xs text-red-700"
            data-testid="corpus-subview-selection-error"
          >
            {selectionError}
          </p>
        ) : null}
      </section>

      {/* Pointer to the standalone /corpus page for delete /
          rebuild profile / etc. — keep it as a single source of
          truth, don't duplicate global management here. */}
      <p className="mt-6 text-sm">
        <Link
          to="/corpus"
          data-testid="corpus-subview-manage-globals"
          className="text-[#114b5f] underline"
        >
          {t("workspace.corpus.manage_globals")}
        </Link>
      </p>
    </section>
  );
}

// PR-C2.b: dedicated lens subview. Audit round-2 finding: lens
// states (FRAMEWORK_LENS_RUNNING / USER_LENS_REVIEW) used to route
// to the novelty subview, but novelty's visibility gate omitted
// those states → empty page. This component is the reachable
// surface. Always-render + disabled + hint pattern per audit
// round-2 principle.
function FrameworkLensSubview({
  runId,
  currentState,
  paperMode,
  frameworkLensBundle,
  isStartingFrameworkLens,
  isStartingIdeator,
  onRunFrameworkLens,
  onRunIdeator,
  synthesizerCompleted,
}: {
  runId: string | undefined;
  currentState: string | undefined;
  paperMode: string | undefined;
  frameworkLensBundle: FrameworkLensBundle | null;
  isStartingFrameworkLens: boolean;
  isStartingIdeator: boolean;
  onRunFrameworkLens: () => Promise<void>;
  onRunIdeator: () => Promise<void>;
  synthesizerCompleted: boolean;
}) {
  const t = useT();
  const [expandedSignalIndex, setExpandedSignalIndex] = useState<number | null>(
    null,
  );
  // Lens runnable when state == USER_FIELD_REVIEW (synthesizer
  // done, lens not yet started) AND synthesis output exists.
  const canRun = currentState === "USER_FIELD_REVIEW" && synthesizerCompleted;
  // Theory_article specifically REQUIRES lens; non-theory modes
  // can choose to skip via direct ideator start.
  const isMandatory = paperMode === "theory_article";
  // PR-C2.b Tier 4: read from the dedicated framework_lens artifact
  // (lens_name + key_concepts + applicability_to_kernel + source_id),
  // not the synthesizer's claim track. The previous render fell back
  // to dual_track.theoretical_lens_track which contained different
  // (claim-shaped) data.
  const lensSignals = frameworkLensBundle?.signals ?? [];
  const hasLensArtifact = frameworkLensBundle?.artifact_present === true;
  const editPendingTooltip = t("workspace.lens.edit_pending_tooltip");

  // PR-394 (extended): twin of the ``pastIdeatorStates`` fix on the
  // Novelty tab. ``synthesizerCompleted`` is derived from the recent
  // events window — at late run states (USER_EXTERNAL_SCAN_APPROVAL,
  // EXPORTS_RUNNING, etc.) the synthesizer phase_done event has
  // scrolled out of that window and the flag goes False, which made
  // the framework_lens tab show a stale
  // "需要先完成综合节点后才能启动框架镜框" hint. Derive a state-based
  // override: any state strictly past synthesizer implies synthesizer
  // completed.
  const postSynthesizerStates = new Set<string | undefined>([
    "USER_FIELD_REVIEW",
    "FRAMEWORK_LENS_RUNNING",
    "USER_LENS_REVIEW",
    "IDEATOR_RUNNING",
    "USER_NOVELTY_REVIEW",
    "DRAFTER_RUNNING",
    "STYLIST_RUNNING",
    "USER_REVISION_REVIEW",
    "REWRITE_RUNNING",
    "CRITIC_RUNNING",
    "USER_EXTERNAL_SCAN_APPROVAL",
    "INTEGRITY_RUNNING",
    "USER_INTEGRITY_REVIEW",
    "USER_FINAL_ACCEPTANCE",
    "EXPORTS_RUNNING",
    "EXPORTS_DONE",
  ]);
  const synthesizerDone =
    synthesizerCompleted || postSynthesizerStates.has(currentState);
  // States strictly past the lens phase — at any of these the lens
  // node has either run successfully or been skipped (non-theory
  // mode). UI should advertise "already done", not "not_yet" / waiting.
  const postLensStates = new Set<string | undefined>([
    "IDEATOR_RUNNING",
    "USER_NOVELTY_REVIEW",
    "DRAFTER_RUNNING",
    "STYLIST_RUNNING",
    "USER_REVISION_REVIEW",
    "REWRITE_RUNNING",
    "CRITIC_RUNNING",
    "USER_EXTERNAL_SCAN_APPROVAL",
    "INTEGRITY_RUNNING",
    "USER_INTEGRITY_REVIEW",
    "USER_FINAL_ACCEPTANCE",
    "EXPORTS_RUNNING",
    "EXPORTS_DONE",
  ]);

  let buttonHint: string | null = null;
  if (currentState === "FRAMEWORK_LENS_RUNNING") {
    buttonHint = t("workspace.lens.running_hint");
  } else if (currentState === "USER_LENS_REVIEW") {
    buttonHint = t("workspace.lens.review_hint");
  } else if (postLensStates.has(currentState)) {
    // PR-394: don't show "waiting_for_synthesizer" or
    // "not_yet_at_field_review" at late states — they're stale.
    buttonHint = null;
  } else if (!synthesizerDone) {
    buttonHint = t("workspace.lens.waiting_for_synthesizer");
  } else if (currentState !== "USER_FIELD_REVIEW") {
    buttonHint = t("workspace.lens.not_yet_at_field_review");
  }

  return (
    <section className={sectionClasses} data-testid="lens-subview">
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.lens.heading")}</h2>
        <div className={inlineActionsClasses}>
          <button
            type="button"
            data-testid="lens-edit-button"
            className={
              secondaryButtonClasses +
              " sm:w-auto disabled:cursor-not-allowed disabled:opacity-60"
            }
            disabled
            title={editPendingTooltip}
            aria-label={`${t("workspace.edit_content.button")} - ${editPendingTooltip}`}
          >
            {t("workspace.edit_content.button")}
          </button>
          <button
            type="button"
            data-testid="phase-action-framework-lens"
            className={primaryButtonClasses}
            onClick={onRunFrameworkLens}
            disabled={!canRun || isStartingFrameworkLens}
            aria-describedby={buttonHint ? "lens-button-hint" : undefined}
          >
            {isStartingFrameworkLens
              ? t("workspace.lens.starting")
              : t("workspace.lens.run")}
          </button>
          {/* PR-real-paper-fix (codex round-1 AGREE): in-place
              "confirm + advance to novelty" button. Lens review is
              the user-facing confirmation gate; the action that
              advances state is "phase-action-ideator" (same handler
              + testid the novelty subview uses for the
              lens-skipped path). Visible + enabled only when state
              is USER_LENS_REVIEW so it reads as the natural next
              step after reviewing lens signals. The novelty
              subview's button is in a different mounted subview
              (only the active tab is rendered), so testid stays
              unique in the DOM. */}
          {currentState === "USER_LENS_REVIEW" ? (
            <button
              type="button"
              data-testid="phase-action-ideator"
              className={primaryButtonClasses}
              onClick={onRunIdeator}
              disabled={isStartingIdeator}
            >
              {isStartingIdeator
                ? t("workspace.lens.starting_ideator")
                : t("workspace.lens.confirm_and_ideate")}
            </button>
          ) : null}
        </div>
      </div>
      {buttonHint ? (
        <p
          id="lens-button-hint"
          role="status"
          data-testid="lens-button-hint"
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {buttonHint}
        </p>
      ) : null}
      {/* PR-real-paper-fix (codex Q4 AGREE-w-amend): mandatory hint
          should only show for theory_article AND only before lens
          completes — currently it would block-look every paper_mode
          with the theory message. Hide once the lens artifact lands. */}
      {isMandatory && !hasLensArtifact ? (
        <p
          data-testid="lens-mandatory-hint"
          className="rounded-md border border-purple-300 bg-purple-50 p-2 text-xs leading-5 text-purple-900"
        >
          {t("workspace.lens.theory_article_mandatory")}
        </p>
      ) : null}
      {currentState === "FRAMEWORK_LENS_RUNNING" ? (
        <>
          <PhaseRunningBanner phase="framework_lens" />
          <p
            data-testid="lens-running-indicator"
            className="leading-7 text-slate-700"
          >
            {t("workspace.lens.running_indicator")}
          </p>
        </>
      ) : null}
      {hasLensArtifact && lensSignals.length > 0 ? (
        <section className="my-4" data-testid="lens-signals">
          <h3 className="mb-2 text-base font-bold text-slate-950">
            {t("workspace.lens.signals_heading")}
          </h3>
          <ul className="grid gap-3">
            {lensSignals.map((signal, idx) => (
              <li
                key={idx}
                className="rounded-lg border border-purple-200 bg-purple-50 p-2 text-sm"
              >
                <button
                  type="button"
                  data-testid={`lens-signal-${idx}`}
                  className="grid w-full gap-2 rounded-md px-2 py-1 text-left transition hover:bg-white/70 focus-visible:ring-2 focus-visible:ring-purple-500 focus-visible:outline-none"
                  aria-expanded={expandedSignalIndex === idx}
                  aria-controls={`lens-signal-${idx}-details`}
                  onClick={() =>
                    setExpandedSignalIndex((current) =>
                      current === idx ? null : idx,
                    )
                  }
                >
                  <span className="text-base font-bold text-slate-950">
                    {signal.lens_name}
                  </span>
                  {signal.key_concepts.length > 0 ? (
                    <span
                      className="flex flex-wrap gap-1"
                      data-testid={`lens-signal-${idx}-concepts`}
                    >
                      {signal.key_concepts.map((concept, ci) => (
                        <span
                          key={ci}
                          className="inline-flex items-center rounded-full border border-purple-300 bg-white px-2 py-0.5 text-xs font-semibold text-purple-900"
                        >
                          {concept}
                        </span>
                      ))}
                    </span>
                  ) : null}
                </button>
                {expandedSignalIndex === idx ? (
                  <div
                    id={`lens-signal-${idx}-details`}
                    data-testid={`lens-signal-${idx}-details`}
                    className="mt-2 px-2 pb-1"
                  >
                    {signal.applicability_to_kernel ? (
                      <p
                        className="leading-6 text-slate-700"
                        data-testid={`lens-signal-${idx}-applicability`}
                      >
                        {signal.applicability_to_kernel}
                      </p>
                    ) : null}
                    <p className="mt-2 text-xs text-slate-500">
                      {t("workspace.lens.signal_source_label")}:{" "}
                      {signal.source_id}
                    </p>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : hasLensArtifact ? (
        <p
          data-testid="lens-empty-artifact"
          className="text-sm italic text-slate-500"
        >
          {t("workspace.lens.empty_artifact")}
        </p>
      ) : currentState === "USER_LENS_REVIEW" ||
        currentState === "FRAMEWORK_LENS_RUNNING" ? (
        <p
          data-testid="lens-no-signals-yet"
          className="text-sm italic text-slate-500"
        >
          {t("workspace.lens.no_signals_yet")}
        </p>
      ) : null}
      {/* runId reserved for future per-lens edit surfaces */}
      {runId ? null : null}
    </section>
  );
}

function NoveltySubview({
  currentState,
  paperMode,
  noveltyBundle,
  discussion,
  isStartingIdeator,
  isSelectingAngle,
  isDiscussing,
  onRunIdeator,
  onSelectAngle,
  onDiscuss,
}: {
  currentState: string | undefined;
  paperMode: string | undefined;
  noveltyBundle: NoveltyBundle | null;
  discussion: NoveltyDiscussionMessage[];
  isStartingIdeator: boolean;
  isSelectingAngle: boolean;
  isDiscussing: boolean;
  onRunIdeator: () => Promise<void>;
  onSelectAngle: (angleId: string) => Promise<void>;
  onDiscuss: (userMessage: string) => Promise<void>;
}) {
  const t = useT();
  const [expandedAngleId, setExpandedAngleId] = useState<string | null>(null);
  const [currentAngleId, setCurrentAngleId] = useState<string | null>(null);
  const angleCards = useMemo(
    () => noveltyBundle?.angle_cards ?? [],
    [noveltyBundle?.angle_cards],
  );
  const outlinesByAngleId = useMemo(() => {
    const map = new Map<string, AngleOutline>();
    for (const outline of noveltyBundle?.detailed_outlines ?? []) {
      if (outline.angle_id) {
        map.set(outline.angle_id, outline);
      }
    }
    return map;
  }, [noveltyBundle?.detailed_outlines]);
  const selectedAngleId = noveltyBundle?.selected_thesis?.angle_id ?? null;
  const acceptedAngleId = currentAngleId ?? angleCards[0]?.angle_id ?? null;

  // Codex round-4 #1 (2026-05-03): theory_article must traverse
  // framework_lens before ideator. start_ideator rejects the
  // skip-direct path; mirror the gate here so the button reflects
  // backend authority.
  const ideatorRunnable =
    (currentState === "USER_FIELD_REVIEW" && paperMode !== "theory_article") ||
    currentState === "USER_LENS_REVIEW";
  // PR-394 (codex AGREE 2026-05-13): set is "states after the
  // ideator phase has produced angle cards". Previously had:
  // - USER_DEEP_DIVE_REVIEW (the curator-output review BEFORE ideator
  //   — never a post-ideator state, stray copy-paste from old design)
  // - USER_DRAFT_REVIEW (not a canonical state in PIPELINE_STATES)
  // …and was missing REWRITE_RUNNING (PR-360 slice E), and the two
  // post-critic checkpoint states USER_EXTERNAL_SCAN_APPROVAL +
  // USER_INTEGRITY_REVIEW. Bug surfaced on Test #3 of the 4-test
  // matrix: auto-pilot in REWRITE_RUNNING showed "上游节点尚未完成"
  // even though ideator + drafter + stylist had all completed.
  const pastIdeatorStates = new Set<string | undefined>([
    "USER_NOVELTY_REVIEW",
    "DRAFTER_RUNNING",
    "STYLIST_RUNNING",
    "USER_REVISION_REVIEW",
    "REWRITE_RUNNING",
    "CRITIC_RUNNING",
    "USER_EXTERNAL_SCAN_APPROVAL",
    "INTEGRITY_RUNNING",
    "USER_INTEGRITY_REVIEW",
    "USER_FINAL_ACCEPTANCE",
    "EXPORTS_RUNNING",
    "EXPORTS_DONE",
  ]);
  const ideatorHint = ideatorRunnable
    ? null
    : pastIdeatorStates.has(currentState)
      ? null
      : currentState === "IDEATOR_RUNNING"
        ? t("workspace.novelty.disabled.running")
        : t("workspace.novelty.disabled.upstream_pending");
  const acceptHint =
    currentState === "USER_NOVELTY_REVIEW"
      ? null
      : currentState === "IDEATOR_RUNNING"
        ? t("workspace.novelty.accept_disabled.running")
        : ideatorRunnable
          ? t("workspace.novelty.accept_disabled.waiting_ideator")
          : pastIdeatorStates.has(currentState)
            ? null
            : t("workspace.novelty.accept_disabled.upstream_pending");

  useEffect(() => {
    if (!angleCards.length) {
      setCurrentAngleId(null);
      return;
    }
    setCurrentAngleId((current) =>
      current && angleCards.some((card) => card.angle_id === current)
        ? current
        : angleCards[0].angle_id,
    );
  }, [angleCards]);

  return (
    <section className={sectionClasses}>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.novelty.heading")}</h2>
        <div className={inlineActionsClasses}>
          {/* PR-C2.b audit round-2: always-render + disabled + hint.
              Ideator reachable from both USER_FIELD_REVIEW
              (lens-skipped path) and USER_LENS_REVIEW (post-lens). */}
          <button
            type="button"
            data-testid="phase-action-ideator"
            className={primaryButtonClasses}
            onClick={onRunIdeator}
            disabled={!ideatorRunnable || isStartingIdeator}
            aria-describedby={ideatorHint ? "ideator-disabled-hint" : undefined}
          >
            {isStartingIdeator
              ? t("workspace.novelty.starting_ideator")
              : t("workspace.novelty.run_ideator")}
          </button>
          <button
            type="button"
            data-testid="novelty-accept-angle"
            className={primaryButtonClasses}
            onClick={() => {
              if (acceptedAngleId) {
                void onSelectAngle(acceptedAngleId);
              }
            }}
            disabled={
              !acceptedAngleId ||
              isSelectingAngle ||
              currentState !== "USER_NOVELTY_REVIEW"
            }
            aria-describedby={
              acceptHint ? "accept-angle-disabled-hint" : undefined
            }
          >
            {isSelectingAngle
              ? t("workspace.novelty.accepting")
              : t("workspace.novelty.accept_current")}
          </button>
        </div>
      </div>
      {ideatorHint || acceptHint ? (
        <p
          id={
            ideatorHint ? "ideator-disabled-hint" : "accept-angle-disabled-hint"
          }
          role="status"
          data-testid="novelty-disabled-hint"
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {ideatorHint || acceptHint}
        </p>
      ) : null}
      {currentState === "IDEATOR_RUNNING" ? (
        <PhaseRunningBanner phase="ideator" />
      ) : null}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1.35fr)_minmax(20rem,0.85fr)] lg:items-start">
        <div>
          {angleCards.length > 0 ? (
            <div className="grid gap-3 p-0">
              {angleCards.map((card) => {
                const isExpanded = expandedAngleId === card.angle_id;
                const isSelected = selectedAngleId === card.angle_id;
                const isCurrent = acceptedAngleId === card.angle_id;
                return (
                  <article
                    className={`rounded-lg border p-4 ${
                      isCurrent ? "border-[#114b5f]" : "border-slate-200"
                    }`}
                    key={card.angle_id}
                  >
                    <button
                      type="button"
                      className="flex min-h-11 w-full items-center justify-between gap-3 rounded-md bg-transparent py-1 text-left font-bold text-[#114b5f]"
                      onClick={() =>
                        setExpandedAngleId(isExpanded ? null : card.angle_id)
                      }
                    >
                      <span className="min-w-0">{card.working_title}</span>
                      <span className="shrink-0">
                        {isExpanded
                          ? t("workspace.novelty.collapse")
                          : t("workspace.novelty.expand")}
                      </span>
                    </button>
                    <p className="mt-3 leading-7 text-slate-700">
                      {card.thesis_one_sentence}
                    </p>
                    {isExpanded ? (
                      <dl className="mt-4 grid gap-2 md:grid-cols-[10rem_minmax(0,1fr)]">
                        <dt className="font-bold text-slate-500">
                          {t("workspace.novelty.why_novel")}
                        </dt>
                        <dd className="m-0 text-slate-700">{card.why_novel}</dd>
                        <dt className="font-bold text-slate-500">
                          {t("workspace.novelty.evidence_so_far")}
                        </dt>
                        <dd className="m-0 text-slate-700">
                          {card.evidence_so_far}
                        </dd>
                        <dt className="font-bold text-slate-500">
                          {t("workspace.novelty.missing_evidence")}
                        </dt>
                        <dd className="m-0 text-slate-700">
                          {card.missing_evidence}
                        </dd>
                        <dt className="font-bold text-slate-500">
                          {t("workspace.novelty.journal_fit")}
                        </dt>
                        <dd className="m-0 text-slate-700">
                          {card.journal_fit_note}
                        </dd>
                        <dt className="font-bold text-slate-500">
                          {t("workspace.novelty.key_claims")}
                        </dt>
                        <dd className="m-0 text-slate-700">
                          {card.key_claim_ids.join(", ") ||
                            t("workspace.common.none_lower")}
                        </dd>
                        <dt className="font-bold text-slate-500">
                          {t("workspace.novelty.risks")}
                        </dt>
                        <dd className="m-0 text-slate-700">
                          {card.risks.join("; ") ||
                            t("workspace.common.none_lower")}
                        </dd>
                        {/* PR-C2.b Tier 4 (PR #157): render
                            framework_lens + methodological_choice
                            when the angle card has them. Both are
                            optional — legacy cards from runs that
                            predate the schema render exactly as
                            before. */}
                        {card.framework_lens &&
                        card.framework_lens.length > 0 ? (
                          <>
                            <dt className="font-bold text-slate-500">
                              {t("workspace.novelty.framework_lens")}
                            </dt>
                            <dd
                              className="m-0 flex flex-wrap gap-1"
                              data-testid={`novelty-card-${card.angle_id}-framework-lens`}
                            >
                              {card.framework_lens.map((name, idx) => (
                                <span
                                  key={idx}
                                  className="inline-flex items-center rounded-full border border-purple-300 bg-purple-50 px-2 py-0.5 text-xs font-semibold text-purple-900"
                                >
                                  {name}
                                </span>
                              ))}
                            </dd>
                          </>
                        ) : null}
                        {card.methodological_choice ? (
                          <>
                            <dt className="font-bold text-slate-500">
                              {t("workspace.novelty.methodological_choice")}
                            </dt>
                            <dd
                              className="m-0 text-slate-700"
                              data-testid={`novelty-card-${card.angle_id}-methodological-choice`}
                            >
                              {card.methodological_choice}
                            </dd>
                          </>
                        ) : null}
                      </dl>
                    ) : null}
                    {isExpanded ? (
                      <DetailedOutlinePanel
                        outline={outlinesByAngleId.get(card.angle_id) ?? null}
                      />
                    ) : null}
                    {currentState === "USER_NOVELTY_REVIEW" ? (
                      <div className="mt-4 grid gap-2 sm:flex sm:flex-wrap">
                        <button
                          type="button"
                          className={secondaryButtonClasses}
                          onClick={() => setCurrentAngleId(card.angle_id)}
                        >
                          {isCurrent
                            ? t("workspace.novelty.current_choice")
                            : t("workspace.novelty.make_current")}
                        </button>
                        <button
                          type="button"
                          className={primaryButtonClasses}
                          onClick={() => onSelectAngle(card.angle_id)}
                          disabled={isSelectingAngle}
                        >
                          {isSelectingAngle
                            ? t("workspace.novelty.selecting")
                            : t("workspace.novelty.select_angle")}
                        </button>
                      </div>
                    ) : null}
                    {isSelected ? (
                      <p className={noticeClasses + " mt-3"}>
                        {t("workspace.novelty.selected_for_drafting")}
                      </p>
                    ) : null}
                  </article>
                );
              })}
            </div>
          ) : (
            <p className="leading-7 text-slate-700">
              {t("workspace.novelty.no_cards")}
            </p>
          )}
          <h2 className={h2Classes}>{t("workspace.novelty.ideator_report")}</h2>
          <pre className={reportPreClasses}>
            {noveltyBundle?.ideator_report ||
              t("workspace.console.report_pending")}
          </pre>
        </div>
        <NoveltyChatPanel
          messages={discussion}
          disabled={isDiscussing || currentState !== "USER_NOVELTY_REVIEW"}
          isDiscussing={isDiscussing}
          onDiscuss={onDiscuss}
        />
      </div>
    </section>
  );
}

function NoveltyChatPanel({
  messages,
  disabled,
  isDiscussing,
  onDiscuss,
}: {
  messages: NoveltyDiscussionMessage[];
  disabled: boolean;
  isDiscussing: boolean;
  onDiscuss: (userMessage: string) => Promise<void>;
}) {
  const t = useT();
  const [message, setMessage] = useState("");

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = message.trim();
    if (!trimmed) {
      return;
    }
    setMessage("");
    void onDiscuss(trimmed);
  }

  return (
    <aside className="rounded-lg border border-slate-200 p-4">
      <h2 className="text-lg font-bold text-slate-950">
        {t("workspace.novelty.discussion_heading")}
      </h2>
      <div className="mt-4 grid max-h-[28rem] gap-3 overflow-y-auto pr-1">
        {messages.length ? (
          messages.map((item) => (
            <div
              className={`flex ${item.role === "user" ? "justify-end" : "justify-start"}`}
              key={item.id}
            >
              <p
                className={`max-w-[85%] rounded-lg px-3 py-2 text-sm leading-6 ${
                  item.role === "user"
                    ? "bg-[#114b5f] text-white"
                    : "bg-slate-100 text-slate-800"
                }`}
              >
                {item.content}
              </p>
            </div>
          ))
        ) : (
          <p className="leading-7 text-slate-700">
            {t("workspace.novelty.no_discussion")}
          </p>
        )}
        {isDiscussing ? (
          <div className="flex justify-start">
            <p className="max-w-[85%] rounded-lg bg-slate-100 px-3 py-2 text-sm leading-6 text-slate-800">
              {t("workspace.novelty.regenerating_cards")}
            </p>
          </div>
        ) : null}
      </div>
      <form className="mt-4 grid gap-2" onSubmit={handleSubmit}>
        <textarea
          className="min-h-28 w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          disabled={disabled}
        />
        <button
          type="submit"
          className={primaryButtonClasses}
          disabled={disabled || !message.trim()}
        >
          {isDiscussing
            ? t("workspace.novelty.submitting")
            : t("workspace.novelty.submit")}
        </button>
      </form>
    </aside>
  );
}

function DraftSubview({
  currentState,
  draftList,
  activeDraft,
  progress,
  drafterCompleted,
}: {
  currentState: string | undefined;
  draftList: DraftListBundle | null;
  activeDraft: DraftBundle | null;
  progress: RunEvent[];
  // PR #319 follow-up: drafter doesn't auto-transition on phase_done
  // (run state stays DRAFTER_RUNNING waiting for the user to start
  // stylist). Without this flag the banner keeps pulsing after the
  // agent has finished — the bug user reported on PR #317.
  drafterCompleted: boolean;
}) {
  const t = useT();
  const bibHref = useMemo(() => {
    if (!activeDraft?.citations_bib) {
      return "";
    }
    return `data:text/plain;charset=utf-8,${encodeURIComponent(activeDraft.citations_bib)}`;
  }, [activeDraft?.citations_bib]);

  return (
    <section className={sectionClasses}>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.draft.heading")}</h2>
        {activeDraft ? (
          <span className="rounded-full bg-slate-100 px-3 py-1 text-sm font-semibold text-slate-700">
            {activeDraft.version}
          </span>
        ) : null}
      </div>
      {currentState === "DRAFTER_RUNNING" ? (
        <PhaseRunningBanner
          phase="drafter"
          progress={progress}
          hintEvent={progress[0] ?? null}
          completed={drafterCompleted}
        />
      ) : null}
      {currentState === "DRAFTER_RUNNING" ? (
        <>
          <h2 className={h2Classes}>{t("workspace.draft.progress_heading")}</h2>
          {progress.length > 0 ? (
            <ul className={cardListClasses}>
              {progress.map((event) => (
                <li
                  className={infoCardClasses + " sm:flex sm:justify-between"}
                  key={event.id}
                >
                  <strong>
                    {String(
                      event.payload.section_title ??
                        t("workspace.common.section_default"),
                    )}
                  </strong>
                  <span>
                    {String(
                      event.payload.status ??
                        t("workspace.common.status_pending"),
                    )}
                  </span>
                  <span>
                    {Number(event.payload.completed ?? 0)} /{" "}
                    {Number(event.payload.total ?? 0)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="leading-7 text-slate-700">
              {t("workspace.draft.no_progress")}
            </p>
          )}
        </>
      ) : null}
      {draftList?.drafts.length ? (
        <p className="leading-7 text-slate-700">
          {t("workspace.draft.versions_label")}:{" "}
          {draftList.drafts.map((draft) => draft.version).join(", ")}
          {activeDraft?.metadata.uncited_claims
            ? ` - ${activeDraft.metadata.uncited_claims} ${t("workspace.draft.uncited_claims_suffix")}`
            : ""}
        </p>
      ) : null}
      {activeDraft ? (
        <div className="grid gap-5 md:grid-cols-[minmax(0,1fr)_20rem] md:items-start">
          <article className="min-w-0">
            <div className={inlineActionsClasses}>
              {bibHref ? (
                <a
                  className="inline-flex min-h-11 w-full items-center justify-center rounded-md bg-slate-100 px-4 py-2 text-sm font-bold text-[#114b5f] no-underline transition hover:bg-slate-200 sm:w-auto"
                  href={bibHref}
                  download={`${activeDraft.version}.bib`}
                >
                  {t("workspace.draft.download_bibtex")}
                </a>
              ) : null}
            </div>
            <MarkdownView markdown={activeDraft.manuscript} />
          </article>
          <ClaimMapSidebar claims={activeDraft.claim_map} />
        </div>
      ) : (
        <p className="leading-7 text-slate-700">
          {t("workspace.draft.artifact_pending")}
        </p>
      )}
    </section>
  );
}

function StyleSubview({
  currentState,
  styleBundle,
  progress,
  isStartingStylist,
  isStartingCritic,
  onRunStylist,
  onRunCritic,
  drafterCompleted,
  stylistCompleted,
  mathematicalMode,
  onToggleMathematicalMode,
  autoAdvance,
  onToggleAutoAdvance,
}: {
  currentState: string | undefined;
  styleBundle: StyleBundle | null;
  progress: RunEvent[];
  isStartingStylist: boolean;
  isStartingCritic: boolean;
  onRunStylist: () => Promise<void>;
  onRunCritic: () => Promise<void>;
  // PR-366: persisted toggle + writer come from the workspace; the
  // checkbox renders at the very top of this subview so the user
  // can flip the cost/quality lever before kicking off stylist /
  // critic. Disabled while rewriter or critic is running (backend
  // returns 409 in that window).
  mathematicalMode: boolean;
  onToggleMathematicalMode: (next: boolean) => Promise<void>;
  // PR-382: persisted auto_advance flag + setter. Flipping ON
  // immediately fires the backend coordinator (settings PATCH does
  // that server-side).
  autoAdvance: boolean;
  onToggleAutoAdvance: (next: boolean) => Promise<void>;
  // PR #319 follow-up: same regression as DraftSubview — stylist
  // ends at STYLIST_RUNNING on phase_done, banner needs to hide once
  // the agent finished.
  stylistCompleted: boolean;
  /** PR-244 deadlock-fix: phaseToSubview routes USER_REVISION_REVIEW
   * here, but the only existing CTA was phase-action-stylist
   * (re-run only). To advance to critic the user previously had
   * to navigate to the review tab. Same shape as the lens
   * deadlock that PR-243 fixed. PR-245 follow-up dropped the
   * ``stylistCompleted`` gate (raced with event polling on
   * first page load); reaching USER_REVISION_REVIEW already
   * implies stylist completed. */
  /** PR-C2.b follow-up: even when run.state is DRAFTER_RUNNING,
   * stylist may NOT start until the drafter agent has emitted
   * ``phase_done`` (i.e. all 8 sections drafted). Without this
   * gate, the user sees "启动文风修订" enabled while drafter is
   * still on section 1/8 and the click 409s. Mirror the
   * sidebar's gate (`disabled: isStartingStylist || !drafterCompleted`,
   * line 1738). */
  drafterCompleted: boolean;
}) {
  const t = useT();
  const [activeStyleTab, setActiveStyleTab] = useState<
    "manuscript" | "diff" | "score"
  >("manuscript");

  // Round-2 audit (always-render + disabled + hint): the stylist
  // button is always rendered; enable only at the two valid trigger
  // states. Codex DISAGREE #4 (2026-05-03): the previous
  // `showStylistButton ? <button> : null` ternary still hid the
  // affordance from earlier states, violating the discoverability
  // principle.
  const stylistRunnable =
    (currentState === "DRAFTER_RUNNING" && drafterCompleted) ||
    currentState === "USER_REVISION_REVIEW";
  const stylistHint = stylistRunnable
    ? null
    : currentState === "STYLIST_RUNNING"
      ? t("workspace.style.disabled.running")
      : currentState === "DRAFTER_RUNNING" && !drafterCompleted
        ? t("workspace.style.waiting_for_drafter")
        : currentState === "REWRITE_RUNNING" ||
            currentState === "CRITIC_RUNNING" ||
            currentState === "USER_EXTERNAL_SCAN_APPROVAL" ||
            currentState === "USER_INTEGRITY_REVIEW" ||
            currentState === "INTEGRITY_RUNNING" ||
            currentState === "USER_FINAL_ACCEPTANCE" ||
            currentState === "EXPORTS_RUNNING" ||
            currentState === "EXPORTS_DONE"
          ? // PR-394: REWRITE_RUNNING (slice E final_rewrite, post-
            // stylist) was missing from the already_done branch and
            // fell through to upstream_pending — same drift as
            // ``pastIdeatorStates`` above.
            t("workspace.style.disabled.already_done")
          : t("workspace.style.disabled.upstream_pending");

  // PR-366: mid-run toggle is disabled in the same window the backend
  // refuses (rewriter / critic in flight). Stylist-running is allowed
  // — the flag only kicks in at the rewriter+critic round-0 boundary.
  const mathematicalModeLocked =
    currentState === "REWRITE_RUNNING" || currentState === "CRITIC_RUNNING";

  return (
    <section className={sectionClasses}>
      <label
        className="mb-3 flex items-start gap-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800"
        aria-disabled={mathematicalModeLocked || undefined}
      >
        <input
          type="checkbox"
          data-testid="workspace-mathematical-mode"
          className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-slate-400 text-[#114b5f] focus:ring-[#114b5f] disabled:cursor-not-allowed"
          checked={mathematicalMode}
          disabled={mathematicalModeLocked}
          onChange={(event) =>
            void onToggleMathematicalMode(event.target.checked)
          }
        />
        <span className="grid gap-1">
          <span className="font-semibold text-slate-900">
            {t("workspace.style.mathematical_mode.label")}
          </span>
          <span className="text-xs leading-5 text-slate-600">
            {mathematicalModeLocked
              ? t("workspace.style.mathematical_mode.locked")
              : t("workspace.style.mathematical_mode.tooltip")}
          </span>
        </span>
      </label>
      {/* PR-382: 一键全自动 toggle. Flipping on fires the backend
          coordinator immediately, so the user can leave from any
          USER_*_REVIEW gate and the run will run itself to
          EXPORTS_DONE (modulo FAILED_*). */}
      <label className="mb-3 flex items-start gap-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-800">
        <input
          type="checkbox"
          data-testid="workspace-auto-advance"
          className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-slate-400 text-[#114b5f] focus:ring-[#114b5f]"
          checked={autoAdvance}
          onChange={(event) =>
            void onToggleAutoAdvance(event.target.checked)
          }
        />
        <span className="grid gap-1">
          <span className="font-semibold text-slate-900">
            {t("workspace.style.auto_advance.label")}
          </span>
          <span className="text-xs leading-5 text-slate-600">
            {t("workspace.style.auto_advance.tooltip")}
          </span>
        </span>
      </label>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.style.heading")}</h2>
        <div className={inlineActionsClasses}>
          <button
            type="button"
            data-testid="phase-action-stylist"
            className={
              currentState === "USER_REVISION_REVIEW"
                ? secondaryButtonClasses
                : primaryButtonClasses
            }
            onClick={onRunStylist}
            disabled={!stylistRunnable || isStartingStylist}
            aria-describedby={
              stylistHint ? "phase-action-stylist-hint" : undefined
            }
          >
            {isStartingStylist
              ? t("workspace.style.starting_stylist")
              : currentState === "USER_REVISION_REVIEW"
                ? t("workspace.style.rerun_stylist")
                : t("workspace.style.run_stylist")}
          </button>
          {/* PR-244 deadlock-fix (codex AGREE-w-amend Q1+Q5): in-place
              "advance to critic" button visible only at
              USER_REVISION_REVIEW. Same testid as ReviewSubview's
              critic button — DOM uniqueness guaranteed by tab mutex
              render. Re-run stylist demoted to secondary button so
              the advance action reads as the primary path.
              PR-245 follow-up: drop ``stylistCompleted`` check —
              at USER_REVISION_REVIEW stylist has completed by
              definition; the events-polling check raced with
              first page load. */}
          {currentState === "USER_REVISION_REVIEW" ? (
            <button
              type="button"
              data-testid="phase-action-critic"
              className={primaryButtonClasses}
              onClick={onRunCritic}
              disabled={isStartingCritic}
            >
              {isStartingCritic
                ? t("workspace.review.starting_critic")
                : t("workspace.style.advance_to_critic")}
            </button>
          ) : null}
        </div>
        {stylistHint ? (
          <p
            id="phase-action-stylist-hint"
            data-testid="phase-action-stylist-waiting"
            role="status"
            className="text-sm italic text-slate-600"
          >
            {stylistHint}
          </p>
        ) : null}
      </div>
      {currentState === "STYLIST_RUNNING" ? (
        <PhaseRunningBanner
          phase="stylist"
          progress={progress}
          hintEvent={progress[0] ?? null}
          completed={stylistCompleted}
        />
      ) : null}
      {currentState === "REWRITE_RUNNING" ? (
        <PhaseRunningBanner phase="final_rewrite" />
      ) : null}
      {currentState === "STYLIST_RUNNING" ? (
        <>
          <h2 className={h2Classes}>{t("workspace.style.progress_heading")}</h2>
          {progress.length > 0 ? (
            <ul className={cardListClasses}>
              {progress.map((event) => (
                <li
                  className={infoCardClasses + " sm:flex sm:justify-between"}
                  key={event.id}
                >
                  <strong>
                    {String(
                      event.payload.section_title ??
                        t("workspace.common.section_default"),
                    )}
                  </strong>
                  <span>
                    {String(
                      event.payload.status ??
                        t("workspace.common.status_pending"),
                    )}
                  </span>
                  <span>
                    {Number(event.payload.completed ?? 0)} /{" "}
                    {Number(event.payload.total ?? 0)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="leading-7 text-slate-700">
              {t("workspace.style.no_progress")}
            </p>
          )}
        </>
      ) : null}
      {currentState === "USER_REVISION_REVIEW" && styleBundle ? (
        <>
          <div
            className="my-4 flex snap-x gap-2 overflow-x-auto pb-2 md:inline-flex md:overflow-visible md:rounded-lg md:border md:border-slate-200 md:bg-slate-50 md:p-1"
            role="tablist"
            aria-label={t("workspace.style.tablist_label")}
          >
            <button
              type="button"
              className={tabButtonClasses(activeStyleTab === "manuscript")}
              onClick={() => setActiveStyleTab("manuscript")}
            >
              {t("workspace.style.tab_manuscript")}
            </button>
            <button
              type="button"
              className={tabButtonClasses(activeStyleTab === "diff")}
              onClick={() => setActiveStyleTab("diff")}
            >
              {t("workspace.style.tab_diff")}
            </button>
            <button
              type="button"
              className={tabButtonClasses(activeStyleTab === "score")}
              onClick={() => setActiveStyleTab("score")}
            >
              {t("workspace.style.tab_score")}
            </button>
          </div>
          {activeStyleTab === "manuscript" ? (
            <article className="min-w-0">
              <MarkdownView markdown={styleBundle.paper_styled} />
            </article>
          ) : null}
          {activeStyleTab === "diff" ? (
            <pre className={reportPreClasses}>
              {styleBundle.style_delta || t("workspace.style.diff_pending")}
            </pre>
          ) : null}
          {activeStyleTab === "score" ? (
            <ScoreBars score={styleBundle.stop_slop_score} />
          ) : null}
        </>
      ) : currentState !== "STYLIST_RUNNING" ? (
        <p className="leading-7 text-slate-700">
          {t("workspace.style.artifacts_pending")}
        </p>
      ) : null}
    </section>
  );
}

function ReviewSubview({
  currentState,
  criticBundle,
  isStartingCritic,
  isStartingIntegrity,
  onRunCritic,
  onApproveExternalScan,
  onSkipExternalScan,
  stylistCompleted,
  criticCompleted,
}: {
  currentState: string | undefined;
  criticBundle: CriticBundle | null;
  isStartingCritic: boolean;
  isStartingIntegrity: boolean;
  onRunCritic: () => Promise<void>;
  onApproveExternalScan: (
    scanKinds: Array<"plagiarism" | "ai_style">,
  ) => Promise<void>;
  onSkipExternalScan: (skipReason: string) => Promise<void>;
  // PR #319 follow-up: critic ends at CRITIC_RUNNING on phase_done
  // waiting for the user to approve external scan; banner needs to
  // hide once the agent has finished.
  criticCompleted: boolean;
  /** PR-C2.b audit round-2: always-render + disabled + hint
   * principle. Critic button visible at every state; enabled
   * only when stylist phase_done has been observed AND state is
   * USER_REVISION_REVIEW. */
  stylistCompleted: boolean;
}) {
  const t = useT();
  const [scanKinds, setScanKinds] = useState<Array<"plagiarism" | "ai_style">>([
    "plagiarism",
    "ai_style",
  ]);
  const [skipReason, setSkipReason] = useState("");
  const blockers = [...(criticBundle?.blocking_issues.issues ?? [])].sort(
    (left, right) => severityRank(left.severity) - severityRank(right.severity),
  );

  function toggleScanKind(kind: "plagiarism" | "ai_style") {
    setScanKinds((current) =>
      current.includes(kind)
        ? current.filter((item) => item !== kind)
        : [...current, kind],
    );
  }

  // PR-C2.b audit round-2: always-render + disabled + hint.
  // Backend ``apply_phase_user_edit`` + ``start_critic`` reject
  // mid-flight via RUNNING_STATES anyway; rendering the button
  // hidden didn't hide the feature's existence from users at
  // earlier stages — they had no idea critic would even happen.
  const canRunCritic =
    currentState === "USER_REVISION_REVIEW" && stylistCompleted;
  let criticDisabledHint: string | null = null;
  if (currentState === "CRITIC_RUNNING") {
    criticDisabledHint = t("workspace.review.disabled.running");
  } else if (currentState === "USER_REVISION_REVIEW" && !stylistCompleted) {
    criticDisabledHint = t("workspace.review.disabled.waiting_stylist");
  } else if (
    currentState === "STYLIST_RUNNING" ||
    currentState === "DRAFTER_RUNNING"
  ) {
    criticDisabledHint = t("workspace.review.disabled.waiting_stylist");
  } else if (
    currentState === "USER_EXTERNAL_SCAN_APPROVAL" ||
    currentState === "INTEGRITY_RUNNING" ||
    currentState === "USER_INTEGRITY_REVIEW" ||
    currentState === "USER_FINAL_ACCEPTANCE" ||
    currentState === "EXPORTS_RUNNING" ||
    currentState === "EXPORTS_DONE"
  ) {
    criticDisabledHint = t("workspace.review.disabled.already_done");
  } else if (!canRunCritic) {
    criticDisabledHint = t("workspace.review.disabled.upstream_pending");
  }

  return (
    <section className={sectionClasses}>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.review.heading")}</h2>
        <div className={inlineActionsClasses}>
          <button
            type="button"
            data-testid="phase-action-critic"
            className={primaryButtonClasses}
            onClick={onRunCritic}
            disabled={!canRunCritic || isStartingCritic}
            aria-describedby={
              criticDisabledHint ? "critic-disabled-hint" : undefined
            }
          >
            {isStartingCritic
              ? t("workspace.review.starting_critic")
              : t("workspace.review.run_critic")}
          </button>
        </div>
      </div>
      {criticDisabledHint ? (
        <p
          id="critic-disabled-hint"
          role="status"
          data-testid="critic-disabled-hint"
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {criticDisabledHint}
        </p>
      ) : null}
      {currentState === "CRITIC_RUNNING" ? (
        <PhaseRunningBanner phase="critic" completed={criticCompleted} />
      ) : null}
      {/* PR-G-Review-CTA-Top (2026-05-07): when critic finished and
          run is awaiting USER_EXTERNAL_SCAN_APPROVAL, the approval
          panel is the only actionable item on this page — but it
          used to render below blockers + citation audit table +
          revision plan + critic report, so on mobile the user had
          to scroll past several screens of read-only content before
          spotting "批准并扫描". Move the panel up so it sits
          immediately under the heading. The audit/plan/report
          remain below as reference material.*/}
      {currentState === "USER_EXTERNAL_SCAN_APPROVAL" ? (
        <section
          className="my-5 grid gap-3 rounded-lg border border-slate-200 p-4"
          data-testid="review-external-scan-approval-panel"
        >
          <h2 className={h2Classes}>
            {t("workspace.review.external_scan_approval")}
          </h2>
          <div className="flex flex-wrap gap-4">
            <label className="flex min-h-11 items-center gap-2 text-sm font-semibold text-slate-800">
              <input
                className="h-4 w-4 accent-[#114b5f]"
                type="checkbox"
                checked={scanKinds.includes("plagiarism")}
                onChange={() => toggleScanKind("plagiarism")}
              />
              {t("workspace.review.plagiarism")}
            </label>
            <label className="flex min-h-11 items-center gap-2 text-sm font-semibold text-slate-800">
              <input
                className="h-4 w-4 accent-[#114b5f]"
                type="checkbox"
                checked={scanKinds.includes("ai_style")}
                onChange={() => toggleScanKind("ai_style")}
              />
              {t("workspace.review.ai_style")}
            </label>
          </div>
          <div className={inlineActionsClasses}>
            <button
              type="button"
              data-testid="review-approve-external-scan"
              className={primaryButtonClasses}
              onClick={() => onApproveExternalScan(scanKinds)}
              disabled={isStartingIntegrity || scanKinds.length === 0}
            >
              {isStartingIntegrity
                ? t("workspace.review.starting_integrity")
                : t("workspace.review.approve_and_scan")}
            </button>
          </div>
          <label className="grid gap-2 text-sm font-semibold text-slate-800">
            {t("workspace.review.skip_note")}
            <input
              data-testid="review-skip-reason-input"
              className={inputClasses}
              value={skipReason}
              onChange={(event) => setSkipReason(event.target.value)}
              placeholder={t("workspace.review.skip_placeholder")}
            />
          </label>
          <button
            type="button"
            data-testid="review-skip-external-scan"
            className={secondaryButtonClasses}
            onClick={() => onSkipExternalScan(skipReason)}
            disabled={!skipReason.trim()}
          >
            {t("workspace.review.skip_with_note")}
          </button>
        </section>
      ) : null}
      {blockers.length > 0 ? (
        <>
          <h2 className={h2Classes}>{t("workspace.review.blocking_issues")}</h2>
          <ul className={cardListClasses}>
            {blockers.map((issue) => (
              <li
                className="grid list-none gap-2 rounded-lg border border-slate-200 p-3 sm:grid-cols-[7rem_9rem_minmax(7rem,auto)_minmax(0,1fr)] sm:p-4"
                key={issue.issue_id}
              >
                <strong>{issue.severity}</strong>
                <span>{issue.dimension}</span>
                <a
                  className={linkClasses}
                  href={
                    issue.paragraph_id ? `#${issue.paragraph_id}` : undefined
                  }
                >
                  {issue.paragraph_id ?? t("workspace.review.no_paragraph")}
                </a>
                <p className="m-0 leading-7 text-slate-700">
                  {issue.description}
                </p>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="leading-7 text-slate-700">
          {t("workspace.review.no_blockers")}
        </p>
      )}
      <h2 className={h2Classes}>{t("workspace.review.citation_audit")}</h2>
      {criticBundle?.claim_audit.length ? (
        <div
          className="overflow-hidden rounded-lg border border-slate-200"
          role="table"
          aria-label={t("workspace.review.citation_audit_aria")}
        >
          <div
            role="row"
            className="hidden gap-4 bg-slate-100 px-3 py-3 text-sm font-bold text-slate-600 md:grid md:grid-cols-[8rem_5rem_minmax(0,16rem)_minmax(0,1fr)]"
          >
            <span>{t("workspace.review.col_paragraph")}</span>
            <span>{t("workspace.review.col_status")}</span>
            <span>{t("workspace.review.col_sources")}</span>
            <span>{t("workspace.review.col_claim")}</span>
          </div>
          {criticBundle.claim_audit.map((row) => (
            <div
              role="row"
              className="grid gap-2 border-t border-slate-200 px-3 py-3 text-sm first:border-t-0 md:grid-cols-[8rem_5rem_minmax(0,16rem)_minmax(0,1fr)] md:gap-4"
              key={`${row.paragraph_id}-${row.claim_index}`}
            >
              <a
                className={`${linkClasses} min-w-0 break-all`}
                href={`#${row.paragraph_id}`}
              >
                {row.paragraph_id}
              </a>
              <strong className="min-w-0">{row.status}</strong>
              <span className="min-w-0 break-all text-xs leading-5 text-slate-600">
                {row.source_ids.length
                  ? row.source_ids.map((sid) => (
                      <span key={sid} className="block">
                        {sid}
                      </span>
                    ))
                  : t("workspace.common.none_lower")}
              </span>
              <span className="min-w-0 break-words leading-6">
                {row.claim_text}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="leading-7 text-slate-700">
          {t("workspace.review.citation_audit_pending")}
        </p>
      )}
      <h2 className={h2Classes}>{t("workspace.review.revision_plan")}</h2>
      <pre className={reportPreClasses}>
        {criticBundle?.revision_plan ||
          t("workspace.review.revision_plan_pending")}
      </pre>
      <h2 className={h2Classes}>{t("workspace.review.critic_report")}</h2>
      <pre className={reportPreClasses}>
        {criticBundle?.critic_report ||
          t("workspace.review.critic_report_pending")}
      </pre>
    </section>
  );
}

function IntegritySubview({
  currentState,
  integrityBundle,
  manuscript,
  onAcceptIntegrity,
  onRequestRevision,
  integrityCompleted,
}: {
  currentState: string | undefined;
  integrityBundle: IntegrityBundle | null;
  manuscript: string;
  onAcceptIntegrity: (
    spanDecisions: Array<Record<string, unknown>>,
  ) => Promise<void>;
  onRequestRevision: (
    spanDecisions: Array<Record<string, unknown>>,
    nextRevisionDimension: string,
  ) => Promise<void>;
  /** PR-C2.b audit round-2: enable buttons only when integrity
   * scans have actually finished (phase_done event observed) AND
   * state == USER_INTEGRITY_REVIEW. */
  integrityCompleted: boolean;
}) {
  const t = useT();
  const spans = scanSpans(integrityBundle);
  const [decisions, setDecisions] = useState<Record<string, string>>({});
  const [nextDimension, setNextDimension] = useState("prose");
  const spanDecisions = spans.map((span) => ({
    span_id: span.span_id,
    decision: decisions[span.span_id] ?? "accept",
  }));

  function setDecision(spanId: string, decision: string) {
    setDecisions((current) => ({ ...current, [spanId]: decision }));
  }

  // PR-C2.b audit round-2: always-render + disabled + hint.
  const canAct = currentState === "USER_INTEGRITY_REVIEW" && integrityCompleted;
  let integrityHint: string | null = null;
  if (currentState === "INTEGRITY_RUNNING") {
    integrityHint = t("workspace.integrity.disabled.running");
  } else if (
    currentState === "USER_EXTERNAL_SCAN_APPROVAL" ||
    !integrityCompleted
  ) {
    integrityHint = t("workspace.integrity.disabled.waiting_scans");
  } else if (
    currentState === "USER_FINAL_ACCEPTANCE" ||
    currentState === "EXPORTS_RUNNING" ||
    currentState === "EXPORTS_DONE"
  ) {
    integrityHint = t("workspace.integrity.disabled.already_done");
  } else if (!canAct) {
    integrityHint = t("workspace.integrity.disabled.upstream_pending");
  }

  return (
    <section className={sectionClasses}>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.integrity.heading")}</h2>
        <div className={inlineActionsClasses}>
          <button
            type="button"
            data-testid="review-accept-integrity"
            className={primaryButtonClasses}
            onClick={() => onAcceptIntegrity(spanDecisions)}
            disabled={!canAct}
            aria-describedby={
              integrityHint ? "integrity-disabled-hint" : undefined
            }
          >
            {t("workspace.integrity.accept_findings")}
          </button>
          <button
            type="button"
            data-testid="review-request-revision"
            className={secondaryButtonClasses}
            onClick={() => onRequestRevision(spanDecisions, nextDimension)}
            disabled={!canAct}
            aria-describedby={
              integrityHint ? "integrity-disabled-hint" : undefined
            }
          >
            {t("workspace.integrity.revise_selected")}
          </button>
        </div>
      </div>
      {integrityHint ? (
        <p
          id="integrity-disabled-hint"
          role="status"
          data-testid="integrity-disabled-hint"
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {integrityHint}
        </p>
      ) : null}
      {currentState === "INTEGRITY_RUNNING" ? (
        <PhaseRunningBanner phase="integrity" completed={integrityCompleted} />
      ) : null}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {Object.entries(integrityBundle?.integrity_summary.scans ?? {}).map(
          ([kind, scan]) => (
            <article
              className="grid gap-2 rounded-lg border border-slate-200 p-3"
              key={kind}
            >
              <strong>{kind}</strong>
              <span>
                {scan.vendor ?? t("workspace.integrity.vendor_pending")}
              </span>
              <span>
                {scan.score == null
                  ? t("workspace.integrity.score_na")
                  : t("workspace.integrity.score_label", {
                      score: String(scan.score),
                    })}
              </span>
              <span>
                {scan.span_count ?? 0} {t("workspace.integrity.spans_suffix")}
              </span>
            </article>
          ),
        )}
      </div>
      {spans.length > 0 ? (
        <>
          <h2 className={h2Classes}>
            {t("workspace.integrity.span_decisions")}
          </h2>
          <label className="grid gap-2 text-sm font-semibold text-slate-800">
            {t("workspace.integrity.revision_dimension")}
            <select
              className={selectClasses}
              value={nextDimension}
              onChange={(event) => setNextDimension(event.target.value)}
            >
              <option value="thesis">
                {t("workspace.integrity.dim_thesis")}
              </option>
              <option value="structure">
                {t("workspace.integrity.dim_structure")}
              </option>
              <option value="evidence">
                {t("workspace.integrity.dim_evidence")}
              </option>
              <option value="prose">
                {t("workspace.integrity.dim_prose")}
              </option>
            </select>
          </label>
          <ul className={cardListClasses}>
            {spans.map((span) => (
              <li
                className="grid list-none gap-3 rounded-lg border border-slate-200 p-3 md:grid-cols-[minmax(0,1fr)_7rem_9rem_9rem] md:items-center"
                key={span.span_id}
              >
                <strong>{span.label}</strong>
                <span>
                  {span.start}-{span.end}
                </span>
                <span>
                  {span.confidence == null
                    ? t("workspace.integrity.confidence_na")
                    : span.confidence}
                </span>
                <select
                  className={selectClasses}
                  value={decisions[span.span_id] ?? "accept"}
                  onChange={(event) =>
                    setDecision(span.span_id, event.target.value)
                  }
                >
                  <option value="accept">
                    {t("workspace.integrity.decision_accept")}
                  </option>
                  <option value="revise">
                    {t("workspace.integrity.decision_revise")}
                  </option>
                  <option value="ignore">
                    {t("workspace.integrity.decision_ignore")}
                  </option>
                </select>
              </li>
            ))}
          </ul>
          <h2 className={h2Classes}>
            {t("workspace.integrity.manuscript_highlights")}
          </h2>
          <HighlightedManuscript manuscript={manuscript} spans={spans} />
        </>
      ) : (
        <p className="leading-7 text-slate-700">
          {t("workspace.integrity.no_spans")}
        </p>
      )}
      <h2 className={h2Classes}>
        {t("workspace.integrity.plagiarism_report")}
      </h2>
      <pre className={reportPreClasses}>
        {integrityBundle?.plagiarism_report ||
          t("workspace.integrity.plagiarism_pending")}
      </pre>
      <h2 className={h2Classes}>{t("workspace.integrity.ai_style_report")}</h2>
      <pre className={reportPreClasses}>
        {integrityBundle?.ai_style_report ||
          t("workspace.integrity.ai_pending")}
      </pre>
    </section>
  );
}

function ExportSubview({
  currentState,
  exportsBundle,
  isStartingExports,
  onAcceptFinalDraft,
  onRunExports,
  criticCompleted,
  integrityCompleted,
}: {
  currentState: string | undefined;
  exportsBundle: ExportsBundle | null;
  isStartingExports: boolean;
  onAcceptFinalDraft: (exportFormats: string[]) => Promise<void>;
  onRunExports: () => Promise<void>;
  /** PR-C2.b audit round-2: enable buttons only when integrity
   * (or critic-with-skip path) has produced output AND state is
   * USER_FINAL_ACCEPTANCE. */
  criticCompleted: boolean;
  integrityCompleted: boolean;
}) {
  const t = useT();
  const [formats, setFormats] = useState([
    "markdown",
    "docx",
    "html",
    "bibtex",
    "csl_json",
  ]);

  function toggleFormat(format: string) {
    setFormats((current) =>
      current.includes(format)
        ? current.filter((item) => item !== format)
        : [...current, format],
    );
  }

  // PR-C2.b audit round-2: always-render + disabled + hint.
  // USER_FINAL_ACCEPTANCE is reached either via integrity-accept
  // OR via external-scan-skip (critic-only path), so EITHER
  // integrity OR critic completion qualifies as "upstream done".
  const upstreamDone = integrityCompleted || criticCompleted;
  const canAccept =
    currentState === "USER_FINAL_ACCEPTANCE" &&
    upstreamDone &&
    formats.length > 0;
  const canRunExports =
    currentState === "USER_FINAL_ACCEPTANCE" && upstreamDone;
  let exportHint: string | null = null;
  if (currentState === "EXPORTS_RUNNING") {
    exportHint = t("workspace.export.disabled.running");
  } else if (currentState === "EXPORTS_DONE") {
    exportHint = t("workspace.export.disabled.already_done");
  } else if (currentState === "USER_FINAL_ACCEPTANCE" && !upstreamDone) {
    exportHint = t("workspace.export.disabled.upstream_pending");
  } else if (currentState === "USER_FINAL_ACCEPTANCE" && formats.length === 0) {
    exportHint = t("workspace.export.disabled.pick_format");
  } else if (currentState !== "USER_FINAL_ACCEPTANCE") {
    exportHint = t("workspace.export.disabled.upstream_pending");
  }

  return (
    <section className={sectionClasses}>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.export.heading")}</h2>
        <div className={inlineActionsClasses}>
          <button
            type="button"
            data-testid="review-accept-final-draft"
            className={primaryButtonClasses}
            onClick={() => onAcceptFinalDraft(formats)}
            disabled={!canAccept || isStartingExports}
            aria-describedby={exportHint ? "export-disabled-hint" : undefined}
          >
            {isStartingExports
              ? t("workspace.export.starting")
              : t("workspace.export.accept_and_export")}
          </button>
          <button
            type="button"
            data-testid="review-run-exports"
            className={secondaryButtonClasses}
            onClick={onRunExports}
            disabled={!canRunExports || isStartingExports}
            aria-describedby={exportHint ? "export-disabled-hint" : undefined}
          >
            {t("workspace.export.run_exports")}
          </button>
        </div>
      </div>
      {exportHint ? (
        <p
          id="export-disabled-hint"
          role="status"
          data-testid="export-disabled-hint"
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {exportHint}
        </p>
      ) : null}
      {currentState === "USER_FINAL_ACCEPTANCE" ? (
        <div className="mt-4 flex flex-wrap gap-4">
          {["markdown", "docx", "html", "latex", "bibtex", "csl_json"].map((format) => (
            <label
              className="flex min-h-11 items-center gap-2 text-sm font-semibold text-slate-800"
              key={format}
            >
              <input
                className="h-4 w-4 accent-[#114b5f]"
                type="checkbox"
                checked={formats.includes(format)}
                onChange={() => toggleFormat(format)}
              />
              {format}
            </label>
          ))}
        </div>
      ) : null}
      {currentState === "EXPORTS_RUNNING" ? (
        <PhaseRunningBanner phase="exports" />
      ) : null}
      {exportsBundle?.files.length ? (
        <>
          <h2 className={h2Classes}>{t("workspace.export.files_heading")}</h2>
          <ul className={cardListClasses}>
            {exportsBundle.files.map((file) => {
              // PR-371: show the title-slug-derived ``download_filename``
              // in the link text + ``download`` attribute (so a click
              // on a same-origin link saves under that name even
              // without the server's Content-Disposition). The URL
              // still hits the on-disk ``filename``.
              const displayName = file.download_filename ?? file.filename;
              return (
                <li
                  className={
                    infoCardClasses +
                    " sm:flex sm:items-center sm:justify-between"
                  }
                  key={file.filename}
                  data-testid={`export-file-${file.format}`}
                >
                  <strong>{file.format}</strong>
                  <a
                    className={linkClasses}
                    href={file.url}
                    download={displayName}
                  >
                    {displayName}
                  </a>
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        <p className="leading-7 text-slate-700">
          {t("workspace.export.no_exports")}
        </p>
      )}
      <h2 className={h2Classes}>{t("workspace.export.manifest")}</h2>
      <pre className={reportPreClasses}>
        {exportsBundle
          ? JSON.stringify(exportsBundle.manifest, null, 2)
          : t("workspace.export.manifest_pending")}
      </pre>
    </section>
  );
}

type IntegritySpan = {
  span_id: string;
  start: number;
  end: number;
  label: string;
  confidence?: number | null;
  source_url?: string | null;
  text?: string | null;
};

function scanSpans(bundle: IntegrityBundle | null): IntegritySpan[] {
  const scans = bundle?.integrity_summary.scans ?? {};
  return Object.values(scans)
    .flatMap((scan) => scan.spans ?? [])
    .filter((span) => span.end > span.start)
    .sort((left, right) => left.start - right.start);
}

function HighlightedManuscript({
  manuscript,
  spans,
}: {
  manuscript: string;
  spans: IntegritySpan[];
}) {
  const parts: ReactElement[] = [];
  let cursor = 0;
  spans.forEach((span, index) => {
    const start = Math.max(cursor, Math.min(span.start, manuscript.length));
    const end = Math.max(start, Math.min(span.end, manuscript.length));
    if (start > cursor) {
      parts.push(
        <span key={`text-${index}`}>{manuscript.slice(cursor, start)}</span>,
      );
    }
    parts.push(
      <mark
        className="rounded bg-amber-200 px-1"
        key={span.span_id}
        title={span.label}
      >
        {manuscript.slice(start, end)}
      </mark>,
    );
    cursor = end;
  });
  if (cursor < manuscript.length) {
    parts.push(<span key="tail">{manuscript.slice(cursor)}</span>);
  }
  return (
    <pre className="overflow-auto whitespace-pre-wrap rounded-lg border border-slate-200 bg-slate-50 p-4 text-sm leading-7 text-slate-800">
      {parts}
    </pre>
  );
}

function severityRank(severity: string): number {
  const ranks: Record<string, number> = {
    BLOCKER: 0,
    HIGH: 1,
    MEDIUM: 2,
    LOW: 3,
  };
  return ranks[severity] ?? 4;
}

function ScoreBars({ score }: { score: StyleBundle["stop_slop_score"] }) {
  const t = useT();
  const dimensions = score.final?.dimensions ?? {};
  const total = score.final?.total ?? 0;
  const initialTotal = score.initial?.total ?? 0;
  const names = ["directness", "rhythm", "trust", "authenticity", "density"];
  return (
    <div className="max-w-3xl">
      <p className="leading-7 text-slate-700">
        {t("workspace.style.score_total", { total })}{" "}
        {initialTotal
          ? t("workspace.style.score_initial_suffix", { initial: initialTotal })
          : ""}
      </p>
      <div className="grid gap-3">
        {names.map((name) => {
          const value = Math.max(
            0,
            Math.min(10, Number(dimensions[name] ?? 0)),
          );
          return (
            <div
              className="grid gap-2 md:grid-cols-[8rem_minmax(8rem,1fr)_4rem] md:items-center"
              key={name}
            >
              <span className="capitalize text-slate-700">
                {t(`workspace.style.dim.${name}`)}
              </span>
              <div
                className="h-3 overflow-hidden rounded-md bg-slate-100"
                aria-hidden="true"
              >
                <div
                  className="h-full bg-[#236b45]"
                  style={{ width: `${value * 10}%` }}
                />
              </div>
              <strong>{value}/10</strong>
            </div>
          );
        })}
      </div>
      {score.repolish_attempted ? (
        <p className={noticeClasses}>
          {t("workspace.style.repolish_attempted")}
        </p>
      ) : null}
    </div>
  );
}

function ClaimMapSidebar({ claims }: { claims: DraftClaim[] }) {
  const t = useT();
  if (claims.length === 0) {
    return (
      <aside className="rounded-lg border border-slate-200 p-4 md:sticky md:top-24">
        {t("workspace.draft.no_claim_map")}
      </aside>
    );
  }
  return (
    <aside className="rounded-lg border border-slate-200 p-4 md:sticky md:top-24">
      <h2 className="text-lg font-bold text-slate-950">
        {t("workspace.draft.claim_map_heading")}
      </h2>
      <ul className="grid list-none gap-3 p-0">
        {claims.map((claim) => (
          <li
            className="border-t border-slate-200 pt-3 first:border-t-0 first:pt-0"
            key={`${claim.paragraph_id}-${claim.claim_text}`}
          >
            <a className={linkClasses} href={`#${claim.section_id}`}>
              {claim.paragraph_id}
            </a>
            <p className="my-2 leading-6 text-slate-700">{claim.claim_text}</p>
            <span>
              {claim.uncited
                ? t("workspace.draft.uncited_tag")
                : claim.source_ids.join(", ")}
            </span>
          </li>
        ))}
      </ul>
    </aside>
  );
}

// PR-377: matches the table-separator row ``|---|---|...|`` (with
// optional alignment colons). Mirrors backend ``_MD_TABLE_SEPARATOR_RE``
// in exporter.py so the workspace preview agrees with the export.
const MD_TABLE_SEPARATOR_RE = /^\|[\s:|-]+\|$/;
const MD_IMAGE_RE = /^!\[([^\]]*)\]\(([^)]+)\)$/;
// PR-381 (codex AGREE-WITH-AMENDMENTS amendment 2): numeric-only
// trailing citation. LLM-emitted row-scoped citations are always
// ``|[N]`` / ``|[12]`` / ``|[3][4]`` — strict numeric so we don't
// accidentally peel ``[evidence: §3.2]`` from the last cell of a
// row whose rightmost cell legitimately ends on a bracket.
const MD_TABLE_TRAILING_CITATION_RE = /(\s*(?:\[\s*\d+\s*\])+\s*)$/;

function stripTrailingCitations(line: string): string {
  let s = line;
  // Walk back: ``|[2]``, ``| [12]``, ``|[3][4]`` — repeat strip
  // until no more trailing [N] groups, then assert we still end
  // on a pipe.
  while (true) {
    const match = MD_TABLE_TRAILING_CITATION_RE.exec(s);
    if (!match) break;
    if (!s.slice(0, match.index).trimEnd().endsWith("|")) {
      // The bracket isn't after a closing pipe — it might be
      // part of a normal cell value, leave it alone.
      break;
    }
    s = s.slice(0, match.index).trimEnd();
  }
  return s;
}

function isTableRow(line: string): boolean {
  if (!line.startsWith("|")) return false;
  const stripped = stripTrailingCitations(line);
  return stripped.startsWith("|") && stripped.endsWith("|") && stripped.length >= 2;
}

function parseTableRow(line: string): string[] {
  const stripped = stripTrailingCitations(line);
  return stripped
    .slice(1, -1)
    .split("|")
    .map((c) => c.trim());
}

function MarkdownView({ markdown }: { markdown: string }) {
  // PR-377: gained markdown table + ``$$ display math`` + ``![alt](url)``
  // image rendering. Workspace preview now matches the export pipeline
  // — before this, any ``| ... |`` rows became raw-pipe paragraphs.
  const elements: ReactElement[] = [];
  let listItems: string[] = [];
  let pendingAnchor: string | null = null;
  let tableCount = 0;
  let figureCount = 0;
  let mathBuffer: string[] | null = null;

  function flushList() {
    if (listItems.length === 0) {
      return;
    }
    const index = elements.length;
    elements.push(
      <ul className="my-4 list-disc space-y-2 pl-5" key={`list-${index}`}>
        {listItems.map((item, itemIndex) => (
          <li key={`${index}-${itemIndex}`}>{item}</li>
        ))}
      </ul>,
    );
    listItems = [];
  }

  function flushMath() {
    if (mathBuffer === null) {
      return;
    }
    const content = mathBuffer.join("\n").trim();
    if (content) {
      const index = elements.length;
      elements.push(
        <pre
          className="my-4 overflow-x-auto rounded-md border border-slate-200 bg-slate-50 px-4 py-3 font-mono text-sm text-slate-900"
          key={`math-${index}`}
          data-testid="markdown-math-block"
        >
          {content}
        </pre>,
      );
    }
    mathBuffer = null;
  }

  const lines = markdown.split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (mathBuffer !== null) {
      if (trimmed === "$$" || trimmed.endsWith("$$")) {
        if (trimmed !== "$$") {
          mathBuffer.push(trimmed.replace(/\$\$\s*$/, ""));
        }
        flushMath();
        i += 1;
        continue;
      }
      mathBuffer.push(line);
      i += 1;
      continue;
    }
    if (trimmed === "$$" || (trimmed.startsWith("$$") && !trimmed.endsWith("$$"))) {
      flushList();
      mathBuffer = [];
      if (trimmed !== "$$") {
        mathBuffer.push(trimmed.slice(2));
      }
      i += 1;
      continue;
    }
    if (trimmed.startsWith("$$") && trimmed.endsWith("$$") && trimmed.length > 4) {
      flushList();
      const content = trimmed.slice(2, -2).trim();
      const idx = elements.length;
      elements.push(
        <pre
          className="my-4 overflow-x-auto rounded-md border border-slate-200 bg-slate-50 px-4 py-3 font-mono text-sm text-slate-900"
          key={`math-inline-${idx}`}
          data-testid="markdown-math-block"
        >
          {content}
        </pre>,
      );
      i += 1;
      continue;
    }

    const anchorMatch = line.match(/^<a id="([^"]+)"><\/a>$/);
    if (anchorMatch) {
      pendingAnchor = anchorMatch[1];
      i += 1;
      continue;
    }
    if (!trimmed) {
      flushList();
      i += 1;
      continue;
    }

    if (
      trimmed.startsWith("|") &&
      isTableRow(trimmed) &&
      i + 1 < lines.length &&
      MD_TABLE_SEPARATOR_RE.test(lines[i + 1].trim())
    ) {
      flushList();
      const headerCells = parseTableRow(trimmed);
      let j = i + 2;
      const rows: string[][] = [];
      while (j < lines.length) {
        const sub = lines[j].trim();
        if (!isTableRow(sub)) {
          break;
        }
        rows.push(parseTableRow(sub));
        j += 1;
      }
      tableCount += 1;
      const tIdx = elements.length;
      elements.push(
        <figure
          className="my-6"
          key={`table-${tIdx}`}
          data-testid={`markdown-table-${tableCount}`}
        >
          <figcaption className="mb-2 text-sm font-semibold text-slate-900">
            表 {tableCount}
          </figcaption>
          <table className="min-w-full border-collapse text-sm">
            <thead>
              <tr>
                {headerCells.map((cell, idx) => (
                  <th
                    key={`th-${tIdx}-${idx}`}
                    className="border border-slate-300 bg-slate-100 px-3 py-2 text-left font-semibold text-slate-900"
                  >
                    {cell}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rIdx) => (
                <tr key={`tr-${tIdx}-${rIdx}`}>
                  {Array.from({ length: headerCells.length }).map((_, cIdx) => (
                    <td
                      key={`td-${tIdx}-${rIdx}-${cIdx}`}
                      className="border border-slate-300 px-3 py-2 align-top text-slate-800"
                    >
                      {row[cIdx] ?? ""}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </figure>,
      );
      i = j;
      continue;
    }

    const imgMatch = trimmed.match(MD_IMAGE_RE);
    if (imgMatch) {
      flushList();
      figureCount += 1;
      const [, alt, url] = imgMatch;
      const fIdx = elements.length;
      elements.push(
        <figure
          className="my-6"
          key={`figure-${fIdx}`}
          data-testid={`markdown-figure-${figureCount}`}
        >
          <img
            src={url}
            alt={alt}
            className="mx-auto block max-w-full rounded-md border border-slate-200"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
          />
          <figcaption className="mt-2 text-center text-sm font-semibold text-slate-900">
            图 {figureCount}
            {alt ? `  ${alt}` : ""}
          </figcaption>
        </figure>,
      );
      i += 1;
      continue;
    }

    if (line.startsWith("- ")) {
      listItems.push(line.slice(2));
      i += 1;
      continue;
    }
    flushList();
    if (line.startsWith("## ")) {
      elements.push(
        <h3
          className="mt-6 text-lg font-bold text-slate-950"
          id={pendingAnchor ?? undefined}
          key={`heading-${i}`}
        >
          {line.slice(3)}
        </h3>,
      );
      pendingAnchor = null;
      i += 1;
      continue;
    }
    if (line.startsWith("# ")) {
      elements.push(
        <h2
          className="mt-6 text-xl font-bold text-slate-950"
          id={pendingAnchor ?? undefined}
          key={`heading-${i}`}
        >
          {line.slice(2)}
        </h2>,
      );
      pendingAnchor = null;
      i += 1;
      continue;
    }
    elements.push(
      <p
        className="my-4 leading-7 text-slate-800"
        key={`paragraph-${i}`}
      >
        {line}
      </p>,
    );
    i += 1;
  }
  flushList();
  flushMath();

  return <div className="min-w-0 leading-7 text-slate-800">{elements}</div>;
}

// SynthesisSubview — PR-C2.b audit round-2: always-render principle.
export function SynthesisSubview({
  runId,
  currentState,
  paperMode,
  synthesisBundle,
  progress,
  isStartingSynthesizer,
  isStartingFrameworkLens,
  isStartingIdeator,
  onRunSynthesizer,
  onRunFrameworkLens,
  onRunIdeator,
  curatorCompleted,
  synthesizerCompleted,
}: {
  runId: string | undefined;
  currentState: string | undefined;
  paperMode?: string;
  synthesisBundle: SynthesisBundle | null;
  progress: RunEvent[];
  isStartingSynthesizer: boolean;
  isStartingFrameworkLens: boolean;
  isStartingIdeator: boolean;
  onRunSynthesizer: () => Promise<void>;
  onRunFrameworkLens: () => Promise<void>;
  onRunIdeator: () => Promise<void>;
  /** PR-C2.b audit round-2: gate synthesizer button visibility hint. */
  curatorCompleted: boolean;
  // PR #319 follow-up: synthesizer ends at SYNTHESIZER_RUNNING on
  // phase_done; banner needs `completed` to hide once finished.
  synthesizerCompleted: boolean;
}) {
  const t = useT();
  const [page, setPage] = useState(0);
  // PR-C1.b: synthesis-internal sub-tabs.
  const [innerTab, setInnerTab] = useState<"claims" | "ledger">("claims");
  const dualTrack = synthesisBundle?.dual_track ?? null;
  const partition = useMemo(() => partitionDualTrack(dualTrack), [dualTrack]);
  const sortedClaims = useMemo(() => {
    const claims = synthesisBundle?.claims ?? [];
    return [...claims].sort((left, right) =>
      left.claim_type.localeCompare(right.claim_type),
    );
  }, [synthesisBundle?.claims]);
  const totalPages = Math.max(
    1,
    Math.ceil(sortedClaims.length / CLAIM_PAGE_SIZE),
  );
  const visibleClaims = useMemo(() => {
    const start = page * CLAIM_PAGE_SIZE;
    return sortedClaims.slice(start, start + CLAIM_PAGE_SIZE);
  }, [page, sortedClaims]);
  const groupedClaims = useMemo(
    () => groupClaimsByType(visibleClaims),
    [visibleClaims],
  );

  // PR-C2.b audit round-2: always-render + disabled + hint.
  const requiresDeepDiveSourceReview = currentState === "USER_DEEP_DIVE_REVIEW";
  // Synthesis must start from SourcesSubview so the deep-dive review
  // checkpoint is saved before the backend start gate runs.
  const canRunSynthesizer = false;
  let synthesizerHint: string | null = null;
  if (currentState === "SYNTHESIZER_RUNNING") {
    synthesizerHint = t("workspace.synthesis.disabled.running");
  } else if (requiresDeepDiveSourceReview) {
    synthesizerHint = curatorCompleted
      ? t("workspace.synthesis.disabled.deep_dive_review_required")
      : t("workspace.synthesis.disabled.waiting_curator");
  } else if (
    currentState === "USER_FIELD_REVIEW" ||
    currentState === "FRAMEWORK_LENS_RUNNING" ||
    currentState === "USER_LENS_REVIEW" ||
    currentState === "IDEATOR_RUNNING" ||
    currentState === "USER_NOVELTY_REVIEW" ||
    currentState === "DRAFTER_RUNNING" ||
    currentState === "STYLIST_RUNNING" ||
    currentState === "USER_REVISION_REVIEW" ||
    currentState === "CRITIC_RUNNING" ||
    currentState === "USER_EXTERNAL_SCAN_APPROVAL" ||
    currentState === "INTEGRITY_RUNNING" ||
    currentState === "USER_INTEGRITY_REVIEW" ||
    currentState === "USER_FINAL_ACCEPTANCE" ||
    currentState === "EXPORTS_RUNNING" ||
    currentState === "EXPORTS_DONE"
  ) {
    synthesizerHint = t("workspace.synthesis.disabled.already_done");
  } else if (!canRunSynthesizer) {
    synthesizerHint = t("workspace.synthesis.disabled.upstream_pending");
  }

  return (
    <section className={sectionClasses}>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.synthesis.heading")}</h2>
        <div className={inlineActionsClasses}>
          <button
            type="button"
            data-testid="phase-action-synthesizer"
            className={primaryButtonClasses}
            onClick={onRunSynthesizer}
            disabled={
              !canRunSynthesizer ||
              isStartingSynthesizer ||
              requiresDeepDiveSourceReview
            }
            aria-describedby={
              synthesizerHint ? "synthesizer-disabled-hint" : undefined
            }
          >
            {isStartingSynthesizer
              ? t("phase.synthesizer.starting")
              : t("phase.synthesizer.start")}
          </button>
          {/* PR-244 deadlock-fix (codex AGREE-w-amend Q1+Q5): in-place
              "advance to lens" button visible only at
              USER_FIELD_REVIEW. Mandatory for theory_article;
              optional for case_analysis. Same testid as
              FrameworkLensSubview's button — DOM uniqueness
              guaranteed by tab mutex render.
              PR-245 follow-up: drop ``synthesizerCompleted``
              check — at USER_FIELD_REVIEW the synthesizer phase
              has by definition completed (state-machine
              guarantee); the events-polling check raced with
              first page load and blocked legitimate clicks. */}
          {currentState === "USER_FIELD_REVIEW" ? (
            <button
              type="button"
              data-testid="phase-action-framework-lens"
              className={primaryButtonClasses}
              onClick={onRunFrameworkLens}
              disabled={isStartingFrameworkLens}
            >
              {isStartingFrameworkLens
                ? t("workspace.lens.starting")
                : t("workspace.synthesis.advance_to_lens")}
            </button>
          ) : null}
          {/* PR-244 deadlock-fix: skip-lens / advance-to-novelty
              path. Hidden for theory_article (lens is mandatory
              there per backend gate). For case_analysis +
              empirical, lens is optional, so user can skip
              straight to ideator from synthesis. */}
          {currentState === "USER_FIELD_REVIEW" &&
          paperMode !== "theory_article" ? (
            <button
              type="button"
              data-testid="phase-action-ideator"
              className={secondaryButtonClasses}
              onClick={onRunIdeator}
              disabled={isStartingIdeator}
            >
              {isStartingIdeator
                ? t("workspace.lens.starting_ideator")
                : t("workspace.synthesis.skip_lens_to_novelty")}
            </button>
          ) : null}
        </div>
      </div>
      {synthesizerHint ? (
        <p
          id="synthesizer-disabled-hint"
          role="status"
          data-testid="synthesizer-disabled-hint"
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {synthesizerHint}
        </p>
      ) : null}
      {currentState === "SYNTHESIZER_RUNNING" ? (
        <PhaseRunningBanner
          phase="synthesizer"
          progress={progress}
          hintEvent={progress[0] ?? null}
          completed={synthesizerCompleted}
        />
      ) : null}
      {currentState === "TENSION_EXTRACTION_RUNNING" ? (
        <PhaseRunningBanner phase="tension_extraction" />
      ) : null}
      {currentState === "SYNTHESIZER_RUNNING" ? (
        <>
          <h2 className={h2Classes}>
            {t("workspace.synthesis.progress_heading")}
          </h2>
          {progress.length > 0 ? (
            <ul className={cardListClasses}>
              {progress.map((event) => (
                <li
                  className={infoCardClasses + " sm:flex sm:justify-between"}
                  key={event.id}
                >
                  <strong>
                    {String(
                      event.payload.source_id ??
                        t("workspace.common.source_default"),
                    )}
                  </strong>
                  <span>
                    {String(
                      event.payload.status ??
                        t("workspace.common.status_pending"),
                    )}
                  </span>
                  <span>
                    {Number(event.payload.completed ?? 0)} /{" "}
                    {Number(event.payload.total ?? 0)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="leading-7 text-slate-700">
              {t("workspace.synthesis.no_progress")}
            </p>
          )}
        </>
      ) : null}
      {currentState === "USER_FIELD_REVIEW" ? (
        <>
          <MaterialDiagnosticPanel
            diagnostic={synthesisBundle?.material_diagnostic ?? null}
          />
          {/* PR-C1.b: inner sub-tabs */}
          <div
            className="my-4 inline-flex gap-1 rounded-lg border border-slate-200 bg-slate-50 p-1"
            role="tablist"
          >
            <button
              type="button"
              role="tab"
              data-testid="synthesis-inner-tab-claims"
              data-active={innerTab === "claims" ? "true" : "false"}
              className={tabButtonClasses(innerTab === "claims")}
              onClick={() => setInnerTab("claims")}
            >
              {t("workspace.synthesis.claims_heading")}
            </button>
            <button
              type="button"
              role="tab"
              data-testid="synthesis-inner-tab-ledger"
              data-active={innerTab === "ledger" ? "true" : "false"}
              className={tabButtonClasses(innerTab === "ledger")}
              onClick={() => setInnerTab("ledger")}
            >
              {t("workspace.evidence_ledger.tab_label")}
            </button>
          </div>

          {innerTab === "ledger" ? (
            <EvidenceLedgerPanel
              runId={runId}
              dualTrackPresent={Boolean(dualTrack)}
              synthesisRunCompleted={Boolean(
                synthesisBundle?.synthesizer_report,
              )}
            />
          ) : null}

          {innerTab === "claims" ? (
            <>
              {dualTrack ? <DualTrackPanel partition={partition} /> : null}
              <h2 className={h2Classes}>
                {t("workspace.synthesis.report_heading")}
              </h2>
              <pre className={reportPreClasses}>
                {synthesisBundle?.synthesizer_report ||
                  t("workspace.console.report_pending")}
              </pre>
              <h2 className={h2Classes}>
                {t("workspace.synthesis.claims_heading")}
              </h2>
            </>
          ) : null}
          {innerTab === "claims" && visibleClaims.length > 0 ? (
            <>
              {Object.entries(groupedClaims).map(([claimType, items]) => (
                <section className="mt-5" key={claimType}>
                  <h3 className="mb-2 text-base font-bold text-slate-950">
                    {claimType}
                  </h3>
                  <ul className={cardListClasses}>
                    {items.map((claim) => (
                      <li
                        className="grid list-none gap-2 rounded-lg border border-slate-200 p-3 md:grid-cols-[10rem_minmax(0,1fr)_8rem] md:items-start"
                        key={claim.claim_id}
                      >
                        <strong>{claim.source_id}</strong>
                        <span>{claim.text}</span>
                        {claim.page_anchor ? (
                          <span>{claim.page_anchor}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
              <div className="mt-4 grid gap-3 sm:flex sm:items-center sm:justify-end">
                <button
                  type="button"
                  className={secondaryButtonClasses}
                  onClick={() => setPage((current) => Math.max(0, current - 1))}
                  disabled={page === 0}
                >
                  {t("workspace.common.previous")}
                </button>
                <span>
                  {t("workspace.common.page_of", {
                    current: page + 1,
                    total: totalPages,
                  })}
                </span>
                <button
                  type="button"
                  className={secondaryButtonClasses}
                  onClick={() =>
                    setPage((current) => Math.min(totalPages - 1, current + 1))
                  }
                  disabled={page + 1 >= totalPages}
                >
                  {t("workspace.common.next")}
                </button>
              </div>
            </>
          ) : innerTab === "claims" ? (
            <p className="leading-7 text-slate-700">
              {t("workspace.synthesis.no_claims")}
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function DualTrackPanel({
  partition,
}: {
  partition: ReturnType<typeof partitionDualTrack>;
}) {
  const t = useT();
  return (
    <section className="my-4" data-testid="synthesis-dual-track">
      <h2 className={h2Classes}>
        {t("workspace.synthesis.dual_track.heading")}
      </h2>
      <div className="grid gap-4 lg:grid-cols-2">
        <div data-testid="synthesis-dual-track-primary">
          <h3 className="mb-2 text-base font-bold text-slate-950">
            {t("workspace.synthesis.dual_track.primary_heading")}
          </h3>
          {partition.primary.length > 0 ? (
            <ul className="grid gap-2">
              {partition.primary.map((claim) => (
                <li
                  key={claim.claim_id}
                  className="rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm"
                >
                  <p className="font-semibold text-slate-900">{claim.text}</p>
                  <p className="mt-1 text-xs text-slate-600">
                    {claim.source_id}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-slate-500">
              {t("workspace.synthesis.dual_track.no_primary")}
            </p>
          )}
        </div>
        <div data-testid="synthesis-dual-track-secondary">
          <h3 className="mb-2 text-base font-bold text-slate-950">
            {t("workspace.synthesis.dual_track.secondary_heading")}
          </h3>
          {partition.secondary.length > 0 ? (
            <ul className="grid gap-2">
              {partition.secondary.map((claim) => (
                <li
                  key={claim.claim_id}
                  className="rounded-lg border border-slate-200 bg-white p-3 text-sm"
                >
                  <p className="font-semibold text-slate-900">{claim.text}</p>
                  <p className="mt-1 text-xs text-slate-600">
                    {claim.source_id}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-slate-500">
              {t("workspace.synthesis.dual_track.no_secondary")}
            </p>
          )}
        </div>
      </div>
      {partition.lens.length > 0 ? (
        <details className="mt-4" data-testid="synthesis-dual-track-lens">
          <summary className="cursor-pointer text-sm font-semibold text-purple-900">
            {t("workspace.synthesis.dual_track.lens_heading")}
          </summary>
          <ul className="mt-2 grid gap-2">
            {partition.lens.map((claim) => (
              <li
                key={claim.claim_id}
                className="rounded-md border border-purple-200 bg-purple-50 p-2 text-xs"
              >
                <p className="font-semibold">{claim.text}</p>
                <p className="text-[11px] text-slate-600">{claim.source_id}</p>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {partition.method.length > 0 ? (
        <details className="mt-2" data-testid="synthesis-dual-track-method">
          <summary className="cursor-pointer text-sm font-semibold text-amber-900">
            {t("workspace.synthesis.dual_track.method_heading")}
          </summary>
          <ul className="mt-2 grid gap-2">
            {partition.method.map((claim) => (
              <li
                key={claim.claim_id}
                className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs"
              >
                <p className="font-semibold">{claim.text}</p>
                <p className="text-[11px] text-slate-600">{claim.source_id}</p>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function EvidenceLedgerPanel({
  runId,
  dualTrackPresent,
  synthesisRunCompleted,
}: {
  runId: string | undefined;
  dualTrackPresent: boolean;
  synthesisRunCompleted: boolean;
}) {
  const t = useT();
  const [data, setData] = useState<EvidenceLedgerResponse | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [pendingClaim, setPendingClaim] = useState<string | null>(null);

  useEffect(() => {
    if (!runId) {
      setData(null);
      return;
    }
    let cancelled = false;
    getEvidenceLedger(runId)
      .then((res) => {
        if (!cancelled) setData(res);
      })
      .catch(() => {
        if (!cancelled) setData(null);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, refreshTick]);

  async function applyOverride(
    sourceId: string,
    claimId: string | null,
    action: "attribute_to_user" | "cite_normally",
  ) {
    if (!runId || pendingClaim) return;
    setPendingClaim(claimId ?? `__source_${sourceId}__`);
    try {
      await appendEvidenceLedgerOverride(runId, {
        source_id: sourceId,
        claim_id: claimId,
        action,
      });
      setRefreshTick((tick) => tick + 1);
    } catch (err) {
      console.warn("evidence ledger override failed", err);
    } finally {
      setPendingClaim(null);
    }
  }

  const reason = evidenceLedgerEmptyReason(
    data?.artifact_present ?? dualTrackPresent ?? false,
    data?.entries.length ?? 0,
    synthesisRunCompleted,
  );

  return (
    <section
      className="mt-3"
      data-testid="evidence-ledger-panel"
      data-empty-reason={reason}
    >
      <h2 className={h2Classes}>{t("workspace.evidence_ledger.heading")}</h2>
      {reason !== "ready" ? (
        <p
          className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600"
          data-testid={`evidence-ledger-empty-${reason}`}
        >
          {t(`workspace.evidence_ledger.empty.${reason}`)}
        </p>
      ) : null}
      {reason === "ready" && data ? (
        <EvidenceLedgerTable
          entries={data.entries}
          pendingClaim={pendingClaim}
          onOverride={applyOverride}
        />
      ) : null}
    </section>
  );
}

function EvidenceLedgerTable({
  entries,
  pendingClaim,
  onOverride,
}: {
  entries: EvidenceLedgerEntry[];
  pendingClaim: string | null;
  onOverride: (
    sourceId: string,
    claimId: string | null,
    action: "attribute_to_user" | "cite_normally",
  ) => Promise<void>;
}) {
  const t = useT();
  // Group entries by source so the "attribute entire source"
  // button has somewhere to live.
  const groups = useMemo(() => {
    const map = new Map<string, EvidenceLedgerEntry[]>();
    for (const e of entries) {
      if (!map.has(e.source_id)) map.set(e.source_id, []);
      map.get(e.source_id)!.push(e);
    }
    return [...map.entries()];
  }, [entries]);
  return (
    <div className="grid gap-4" data-testid="evidence-ledger-table">
      {groups.map(([sourceId, rows]) => (
        <div
          key={sourceId}
          data-testid={`evidence-ledger-group-${sourceId}`}
          className="rounded-lg border border-slate-200 p-3"
        >
          <div className="mb-2 flex items-center justify-between gap-2">
            <strong className="text-sm">{sourceId}</strong>
            <button
              type="button"
              data-testid={`evidence-ledger-attribute-source-${sourceId}`}
              className="text-xs text-amber-800 underline-offset-2 hover:underline"
              disabled={pendingClaim !== null}
              onClick={() =>
                void onOverride(sourceId, null, "attribute_to_user")
              }
            >
              {t("workspace.evidence_ledger.attribute_all_button")}
            </button>
          </div>
          <ul className="grid gap-2">
            {rows.map((entry) => {
              const isPending = pendingClaim === entry.claim_id;
              const action = entry.override_action;
              return (
                <li
                  key={entry.claim_id}
                  data-testid={`evidence-ledger-row-${entry.claim_id}`}
                  data-override={action ?? "none"}
                  className="grid gap-1 rounded-md border border-slate-100 p-2 text-xs"
                >
                  <p className="font-semibold text-slate-800">
                    {entry.claim_text}
                  </p>
                  <p className="text-slate-500">
                    {t("workspace.evidence_ledger.column.citation")}:{" "}
                    {entry.citation_target || "—"} ·{" "}
                    {t("workspace.evidence_ledger.column.confidence")}:{" "}
                    {entry.confidence.toFixed(2)}
                  </p>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-slate-600">
                      {t("workspace.evidence_ledger.column.action")}:{" "}
                      {action
                        ? t(`workspace.evidence_ledger.action.${action}`)
                        : t("workspace.evidence_ledger.action.none")}
                    </span>
                    {action === "attribute_to_user" ? (
                      <button
                        type="button"
                        data-testid={`evidence-ledger-cancel-${entry.claim_id}`}
                        className="text-amber-700 hover:underline"
                        disabled={isPending}
                        onClick={() =>
                          void onOverride(
                            entry.source_id,
                            entry.claim_id,
                            "cite_normally",
                          )
                        }
                      >
                        {t("workspace.evidence_ledger.action.cite_normally")}
                      </button>
                    ) : (
                      <button
                        type="button"
                        data-testid={`evidence-ledger-attribute-${entry.claim_id}`}
                        className="text-emerald-800 hover:underline"
                        disabled={isPending}
                        onClick={() =>
                          void onOverride(
                            entry.source_id,
                            entry.claim_id,
                            "attribute_to_user",
                          )
                        }
                      >
                        {t(
                          "workspace.evidence_ledger.action.attribute_to_user",
                        )}
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}

function DetailedOutlinePanel({ outline }: { outline: AngleOutline | null }) {
  const t = useT();
  const heading = (
    <h4 className="mb-2 mt-5 text-base font-bold text-slate-950">
      {t("workspace.novelty.outline_heading")}
    </h4>
  );
  if (!outline || !outline.sections.length) {
    return (
      <>
        {heading}
        <p className="leading-7 text-slate-700">
          {t("workspace.novelty.outline_pending")}
        </p>
      </>
    );
  }
  const empty = t("workspace.novelty.outline_empty_field");
  return (
    <>
      {heading}
      <ol className="grid gap-3 pl-0">
        {outline.sections.map((section, idx) => (
          <li
            className="rounded-md border border-slate-200 bg-slate-50 p-3"
            key={`${section.section_id || idx}-${section.title}`}
          >
            <strong className="block text-slate-950">
              {section.title || section.section_id}
            </strong>
            <dl className="mt-2 grid gap-2 md:grid-cols-[10rem_minmax(0,1fr)]">
              <dt className="font-bold text-slate-500">
                {t("workspace.novelty.outline_function")}
              </dt>
              <dd className="m-0 text-slate-700">
                {section.function || empty}
              </dd>
              <dt className="font-bold text-slate-500">
                {t("workspace.novelty.outline_argument")}
              </dt>
              <dd className="m-0 text-slate-700">
                {section.argument || empty}
              </dd>
              <dt className="font-bold text-slate-500">
                {t("workspace.novelty.outline_literature")}
              </dt>
              <dd className="m-0 text-slate-700">
                {section.literature || empty}
              </dd>
              <dt className="font-bold text-slate-500">
                {t("workspace.novelty.outline_materials")}
              </dt>
              <dd className="m-0 text-slate-700">
                {section.materials || empty}
              </dd>
              <dt className="font-bold text-slate-500">
                {t("workspace.novelty.outline_relation")}
              </dt>
              <dd className="m-0 text-slate-700">
                {section.relation_to_thesis || empty}
              </dd>
              <dt className="font-bold text-slate-500">
                {t("workspace.novelty.outline_weakness")}
              </dt>
              <dd className="m-0 text-slate-700">
                {section.weakness || empty}
              </dd>
            </dl>
          </li>
        ))}
      </ol>
    </>
  );
}

function MaterialDiagnosticPanel({
  diagnostic,
}: {
  diagnostic: MaterialDiagnostic | null;
}) {
  const t = useT();
  const heading = (
    <h2 className={h2Classes}>{t("workspace.synthesis.diagnostic_heading")}</h2>
  );
  if (!diagnostic) {
    return (
      <>
        {heading}
        <p className="leading-7 text-slate-700">
          {t("workspace.synthesis.diagnostic_pending")}
        </p>
      </>
    );
  }
  const sufficientLabel = diagnostic.sufficient
    ? t("workspace.synthesis.diagnostic_yes")
    : t("workspace.synthesis.diagnostic_no");
  const actionKey = `workspace.synthesis.diagnostic_action_${diagnostic.recommended_action}`;
  const actionLabel = t(actionKey);
  const empty = t("workspace.synthesis.diagnostic_none");
  const renderList = (items: string[]) =>
    items.length > 0 ? (
      <ul className={cardListClasses}>
        {items.map((item, idx) => (
          <li className={infoCardClasses} key={`${idx}-${item.slice(0, 24)}`}>
            {item}
          </li>
        ))}
      </ul>
    ) : (
      <p className="leading-7 text-slate-700">{empty}</p>
    );
  return (
    <>
      {heading}
      <ul className={cardListClasses}>
        <li className={infoCardClasses + " sm:flex sm:justify-between"}>
          <strong>{t("workspace.synthesis.diagnostic_sufficient")}</strong>
          <span>{sufficientLabel}</span>
        </li>
        <li className={infoCardClasses + " sm:flex sm:justify-between"}>
          <strong>{t("workspace.synthesis.diagnostic_action")}</strong>
          <span>{actionLabel}</span>
        </li>
      </ul>
      <h3 className="mb-2 mt-5 text-base font-bold text-slate-950">
        {t("workspace.synthesis.diagnostic_candidate_titles")}
      </h3>
      {renderList(diagnostic.candidate_titles)}
      <h3 className="mb-2 mt-5 text-base font-bold text-slate-950">
        {t("workspace.synthesis.diagnostic_missing")}
      </h3>
      {renderList(diagnostic.missing_materials)}
      <h3 className="mb-2 mt-5 text-base font-bold text-slate-950">
        {t("workspace.synthesis.diagnostic_risks")}
      </h3>
      {renderList(diagnostic.risks)}
      <h3 className="mb-2 mt-5 text-base font-bold text-slate-950">
        {t("workspace.synthesis.diagnostic_rationale")}
      </h3>
      <p className="leading-7 text-slate-700">
        {diagnostic.rationale || empty}
      </p>
    </>
  );
}

function groupClaimsByType(
  claims: SynthesisClaim[],
): Record<string, SynthesisClaim[]> {
  return claims.reduce<Record<string, SynthesisClaim[]>>((groups, claim) => {
    const claimType = claim.claim_type || "finding";
    groups[claimType] = [...(groups[claimType] ?? []), claim];
    return groups;
  }, {});
}

export function SourcesSubview({
  runId,
  currentState,
  skimCandidates,
  shortlist,
  manifest,
  manualRequests,
  curationReport,
  sourceQualityCounts,
  isStartingCurator,
  isStartingSynthesizer,
  isUploadingPdf,
  blockedPhase,
  onRunCurator,
  onRunSynthesizer,
  onUploadPdf,
  onRefresh,
  synthesisArtifactPresent,
  scoutCompleted,
  curatorCompleted,
  scoutProgress,
  curatorProgress,
}: {
  runId: string | undefined;
  currentState: string | undefined;
  skimCandidates: DiscoverySource[];
  shortlist: DiscoverySource[];
  manifest: SourcesBundle["fulltext_manifest"];
  manualRequests: ManualUploadRequest[];
  curationReport: string;
  sourceQualityCounts: NonNullable<SourcesBundle["source_quality_counts"]>;
  isStartingCurator: boolean;
  isStartingSynthesizer: boolean;
  isUploadingPdf: boolean;
  blockedPhase?: string | null;
  onRunCurator: () => Promise<void>;
  onRunSynthesizer: () => Promise<void>;
  onUploadPdf: (formData: FormData) => Promise<void>;
  // PR-C1.b: parent triggers a sources refetch after a research-role
  // override succeeds so the new badge appears in the list.
  onRefresh?: () => void;
  // PR-C1.b: when true, the synthesis dual-track artifact already
  // exists; warning copy is shown that overriding will mark synthesis
  // stale (per backend's set_branch_stale on PUT).
  synthesisArtifactPresent?: boolean;
  /** PR-C2.b audit round-2: gate curator-start button visibility hint. */
  scoutCompleted: boolean;
  // PR #319 follow-up: scout / curator end at *_RUNNING on phase_done
  // waiting for the user to advance; banners need these flags to hide
  // once finished.
  curatorCompleted: boolean;
  // ``source_progress`` events tagged with the scout / curator phase,
  // forwarded so the running banner can render "已完成 N/M".
  scoutProgress: RunEvent[];
  curatorProgress: RunEvent[];
}) {
  const t = useT();
  const initialSourceTab = resolveDefaultSourceTab(
    currentState,
    shortlist.length,
    skimCandidates.length,
  );
  const [activeSourceTab, setActiveSourceTab] =
    useState<SourceTabId>(initialSourceTab);
  const [page, setPage] = useState(0);
  const [showUploadForm, setShowUploadForm] = useState(false);
  const [searchReviewDecisions, setSearchReviewDecisions] = useState<
    Record<string, SourceReviewDecision>
  >({});
  const [deepDiveReviewDecisions, setDeepDiveReviewDecisions] = useState<
    Record<string, SourceReviewDecision>
  >({});
  const [isSavingSourceReview, setIsSavingSourceReview] = useState(false);
  const [sourceReviewError, setSourceReviewError] = useState<string | null>(
    null,
  );
  const userSelectedSourceTab = useRef(false);
  const sortedSkimCandidates = useMemo(
    () =>
      skimCandidates
        .map((source, index) => ({ source, index }))
        .sort((left, right) => {
          const byScore =
            (Number(right.source.rank_score) || 0) -
            (Number(left.source.rank_score) || 0);
          return byScore !== 0 ? byScore : left.index - right.index;
        })
        .map(({ source }) => source),
    [skimCandidates],
  );
  const skimCandidateIds = useMemo(
    () => sortedSkimCandidates.map((source) => source.source_id),
    [sortedSkimCandidates],
  );
  const shortlistIds = useMemo(
    () => shortlist.map((source) => source.source_id),
    [shortlist],
  );

  const currentRows = useMemo(() => {
    if (activeSourceTab === "shortlist") return shortlist;
    if (activeSourceTab === "skimmed") return sortedSkimCandidates;
    return [];
  }, [activeSourceTab, shortlist, sortedSkimCandidates]);
  const totalPages = Math.max(
    1,
    Math.ceil(currentRows.length / SOURCE_PAGE_SIZE),
  );
  const visibleRows = useMemo(() => {
    const start = page * SOURCE_PAGE_SIZE;
    return currentRows.slice(start, start + SOURCE_PAGE_SIZE);
  }, [currentRows, page]);

  useEffect(() => {
    userSelectedSourceTab.current = false;
    setSearchReviewDecisions({});
    setDeepDiveReviewDecisions({});
    setSourceReviewError(null);
  }, [runId]);

  useEffect(() => {
    const sourceIds = new Set(skimCandidateIds);
    setSearchReviewDecisions((current) =>
      pruneSourceReviewDecisions(current, sourceIds),
    );
  }, [skimCandidateIds]);

  useEffect(() => {
    if (currentState !== "USER_DEEP_DIVE_REVIEW") return;
    setDeepDiveReviewDecisions((current) =>
      withDefaultSourceReviewDecisions(current, shortlistIds, "approved"),
    );
  }, [currentState, shortlistIds]);

  useEffect(() => {
    if (userSelectedSourceTab.current) return;
    const nextTab = resolveDefaultSourceTab(
      currentState,
      shortlist.length,
      skimCandidates.length,
    );
    if (activeSourceTab !== nextTab) {
      setActiveSourceTab(nextTab);
      setPage(0);
    }
  }, [activeSourceTab, currentState, shortlist.length, skimCandidates.length]);

  function switchTab(nextTab: SourceTabId) {
    userSelectedSourceTab.current = true;
    setActiveSourceTab(nextTab);
    setPage(0);
  }

  function setReviewDecision(
    scope: SourceReviewScope,
    sourceId: string,
    decision: SourceReviewDecision,
  ) {
    setSourceReviewError(null);
    const setter =
      scope === "search_review"
        ? setSearchReviewDecisions
        : setDeepDiveReviewDecisions;
    setter((current) => {
      const currentDecision = current[sourceId];
      if (currentDecision === decision && scope === "search_review") {
        const next = { ...current };
        delete next[sourceId];
        return next;
      }
      if (currentDecision === decision && scope === "deep_dive_review") {
        return current;
      }
      return { ...current, [sourceId]: decision };
    });
  }

  function setAllReviewDecisions(
    scope: SourceReviewScope,
    sourceIds: string[],
    decision: SourceReviewDecision,
  ) {
    setSourceReviewError(null);
    const next = Object.fromEntries(
      sourceIds.map((sourceId) => [sourceId, decision]),
    ) as Record<string, SourceReviewDecision>;
    if (scope === "search_review") {
      setSearchReviewDecisions(next);
    } else {
      setDeepDiveReviewDecisions(next);
    }
  }

  function clearReviewDecisions(scope: SourceReviewScope) {
    setSourceReviewError(null);
    if (scope === "search_review") {
      setSearchReviewDecisions({});
    } else {
      setDeepDiveReviewDecisions(
        Object.fromEntries(
          shortlistIds.map((sourceId) => [sourceId, "approved"]),
        ) as Record<string, SourceReviewDecision>,
      );
    }
  }

  async function handleUploadSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    await onUploadPdf(formData);
    event.currentTarget.reset();
    setShowUploadForm(false);
    userSelectedSourceTab.current = true;
    setActiveSourceTab("shortlist");
  }

  // PR-C2.b audit round-2: always-render curator button + hint.
  const searchReviewStats = reviewStatsFor(
    skimCandidateIds,
    searchReviewDecisions,
    null,
  );
  const deepDiveReviewStats = reviewStatsFor(
    shortlistIds,
    deepDiveReviewDecisions,
    "approved",
  );
  const isSearchReviewGate = currentState === "USER_SEARCH_REVIEW";
  const isDeepDiveReviewGate = currentState === "USER_DEEP_DIVE_REVIEW";
  const curatorReadiness = resolveCuratorReadiness({
    currentState,
    scoutCompleted,
    blockedPhase,
  });
  const searchReviewNeedsSelection =
    isSearchReviewGate && searchReviewStats.selected === 0;
  const canRunCurator =
    curatorReadiness.canRun &&
    !searchReviewNeedsSelection &&
    !isSavingSourceReview;
  const curatorHint = curatorReadiness.reasonKey
    ? t(curatorReadiness.reasonKey, curatorReadiness.reasonValues)
    : searchReviewNeedsSelection
      ? t("workspace.sources.review.select_before_curator")
      : null;
  const deepDiveNeedsSelection =
    isDeepDiveReviewGate && deepDiveReviewStats.selected === 0;
  const synthesizerHint = deepDiveNeedsSelection
    ? t("workspace.sources.review.select_before_synthesizer")
    : null;
  const showScoutCandidateNotice =
    currentState === "USER_SEARCH_REVIEW" &&
    activeSourceTab === "skimmed" &&
    sortedSkimCandidates.length > 0;
  const emptySourceMessageKey =
    activeSourceTab === "shortlist" && skimCandidates.length > 0
      ? currentState === "USER_SEARCH_REVIEW"
        ? "workspace.sources.empty_shortlist_search_review"
        : "workspace.sources.empty_shortlist_has_skimmed"
      : activeSourceTab === "skimmed"
        ? currentState === "USER_SEARCH_REVIEW"
          ? "workspace.sources.empty_skimmed_search_review"
        : "workspace.sources.empty_skimmed"
        : "workspace.sources.no_sources";
  const activeReviewScope =
    isSearchReviewGate && activeSourceTab === "skimmed"
      ? "search_review"
      : isDeepDiveReviewGate && activeSourceTab === "shortlist"
        ? "deep_dive_review"
        : null;
  const activeReviewDecisions =
    activeReviewScope === "search_review"
      ? searchReviewDecisions
      : deepDiveReviewDecisions;
  const activeReviewDefaultDecision =
    activeReviewScope === "deep_dive_review" ? "approved" : null;

  async function saveReviewCheckpoint(
    scope: SourceReviewScope,
    sourceIds: string[],
    decisions: Record<string, SourceReviewDecision>,
    defaultDecision: SourceReviewDecision | null,
  ) {
    if (!runId) return;
    const payload = buildSourceReviewPayload(
      sourceIds,
      decisions,
      defaultDecision,
      scope,
    );
    await saveSourceReviewCheckpoint(
      runId,
      scope === "search_review" ? "USER_SEARCH_REVIEW" : "USER_DEEP_DIVE_REVIEW",
      payload,
    );
  }

  async function handleRunCuratorWithReview() {
    if (!canRunCurator || isStartingCurator) return;
    setSourceReviewError(null);
    setIsSavingSourceReview(true);
    try {
      if (isSearchReviewGate) {
        await saveReviewCheckpoint(
          "search_review",
          skimCandidateIds,
          searchReviewDecisions,
          null,
        );
      }
      await onRunCurator();
    } catch (err) {
      setSourceReviewError(
        err instanceof Error
          ? err.message
          : t("workspace.sources.review.save_failed"),
      );
    } finally {
      setIsSavingSourceReview(false);
    }
  }

  async function handleRunSynthesizerWithReview() {
    if (deepDiveNeedsSelection || isStartingSynthesizer || isSavingSourceReview) {
      return;
    }
    setSourceReviewError(null);
    setIsSavingSourceReview(true);
    try {
      if (isDeepDiveReviewGate) {
        await saveReviewCheckpoint(
          "deep_dive_review",
          shortlistIds,
          deepDiveReviewDecisions,
          "approved",
        );
      }
      await onRunSynthesizer();
    } catch (err) {
      setSourceReviewError(
        err instanceof Error
          ? err.message
          : t("workspace.sources.review.save_failed"),
      );
    } finally {
      setIsSavingSourceReview(false);
    }
  }

  return (
    <section className={sectionClasses}>
      <div className={sectionHeadingClasses}>
        <h2 className={h2Classes}>{t("workspace.sources.heading")}</h2>
        <div className={inlineActionsClasses}>
          <button
            type="button"
            data-testid="phase-action-curator"
            className={primaryButtonClasses}
            onClick={() => void handleRunCuratorWithReview()}
            disabled={!canRunCurator || isStartingCurator || isSavingSourceReview}
            aria-describedby={
              curatorHint ? "workspace-sources-curator-disabled-hint" : undefined
            }
          >
            {isStartingCurator || (isSavingSourceReview && isSearchReviewGate)
              ? t("phase.curator.starting")
              : t("phase.curator.start")}
          </button>
          {/* PR-244 deadlock-fix (codex AGREE-w-amend Q1+Q5): in-place
              "advance to synthesizer" button visible only at
              USER_DEEP_DIVE_REVIEW. Same testid as
              SynthesisSubview's button — DOM uniqueness guaranteed
              because tabs render only the active subview. Original
              novelty/lens deadlock was the same shape.
              PR-245 follow-up: gate is just the state — the
              ``curatorCompleted`` check would race with event
              polling on first page load and block legitimate
              clicks for ~15s; reaching USER_DEEP_DIVE_REVIEW
              already implies curator completed (state-machine
              guarantee). */}
          {currentState === "USER_DEEP_DIVE_REVIEW" ? (
            <button
              type="button"
              data-testid="phase-action-synthesizer"
              className={primaryButtonClasses}
              onClick={() => void handleRunSynthesizerWithReview()}
              disabled={
                isStartingSynthesizer ||
                isSavingSourceReview ||
                deepDiveNeedsSelection
              }
              aria-describedby={
                synthesizerHint
                  ? "workspace-sources-synthesizer-disabled-hint"
                  : undefined
              }
            >
              {isStartingSynthesizer ||
              (isSavingSourceReview && isDeepDiveReviewGate)
                ? t("phase.synthesizer.starting")
                : t("workspace.sources.advance_to_synthesizer")}
            </button>
          ) : null}
          <button
            type="button"
            data-testid="sources-global-upload-pdf-button"
            className={secondaryButtonClasses}
            onClick={() => setShowUploadForm((current) => !current)}
          >
            {t("workspace.sources.upload_pdf")}
          </button>
        </div>
      </div>
      {curatorHint ? (
        <p
          id="workspace-sources-curator-disabled-hint"
          role="status"
          data-testid="workspace-sources-curator-disabled-hint"
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {curatorHint}
        </p>
      ) : null}
      {synthesizerHint ? (
        <p
          id="workspace-sources-synthesizer-disabled-hint"
          role="status"
          data-testid="workspace-sources-synthesizer-disabled-hint"
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {synthesizerHint}
        </p>
      ) : null}
      {sourceReviewError ? (
        <p
          role="alert"
          data-testid="workspace-source-review-error"
          className="rounded-md border border-red-300 bg-red-50 p-2 text-xs leading-5 text-red-800"
        >
          {sourceReviewError}
        </p>
      ) : null}
      {showScoutCandidateNotice ? (
        <p
          role="status"
          data-testid="workspace-sources-scout-candidates-notice"
          className="rounded-md border border-sky-200 bg-sky-50 p-2 text-xs leading-5 text-sky-900"
        >
          {t("workspace.sources.scout_candidates_notice")}
        </p>
      ) : null}
      <SourceQualityCounts counts={sourceQualityCounts} />
      {currentState === "SCOUT_RUNNING" ? (
        <PhaseRunningBanner
          phase="scout"
          progress={scoutProgress}
          hintEvent={scoutProgress[0] ?? null}
          completed={scoutCompleted}
        />
      ) : null}
      {currentState === "CURATOR_RUNNING" ? (
        <PhaseRunningBanner
          phase="curator"
          progress={curatorProgress}
          hintEvent={curatorProgress[0] ?? null}
          completed={curatorCompleted}
        />
      ) : null}
      {showUploadForm ? (
        <form
          className="my-4 grid gap-3 rounded-lg border border-slate-200 p-3 sm:grid-cols-2 lg:grid-cols-3"
          onSubmit={handleUploadSubmit}
        >
          <input
            className={inputClasses}
            name="source_id"
            defaultValue="new"
            aria-label={t("workspace.sources.aria_source_id")}
          />
          <input
            className={inputClasses}
            name="title"
            placeholder={t("workspace.sources.title_placeholder")}
            aria-label={t("workspace.sources.title_placeholder")}
            required
          />
          <input
            className={inputClasses}
            name="authors"
            placeholder={t("workspace.sources.authors_placeholder")}
            aria-label={t("workspace.sources.authors_placeholder")}
          />
          <input
            className={inputClasses}
            name="year"
            placeholder={t("workspace.sources.year_placeholder")}
            aria-label={t("workspace.sources.year_placeholder")}
            type="number"
          />
          <input
            className={inputClasses}
            name="doi"
            placeholder={t("workspace.sources.doi_placeholder")}
            aria-label={t("workspace.sources.doi_placeholder")}
          />
          <input
            className={inputClasses}
            name="url"
            placeholder={t("workspace.sources.url_placeholder")}
            aria-label={t("workspace.sources.url_placeholder")}
          />
          <input
            className={inputClasses + " sm:col-span-2 lg:col-span-1"}
            name="pdf"
            aria-label={t("workspace.sources.pdf_aria")}
            type="file"
            accept="application/pdf"
            required
          />
          <button
            type="submit"
            className={primaryButtonClasses}
            disabled={isUploadingPdf}
          >
            {isUploadingPdf
              ? t("workspace.sources.uploading")
              : t("workspace.sources.upload")}
          </button>
        </form>
      ) : null}
      <div
        className="my-4 flex snap-x gap-2 overflow-x-auto pb-2 md:inline-flex md:overflow-visible md:rounded-lg md:border md:border-slate-200 md:bg-slate-50 md:p-1"
        role="tablist"
        aria-label={t("workspace.sources.tablist_label")}
      >
        <button
          type="button"
          data-testid="workspace-sources-tab-shortlist"
          data-active={activeSourceTab === "shortlist" ? "true" : "false"}
          className={tabButtonClasses(activeSourceTab === "shortlist")}
          onClick={() => switchTab("shortlist")}
        >
          {t("workspace.sources.tab_shortlist")}
        </button>
        <button
          type="button"
          data-testid="workspace-sources-tab-manual"
          data-active={activeSourceTab === "manual" ? "true" : "false"}
          className={tabButtonClasses(activeSourceTab === "manual")}
          onClick={() => switchTab("manual")}
        >
          {t("workspace.sources.tab_manual")}
        </button>
        <button
          type="button"
          data-testid="workspace-sources-tab-skimmed"
          data-active={activeSourceTab === "skimmed" ? "true" : "false"}
          className={tabButtonClasses(activeSourceTab === "skimmed")}
          onClick={() => switchTab("skimmed")}
        >
          {t("workspace.sources.tab_skimmed")}
        </button>
      </div>
      {activeReviewScope ? (
        <SourceReviewPanel
          scope={activeReviewScope}
          stats={
            activeReviewScope === "search_review"
              ? searchReviewStats
              : deepDiveReviewStats
          }
          onApproveAll={() =>
            setAllReviewDecisions(
              activeReviewScope,
              activeReviewScope === "search_review"
                ? skimCandidateIds
                : shortlistIds,
              "approved",
            )
          }
          onClear={() => clearReviewDecisions(activeReviewScope)}
        />
      ) : null}
      {activeSourceTab === "manual" ? (
        <ManualRequestsList
          requests={manualRequests}
          isUploadingPdf={isUploadingPdf}
          onUploadPdf={onUploadPdf}
        />
      ) : visibleRows.length > 0 ? (
        <>
          <div className="grid gap-3">
            {visibleRows.map((source) => (
              <SourceRow
                key={source.source_id}
                runId={runId}
                source={source}
                hasPdf={Boolean(manifest[source.source_id])}
                isUploadingPdf={isUploadingPdf}
                onUploadPdf={onUploadPdf}
                onRoleChanged={onRefresh}
                synthesisArtifactPresent={synthesisArtifactPresent ?? false}
                reviewScope={activeReviewScope}
                reviewDecision={
                  activeReviewScope
                    ? reviewDecisionFor(
                        activeReviewDecisions,
                        source.source_id,
                        activeReviewDefaultDecision,
                      )
                    : null
                }
                onReviewDecision={setReviewDecision}
              />
            ))}
          </div>
          <div className="mt-4 grid gap-3 sm:flex sm:items-center sm:justify-end">
            <button
              type="button"
              className={secondaryButtonClasses}
              onClick={() => setPage((current) => Math.max(0, current - 1))}
              disabled={page === 0}
            >
              {t("workspace.common.previous")}
            </button>
            <span>
              {t("workspace.common.page_of", {
                current: page + 1,
                total: totalPages,
              })}
            </span>
            <button
              type="button"
              className={secondaryButtonClasses}
              onClick={() =>
                setPage((current) => Math.min(totalPages - 1, current + 1))
              }
              disabled={page + 1 >= totalPages}
            >
              {t("workspace.common.next")}
            </button>
          </div>
        </>
      ) : (
        <p
          className="leading-7 text-slate-700"
          data-testid="workspace-sources-empty-state"
        >
          {t(emptySourceMessageKey)}
        </p>
      )}
      {currentState === "USER_DEEP_DIVE_REVIEW" ? (
        <section className="mt-6">
          <h2 className={h2Classes}>{t("workspace.sources.curator_report")}</h2>
          <pre className={reportPreClasses}>
            {curationReport || t("workspace.console.report_pending")}
          </pre>
        </section>
      ) : null}
    </section>
  );
}

function SourceQualityCounts({
  counts,
}: {
  counts: NonNullable<SourcesBundle["source_quality_counts"]>;
}) {
  const t = useT();
  const items = [
    {
      key: "off_topic_dropped",
      value: counts.off_topic_dropped ?? 0,
      label: t("workspace.sources.quality.off_topic_dropped"),
    },
    {
      key: "verification_rejected",
      value: counts.verification_rejected ?? 0,
      label: t("workspace.sources.quality.verification_rejected"),
    },
    {
      key: "runner_up",
      value: counts.runner_up ?? 0,
      label: t("workspace.sources.quality.runner_up"),
    },
    {
      key: "weak_anchor",
      value: counts.weak_anchor ?? 0,
      label: t("workspace.sources.quality.weak_anchor"),
    },
  ];
  if (items.every((item) => item.value === 0)) {
    return null;
  }
  return (
    <dl
      className="grid gap-2 rounded-md border border-slate-200 bg-slate-50 p-3 text-xs text-slate-700 sm:grid-cols-4"
      data-testid="workspace-sources-quality-counts"
    >
      {items.map((item) => (
        <div
          key={item.key}
          data-testid={`workspace-sources-quality-count-${item.key}`}
          className="grid gap-1"
        >
          <dt className="font-semibold">{item.label}</dt>
          <dd className="text-base font-bold text-slate-950">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function SourceReviewPanel({
  scope,
  stats,
  onApproveAll,
  onClear,
}: {
  scope: SourceReviewScope;
  stats: SourceReviewStats;
  onApproveAll: () => void;
  onClear: () => void;
}) {
  const t = useT();
  return (
    <section
      className="mb-4 grid gap-3 rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-950"
      data-testid="workspace-source-review-panel"
      data-review-scope={scope}
    >
      <div className="grid gap-2 sm:flex sm:items-center sm:justify-between">
        <div>
          <h3 className="text-sm font-bold">
            {scope === "search_review"
              ? t("workspace.sources.review.search_heading")
              : t("workspace.sources.review.deep_heading")}
          </h3>
          <p
            className="mt-1 text-xs leading-5 text-sky-900"
            data-testid="workspace-source-review-summary"
          >
            {t("workspace.sources.review.summary", {
              selected: stats.selected,
              rejected: stats.rejected,
              pinned: stats.pinned,
              pending: stats.pending,
              total: stats.total,
            })}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            data-testid="workspace-source-review-approve-all-button"
            className={secondaryButtonClasses}
            onClick={onApproveAll}
            disabled={stats.total === 0}
          >
            {t("workspace.sources.review.approve_all")}
          </button>
          <button
            type="button"
            data-testid="workspace-source-review-clear-button"
            className={secondaryButtonClasses}
            onClick={onClear}
            disabled={stats.total === 0}
          >
            {scope === "search_review"
              ? t("workspace.sources.review.clear")
              : t("workspace.sources.review.reset")}
          </button>
        </div>
      </div>
    </section>
  );
}

function SourceReviewControls({
  sourceId,
  scope,
  decision,
  onDecision,
}: {
  sourceId: string;
  scope: SourceReviewScope;
  decision: SourceReviewDecision | null;
  onDecision: (
    scope: SourceReviewScope,
    sourceId: string,
    decision: SourceReviewDecision,
  ) => void;
}) {
  const t = useT();
  return (
    <div
      className="mt-3 flex flex-wrap items-center gap-2"
      data-testid={`source-row-${sourceId}-review-controls`}
      data-review-scope={scope}
      data-review-decision={decision ?? "pending"}
    >
      <span
        className="text-xs font-semibold text-slate-600"
        data-testid={`source-row-${sourceId}-review-status`}
      >
        {decision
          ? t(`workspace.sources.review.status_${decision}`)
          : t("workspace.sources.review.status_pending")}
      </span>
      {SOURCE_REVIEW_DECISIONS.map((nextDecision) => {
        const isActive = decision === nextDecision;
        return (
          <button
            key={nextDecision}
            type="button"
            data-testid={`source-row-${sourceId}-review-${nextDecision}-button`}
            data-active={isActive ? "true" : "false"}
            className={
              isActive
                ? "inline-flex min-h-9 items-center justify-center rounded border border-[#245d49] bg-[#245d49] px-3 py-1.5 text-xs font-bold text-white"
                : "inline-flex min-h-9 items-center justify-center rounded border border-slate-200 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50"
            }
            onClick={() => onDecision(scope, sourceId, nextDecision)}
          >
            {t(`workspace.sources.review.${nextDecision}`)}
          </button>
        );
      })}
    </div>
  );
}

function SourceRow({
  runId,
  source,
  hasPdf,
  isUploadingPdf,
  onUploadPdf,
  onRoleChanged,
  synthesisArtifactPresent,
  reviewScope,
  reviewDecision,
  onReviewDecision,
}: {
  runId: string | undefined;
  source: DiscoverySource;
  hasPdf: boolean;
  isUploadingPdf: boolean;
  onUploadPdf: (formData: FormData) => Promise<void>;
  onRoleChanged?: () => void;
  synthesisArtifactPresent: boolean;
  reviewScope: SourceReviewScope | null;
  reviewDecision: SourceReviewDecision | null;
  onReviewDecision: (
    scope: SourceReviewScope,
    sourceId: string,
    decision: SourceReviewDecision,
  ) => void;
}) {
  const t = useT();
  const [popoverOpen, setPopoverOpen] = useState(false);
  const [pendingRole, setPendingRole] = useState<ResearchRole | null>(null);
  const role = roleOf(source);
  const badge = badgeStyleFor(role);
  const sid = source.source_id;
  const showBoundUpload = !hasPdf && sourceNeedsBoundUpload(source);

  // PR-I4.b A7: bubble the backend error message to the user
  // (esp. the 409 when a phase is running). Pre-fix this was
  // silently swallowed via `console.warn` and the popover just
  // stayed open with no feedback — codex audit called this
  // "接近 silent no-op".
  const [roleError, setRoleError] = useState<string | null>(null);
  async function applyRole(next: ResearchRole) {
    if (!runId || pendingRole) return;
    setPendingRole(next);
    setRoleError(null);
    try {
      await updateResearchRole(runId, sid, next);
      onRoleChanged?.();
      setPopoverOpen(false);
    } catch (err) {
      setRoleError(err instanceof Error ? err.message : String(err));
    } finally {
      setPendingRole(null);
    }
  }

  return (
    <article
      data-testid={`source-row-${sid}`}
      data-research-role={role}
      className="grid gap-4 rounded-lg border border-slate-200 p-3 md:grid-cols-[minmax(0,1fr)_14rem] md:items-start md:p-4"
    >
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h3 className="text-base font-bold text-slate-950">{source.title}</h3>
          <span
            data-testid={`source-row-${sid}-role-badge`}
            data-role={role}
            className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-semibold ${badge.bg} ${badge.text} ${badge.border}`}
          >
            {t(badge.labelKey)}
          </span>
        </div>
        <p className="m-0 leading-6 text-slate-600">
          {source.authors.join(", ") || t("workspace.sources.unknown_authors")}{" "}
          - {source.year ?? t("workspace.sources.no_date")} -{" "}
          {source.venue ?? t("workspace.sources.unknown_venue")}
        </p>
        {source.risk_flags.length > 0 ? (
          <p className="m-0 mt-2 leading-6 text-slate-600">
            {source.risk_flags.join(", ")}
          </p>
        ) : null}
        {source.risk_flags.includes("weak_entity_anchor") ? (
          <span
            data-testid={`source-row-${sid}-weak-anchor-badge`}
            className="mt-2 inline-flex rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-900"
          >
            {t("workspace.sources.quality.weak_anchor_badge")}
          </span>
        ) : null}
        {reviewScope ? (
          <SourceReviewControls
            sourceId={sid}
            scope={reviewScope}
            decision={reviewDecision}
            onDecision={onReviewDecision}
          />
        ) : null}
      </div>
      <div className="grid min-w-0 gap-2 md:justify-items-end">
        <span
          className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-700"
          data-testid={`source-row-${sid}-access-status`}
        >
          {source.access_status}
        </span>
        {hasPdf && runId ? (
          <a
            className={linkClasses}
            href={`/api/runs/${runId}/sources/${encodeURIComponent(sid)}/pdf`}
          >
            PDF
          </a>
        ) : showBoundUpload ? (
          <InlineSourceUploadButton
            target={{
              source_id: sid,
              title: source.title,
              authors: source.authors,
              year: source.year,
              doi: source.doi,
              url: source.url ?? source.pdf_url,
              suggested_filename: suggestedPdfFilename(sid),
            }}
            disabled={isUploadingPdf}
            onUploadPdf={onUploadPdf}
            testId={`source-row-${sid}-upload-pdf`}
          />
        ) : null}
        {source.url ? (
          <a
            className={`${linkClasses} min-w-0 break-all md:text-right`}
            href={source.url}
            target="_blank"
            rel="noreferrer"
          >
            {sid}
          </a>
        ) : (
          <span className="min-w-0 break-all md:text-right">{sid}</span>
        )}
        <button
          type="button"
          data-testid={`source-row-${sid}-adjust-tier`}
          className="text-xs font-semibold text-slate-700 underline underline-offset-2 hover:text-slate-900"
          onClick={() => setPopoverOpen((open) => !open)}
        >
          {t("workspace.sources.research_role.adjust_button")}
        </button>
      </div>
      {popoverOpen ? (
        <div
          data-testid={`source-row-${sid}-role-popover`}
          className="md:col-span-2 grid gap-2 rounded-md border border-slate-300 bg-slate-50 p-3 text-sm"
        >
          <p className="text-sm font-semibold text-slate-800">
            {t("workspace.sources.research_role.adjust_heading")}
          </p>
          {synthesisArtifactPresent ? (
            <p className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
              {t("workspace.sources.research_role.synthesis_stale_warning")}
            </p>
          ) : null}
          {roleError ? (
            <p
              className="rounded-md border border-red-300 bg-red-50 p-2 text-xs text-red-800"
              data-testid={`source-row-${sid}-role-error`}
            >
              {roleError}
            </p>
          ) : null}
          {RESEARCH_ROLES.map((r) => {
            const style = badgeStyleFor(r);
            return (
              <label
                key={r}
                data-testid={`source-row-${sid}-role-option-${r}`}
                className="flex cursor-pointer items-center gap-2"
              >
                <input
                  type="radio"
                  name={`role-${sid}`}
                  value={r}
                  checked={role === r && pendingRole === null}
                  disabled={pendingRole !== null}
                  onChange={() => void applyRole(r)}
                />
                <span
                  className={`rounded px-1.5 py-0.5 text-xs ${style.bg} ${style.text}`}
                >
                  {t(style.labelKey)}
                </span>
                <span className="text-xs text-slate-500">
                  {t(`research_role.${r}.description`)}
                </span>
              </label>
            );
          })}
          <div className="flex justify-end">
            <button
              type="button"
              data-testid={`source-row-${sid}-role-cancel`}
              className="text-xs text-slate-500 hover:text-slate-700"
              onClick={() => setPopoverOpen(false)}
            >
              {t("workspace.sources.research_role.cancel")}
            </button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

export function ManualRequestsList({
  requests,
  isUploadingPdf,
  onUploadPdf,
}: {
  requests: ManualUploadRequest[];
  isUploadingPdf: boolean;
  onUploadPdf: (formData: FormData) => Promise<void>;
}) {
  const t = useT();
  if (requests.length === 0) {
    return (
      <p className="leading-7 text-slate-700">
        {t("workspace.sources.no_manual")}
      </p>
    );
  }
  return (
    <div className="grid gap-3">
      {requests.map((request) => (
        <article
          className="grid gap-4 rounded-lg border border-slate-200 p-3 md:grid-cols-[minmax(0,1fr)_14rem] md:items-start md:p-4"
          key={request.source_id}
        >
          <div>
            <h3 className="mb-2 text-base font-bold text-slate-950">
              {request.title}
            </h3>
            <p className="m-0 leading-6 text-slate-600">{request.reason}</p>
          </div>
          <div className="grid min-w-0 gap-2 md:justify-items-end">
            <span
              className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900"
              data-testid={`manual-request-${request.source_id}-status`}
            >
              {request.suggested_location}
            </span>
            {request.url ? (
              <a
                className={`${linkClasses} min-w-0 break-all md:text-right`}
                href={request.url}
                target="_blank"
                rel="noreferrer"
              >
                {request.source_id}
              </a>
            ) : (
              <span className="min-w-0 break-all md:text-right">
                {request.source_id}
              </span>
            )}
            <InlineSourceUploadButton
              target={{
                source_id: request.source_id,
                title: request.title,
                doi: request.doi,
                url: request.url,
                suggested_filename: request.suggested_location,
              }}
              disabled={isUploadingPdf}
              onUploadPdf={onUploadPdf}
              testId={`manual-request-${request.source_id}-upload-pdf`}
            />
          </div>
        </article>
      ))}
    </div>
  );
}

function InlineSourceUploadButton({
  target,
  disabled,
  onUploadPdf,
  testId,
}: {
  target: SourceUploadTarget;
  disabled: boolean;
  onUploadPdf: (formData: FormData) => Promise<void>;
  testId: string;
}) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [isLocalUploading, setIsLocalUploading] = useState(false);
  const isDisabled = disabled || isLocalUploading;

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    if (!file) {
      return;
    }
    setIsLocalUploading(true);
    try {
      await onUploadPdf(buildSourceUploadFormData(target, file));
    } finally {
      setIsLocalUploading(false);
      event.currentTarget.value = "";
    }
  }

  return (
    <div className="grid justify-items-start gap-1 md:justify-items-end">
      <input
        ref={inputRef}
        data-testid={`${testId}-input`}
        className="sr-only"
        type="file"
        accept="application/pdf"
        aria-label={t("workspace.sources.upload_pdf_for", {
          title: target.title,
        })}
        onChange={(event) => void handleFileChange(event)}
      />
      <button
        type="button"
        data-testid={`${testId}-button`}
        className={secondaryButtonClasses}
        disabled={isDisabled}
        onClick={() => inputRef.current?.click()}
      >
        {isLocalUploading
          ? t("workspace.sources.uploading")
          : t("workspace.sources.upload_pdf")}
      </button>
      {target.suggested_filename ? (
        <span
          className="text-xs text-slate-500"
          data-testid={`${testId}-suggested-filename`}
        >
          {target.suggested_filename}
        </span>
      ) : null}
    </div>
  );
}

function sourceNeedsBoundUpload(source: DiscoverySource): boolean {
  const accessStatus = source.access_status.toLowerCase();
  return (
    accessStatus.includes("fetch_failed") ||
    accessStatus.includes("manual_upload_required") ||
    source.risk_flags.some((flag) =>
      flag.toLowerCase().includes("manual_upload_required"),
    )
  );
}

// PR-C0.b2.ui: research-kernel edit modal. Composes
// KernelIntakeForm + handles the PUT /api/runs/{id}/research_kernel
// concurrency dance (stale_token-only conflict surfaces side-by-
// side comparison; running/lock/cancelled is inline error).

import { KernelIntakeForm } from "../components/KernelIntakeForm";
import {
  editResearchKernelWithConflictTyping,
  ResearchKernelConflictError,
  getPaperModes,
} from "../lib/api";
import {
  buildKernelPayload,
  isPaperModeReadOnly,
  isIntakeSubmittable,
  intakeSubmitDisabledReason,
  kernelToFormState,
  modeSpecOrFallback,
  type KernelIntakeFormState,
  type PaperModeSpec,
} from "../lib/kernelValidation";

function KernelEditModal({
  run,
  onClose,
  onSaved,
}: {
  run: Run;
  onClose: () => void;
  onSaved: (refreshed: Run) => void;
}) {
  const t = useT();
  const [paperModes, setPaperModes] = useState<PaperModeSpec[] | null>(null);
  const [form, setForm] = useState<KernelIntakeFormState>(() =>
    kernelToFormState(
      run.paper_mode || "case_analysis",
      run.research_kernel,
      run.paper_mode === "empirical", // existing preview = pre-acked
    ),
  );
  const [error, setError] = useState<string | null>(null);
  const [conflictServerSnapshot, setConflictServerSnapshot] =
    useState<KernelIntakeFormState | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const closeBtnRef = useRef<HTMLButtonElement | null>(null);
  const proposalVersion = run.proposal_version || 0;
  const readOnlyMode = isPaperModeReadOnly(proposalVersion);

  // Initial focus on close button (codex round-1.b2.ui modal a11y).
  // Escape close.
  useEffect(() => {
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    closeBtnRef.current?.focus();
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  // Fetch paper-modes registry; on failure, modes stays null and
  // ModeAvailability falls back to the single-mode degraded UI.
  useEffect(() => {
    let cancelled = false;
    getPaperModes()
      .then((res) => {
        if (!cancelled) setPaperModes(res.modes);
      })
      .catch(() => {
        if (!cancelled) setPaperModes(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const modeSpec = modeSpecOrFallback(paperModes, form.paper_mode);
  const submitDisabledReason = intakeSubmitDisabledReason(form, modeSpec);
  const isFormValid = isIntakeSubmittable(form, modeSpec);

  async function handleSave() {
    if (isSaving) return;
    setIsSaving(true);
    setError(null);
    try {
      const refreshed = await editResearchKernelWithConflictTyping(run.id, {
        paper_mode: form.paper_mode,
        kernel: buildKernelPayload(form),
        base_proposal_version: proposalVersion,
        base_kernel_hash: run.research_kernel_hash || "",
        accept_developer_preview: form.accept_developer_preview,
      });
      // Refetch full run; PUT response is partial.
      const fresh = await getRun(run.id);
      // Surface "saved as new version" hint via the refreshed run.
      if (fresh.proposal_version && fresh.proposal_version > proposalVersion) {
        // Toast happens implicitly via the parent's onSaved →
        // setRun → bundleRefresh; no extra UI here.
      }
      void refreshed;
      onSaved(fresh);
    } catch (caught) {
      if (caught instanceof ResearchKernelConflictError) {
        // Stale-token conflict: refetch run, render side-by-side
        // server snapshot for the user to compare. KEEP the
        // user's draft visible; don't overwrite silently.
        try {
          const fresh = await getRun(run.id);
          const snapshot = kernelToFormState(
            fresh.paper_mode || "case_analysis",
            fresh.research_kernel,
            fresh.paper_mode === "empirical",
          );
          setConflictServerSnapshot(snapshot);
          setError(t("workspace.kernel.conflict_message"));
        } catch {
          setError(t("workspace.kernel.conflict_fetch_failed"));
        }
      } else {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    } finally {
      setIsSaving(false);
    }
  }

  function applyServerSnapshot() {
    if (conflictServerSnapshot) {
      setForm(conflictServerSnapshot);
      setConflictServerSnapshot(null);
      setError(null);
    }
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="kernel-edit-modal-title"
      data-testid="kernel-edit-modal"
      className="fixed inset-0 z-50 grid place-items-center bg-slate-950/40 p-4"
    >
      <section className="grid max-h-[90vh] w-full max-w-3xl grid-rows-[auto_1fr_auto] rounded-lg bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-slate-200 p-5">
          <div>
            <p className={eyebrowClasses}>{t("workspace.kernel.eyebrow")}</p>
            <h2
              id="kernel-edit-modal-title"
              className="text-xl font-bold text-slate-950"
            >
              {t("workspace.kernel.modal_title")}
            </h2>
            <p className="mt-1 text-sm leading-6 text-slate-700">
              {t("workspace.kernel.modal_description")}
            </p>
            {readOnlyMode ? (
              <p className="mt-2 text-xs text-slate-500">
                {t("workspace.kernel.readonly_mode_hint")}
              </p>
            ) : null}
          </div>
          <button
            ref={closeBtnRef}
            type="button"
            data-testid="kernel-edit-modal-close"
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md bg-slate-100 text-xl font-bold text-[#114b5f] transition hover:bg-slate-200"
            aria-label={t("workspace.kernel.close_aria_label")}
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <div className="overflow-y-auto p-5">
          {error ? (
            <p
              data-testid="kernel-edit-error"
              className="mb-3 rounded-md border border-red-300 bg-red-50 p-2 text-sm text-red-900"
            >
              {error}
            </p>
          ) : null}
          {conflictServerSnapshot ? (
            <div
              data-testid="kernel-edit-conflict-panel"
              className="mb-3 grid gap-2 rounded-md border border-amber-300 bg-amber-50 p-3"
            >
              <p className="text-sm font-bold text-amber-900">
                {t("workspace.kernel.conflict_panel_heading")}
              </p>
              <ul className="grid gap-1 text-xs text-slate-700">
                <li>
                  <strong>{t("workspace.kernel.conflict_field.mode")}</strong>：
                  {conflictServerSnapshot.paper_mode}
                </li>
                <li>
                  <strong>
                    {t("workspace.kernel.conflict_field.observed_puzzle")}
                  </strong>
                  ：{conflictServerSnapshot.observed_puzzle.slice(0, 80) || "—"}
                  {conflictServerSnapshot.observed_puzzle.length > 80
                    ? "…"
                    : ""}
                </li>
                <li>
                  <strong>
                    {t("workspace.kernel.conflict_field.tentative_question")}
                  </strong>
                  ：{conflictServerSnapshot.tentative_question || "—"}
                </li>
                <li>
                  <strong>{t("workspace.kernel.conflict_field.scope")}</strong>
                  ：{conflictServerSnapshot.scope || "—"}
                </li>
                <li>
                  <strong>
                    {t("workspace.kernel.conflict_field.primary_materials")}
                  </strong>
                  ：{conflictServerSnapshot.primary_materials_status}
                </li>
              </ul>
              <button
                type="button"
                data-testid="kernel-edit-conflict-apply-server"
                className={secondaryButtonClasses + " w-fit"}
                onClick={applyServerSnapshot}
              >
                {t("workspace.kernel.conflict_apply_server")}
              </button>
            </div>
          ) : null}
          <KernelIntakeForm
            state={form}
            onChange={setForm}
            modes={paperModes}
            language={(run.project_language as "en" | "zh" | "ja") || "en"}
            readOnlyMode={readOnlyMode}
            testIdPrefix="kernel-edit"
            reasonElementId="kernel-edit-submit-reason"
          />
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-200 p-4">
          <button
            type="button"
            data-testid="kernel-edit-cancel"
            className={secondaryButtonClasses}
            onClick={onClose}
          >
            {t("workspace.kernel.cancel")}
          </button>
          <button
            type="button"
            data-testid="kernel-edit-save"
            className={primaryButtonClasses}
            disabled={!isFormValid || isSaving}
            aria-describedby={
              submitDisabledReason ? "kernel-edit-submit-reason" : undefined
            }
            onClick={() => void handleSave()}
          >
            {isSaving
              ? t("workspace.kernel.saving")
              : t("workspace.kernel.save")}
          </button>
        </div>
      </section>
    </div>
  );
}
