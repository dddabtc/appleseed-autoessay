export type ProjectLanguage = "en" | "zh" | "ja";
export type GenerationMode = "express" | "deep";

export interface Project {
  id: string;
  user_id: string;
  title: string;
  domain_id: string;
  domain_version: string;
  target_journal: string | null;
  language: ProjectLanguage;
  status: string;
  deleted_at?: string | null;
}

export interface Domain {
  id: string;
  display_name: string;
  version: string;
  description: string;
  target_journals: string[];
}

export interface CreateProjectRequest {
  title: string;
  domain_id?: string;
  target_journal?: string | null;
  language?: ProjectLanguage;
}

export interface RunEvent {
  id: string;
  run_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ActivePhaseLock {
  phase: string;
  job_id: string | null;
  claimed_at: string | null;
}

export interface ForceApproveHint {
  applicable: boolean;
  target_state: string | null;
  consequence: string | null;
  blockers_to_resolve: number;
}

export interface Run {
  id: string;
  project_id: string;
  project_title: string;
  project_language: ProjectLanguage;
  state: string;
  mode: GenerationMode;
  domain_id: string;
  domain_version: string;
  created_at: string;
  updated_at: string;
  last_event: RunEvent | null;
  deleted_at?: string | null;
  project_deleted_at?: string | null;
  stale_from_phase?: string | null;
  active_phase_lock?: ActivePhaseLock | null;
  force_approve?: ForceApproveHint | null;
  // PR-C0: research-kernel intake-gate state. ``research_kernel_hash``
  // is the concurrency token paired with ``proposal_version`` that
  // PUT /api/runs/{id}/research_kernel uses for lost-update
  // detection. ``research_kernel`` is the live kernel JSON (the
  // intake-gate fields plus kernel_schema_version).
  paper_mode?: string;
  research_kernel?: Record<string, unknown>;
  research_kernel_hash?: string;
  proposal_version?: number;
  // PR-366 (2026-05-13): "数理增强模式" toggle. When true, polish_loop
  // and critic_loop round-0 run the gpt-5.5 holistic A→B→C rewrite.
  mathematical_mode?: boolean;
  // PR-382 (2026-05-13): "一键全自动" auto-pilot. When true, the
  // backend coordinator advances every USER_*_REVIEW gate to the
  // next phase automatically. FAILED_* states still need user.
  auto_advance?: boolean;
}

export interface RerunResponse {
  run_id: string;
  phase: string;
  state: string;
  stale_from_phase: string | null;
}

export interface RerunBody {
  draft_hash?: string | null;
  /** Stage 3.A.4: which prompt surface ``draft_hash`` refers to.
   * Defaults to ``"main"`` server-side; explicit clients (e.g. the
   * multi-key prompt modal after the user selects a non-main key)
   * pass the resolved key. */
  prompt_key?: string;
}

export interface BranchEntry {
  id: string;
  run_id: string;
  name: string;
  parent_branch_id: string | null;
  forked_from_pv_id: string | null;
  forked_phase: string | null;
  stale_from_phase: string | null;
  is_active: boolean;
  created_at: string;
  deleted_at: string | null;
}

export interface BranchListResponse {
  run_id: string;
  active_branch_id: string | null;
  branches: BranchEntry[];
}

export function listBranches(runId: string): Promise<BranchListResponse> {
  return request<BranchListResponse>(`/api/runs/${runId}/branches`);
}

export function createBranch(
  runId: string,
  payload: { name: string; base_pv_id: string; base_branch_id?: string },
): Promise<BranchEntry> {
  return request<BranchEntry>(`/api/runs/${runId}/branches`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function switchActiveBranch(
  runId: string,
  branchId: string,
): Promise<BranchListResponse> {
  return request<BranchListResponse>(`/api/runs/${runId}/branches/active`, {
    method: "POST",
    body: JSON.stringify({ branch_id: branchId }),
  });
}

export function deleteBranch(runId: string, branchId: string): Promise<void> {
  return request<void>(`/api/runs/${runId}/branches/${branchId}`, {
    method: "DELETE",
  });
}

export function rerunPhase(
  runId: string,
  phase: string,
  body: RerunBody = {},
): Promise<RerunResponse> {
  return request<RerunResponse>(`/api/runs/${runId}/phases/${phase}/rerun`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface PhaseEditableEntry {
  path: string;
  kind: "markdown" | "json" | "jsonl" | string;
  required_with: string | null;
  current_content: string;
}

export interface PhaseEditableResponse {
  phase: string;
  base_version_id: string | null;
  entries: PhaseEditableEntry[];
  // PR-A3: when true, the UI offers a "replace current version"
  // option in addition to "publish new version". False whenever
  // a downstream phase has produced output OR the head pv is
  // shared with another branch — in either case, only "new" is
  // safe.
  replace_eligible?: boolean;
}

export interface PhaseUserEditResponse {
  phase_version_id: string;
  version_no: number;
  branch_id: string;
  source: string;
  stale_from_phase: string | null;
  // PR-A3: echoes back whichever mode the server applied
  // ("new" or "replace").
  mode?: string | null;
}

export function listEditableArtifacts(
  runId: string,
  phase: string,
): Promise<PhaseEditableResponse> {
  return request<PhaseEditableResponse>(
    `/api/runs/${runId}/phases/${phase}/editable`,
  );
}

export function editPhaseArtifacts(
  runId: string,
  phase: string,
  body: {
    base_version_id: string | null;
    files: Record<string, string>;
    mode?: "new" | "replace";
  },
): Promise<PhaseUserEditResponse> {
  return request<PhaseUserEditResponse>(
    `/api/runs/${runId}/phases/${phase}/edit`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    },
  );
}

// PR-A4.4 phase-history modal data contract.
// Backend: backend/src/autoessay/phase_history.py.

export interface PhaseHistoryStateFlags {
  head_missing: boolean;
  prompt_dirty: boolean;
  lineage_dirty: boolean;
}

export interface PhaseHistoryUpstreamHead {
  upstream_phase: string;
  head_version_no: number | null;
  head_pv_id: string | null;
  matches_my_lineage: boolean;
}

export interface PhaseHistoryVersionLineage {
  upstream_phase: string;
  upstream_pv_id: string;
  upstream_version_no: number;
}

export interface PhaseHistoryVersionEntry {
  pv_id: string;
  version_no: number;
  source: string;
  status: string;
  created_at: string;
  is_head: boolean;
  upstream_lineage: PhaseHistoryVersionLineage[];
  has_downstream_dependents: boolean;
  dependent_summary: string | null;
  delete_blocked: boolean;
  delete_block_reason: string | null;
}

export interface PhaseHistoryEntry {
  phase: string;
  state_flags: PhaseHistoryStateFlags;
  head_pv_id: string | null;
  head_version_no: number | null;
  upstream_summary: PhaseHistoryUpstreamHead[];
  versions: PhaseHistoryVersionEntry[];
  runnable_now: boolean;
}

export interface PhaseHistoryResponse {
  run_id: string;
  branch_id: string;
  phases: PhaseHistoryEntry[];
}

export function getPhaseHistory(
  runId: string,
  options: { signal?: AbortSignal } = {},
): Promise<PhaseHistoryResponse> {
  return request<PhaseHistoryResponse>(`/api/runs/${runId}/phase-history`, {
    signal: options.signal,
  });
}

export function activateLineageMatch(
  runId: string,
  phase: string,
): Promise<RerunResponse> {
  return request<RerunResponse>(
    `/api/runs/${encodeURIComponent(runId)}/phases/${encodeURIComponent(phase)}/versions/activate-lineage-match`,
    { method: "POST" },
  );
}

export function cancelPhasePromptDrafts(
  runId: string,
  phase: string,
): Promise<void> {
  return request<void>(
    `/api/runs/${encodeURIComponent(runId)}/phases/${encodeURIComponent(phase)}/prompts/drafts`,
    { method: "DELETE" },
  );
}

export function deletePhaseVersion(
  runId: string,
  phase: string,
  pvId: string,
): Promise<void> {
  return request<void>(
    `/api/runs/${encodeURIComponent(runId)}/phases/${encodeURIComponent(phase)}/versions/${encodeURIComponent(pvId)}`,
    { method: "DELETE" },
  );
}

export interface PhasePromptResponse {
  run_id: string;
  phase: string;
  prompt_key: string;
  label: string;
  template_id: string | null;
  default_content: string;
  override_content: string | null;
  draft_hash: string | null;
  supported: boolean;
  /** All prompt_keys this phase supports overriding, sorted
   * alphabetically (Stage 3.A.4). Empty for phases that have no
   * registered surface. Lets the modal render a dropdown without a
   * separate metadata round-trip. */
  supported_keys: string[];
}

export interface PhaseVersionPromptEntry {
  prompt_key: string;
  source: "default" | "override";
  content: string;
  content_hash: string;
  template_id: string | null;
}

export function getPhasePrompt(
  runId: string,
  phase: string,
  promptKey?: string,
): Promise<PhasePromptResponse> {
  // Stage 3.A.4: when ``promptKey`` is undefined we OMIT the query
  // parameter entirely. The backend then runs its discovery
  // fallback (e.g. for curator, where ``main`` is unsupported but
  // ``ranking`` exists). Sending ``?prompt_key=main`` here would
  // bypass that fallback and 404.
  const suffix =
    promptKey === undefined
      ? ""
      : `?prompt_key=${encodeURIComponent(promptKey)}`;
  return request<PhasePromptResponse>(
    `/api/runs/${runId}/phases/${phase}/prompt${suffix}`,
  );
}

export function upsertPhasePrompt(
  runId: string,
  phase: string,
  content: string | null,
  promptKey: string = "main",
): Promise<PhasePromptResponse> {
  return request<PhasePromptResponse>(
    `/api/runs/${runId}/phases/${phase}/prompt`,
    {
      method: "PUT",
      body: JSON.stringify({ content, prompt_key: promptKey }),
    },
  );
}

export function listPhaseVersionPrompts(
  runId: string,
  phase: string,
  pvId: string,
): Promise<PhaseVersionPromptEntry[]> {
  return request<PhaseVersionPromptEntry[]>(
    `/api/runs/${runId}/phases/${phase}/versions/${pvId}/prompts`,
  );
}

export interface DiffVersionInfo {
  id: string;
  version_no: number;
  status: string;
  prompt_hash: string | null;
  input_snapshot_hash: string | null;
  created_on_branch_id: string | null;
  created_at: string | null;
}

export interface DiffContext {
  same_upstream_inputs: boolean;
  prompt_hash_changed: boolean;
}

export interface DiffSummary {
  files_added: number;
  files_removed: number;
  files_changed: number;
  files_unchanged: number;
}

export interface FileDiffEntry {
  logical_path: string;
  file_status: "added" | "removed" | "changed" | "unchanged";
  diff_type:
    | "text_unified"
    | "jsonl_records"
    | "json_structural"
    | "binary"
    | "unchanged";
  body: Record<string, unknown>;
  match_basis?: string | null;
}

export interface DiffResponse {
  run_id: string;
  phase: string;
  from_version: DiffVersionInfo;
  to_version: DiffVersionInfo;
  context: DiffContext;
  summary: DiffSummary;
  files: FileDiffEntry[];
}

export function diffPhaseVersions(
  runId: string,
  phase: string,
  toPvId: string,
  againstPvId?: string,
): Promise<DiffResponse> {
  const url = againstPvId
    ? `/api/runs/${runId}/phases/${phase}/versions/${toPvId}/diff?against=${encodeURIComponent(againstPvId)}`
    : `/api/runs/${runId}/phases/${phase}/versions/${toPvId}/diff`;
  return request<DiffResponse>(url);
}

export interface PhaseVersionEntry {
  id: string;
  version_no: number;
  status: string;
  parent_pv_id: string | null;
  is_active: boolean;
  input_snapshot_hash: string | null;
  created_at: string;
  completed_at: string | null;
  artifact_count: number;
  /** Origin of this version. ``"agent"`` for the canonical
   * agent-run output (the default for every version produced
   * before PR-A1 landed), ``"user_edit"`` for inline-edited
   * versions written by PR-A2's PUT endpoints. The phase-history
   * modal labels non-agent sources distinctly. */
  source?: string;
}

export interface PhaseVersionsResponse {
  run_id: string;
  phase: string;
  active_version_id: string | null;
  versions: PhaseVersionEntry[];
  /** Stage 3.E: true iff the phase produced output at least once
   * on the active branch (covers both versioned reruns and the
   * initial vanilla run that does not create a phase_version row).
   * Used to gate the "Rerun phase" / "Edit prompt and rerun" UI. */
  has_completed_output: boolean;
}

export function listPhaseVersions(
  runId: string,
  phase: string,
): Promise<PhaseVersionsResponse> {
  return request<PhaseVersionsResponse>(
    `/api/runs/${runId}/phases/${phase}/versions`,
  );
}

export function activatePhaseVersion(
  runId: string,
  phase: string,
  pvId: string,
): Promise<RerunResponse> {
  return request<RerunResponse>(
    `/api/runs/${runId}/phases/${phase}/versions/${pvId}/activate`,
    { method: "POST" },
  );
}

export interface ScoutJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface ProposalJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface ProposalContent {
  research_question: string;
  significance: string;
  preliminary_approach: string;
  expected_contribution: string;
  scope: string;
  preliminary_keywords: string[];
}

export interface ProposalBundle {
  run_id: string;
  version: number;
  proposal_json: ProposalContent;
  markdown: string;
  path: string;
}

export interface CuratorJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface SynthesizerJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface IdeatorJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface DrafterJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface StylistJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface CriticJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface IntegrityJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export interface ExportsJob {
  run_id: string;
  job_id: string;
  expected_state: string;
}

export type ResearchRole =
  | "primary_source"
  | "secondary_argument"
  | "theoretical_lens"
  | "methodological_reference";

export interface DiscoverySource {
  source_id: string;
  title: string;
  authors: string[];
  year: number | null;
  venue: string | null;
  doi: string | null;
  url: string | null;
  pdf_url: string | null;
  abstract: string | null;
  source_client: string;
  access_status: string;
  license: string | null;
  rank_score: number;
  risk_flags: string[];
  // PR-C1.a: server-attached classification tier. Defaults to
  // ``secondary_argument`` for legacy runs (alembic 019 backfill).
  research_role?: ResearchRole;
}

export interface Discovery {
  run_id: string;
  skim_candidates: DiscoverySource[];
  scout_report: string;
}

export interface FulltextManifestEntry {
  pdf_path: string;
  sha256: string;
  size_bytes: number;
  fetched_at: string;
  license: string | null;
}

export interface ManualUploadRequest {
  source_id: string;
  title: string;
  doi: string | null;
  url: string | null;
  suggested_location: string;
  reason: string;
}

export interface SourcesBundle {
  run_id: string;
  shortlist: DiscoverySource[];
  fulltext_manifest: Record<string, FulltextManifestEntry>;
  manual_upload_requests: ManualUploadRequest[];
  curation_report: string;
  skim_candidates: DiscoverySource[];
  source_quality_counts?: {
    off_topic_dropped?: number;
    verification_rejected?: number;
    runner_up?: number;
    weak_anchor?: number;
  };
}

export interface SourceUpload {
  run_id: string;
  source_id: string;
  manifest_entry: FulltextManifestEntry;
  shortlist_entry: DiscoverySource;
}

export interface SynthesisClaim {
  source_id: string;
  claim_id: string;
  text: string;
  claim_type: string;
  n_sources_supporting: number | null;
  page_anchor: string | null;
}

export interface MaterialDiagnostic {
  sufficient: boolean;
  candidate_titles: string[];
  missing_materials: string[];
  risks: string[];
  recommended_action: "proceed" | "iterate" | "incomplete";
  rationale: string;
}

export interface DualTrackPayload {
  schema_version: number;
  primary_track: SynthesisClaim[];
  secondary_track: SynthesisClaim[];
  theoretical_lens_track: SynthesisClaim[];
  methodological_track: SynthesisClaim[];
  tension_summary_ref: string | null;
  framework_lens_summary_ref: string | null;
}

export interface SynthesisBundle {
  run_id: string;
  claims: SynthesisClaim[];
  source_notes: Record<string, unknown>;
  synthesizer_report: string;
  material_diagnostic: MaterialDiagnostic | null;
  material_diagnostic_md: string;
  // PR-C1.b: ``null`` for legacy runs that completed synthesizer
  // before C1.a (no synthesizer.json artifact). UI fallback is
  // the existing flat ``claims`` list.
  dual_track: DualTrackPayload | null;
}

export interface EvidenceLedgerEntry {
  source_id: string;
  claim_id: string;
  claim_text: string;
  citation_target: string;
  confidence: number;
  extra: Record<string, unknown>;
  override_action: "attribute_to_user" | "cite_normally" | null;
  override_recorded_at: string | null;
  override_user: string | null;
}

export interface EvidenceLedgerResponse {
  run_id: string;
  artifact_present: boolean;
  entries: EvidenceLedgerEntry[];
}

export interface ResearchRoleUpdateResponse {
  source_id: string;
  research_role: ResearchRole;
  synthesis_marked_stale: boolean;
}

export interface AngleCard {
  angle_id: string;
  working_title: string;
  thesis_one_sentence: string;
  key_claim_ids: string[];
  why_novel: string;
  evidence_so_far: string;
  missing_evidence: string;
  journal_fit_note: string;
  risks: string[];
  // PR-C2.b Tier 4 (2026-05-03): optional structured fields. Both
  // default to empty arrays/strings on the backend so legacy
  // angle_cards.json payloads remain compatible. NoveltySubview
  // can render these as chips when present.
  framework_lens?: string[];
  methodological_choice?: string;
}

export interface OutlineSection {
  section_id: string;
  title: string;
  function: string;
  argument: string;
  literature: string;
  materials: string;
  relation_to_thesis: string;
  weakness: string;
}

export interface AngleOutline {
  angle_id: string;
  working_title: string;
  sections: OutlineSection[];
}

export interface NoveltyBundle {
  run_id: string;
  angle_cards: AngleCard[];
  ideator_report: string;
  selected_thesis: AngleCard | null;
  detailed_outlines: AngleOutline[];
  detailed_outlines_md: string;
}

export interface NoveltyDiscussionMessage {
  id: string;
  run_id: string;
  role: "user" | "assistant";
  content: string;
  generation_token: number;
  created_at: string;
}

export interface NoveltyDiscussResponse {
  run_id: string;
  angle_cards: AngleCard[];
  user_message: NoveltyDiscussionMessage;
  assistant_message: NoveltyDiscussionMessage;
}

export interface DraftMetadata {
  version: string;
  created_at?: string;
  sections?: number;
  failed_sections?: number;
  uncited_claims?: number;
  cited_sources?: string[];
  manuscript_path?: string;
  claim_map_path?: string;
  citations_path?: string;
  rationale_path?: string;
}

export interface DraftListBundle {
  run_id: string;
  drafts: DraftMetadata[];
}

export interface DraftClaim {
  draft_version: string;
  section_id: string;
  section_title: string;
  paragraph_id: string;
  claim_text: string;
  source_ids: string[];
  uncited: boolean;
}

export interface DraftBundle {
  run_id: string;
  version: string;
  metadata: DraftMetadata;
  manuscript: string;
  claim_map: DraftClaim[];
  citations_bib: string;
  draft_rationale: string;
}

export interface StopSlopScore {
  initial?: ScoreSnapshot;
  final?: ScoreSnapshot;
  threshold?: number;
  repolish_attempted?: boolean;
  dimension_deltas?: Record<string, number>;
}

export interface ScoreSnapshot {
  dimensions?: Record<string, number>;
  total?: number;
  findings?: Array<Record<string, unknown>>;
}

export interface StyleBundle {
  run_id: string;
  version: string;
  paper_styled: string;
  style_delta: string;
  stop_slop_score: StopSlopScore;
  n_gram_violations: unknown[] | null;
}

export interface CriticIssue {
  issue_id: string;
  severity: "BLOCKER" | "HIGH" | "MEDIUM" | "LOW";
  dimension: "thesis" | "structure" | "evidence" | "prose";
  paragraph_id: string | null;
  source_ids: string[];
  description: string;
  suggested_action: string;
}

export interface ClaimAuditRow {
  claim_index: number;
  paragraph_id: string;
  claim_text: string;
  source_ids: string[];
  status: string;
  failures: Array<Record<string, unknown>>;
}

export interface CriticBundle {
  run_id: string;
  critic_report: string;
  claim_audit: ClaimAuditRow[];
  revision_plan: string;
  blocking_issues: {
    issues?: CriticIssue[];
  };
}

export interface IntegritySummary {
  draft_version?: string;
  scans?: Record<
    string,
    {
      vendor?: string;
      scan_id?: string;
      score?: number | null;
      status?: string;
      span_count?: number;
      spans?: Array<{
        span_id: string;
        start: number;
        end: number;
        label: string;
        confidence?: number | null;
        source_url?: string | null;
        text?: string | null;
      }>;
      raw_report_path?: string | null;
    }
  >;
  span_counts?: Record<string, number>;
  scanned_at?: string;
  mode?: string;
}

export interface IntegrityBundle {
  run_id: string;
  plagiarism_report: string;
  ai_style_report: string;
  integrity_summary: IntegritySummary;
}

export interface ExportFileLink {
  format: string;
  filename: string;
  url: string;
  // PR-371: title-slug-derived name to surface in the UI + use as
  // the suggested download filename. Server still serves the file
  // under the on-disk ``filename`` (URL is unchanged); the browser
  // saves it under ``download_filename`` via Content-Disposition.
  // Older backends omit this field, so it stays optional.
  download_filename?: string;
}

export interface ExportsBundle {
  run_id: string;
  manifest: Record<string, unknown>;
  files: ExportFileLink[];
}

export interface CorpusDocument {
  id: string;
  title: string;
  document_type: string;
  ingest_status: string;
  original_size_bytes: number | null;
  created_at: string;
}

export interface CorpusUploadResponse {
  document: CorpusDocument;
  task_id: string;
}

export interface CorpusStyleProfileRebuildResponse {
  task_id: string;
}

export interface StyleProfileSummary {
  paragraph_length_distribution?: {
    mean?: number;
    p25?: number;
    p75?: number;
  };
  sentence_length_distribution?: {
    mean?: number;
    p25?: number;
    p75?: number;
  };
  opener_patterns?: string[];
  hedging_patterns?: string[];
  taboo_phrases?: string[];
  common_domain_terms?: string[];
  short_local_examples?: string[];
  /** PR-B2 diagnostics. Populated whenever the backend ran
   * ``build_style_profile_from_texts``; absent for pre-PR-B2
   * cached profiles. */
  detected_language?: string;
  document_count?: number;
  total_token_count?: number;
  empty_section_warnings?: string[];
}

/** Error thrown by :func:`request` for any 4xx/5xx response.
 *
 * Carries the HTTP status and the parsed JSON body so callers can key
 * off backend error codes (e.g. ``body?.code === "essay_limit"``) to
 * render banner-style messages without scraping ``.message``.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown, message?: string) {
    super(message ?? `Request failed: ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

// PR-385: surface structured FastAPI error bodies as readable strings.
// Previously ``String(body.detail)`` rendered ``[object Object]`` whenever
// a handler raised ``HTTPException(detail={...})`` (e.g. safety gate
// fail-closed) or pydantic returned a 422 validation array.
export function renderApiErrorDetail(body: unknown, status: number): string {
  if (typeof body === "string" && body.trim()) return body;
  if (!body || typeof body !== "object") {
    return `Request failed: ${status}`;
  }
  const detail = (body as { detail?: unknown }).detail;
  const rendered = stringifyDetail(detail);
  if (rendered) return rendered;
  for (const key of ["error", "message", "reason"] as const) {
    const value = (body as Record<string, unknown>)[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return `Request failed: ${status}`;
}

function stringifyDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts: string[] = [];
    for (const item of detail) {
      if (typeof item === "string") {
        parts.push(item);
        continue;
      }
      if (item && typeof item === "object") {
        const obj = item as Record<string, unknown>;
        const msg = typeof obj.msg === "string" ? obj.msg : null;
        const loc = Array.isArray(obj.loc)
          ? (obj.loc as unknown[]).join(".")
          : "";
        if (msg && loc) parts.push(`${loc}: ${msg}`);
        else if (msg) parts.push(msg);
      }
    }
    if (parts.length) return parts.join("; ");
  }
  if (detail && typeof detail === "object") {
    const obj = detail as Record<string, unknown>;
    for (const key of [
      "user_facing_reason",
      "message",
      "error",
      "reason",
    ] as const) {
      const value = obj[key];
      if (typeof value === "string" && value.trim()) return value;
    }
    try {
      return JSON.stringify(detail);
    } catch {
      return "";
    }
  }
  return "";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!(init?.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    headers,
  });
  if (!response.ok) {
    let body: unknown = null;
    const ct = response.headers.get("Content-Type") ?? "";
    if (ct.includes("application/json")) {
      try {
        body = await response.json();
      } catch {
        body = null;
      }
    } else {
      try {
        body = await response.text();
      } catch {
        body = null;
      }
    }
    const detail = renderApiErrorDetail(body, response.status);
    throw new ApiError(response.status, body, detail);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function createProject(payload: CreateProjectRequest): Promise<Project> {
  return request<Project>("/api/projects", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listDomains(): Promise<Domain[]> {
  return request<Domain[]>("/api/domains");
}

export function getProject(projectId: string): Promise<Project> {
  return request<Project>(`/api/projects/${projectId}`);
}

export interface ProjectPatchPayload {
  language?: ProjectLanguage;
  target_journal?: string | null;
  // PR-A3: project title editable from the workspace.
  title?: string;
}

export function patchProject(
  projectId: string,
  payload: ProjectPatchPayload,
): Promise<Project> {
  return request<Project>(`/api/projects/${projectId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export interface CreateRunOptions {
  mode?: GenerationMode;
  // PR-366: opt-in to the "数理增强模式" holistic round-0 at run-create
  // time. Omitted / false → cheap default path.
  mathematical_mode?: boolean;
  // PR-382: opt-in to one-click auto-pilot.
  auto_advance?: boolean;
}

export function createRun(
  projectId: string,
  options: CreateRunOptions = {},
): Promise<Run> {
  const body: Record<string, unknown> = {};
  if (options.mode !== undefined) {
    body.mode = options.mode;
  }
  if (options.mathematical_mode !== undefined) {
    body.mathematical_mode = Boolean(options.mathematical_mode);
  }
  if (options.auto_advance !== undefined) {
    body.auto_advance = Boolean(options.auto_advance);
  }
  return request<Run>(`/api/projects/${projectId}/runs`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface UpdateRunSettingsPayload {
  mode?: GenerationMode;
  mathematical_mode?: boolean;
  // PR-382: flipping this on triggers the backend coordinator
  // immediately, so users who started in manual mode and want to
  // walk away can just toggle and leave.
  auto_advance?: boolean;
}

export interface GenerationModeOption {
  id: GenerationMode;
  label: string;
}

export interface GenerationModesResponse {
  default_mode: GenerationMode;
  modes: GenerationModeOption[];
}

export function getGenerationModes(): Promise<GenerationModesResponse> {
  return request<GenerationModesResponse>("/api/generation_modes");
}

export interface ExpressTransparency {
  run_id: string;
  mode: "express";
  state: string;
  provider: string | null;
  provider_model: string | null;
  token_cap: number | null;
  token_usage: Record<string, unknown>;
  prompt_summary: Record<string, unknown>;
  prompt_excerpt: string | null;
  provenance: Record<string, unknown>;
  audit_summary: Record<string, unknown>;
  outline: Array<{ level?: number; title?: string; line?: number }>;
  manuscript_preview: string | null;
  failure: Record<string, unknown> | null;
}

export function getExpressTransparency(
  runId: string,
): Promise<ExpressTransparency> {
  return request<ExpressTransparency>(
    `/api/runs/${encodeURIComponent(runId)}/express_transparency`,
  );
}

export function updateRunSettings(
  runId: string,
  payload: UpdateRunSettingsPayload,
): Promise<Run> {
  return request<Run>(`/api/runs/${runId}/settings`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function transitionRun(
  runId: string,
  toState: string,
  reason: string,
): Promise<Run> {
  return request<Run>(`/api/runs/${runId}/transitions`, {
    method: "POST",
    body: JSON.stringify({ to_state: toState, reason }),
  });
}

export function clearPhaseLock(runId: string): Promise<Run> {
  return request<Run>(`/api/runs/${runId}/clear-phase-lock`, {
    method: "POST",
  });
}

export function forceApproveRun(runId: string, reason: string): Promise<Run> {
  return request<Run>(`/api/runs/${runId}/force-approve`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function listRuns(
  options: { q?: string; includeDeleted?: boolean } = {},
): Promise<Run[]> {
  const params = new URLSearchParams();
  if (options.q && options.q.trim()) params.set("q", options.q.trim());
  if (options.includeDeleted) params.set("include_deleted", "1");
  const qs = params.toString();
  return request<Run[]>(`/api/runs${qs ? `?${qs}` : ""}`);
}

export function listProjects(
  options: { q?: string; includeDeleted?: boolean } = {},
): Promise<Project[]> {
  const params = new URLSearchParams();
  if (options.q && options.q.trim()) params.set("q", options.q.trim());
  if (options.includeDeleted) params.set("include_deleted", "1");
  const qs = params.toString();
  return request<Project[]>(`/api/projects${qs ? `?${qs}` : ""}`);
}

export async function deleteProject(projectId: string): Promise<void> {
  const response = await fetch(`/api/projects/${projectId}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`delete project ${projectId} failed: ${response.status}`);
  }
}

export async function deleteRun(runId: string): Promise<void> {
  const response = await fetch(`/api/runs/${runId}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`delete run ${runId} failed: ${response.status}`);
  }
}

// PR-389: permanent (hard) delete — only valid after a soft-delete.
// Backend returns 409 if eligibility fails (not deleted, or active
// phase lock); pass through as ApiError so the UI can render the
// detail via ``renderApiErrorDetail``.
export async function hardDeleteRun(runId: string): Promise<void> {
  const response = await fetch(`/api/runs/${runId}/hard`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new ApiError(
      response.status,
      body,
      body || `hard delete run failed: ${response.status}`,
    );
  }
}

export async function hardDeleteProject(projectId: string): Promise<void> {
  const response = await fetch(`/api/projects/${projectId}/hard`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new ApiError(
      response.status,
      body,
      body || `hard delete project failed: ${response.status}`,
    );
  }
}

export function restoreRun(runId: string): Promise<Run> {
  return request<Run>(`/api/runs/${runId}/restore`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function restoreProject(projectId: string): Promise<Project> {
  return request<Project>(`/api/projects/${projectId}/restore`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// ---------------------------------------------------------------------------
// Authors
// ---------------------------------------------------------------------------

export interface Author {
  id: string;
  display_name: string;
  affiliation: string | null;
  email: string | null;
  orcid: string | null;
  is_self: boolean;
  deleted_at?: string | null;
}

export interface AuthorPayload {
  display_name: string;
  affiliation?: string | null;
  email?: string | null;
  orcid?: string | null;
}

export function listAuthors(includeDeleted = false): Promise<Author[]> {
  const qs = includeDeleted ? "?include_deleted=1" : "";
  return request<Author[]>(`/api/authors${qs}`);
}

export function createAuthor(payload: AuthorPayload): Promise<Author> {
  return request<Author>("/api/authors", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function patchAuthor(
  authorId: string,
  payload: Partial<AuthorPayload>,
): Promise<Author> {
  return request<Author>(`/api/authors/${authorId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteAuthor(authorId: string): Promise<void> {
  const response = await fetch(`/api/authors/${authorId}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!response.ok && response.status !== 204) {
    throw new Error(`delete author ${authorId} failed: ${response.status}`);
  }
}

export interface ProjectAuthorEntry {
  author_id: string;
  position: number;
  display_name: string;
  affiliation: string | null;
  email: string | null;
  orcid: string | null;
  deleted: boolean;
}

export interface ProjectAuthorsBundle {
  project_id: string;
  authors: ProjectAuthorEntry[];
}

export function getProjectAuthors(
  projectId: string,
): Promise<ProjectAuthorsBundle> {
  return request<ProjectAuthorsBundle>(`/api/projects/${projectId}/authors`);
}

export function setProjectAuthors(
  projectId: string,
  authors: { author_id: string; position: number }[],
): Promise<ProjectAuthorsBundle> {
  return request<ProjectAuthorsBundle>(`/api/projects/${projectId}/authors`, {
    method: "PUT",
    body: JSON.stringify({ authors }),
  });
}

export function getRun(runId: string): Promise<Run> {
  return request<Run>(`/api/runs/${runId}`);
}

export function startProposal(
  runId: string,
  userDraft?: string,
): Promise<ProposalJob> {
  return request<ProposalJob>(`/api/runs/${runId}/proposal`, {
    method: "POST",
    body: JSON.stringify({ user_draft: userDraft || null }),
  });
}

export function getProposal(runId: string): Promise<ProposalBundle> {
  return request<ProposalBundle>(`/api/runs/${runId}/proposal`);
}

export function saveProposal(
  runId: string,
  proposal: ProposalContent,
  options: {
    // PR-A3: optional concurrency token + replace/new mode.
    base_version?: number;
    mode?: "new" | "replace";
  } = {},
): Promise<ProposalBundle> {
  const body: Record<string, unknown> = { proposal_json: proposal };
  if (options.base_version !== undefined) {
    body.base_version = options.base_version;
  }
  if (options.mode !== undefined) {
    body.mode = options.mode;
  }
  return request<ProposalBundle>(`/api/runs/${runId}/proposal`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function acceptProposal(runId: string): Promise<unknown> {
  return request<unknown>(
    `/api/runs/${runId}/checkpoints/USER_PROPOSAL_REVIEW`,
    {
      method: "POST",
      body: JSON.stringify({ accept: true }),
    },
  );
}

export function startScout(runId: string): Promise<ScoutJob> {
  return request<ScoutJob>(`/api/runs/${runId}/scout`, {
    method: "POST",
  });
}

export function startCurator(runId: string): Promise<CuratorJob> {
  return request<CuratorJob>(`/api/runs/${runId}/curator`, {
    method: "POST",
  });
}

export function startSynthesizer(runId: string): Promise<SynthesizerJob> {
  return request<SynthesizerJob>(`/api/runs/${runId}/synthesizer`, {
    method: "POST",
  });
}

export type SourceReviewCheckpointType =
  | "USER_SEARCH_REVIEW"
  | "USER_DEEP_DIVE_REVIEW";

export interface SourceReviewCheckpointPayload {
  source_ids: string[];
  approved_source_ids: string[];
  rejected_source_ids: string[];
  pinned_source_ids: string[];
  review_scope: "search_review" | "deep_dive_review";
  reviewed_at_client: string;
}

export function saveSourceReviewCheckpoint(
  runId: string,
  checkpointType: SourceReviewCheckpointType,
  payload: SourceReviewCheckpointPayload,
): Promise<unknown> {
  return request<unknown>(`/api/runs/${runId}/checkpoints/${checkpointType}`, {
    method: "POST",
    body: JSON.stringify({
      status: "ACCEPTED",
      decision_payload: payload,
    }),
  });
}

// PR-I3: user-triggered escape hatch for stuck *_RUNNING runs
// (worker SIGKILL during LLM call → run state stuck in *_RUNNING
// forever). Same compound gate as the reaper background sweep —
// the only difference is who pulls the lever. Returns the updated
// Run on success (state moved to FAILED_FIXABLE so
// FailureResolutionBanner takes over). 409 with discriminator body
// when the gate refuses (worker still alive / phase finished /
// state mismatch). 404 for unknown phase or unknown run.
export function recoverStuckPhase(runId: string, phase: string): Promise<Run> {
  return request<Run>(
    `/api/runs/${runId}/phases/${encodeURIComponent(phase)}/recover`,
    { method: "POST" },
  );
}

// PR-I5: backend retry resolver. Replaces PR-I4.a's frontend
// smartRetry static heuristic with a single backend endpoint that
// has full server-side context (has_completed_output, latest event
// payload, lock state). Decision tree codified in `main.py::
// retry_failed_phase` per 2-round codex consensus. Returns the
// chosen action (start/rerun) so the UI can label it; 4xx error
// shapes documented in CHANGELOG.
export interface RetryResponse {
  run_id: string;
  phase: string;
  action: "start" | "rerun";
  expected_state: string;
  job_id: string | null;
}

export function retryFailedPhaseEndpoint(
  runId: string,
  phase: string,
): Promise<RetryResponse> {
  return request<RetryResponse>(
    `/api/runs/${runId}/phases/${encodeURIComponent(phase)}/retry`,
    { method: "POST" },
  );
}

export function startIdeator(runId: string): Promise<IdeatorJob> {
  return request<IdeatorJob>(`/api/runs/${runId}/ideator`, {
    method: "POST",
  });
}

// PR-C2.b: framework_lens start endpoint. Same response shape as
// IdeatorJob (run_id / job_id / expected_state).
export function startFrameworkLens(runId: string): Promise<IdeatorJob> {
  return request<IdeatorJob>(`/api/runs/${runId}/framework_lens`, {
    method: "POST",
  });
}

// PR-C2.b Tier 4: framework_lens artifact GET endpoint. Lens tab
// renders signals (lens_name + key_concepts + applicability_to_kernel)
// + source_id from this payload.
export interface FrameworkLensSignal {
  lens_name: string;
  key_concepts: string[];
  source_id: string;
  applicability_to_kernel: string;
}

export interface FrameworkLensBundle {
  run_id: string;
  artifact_present: boolean;
  schema_version: number | null;
  synthesizer_input_ref: {
    synthesizer_pv_id: string | null;
    synthesizer_artifact_hash: string | null;
  } | null;
  signals: FrameworkLensSignal[];
}

export function getFrameworkLens(runId: string): Promise<FrameworkLensBundle> {
  return request<FrameworkLensBundle>(`/api/runs/${runId}/framework_lens`);
}

export function startDrafter(runId: string): Promise<DrafterJob> {
  return request<DrafterJob>(`/api/runs/${runId}/drafter`, {
    method: "POST",
  });
}

export function startStylist(runId: string): Promise<StylistJob> {
  return request<StylistJob>(`/api/runs/${runId}/stylist`, {
    method: "POST",
  });
}

export function startCritic(runId: string): Promise<CriticJob> {
  return request<CriticJob>(`/api/runs/${runId}/critic`, {
    method: "POST",
  });
}

export function startIntegrity(runId: string): Promise<IntegrityJob> {
  return request<IntegrityJob>(`/api/runs/${runId}/integrity`, {
    method: "POST",
  });
}

export function startExports(runId: string): Promise<ExportsJob> {
  return request<ExportsJob>(`/api/runs/${runId}/export`, {
    method: "POST",
  });
}

export function getDiscovery(runId: string): Promise<Discovery> {
  return request<Discovery>(`/api/runs/${runId}/discovery`);
}

export function getSources(runId: string): Promise<SourcesBundle> {
  return request<SourcesBundle>(`/api/runs/${runId}/sources`);
}

export function getSynthesis(runId: string): Promise<SynthesisBundle> {
  return request<SynthesisBundle>(`/api/runs/${runId}/synthesis`);
}

// PR-C1.b: research_role override + evidence-ledger surfaces.

export function updateResearchRole(
  runId: string,
  sourceId: string,
  researchRole: ResearchRole,
): Promise<ResearchRoleUpdateResponse> {
  return request<ResearchRoleUpdateResponse>(
    `/api/runs/${encodeURIComponent(runId)}/sources/${encodeURIComponent(sourceId)}/research_role`,
    {
      method: "PUT",
      body: JSON.stringify({ research_role: researchRole }),
    },
  );
}

export function getEvidenceLedger(
  runId: string,
): Promise<EvidenceLedgerResponse> {
  return request<EvidenceLedgerResponse>(
    `/api/runs/${encodeURIComponent(runId)}/evidence_ledger`,
  );
}

export function appendEvidenceLedgerOverride(
  runId: string,
  body: {
    source_id: string;
    claim_id: string | null;
    action: "attribute_to_user" | "cite_normally";
    user?: string;
  },
): Promise<{
  appended: boolean;
  source_id: string;
  claim_id: string | null;
  action: string;
  recorded_at: string;
}> {
  return request(
    `/api/runs/${encodeURIComponent(runId)}/evidence_ledger/overrides`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export function getNovelty(runId: string): Promise<NoveltyBundle> {
  return request<NoveltyBundle>(`/api/runs/${runId}/novelty`);
}

export function getNoveltyDiscussion(
  runId: string,
): Promise<NoveltyDiscussionMessage[]> {
  return request<NoveltyDiscussionMessage[]>(
    `/api/runs/${runId}/novelty/discussion`,
  );
}

export function discussNovelty(
  runId: string,
  userMessage: string,
): Promise<NoveltyDiscussResponse> {
  return request<NoveltyDiscussResponse>(`/api/runs/${runId}/novelty/discuss`, {
    method: "POST",
    body: JSON.stringify({ user_message: userMessage }),
  });
}

export function selectNoveltyAngle(
  runId: string,
  selectedAngleId: string,
  edits?: Record<string, unknown>,
): Promise<unknown> {
  return request<unknown>(
    `/api/runs/${runId}/checkpoints/USER_NOVELTY_REVIEW`,
    {
      method: "POST",
      body: JSON.stringify({
        selected_angle_id: selectedAngleId,
        edits: edits ?? {},
      }),
    },
  );
}

export function getDrafts(runId: string): Promise<DraftListBundle> {
  return request<DraftListBundle>(`/api/runs/${runId}/drafts`);
}

export function getDraft(runId: string, version: string): Promise<DraftBundle> {
  return request<DraftBundle>(`/api/runs/${runId}/drafts/${version}`);
}

export function getStyle(runId: string): Promise<StyleBundle> {
  return request<StyleBundle>(`/api/runs/${runId}/style`);
}

export function getStyleScore(runId: string): Promise<StopSlopScore> {
  return request<StopSlopScore>(`/api/runs/${runId}/style/score`);
}

export function getCritic(runId: string): Promise<CriticBundle> {
  return request<CriticBundle>(`/api/runs/${runId}/critic`);
}

export function getIntegrity(runId: string): Promise<IntegrityBundle> {
  return request<IntegrityBundle>(`/api/runs/${runId}/integrity`);
}

export function getExports(runId: string): Promise<ExportsBundle> {
  return request<ExportsBundle>(`/api/runs/${runId}/exports`);
}

export function approveExternalScan(
  runId: string,
  scanKinds: Array<"plagiarism" | "ai_style">,
): Promise<unknown> {
  return request<unknown>(
    `/api/runs/${runId}/checkpoints/USER_EXTERNAL_SCAN_APPROVAL`,
    {
      method: "POST",
      body: JSON.stringify({
        approve: true,
        scan_kinds: scanKinds,
      }),
    },
  );
}

export function skipExternalScan(
  runId: string,
  skipReason: string,
): Promise<unknown> {
  return request<unknown>(
    `/api/runs/${runId}/checkpoints/USER_EXTERNAL_SCAN_APPROVAL`,
    {
      method: "POST",
      body: JSON.stringify({
        approve: false,
        skip_reason: skipReason,
      }),
    },
  );
}

export function acceptIntegrity(
  runId: string,
  spanDecisions: Array<Record<string, unknown>> = [],
): Promise<unknown> {
  return request<unknown>(
    `/api/runs/${runId}/checkpoints/USER_INTEGRITY_REVIEW`,
    {
      method: "POST",
      body: JSON.stringify({
        accept: true,
        span_decisions: spanDecisions,
      }),
    },
  );
}

export function requestIntegrityRevision(
  runId: string,
  spanDecisions: Array<Record<string, unknown>>,
  nextRevisionDimension: string,
): Promise<unknown> {
  return request<unknown>(
    `/api/runs/${runId}/checkpoints/USER_INTEGRITY_REVIEW`,
    {
      method: "POST",
      body: JSON.stringify({
        accept: false,
        span_decisions: spanDecisions,
        next_revision_dimension: nextRevisionDimension,
      }),
    },
  );
}

export function acceptFinalDraft(
  runId: string,
  exportFormats: string[],
): Promise<unknown> {
  return request<unknown>(
    `/api/runs/${runId}/checkpoints/USER_FINAL_ACCEPTANCE`,
    {
      method: "POST",
      body: JSON.stringify({
        accept: true,
        export_formats: exportFormats,
      }),
    },
  );
}

export function uploadSourcePdf(
  runId: string,
  formData: FormData,
): Promise<SourceUpload> {
  return request<SourceUpload>(`/api/runs/${runId}/sources/upload`, {
    method: "POST",
    body: formData,
  });
}

export function listCorpusDocuments(): Promise<CorpusDocument[]> {
  return request<CorpusDocument[]>("/api/corpus");
}

export function uploadCorpusDocument(
  formData: FormData,
): Promise<CorpusUploadResponse> {
  return request<CorpusUploadResponse>("/api/corpus/upload", {
    method: "POST",
    body: formData,
  });
}

export function deleteCorpusDocument(documentId: string): Promise<void> {
  return request<void>(`/api/corpus/${documentId}`, {
    method: "DELETE",
  });
}

export function rebuildCorpusStyleProfile(): Promise<CorpusStyleProfileRebuildResponse> {
  return request<CorpusStyleProfileRebuildResponse>(
    "/api/corpus/style-profile/rebuild",
    {
      method: "POST",
    },
  );
}

export function getCorpusStyleProfile(): Promise<StyleProfileSummary> {
  return request<StyleProfileSummary>("/api/corpus/style-profile");
}

// PR-B3: per-project corpus surfaced inside the workspace tab.

export interface ProjectCorpusDocumentEntry {
  id: string;
  title: string;
  document_type: string;
  ingest_status: string;
  original_size_bytes: number | null;
  created_at: string;
}

export interface ProjectCorpusEntry {
  id: string;
  name: string;
  is_global: boolean;
  is_selected: boolean;
  document_count: number;
}

export interface ProjectCorpusResponse {
  project_id: string;
  project_corpus_id: string | null;
  project_documents: ProjectCorpusDocumentEntry[];
  global_corpora: ProjectCorpusEntry[];
}

export interface ProjectCorpusSelectionResponse {
  project_id: string;
  selected_global_corpus_ids: string[];
}

export interface ProjectCorpusUploadResponse {
  document: ProjectCorpusDocumentEntry;
  task_id: string;
}

export function getProjectCorpus(
  projectId: string,
): Promise<ProjectCorpusResponse> {
  return request<ProjectCorpusResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/corpus`,
  );
}

export function setProjectCorpusSelection(
  projectId: string,
  globalCorpusIds: string[],
): Promise<ProjectCorpusSelectionResponse> {
  return request<ProjectCorpusSelectionResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/corpus/selection`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ global_corpus_ids: globalCorpusIds }),
    },
  );
}

export function uploadProjectCorpusDocument(
  projectId: string,
  formData: FormData,
): Promise<ProjectCorpusUploadResponse> {
  return request<ProjectCorpusUploadResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/corpus/upload`,
    {
      method: "POST",
      body: formData,
    },
  );
}

// ===========================================================================
// PR-C0 / C0.b1 / C0.b2 — paper modes registry + research-kernel edit
// ===========================================================================

import type {
  KernelSuggestionFields,
  PaperModesResponse,
  ResearchKernel,
} from "./kernelValidation";

export type {
  KernelSuggestionFields,
  PaperModesResponse,
  PaperModeSpec,
  ResearchKernel,
} from "./kernelValidation";

export function getPaperModes(): Promise<PaperModesResponse> {
  return request<PaperModesResponse>("/api/paper_modes");
}

export interface KernelSuggestBody {
  title: string;
  domain_id: string;
  language: ProjectLanguage;
}

export interface KernelSuggestResponse {
  suggestion: KernelSuggestionFields;
  model: string;
  max_tokens: number;
}

export function suggestResearchKernel(
  body: KernelSuggestBody,
): Promise<KernelSuggestResponse> {
  return request<KernelSuggestResponse>("/api/runs/kernel_suggest", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface ResearchKernelEditResponse {
  paper_mode: string;
  kernel: Record<string, unknown>;
  proposal_version: number;
  research_kernel_hash: string;
  stale_from_phase: string | null;
}

export interface ResearchKernelEditBody {
  paper_mode: string;
  kernel: ResearchKernel;
  base_proposal_version: number;
  base_kernel_hash: string;
  accept_developer_preview?: boolean;
}

export function editResearchKernel(
  runId: string,
  body: ResearchKernelEditBody,
): Promise<ResearchKernelEditResponse> {
  return request<ResearchKernelEditResponse>(
    `/api/runs/${encodeURIComponent(runId)}/research_kernel`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    },
  );
}

/**
 * Distinct conflict typing — narrow to STALE-TOKEN conflicts only
 * (codex C0.b2.ui round-1 amendment 4). Other 409s like
 * "another phase is currently running" / "this run is cancelled"
 * are inline save errors, NOT side-by-side merge conflicts. The
 * frontend modal shows the side-by-side diff UI only for this
 * specific class.
 */
export class ResearchKernelConflictError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ResearchKernelConflictError";
  }
}

/**
 * Wrapper that converts STALE-TOKEN 409 errors into
 * ResearchKernelConflictError for cleaner branch handling in
 * the workspace edit modal. Lock/cancelled/other-409 keep
 * bubbling as plain Errors so the caller renders an inline
 * error banner instead.
 *
 * Detection: backend's stale-token errors carry the strings
 * ``base_proposal_version`` or ``base_kernel_hash`` in the
 * detail; running/lock/cancelled use different copy.
 */
export async function editResearchKernelWithConflictTyping(
  runId: string,
  body: ResearchKernelEditBody,
): Promise<ResearchKernelEditResponse> {
  try {
    return await editResearchKernel(runId, body);
  } catch (caught) {
    const msg = caught instanceof Error ? caught.message : String(caught);
    const isConflictStatus =
      caught instanceof ApiError && caught.status === 409;
    const isStaleTokenMessage =
      msg.includes("base_proposal_version") || msg.includes("base_kernel_hash");
    if (isConflictStatus && isStaleTokenMessage) {
      throw new ResearchKernelConflictError(msg);
    }
    throw caught;
  }
}
