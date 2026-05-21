import { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { KernelIntakeForm } from "../components/KernelIntakeForm";
import {
  ApiError,
  createProject,
  createRun,
  Domain,
  editResearchKernel,
  getGenerationModes,
  getPaperModes,
  listDomains,
  PaperModeSpec,
  Project,
  ProjectLanguage,
  suggestResearchKernel,
} from "../lib/api";
import type { GenerationMode } from "../lib/api";
import { useT, useUILanguage } from "../lib/i18n";
import {
  buildKernelPayload,
  EMPTY_KERNEL_FORM,
  intakeSubmitDisabledReason,
  isIntakeSubmittable,
  mergeKernelSuggestion,
  modeSpecOrFallback,
  type KernelIntakeFormState,
} from "../lib/kernelValidation";

const LANGUAGE_OPTIONS: { value: ProjectLanguage; labelKey: string }[] = [
  { value: "en", labelKey: "lang.en_label" },
  { value: "zh", labelKey: "lang.zh_label" },
  { value: "ja", labelKey: "lang.ja_label" },
];

export default function NewRunPage() {
  const t = useT();
  const navigate = useNavigate();
  const [uiLanguage] = useUILanguage();
  const [title, setTitle] = useState("Untitled Project");
  const [targetJournal, setTargetJournal] = useState("");
  // Paper language defaults to the site UI language at first render. Users can
  // override here if their UI is, say, English but they want a Chinese paper.
  const [paperLanguage, setPaperLanguage] =
    useState<ProjectLanguage>(uiLanguage);
  // Keep paper language in sync with the header switcher until the user
  // explicitly overrides it. Once they pick a value, leave it alone.
  const [paperLanguageTouched, setPaperLanguageTouched] = useState(false);
  useEffect(() => {
    if (!paperLanguageTouched) {
      setPaperLanguage(uiLanguage);
    }
  }, [uiLanguage, paperLanguageTouched]);

  const [domains, setDomains] = useState<Domain[]>([]);
  const [selectedDomainId, setSelectedDomainId] = useState("");
  const [isLoadingDomains, setIsLoadingDomains] = useState(true);
  const [createdProject, setCreatedProject] = useState<Project | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  // PR-C0.b2.ui: research-kernel intake-gate state.
  const [paperModes, setPaperModes] = useState<PaperModeSpec[] | null>(null);
  // PR-366: "数理增强模式" opt-in. Default OFF so cost-sensitive
  // students stay on the cheap path; checked → gpt-5.5 round-0
  // holistic rewrite kicks in at rewriter + critic.
  const [mathematicalMode, setMathematicalMode] = useState(false);
  // PR-382: 一键全自动 opt-in. When ticked the run advances all
  // USER_*_REVIEW gates automatically (FAILED_* still need user).
  const [autoAdvance, setAutoAdvance] = useState(false);
  const [generationMode, setGenerationMode] =
    useState<GenerationMode>("express");
  const [kernelForm, setKernelForm] =
    useState<KernelIntakeFormState>(EMPTY_KERNEL_FORM);
  const [isSuggestingKernel, setIsSuggestingKernel] = useState(false);
  const [kernelSuggestError, setKernelSuggestError] = useState<string | null>(
    null,
  );

  useEffect(() => {
    let isCancelled = false;

    async function loadDomains() {
      setIsLoadingDomains(true);
      setError(null);
      try {
        const loadedDomains = await listDomains();
        if (isCancelled) return;
        const preferredDomain =
          loadedDomains.find((domain) => domain.id === "financial_history") ??
          loadedDomains[0];
        setDomains(loadedDomains);
        setSelectedDomainId((current) => current || preferredDomain?.id || "");
      } catch (caught) {
        if (isCancelled) return;
        setError(
          caught instanceof Error ? caught.message : "Domain loading failed",
        );
      } finally {
        if (!isCancelled) setIsLoadingDomains(false);
      }
    }

    loadDomains();
    return () => {
      isCancelled = true;
    };
  }, []);

  // PR-C0.b2.ui: prefetch paper-modes registry for the kernel
  // intake form. Failure → modes stays null and ModeAvailability
  // renders the degraded fallback (case_analysis only) per
  // codex round-1.b2.ui amendment 1.
  useEffect(() => {
    let isCancelled = false;
    getPaperModes()
      .then((res) => {
        if (isCancelled) return;
        setPaperModes(res.modes);
        if (kernelForm.paper_mode === "" && res.default_mode_id) {
          setKernelForm((prev) => ({
            ...prev,
            paper_mode: res.default_mode_id,
          }));
        }
      })
      .catch(() => {
        if (!isCancelled) setPaperModes(null);
      });
    return () => {
      isCancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    let isCancelled = false;
    getGenerationModes()
      .then((res) => {
        if (!isCancelled) setGenerationMode(res.default_mode);
      })
      .catch(() => {
        if (!isCancelled) setGenerationMode("express");
      });
    return () => {
      isCancelled = true;
    };
  }, []);

  useEffect(() => {
    if (generationMode === "express" && autoAdvance) {
      setAutoAdvance(false);
    }
  }, [autoAdvance, generationMode]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedDomainId) {
      setError("No domain selected");
      return;
    }
    setIsSaving(true);
    setError(null);
    let project: Project | null = null;
    let runId: string | null = null;
    try {
      // Step 1: project (essay-limit error class is unique to this).
      project = await createProject({
        title,
        domain_id: selectedDomainId,
        target_journal: targetJournal || null,
        language: paperLanguage,
      });
      setCreatedProject(project);
      // Step 2: run (creates a default-kernel run on the backend).
      const run = await createRun(project.id, {
        mode: generationMode,
        mathematical_mode: mathematicalMode,
        auto_advance: generationMode === "deep" ? autoAdvance : false,
      });
      runId = run.id;
      // Step 3: write the intake-gate kernel via PUT
      // /api/runs/{id}/research_kernel. Codex round-1.b2.ui
      // amendment 5: partial-failure handling — if step 1+2
      // succeeded but step 3 fails, we still navigate to the
      // workspace with ?repair=kernel so the user can fix it
      // without re-creating the project.
      try {
        await editResearchKernel(run.id, {
          paper_mode: kernelForm.paper_mode,
          kernel: buildKernelPayload(kernelForm),
          base_proposal_version: 0, // brand-new run
          base_kernel_hash: run.research_kernel_hash || "",
          accept_developer_preview: kernelForm.accept_developer_preview,
        });
        navigate(`/runs/${run.id}`);
      } catch (kernelErr) {
        // Run exists with default kernel. Don't strand the user;
        // hand them off to the workspace's repair banner.
        navigate(`/runs/${run.id}?repair=kernel`);

        console.warn(
          "kernel PUT failed; redirecting with repair flag",
          kernelErr,
        );
      }
    } catch (caught) {
      if (
        caught instanceof ApiError &&
        caught.body &&
        typeof caught.body === "object" &&
        (caught.body as { code?: string }).code === "essay_limit"
      ) {
        setError(t("newrun.essay_limit_reached"));
      } else {
        setError(
          caught instanceof Error ? caught.message : "Project creation failed",
        );
      }
      // Keep project + runId visible if step 2 succeeded but later
      // failed; the user can manually navigate.
      if (runId) {
        setError(
          (caught instanceof Error ? caught.message : "Run creation failed") +
            ` — 已创建运行 ${runId}, 请到工作台手动恢复。`,
        );
      }
    } finally {
      setIsSaving(false);
    }
  }

  async function handleSuggestKernel() {
    if (!selectedDomainId) {
      setKernelSuggestError(t("newrun.kernel.suggest_domain_required"));
      return;
    }
    if (title.trim().length < 4) {
      setKernelSuggestError(t("newrun.kernel.suggest_title_required"));
      return;
    }
    setIsSuggestingKernel(true);
    setKernelSuggestError(null);
    try {
      const response = await suggestResearchKernel({
        title,
        domain_id: selectedDomainId,
        language: paperLanguage,
      });
      setKernelForm((prev) => mergeKernelSuggestion(prev, response.suggestion));
    } catch (caught) {
      setKernelSuggestError(
        caught instanceof Error
          ? caught.message
          : t("newrun.kernel.suggest_error_generic"),
      );
    } finally {
      setIsSuggestingKernel(false);
    }
  }

  const hasDomains = domains.length > 0;
  const kernelModeSpec = modeSpecOrFallback(paperModes, kernelForm.paper_mode);
  const isKernelValid = isIntakeSubmittable(kernelForm, kernelModeSpec);
  const kernelDisabledReason = intakeSubmitDisabledReason(
    kernelForm,
    kernelModeSpec,
  );
  const submitDisabledReason: {
    key: string;
    vars?: Record<string, string | number>;
  } | null = !selectedDomainId
    ? { key: "newrun.kernel.domain_required" }
    : !hasDomains
      ? { key: "newrun.kernel.no_domains" }
      : kernelDisabledReason;
  const submitDisabledText = submitDisabledReason
    ? t(submitDisabledReason.key, submitDisabledReason.vars)
    : "";
  const isSubmitDisabled =
    isSaving ||
    isLoadingDomains ||
    !selectedDomainId ||
    !hasDomains ||
    !isKernelValid;
  const kernelSuggestDisabled =
    isSaving ||
    isSuggestingKernel ||
    isLoadingDomains ||
    !selectedDomainId ||
    !hasDomains ||
    title.trim().length < 4;

  return (
    <section className="max-w-4xl rounded-lg border border-slate-200 bg-white p-5 shadow-sm sm:p-6">
      <p className="mb-2 text-xs font-bold uppercase text-slate-500">
        {t("newrun.section_label")}
      </p>
      <h1 className="mb-6 text-2xl font-bold text-slate-950 sm:text-3xl">
        {t("newrun.heading")}
      </h1>
      <form
        className="grid gap-4"
        data-testid="newrun-form"
        onSubmit={handleSubmit}
      >
        <label className="grid gap-2 text-sm font-semibold text-slate-800">
          {t("newrun.domain")}
          <select
            data-testid="newrun-domain"
            className="min-h-11 w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-normal text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20 disabled:bg-slate-100 disabled:text-slate-500"
            value={selectedDomainId}
            onChange={(event) => setSelectedDomainId(event.target.value)}
            disabled={isLoadingDomains || !hasDomains}
          >
            {isLoadingDomains ? (
              <option value="">Loading domains...</option>
            ) : null}
            {!isLoadingDomains && !hasDomains ? (
              <option value="">No domains configured — contact admin</option>
            ) : null}
            {domains.map((domain) => (
              <option key={domain.id} value={domain.id}>
                {formatDomainOption(domain, domains.length > 1)}
              </option>
            ))}
          </select>
          {!isLoadingDomains && !hasDomains ? (
            <span className="font-normal text-red-700">
              No domains configured — contact admin
            </span>
          ) : null}
        </label>
        <label className="grid gap-2 text-sm font-semibold text-slate-800">
          {t("newrun.title")}
          <input
            data-testid="newrun-title"
            className="min-h-11 rounded-md border border-slate-300 px-3 py-2 font-normal text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
          />
        </label>
        <label className="grid gap-2 text-sm font-semibold text-slate-800">
          {t("newrun.target_journal")}
          <input
            className="min-h-11 rounded-md border border-slate-300 px-3 py-2 font-normal text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20"
            value={targetJournal}
            onChange={(event) => setTargetJournal(event.target.value)}
            placeholder={t("newrun.optional")}
          />
        </label>
        <label className="grid gap-2 text-sm font-semibold text-slate-800">
          {t("newrun.paper_language")}
          <select
            data-testid="newrun-paper-language"
            className="min-h-11 w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-normal text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20"
            value={paperLanguage}
            onChange={(event) => {
              setPaperLanguage(event.target.value as ProjectLanguage);
              setPaperLanguageTouched(true);
            }}
          >
            {LANGUAGE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {t(option.labelKey)}
              </option>
            ))}
          </select>
          <span className="font-normal text-xs text-slate-500">
            {t("newrun.paper_language_hint")}
          </span>
        </label>

        <hr className="my-2 border-slate-200" />
        <GenerationModeSelector
          value={generationMode}
          onChange={setGenerationMode}
        />

        <hr className="my-2 border-slate-200" />
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="grid gap-2">
            <p className="text-xs font-bold uppercase text-slate-500">
              {t("newrun.kernel.section_label")}
            </p>
            <p className="text-xs text-slate-600 leading-5">
              {t("newrun.kernel.intro_paragraph")}
            </p>
          </div>
          <button
            type="button"
            data-testid="kernel-suggest-button"
            className="inline-flex min-h-10 shrink-0 items-center justify-center gap-2 rounded-md border border-[#114b5f] bg-white px-3 py-2 text-sm font-bold text-[#114b5f] transition hover:bg-[#eef7f5] disabled:cursor-default disabled:opacity-60"
            disabled={kernelSuggestDisabled}
            onClick={handleSuggestKernel}
            title={t("newrun.kernel.suggest_title")}
          >
            {isSuggestingKernel ? (
              <span
                data-testid="kernel-suggest-loading"
                className="h-4 w-4 rounded-full border-2 border-[#114b5f]/30 border-t-[#114b5f] motion-safe:animate-spin"
                aria-hidden="true"
              />
            ) : null}
            {isSuggestingKernel
              ? t("newrun.kernel.suggest_loading")
              : t("newrun.kernel.suggest_button")}
          </button>
        </div>
        {kernelSuggestError ? (
          <p
            data-testid="kernel-suggest-error"
            role="alert"
            className="rounded-md border border-red-200 bg-red-50 p-2 text-xs leading-5 text-red-700"
          >
            {kernelSuggestError}
          </p>
        ) : null}
        <KernelIntakeForm
          state={kernelForm}
          onChange={setKernelForm}
          modes={paperModes}
          language={paperLanguage}
          testIdPrefix="newrun-kernel"
          reasonElementId="newrun-submit-reason"
        />

        <label className="mt-2 flex items-start gap-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-3 text-sm text-slate-800">
          <input
            type="checkbox"
            data-testid="new-run-mathematical-mode"
            className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-slate-400 text-[#114b5f] focus:ring-[#114b5f]"
            checked={mathematicalMode}
            onChange={(event) => setMathematicalMode(event.target.checked)}
          />
          <span className="grid gap-1">
            <span className="font-semibold text-slate-900">
              {t("newrun.mathematical_mode.label")}
            </span>
            <span className="text-xs leading-5 text-slate-600">
              {t("newrun.mathematical_mode.tooltip")}
            </span>
          </span>
        </label>

        {/* PR-382: 一键全自动 checkbox. Independent of mathematical
            mode — they're orthogonal. */}
        <label className="mt-2 flex items-start gap-3 rounded-md border border-slate-200 bg-slate-50 px-3 py-3 text-sm text-slate-800">
          <input
            type="checkbox"
            data-testid="new-run-auto-advance"
            className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-slate-400 text-[#114b5f] focus:ring-[#114b5f]"
            checked={autoAdvance}
            disabled={generationMode === "express"}
            onChange={(event) => setAutoAdvance(event.target.checked)}
          />
          <span className="grid gap-1">
            <span className="font-semibold text-slate-900">
              {t("newrun.auto_advance.label")}
            </span>
            <span className="text-xs leading-5 text-slate-600">
              {generationMode === "express"
                ? t("newrun.auto_advance.deep_only")
                : t("newrun.auto_advance.tooltip")}
            </span>
          </span>
        </label>

        <button
          type="submit"
          data-testid="newrun-submit"
          className="inline-flex min-h-11 w-full items-center justify-center rounded-md bg-[#114b5f] px-4 py-2 text-sm font-bold text-white transition hover:bg-[#0d3d4d] disabled:cursor-default disabled:opacity-65 sm:w-auto"
          disabled={isSubmitDisabled}
          aria-describedby={
            submitDisabledText ? "newrun-submit-reason" : undefined
          }
        >
          {isSaving ? t("newrun.creating_button") : t("newrun.create_and_open")}
        </button>
        {submitDisabledText && !isKernelValid ? null : submitDisabledText ? (
          <p
            id="newrun-submit-reason"
            role="status"
            data-testid="newrun-submit-reason"
            className="text-xs italic text-amber-700"
          >
            {submitDisabledText}
          </p>
        ) : null}
      </form>
      {createdProject ? (
        <p className="mt-4 leading-7 text-[#236b45]">
          {t("newrun.created")} {createdProject.id}
        </p>
      ) : null}
      {error ? <p className="mt-4 leading-7 text-red-700">{error}</p> : null}
    </section>
  );
}

function GenerationModeSelector({
  value,
  onChange,
}: {
  value: GenerationMode;
  onChange: (mode: GenerationMode) => void;
}) {
  const t = useT();
  const options: Array<{
    mode: GenerationMode;
    labelKey: string;
    detailKey: string;
  }> = [
    {
      mode: "express",
      labelKey: "newrun.generation_mode.express.label",
      detailKey: "newrun.generation_mode.express.detail",
    },
    {
      mode: "deep",
      labelKey: "newrun.generation_mode.deep.label",
      detailKey: "newrun.generation_mode.deep.detail",
    },
  ];
  return (
    <fieldset
      data-testid="mode-selector"
      className="grid gap-3"
      aria-label={t("newrun.generation_mode.label")}
    >
      <legend className="text-sm font-bold text-slate-900">
        {t("newrun.generation_mode.label")}
      </legend>
      <p className="mt-1 text-xs leading-5 text-slate-600">
        {t("newrun.generation_mode.hint")}
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        {options.map((option) => {
          const selected = value === option.mode;
          return (
            <label
              key={option.mode}
              data-testid={`mode-option-${option.mode}`}
              data-selected={selected ? "true" : "false"}
              className={`grid cursor-pointer gap-2 rounded-md border px-4 py-3 text-sm transition ${
                selected
                  ? "border-[#114b5f] bg-[#eef7f5] text-slate-950 shadow-sm"
                  : "border-slate-200 bg-white text-slate-800 hover:border-[#114b5f]/50"
              }`}
            >
              <span className="flex items-center gap-2">
                <input
                  type="radio"
                  name="generation-mode"
                  value={option.mode}
                  checked={selected}
                  onChange={() => onChange(option.mode)}
                  className="h-4 w-4 cursor-pointer border-slate-400 text-[#114b5f] focus:ring-[#114b5f]"
                />
                <span className="font-bold">{t(option.labelKey)}</span>
              </span>
              <span className="text-xs leading-5 text-slate-600">
                {t(option.detailKey)}
              </span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function formatDomainOption(
  domain: Domain,
  includeDescription: boolean,
): string {
  if (includeDescription && domain.description) {
    return `${domain.display_name} - ${domain.description}`;
  }
  return domain.display_name;
}
