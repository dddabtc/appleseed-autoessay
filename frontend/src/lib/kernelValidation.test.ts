import { describe, expect, it } from "vitest";

import {
  EMPTY_KERNEL_FORM,
  FALLBACK_MODE_SPEC,
  buildKernelPayload,
  findModeSpec,
  intakeSubmitDisabledReason,
  isIntakeSubmittable,
  isPaperModeReadOnly,
  kernelToFormState,
  mergeKernelSuggestion,
  modeAvailabilityState,
  modeSpecOrFallback,
  type KernelIntakeFormState,
  type PaperModeSpec,
} from "./kernelValidation";

const CASE_ANALYSIS: PaperModeSpec = {
  mode_id: "case_analysis",
  label_en: "Case analysis",
  label_zh: "个案分析",
  label_ja: "個別事例分析",
  description_en: "...",
  description_zh: "...",
  description_ja: "...",
  status: "available",
  requires_capability: [],
  permits_empirical_chapters: true,
  primary_material_required: false,
};

const EMPIRICAL_PREVIEW: PaperModeSpec = {
  mode_id: "empirical",
  label_en: "Empirical research",
  label_zh: "实证研究",
  label_ja: "実証研究",
  description_en: "...",
  description_zh: "...",
  description_ja: "...",
  status: "developer_preview",
  requires_capability: ["evidence_ledger"],
  permits_empirical_chapters: true,
  primary_material_required: true,
};

const THEORY_COMING: PaperModeSpec = {
  mode_id: "theory_article",
  label_en: "Theoretical article",
  label_zh: "理论论文",
  label_ja: "理論論文",
  description_en: "...",
  description_zh: "...",
  description_ja: "...",
  status: "coming_soon",
  requires_capability: ["framework_lens"],
  permits_empirical_chapters: false,
  primary_material_required: false,
};

function withFields(
  overrides: Partial<KernelIntakeFormState> = {},
): KernelIntakeFormState {
  return {
    ...EMPTY_KERNEL_FORM,
    observed_puzzle:
      "I have noticed that traditional accounts of X overstate the role of Y.",
    tentative_question: "How does Y actually function in context Z?",
    scope: "Late Qing administrative records, 1860–1895.",
    ...overrides,
  };
}

describe("intakeSubmitDisabledReason", () => {
  it("blocks when no mode spec", () => {
    const r = intakeSubmitDisabledReason(withFields(), null);
    expect(r?.key).toBe("kernel.validation.no_mode");
  });

  it("blocks coming_soon modes", () => {
    const r = intakeSubmitDisabledReason(withFields(), THEORY_COMING);
    expect(r?.key).toBe("kernel.validation.mode_coming_soon");
    expect(r?.vars).toEqual({ mode_id: "theory_article" });
  });

  it("blocks developer_preview without ack", () => {
    const r = intakeSubmitDisabledReason(
      withFields({ paper_mode: "empirical", accept_developer_preview: false }),
      EMPIRICAL_PREVIEW,
    );
    expect(r?.key).toBe("kernel.validation.preview_ack_required");
  });

  it("allows developer_preview with ack", () => {
    expect(
      intakeSubmitDisabledReason(
        withFields({
          paper_mode: "empirical",
          accept_developer_preview: true,
          primary_materials_status: "yes",
        }),
        EMPIRICAL_PREVIEW,
      ),
    ).toBeNull();
  });

  it("blocks short observed_puzzle", () => {
    const r = intakeSubmitDisabledReason(
      withFields({ observed_puzzle: "too short" }),
      CASE_ANALYSIS,
    );
    expect(r?.key).toBe("kernel.validation.puzzle_too_short");
    expect(r?.vars).toEqual({ min: 30 });
  });

  it("blocks empty tentative_question", () => {
    const r = intakeSubmitDisabledReason(
      withFields({ tentative_question: "   " }),
      CASE_ANALYSIS,
    );
    expect(r?.key).toBe("kernel.validation.question_required");
  });

  it("blocks empty scope", () => {
    const r = intakeSubmitDisabledReason(
      withFields({ scope: "" }),
      CASE_ANALYSIS,
    );
    expect(r?.key).toBe("kernel.validation.scope_required");
  });

  it("blocks scope > 200 chars", () => {
    const r = intakeSubmitDisabledReason(
      withFields({ scope: "x".repeat(201) }),
      CASE_ANALYSIS,
    );
    expect(r?.key).toBe("kernel.validation.scope_too_long");
    expect(r?.vars).toEqual({ max: 200 });
  });

  it("blocks empirical when primary_materials_status=none", () => {
    const r = intakeSubmitDisabledReason(
      withFields({
        paper_mode: "empirical",
        accept_developer_preview: true,
        primary_materials_status: "none",
      }),
      EMPIRICAL_PREVIEW,
    );
    expect(r?.key).toBe("kernel.validation.primary_required");
  });

  it("permits empirical with primary=will_upload_later", () => {
    expect(
      intakeSubmitDisabledReason(
        withFields({
          paper_mode: "empirical",
          accept_developer_preview: true,
          primary_materials_status: "will_upload_later",
        }),
        EMPIRICAL_PREVIEW,
      ),
    ).toBeNull();
  });

  it("returns null for valid case_analysis form", () => {
    expect(intakeSubmitDisabledReason(withFields(), CASE_ANALYSIS)).toBeNull();
  });
});

