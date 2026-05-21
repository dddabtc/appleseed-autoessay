// PR-C0.b2: pure validation + helper functions for the
// research-kernel intake gate. Keeps NewRunPage / WorkspacePage
// free of validation logic. Vitest covers these directly.
//
// Backend treats the kernel as an opaque blob (see
// `backend/src/autoessay/research_kernel.py`); intake constraints
// (puzzle length, scope max, required fields, primary-material
// vs status) are FRONTEND-only guards. A curl/SDK caller can store
// any kernel shape that's parseable JSON. This is acceptable per
// the C0 design (codex round-2 amendment 6: "frontend-only is
// fine for this PR; backend already enforces opaque dict +
// paper_mode validity").

export interface ResearchKernel {
  kernel_schema_version: number;
  observed_puzzle?: string;
  tentative_question?: string;
  scope?: string;
  method_preference?: string;
  theory_preference?: string;
  primary_materials_status?: "yes" | "will_upload_later" | "none";
}

export interface PaperModeSpec {
  mode_id: string;
  label_en: string;
  label_zh: string;
  label_ja: string;
  description_en: string;
  description_zh: string;
  description_ja: string;
  status: "available" | "developer_preview" | "coming_soon";
  requires_capability: string[];
  permits_empirical_chapters: boolean;
  primary_material_required: boolean;
}

export interface PaperModesResponse {
  registry_version: string;
  default_mode_id: string;
  modes: PaperModeSpec[];
}

export interface KernelIntakeFormState {
  paper_mode: string;
  observed_puzzle: string;
  tentative_question: string;
  scope: string;
  method_preference: string;
  theory_preference: string;
  primary_materials_status: "yes" | "will_upload_later" | "none";
  accept_developer_preview: boolean;
}

export interface KernelSuggestionFields {
  observed_puzzle: string;
  tentative_question: string;
  scope: string;
  method_preference: string;
  theory_preference: string;
}

export const EMPTY_KERNEL_FORM: KernelIntakeFormState = {
  paper_mode: "case_analysis",
  observed_puzzle: "",
  tentative_question: "",
  scope: "",
  method_preference: "",
  theory_preference: "",
  primary_materials_status: "none",
  accept_developer_preview: false,
};

export const PUZZLE_MIN_CHARS = 30;
export const SCOPE_MAX_CHARS = 200;

/**
 * Structured reason for why submit is currently disabled. The
 * caller resolves ``key`` via ``t(key, vars)`` for the rendered
 * text. Validator returns ``null`` when the form is submittable.
 *
 * Why a struct, not a translated string: vitest is node-env (no
 * React, no i18n hook). Returning a key keeps the validator pure
 * and language-agnostic — vitest asserts on the key, the React
 * tree resolves to text.
 */
export interface ValidationReason {
  key: string;
  vars?: Record<string, string | number>;
}

/**
 * Reason ordering (most specific first) matches the form's vertical
 * order so the user's eye lands on the relevant field. Caller sets
 * `aria-describedby` on the submit button to the inline status text.
 */
export function intakeSubmitDisabledReason(
  state: KernelIntakeFormState,
  modeSpec: PaperModeSpec | null,
): ValidationReason | null {
  if (!modeSpec) {
    // Round-1.b2.ui amendment 1: caller should pass FALLBACK_MODE_SPEC
    // (or modeSpecOrFallback). Hitting null here means the caller
    // forgot; surface a clear error.
    return { key: "kernel.validation.no_mode" };
  }
  if (modeSpec.status === "coming_soon") {
    return {
      key: "kernel.validation.mode_coming_soon",
      vars: { mode_id: modeSpec.mode_id },
    };
  }
  if (
    modeSpec.status === "developer_preview" &&
    !state.accept_developer_preview
  ) {
    return { key: "kernel.validation.preview_ack_required" };
  }
  if (state.observed_puzzle.trim().length < PUZZLE_MIN_CHARS) {
    return {
      key: "kernel.validation.puzzle_too_short",
      vars: { min: PUZZLE_MIN_CHARS },
    };
  }
  if (!state.tentative_question.trim()) {
    return { key: "kernel.validation.question_required" };
  }
  if (!state.scope.trim()) {
    return { key: "kernel.validation.scope_required" };
  }
  if (state.scope.length > SCOPE_MAX_CHARS) {
    return {
      key: "kernel.validation.scope_too_long",
      vars: { max: SCOPE_MAX_CHARS },
    };
  }
  if (
    modeSpec.primary_material_required &&
    state.primary_materials_status === "none"
  ) {
    return { key: "kernel.validation.primary_required" };
  }
  return null;
}

export function isIntakeSubmittable(
  state: KernelIntakeFormState,
  modeSpec: PaperModeSpec | null,
): boolean {
  return intakeSubmitDisabledReason(state, modeSpec) === null;
}

/** Build the kernel JSON sent to PUT /api/runs/{id}/research_kernel. */
export function buildKernelPayload(
  state: KernelIntakeFormState,
): ResearchKernel {
  const trimmedOrUndef = (s: string): string | undefined => {
    const t = s.trim();
    return t === "" ? undefined : t;
  };
  return {
    kernel_schema_version: 1,
    observed_puzzle: trimmedOrUndef(state.observed_puzzle),
    tentative_question: trimmedOrUndef(state.tentative_question),
    scope: trimmedOrUndef(state.scope),
    method_preference: trimmedOrUndef(state.method_preference),
    theory_preference: trimmedOrUndef(state.theory_preference),
    primary_materials_status: state.primary_materials_status,
  };
}

export function mergeKernelSuggestion(
  state: KernelIntakeFormState,
  suggestion: KernelSuggestionFields,
): KernelIntakeFormState {
  const keepOrSuggest = (current: string, next: string): string =>
    current.trim() ? current : next;
  return {
    ...state,
    observed_puzzle: keepOrSuggest(
      state.observed_puzzle,
      suggestion.observed_puzzle,
    ),
    tentative_question: keepOrSuggest(
      state.tentative_question,
      suggestion.tentative_question,
    ),
    scope: keepOrSuggest(state.scope, suggestion.scope),
    method_preference: keepOrSuggest(
      state.method_preference,
      suggestion.method_preference,
    ),
    theory_preference: keepOrSuggest(
      state.theory_preference,
      suggestion.theory_preference,
    ),
  };
}

/** Hydrate form state from a stored kernel + paper_mode (for the edit modal). */
export function kernelToFormState(
  paperMode: string,
  kernel: ResearchKernel | Record<string, unknown> | null | undefined,
  acceptDeveloperPreview: boolean = false,
): KernelIntakeFormState {
  const k = (kernel ?? {}) as Record<string, unknown>;
  const status = String(k.primary_materials_status ?? "none");
  const allowed: KernelIntakeFormState["primary_materials_status"][] = [
    "yes",
    "will_upload_later",
    "none",
  ];
  const sanitizedStatus = (
    allowed.includes(
      status as KernelIntakeFormState["primary_materials_status"],
    )
      ? status
      : "none"
  ) as KernelIntakeFormState["primary_materials_status"];
  return {
    paper_mode: paperMode || "case_analysis",
    observed_puzzle: String(k.observed_puzzle ?? ""),
    tentative_question: String(k.tentative_question ?? ""),
    scope: String(k.scope ?? ""),
    method_preference: String(k.method_preference ?? ""),
    theory_preference: String(k.theory_preference ?? ""),
    primary_materials_status: sanitizedStatus,
    accept_developer_preview: acceptDeveloperPreview,
  };
}

/**
 * For the workspace edit modal: returns true if paper_mode should be
 * read-only (i.e. proposal already exists, so changing the mode is
 * blocked by the backend mode-change guard from PR-C0.b2).
 */