describe("isIntakeSubmittable", () => {
  it("matches reason emptiness", () => {
    expect(isIntakeSubmittable(withFields(), CASE_ANALYSIS)).toBe(true);
    expect(isIntakeSubmittable(withFields(), null)).toBe(false);
  });
});

describe("buildKernelPayload", () => {
  it("strips empty optional fields", () => {
    const payload = buildKernelPayload(withFields());
    expect(payload.kernel_schema_version).toBe(1);
    expect(payload.observed_puzzle).toBeDefined();
    expect(payload.method_preference).toBeUndefined();
    expect(payload.theory_preference).toBeUndefined();
  });

  it("preserves filled optional fields", () => {
    const payload = buildKernelPayload(
      withFields({
        method_preference: "qualitative",
        theory_preference: "Bourdieu",
      }),
    );
    expect(payload.method_preference).toBe("qualitative");
    expect(payload.theory_preference).toBe("Bourdieu");
  });

  it("trims whitespace", () => {
    const payload = buildKernelPayload(
      withFields({ observed_puzzle: "  trimmed  " }),
    );
    expect(payload.observed_puzzle).toBe("trimmed");
  });
});

describe("mergeKernelSuggestion", () => {
  const suggestion = {
    observed_puzzle: "生成的疑点需要足够长，以满足研究内核表单的最小长度要求。",
    tentative_question: "生成的问题是什么？",
    scope: "生成的范围",
    method_preference: "生成的方法",
    theory_preference: "生成的理论",
  };

  it("fills empty fields from the suggestion", () => {
    const merged = mergeKernelSuggestion(EMPTY_KERNEL_FORM, suggestion);
    expect(merged.observed_puzzle).toBe(suggestion.observed_puzzle);
    expect(merged.tentative_question).toBe(suggestion.tentative_question);
    expect(merged.scope).toBe(suggestion.scope);
    expect(merged.method_preference).toBe(suggestion.method_preference);
    expect(merged.theory_preference).toBe(suggestion.theory_preference);
  });

  it("preserves fields the user already wrote", () => {
    const merged = mergeKernelSuggestion(
      withFields({
        method_preference: "hand-written method",
        theory_preference: "",
      }),
      suggestion,
    );
    expect(merged.observed_puzzle).toBe(withFields().observed_puzzle);
    expect(merged.method_preference).toBe("hand-written method");
    expect(merged.theory_preference).toBe(suggestion.theory_preference);
  });
});

describe("kernelToFormState", () => {
  it("hydrates from stored kernel", () => {
    const state = kernelToFormState("empirical", {
      kernel_schema_version: 1,
      observed_puzzle: "x",
      tentative_question: "y",
      scope: "z",
      primary_materials_status: "yes",
    });
    expect(state.paper_mode).toBe("empirical");
    expect(state.observed_puzzle).toBe("x");
    expect(state.primary_materials_status).toBe("yes");
  });

  it("falls back to defaults for null kernel", () => {
    const state = kernelToFormState("case_analysis", null);
    expect(state.observed_puzzle).toBe("");
    expect(state.primary_materials_status).toBe("none");
  });

  it("sanitizes invalid primary_materials_status", () => {
    const state = kernelToFormState("case_analysis", {
      kernel_schema_version: 1,
      primary_materials_status: "garbage",
    });
    expect(state.primary_materials_status).toBe("none");
  });
});

describe("isPaperModeReadOnly", () => {
  it("editable when proposal_version=0", () => {
    expect(isPaperModeReadOnly(0)).toBe(false);
  });
  it("read-only when proposal_version>=1", () => {
    expect(isPaperModeReadOnly(1)).toBe(true);
    expect(isPaperModeReadOnly(5)).toBe(true);
  });
});

describe("findModeSpec", () => {
  it("returns null on empty registry", () => {
    expect(findModeSpec(null, "anything")).toBe(null);
    expect(findModeSpec([], "anything")).toBe(null);
  });

  it("returns spec by id", () => {
    expect(
      findModeSpec([CASE_ANALYSIS, EMPIRICAL_PREVIEW], "empirical")?.mode_id,
    ).toBe("empirical");
  });

  it("returns null for unknown id", () => {
    expect(findModeSpec([CASE_ANALYSIS, EMPIRICAL_PREVIEW], "not_a_mode")).toBe(
      null,
    );
  });
});

// ---------------------------------------------------------------------------
// Codex C0.b2.ui round-1 amendments: FALLBACK_MODE_SPEC + modeSpecOrFallback +
// modeAvailabilityState pure-decision helpers.
// ---------------------------------------------------------------------------

describe("FALLBACK_MODE_SPEC", () => {
  it("is case_analysis with available status", () => {
    expect(FALLBACK_MODE_SPEC.mode_id).toBe("case_analysis");
    expect(FALLBACK_MODE_SPEC.status).toBe("available");
    expect(FALLBACK_MODE_SPEC.primary_material_required).toBe(false);
  });

  it("populates ja fields (PR-C0.b2.tests)", () => {
    expect(FALLBACK_MODE_SPEC.label_ja).toBeTruthy();
    expect(FALLBACK_MODE_SPEC.description_ja).toBeTruthy();
  });
});

describe("modeSpecOrFallback", () => {
  it("returns spec from registry when mode found", () => {
    expect(modeSpecOrFallback([CASE_ANALYSIS], "case_analysis")).toBe(
      CASE_ANALYSIS,
    );
  });

  it("returns FALLBACK_MODE_SPEC when registry empty", () => {
    expect(modeSpecOrFallback(null, "anything")).toBe(FALLBACK_MODE_SPEC);
    expect(modeSpecOrFallback([], "anything")).toBe(FALLBACK_MODE_SPEC);
  });

  it("returns FALLBACK_MODE_SPEC when mode_id unknown", () => {
    expect(modeSpecOrFallback([CASE_ANALYSIS], "not_a_mode")).toBe(
      FALLBACK_MODE_SPEC,
    );
  });
});

describe("modeAvailabilityState", () => {
  it("available mode is selectable, no ack, no reason", () => {
    const s = modeAvailabilityState(CASE_ANALYSIS, false);
    expect(s.selectable).toBe(true);
    expect(s.requiresAck).toBe(false);
    expect(s.isComingSoon).toBe(false);
    expect(s.isPreview).toBe(false);
    expect(s.reason).toBeNull();
  });

  it("coming_soon mode never selectable", () => {
    const s = modeAvailabilityState(THEORY_COMING, true);
    expect(s.selectable).toBe(false);
    expect(s.isComingSoon).toBe(true);
    expect(s.reason?.key).toBe("paper_mode.reason.coming_soon");
  });

  it("developer_preview without ack: SELECTABLE (PR-C0.b2.tests UX change), but requiresAck", () => {
    const s = modeAvailabilityState(EMPIRICAL_PREVIEW, false);
    expect(s.selectable).toBe(true);
    expect(s.requiresAck).toBe(true);
    expect(s.isPreview).toBe(true);
    expect(s.reason?.key).toBe("paper_mode.reason.preview_needs_ack");
  });

  it("developer_preview with ack: selectable, no requiresAck", () => {
    const s = modeAvailabilityState(EMPIRICAL_PREVIEW, true);
    expect(s.selectable).toBe(true);
    expect(s.requiresAck).toBe(false);
    expect(s.isPreview).toBe(true);
    expect(s.reason).toBeNull();
  });
});