export function isPaperModeReadOnly(proposalVersion: number): boolean {
  return proposalVersion >= 1;
}

/**
 * Find the mode spec by id, or return null if registry is empty /
 * mode_id unknown. Caller renders a degraded UI with a single
 * "case_analysis" pseudo-spec when this returns null after a
 * GET /api/paper_modes failure (codex round-1 question 4).
 */
export function findModeSpec(
  modes: PaperModeSpec[] | null | undefined,
  modeId: string,
): PaperModeSpec | null {
  if (!modes || modes.length === 0) return null;
  return modes.find((m) => m.mode_id === modeId) ?? null;
}

/**
 * Pseudo-spec used when ``GET /api/paper_modes`` fails or returns
 * an empty list. Round-1.b2.ui codex amendment 1: validation must
 * use this fallback (NOT null) so the form remains submittable in
 * a degraded state. Render with a warning banner.
 */
export const FALLBACK_MODE_SPEC: PaperModeSpec = {
  mode_id: "case_analysis",
  label_en: "Case analysis",
  label_zh: "个案分析",
  label_ja: "個別事例分析",
  description_en: "(Mode registry unavailable — using safe default)",
  description_zh: "（模式注册表加载失败 — 使用安全默认）",
  description_ja: "（モードレジストリ未取得 — 安全な既定値を使用）",
  status: "available",
  requires_capability: [],
  permits_empirical_chapters: true,
  primary_material_required: false,
};

/**
 * Look up the spec for a mode id, or return FALLBACK_MODE_SPEC
 * if registry is empty. Use this in validation calls so the form
 * has something to grade against even when the API is down.
 */
export function modeSpecOrFallback(
  modes: PaperModeSpec[] | null | undefined,
  modeId: string,
): PaperModeSpec {
  return findModeSpec(modes, modeId) ?? FALLBACK_MODE_SPEC;
}

/**
 * Per-mode UI state for the radio-card picker. Centralizes the
 * "is this row selectable / does it need an ack / is it grayed?"
 * decision so component logic is just a render of these flags.
 *
 * Codex round-1.b2.ui amendment 6: vitest is node-env (no jsdom),
 * so component-level rendering tests are out. Instead we test the
 * pure decision logic here via vitest, and rely on Playwright for
 * the rendered DOM.
 */
export interface ModeAvailabilityState {
  selectable: boolean;
  requiresAck: boolean;
  isComingSoon: boolean;
  isPreview: boolean;
  /** ``null`` when no caveat. Caller resolves via ``t(reason.key)``
   * for aria-describedby render. */
  reason: ValidationReason | null;
}

/**
 * Per-mode UI flags. Round-2.b2.tests update: developer_preview
 * modes are now always ``selectable=true`` so the user can click
 * the radio, see the ack row appear, and then tick ack to enable
 * submit. The previous "ack-first-then-selectable" gating made
 * the ack row unreachable (chicken-and-egg).
 */
export function modeAvailabilityState(
  spec: PaperModeSpec,
  acceptDeveloperPreview: boolean,
): ModeAvailabilityState {
  const isComingSoon = spec.status === "coming_soon";
  const isPreview = spec.status === "developer_preview";
  if (isComingSoon) {
    return {
      selectable: false,
      requiresAck: false,
      isComingSoon: true,
      isPreview: false,
      reason: { key: "paper_mode.reason.coming_soon" },
    };
  }
  if (isPreview) {
    return {
      selectable: true,
      requiresAck: !acceptDeveloperPreview,
      isComingSoon: false,
      isPreview: true,
      reason: acceptDeveloperPreview
        ? null
        : { key: "paper_mode.reason.preview_needs_ack" },
    };
  }
  return {
    selectable: true,
    requiresAck: false,
    isComingSoon: false,
    isPreview: false,
    reason: null,
  };
}
