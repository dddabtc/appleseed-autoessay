// PR-C0.b2.ui: research-kernel intake form. 7 fields + paper-mode
// picker (ModeAvailability). Composed by NewRunPage (creation) and
// KernelEditModal (edit-after-creation).
//
// Validation lives in `lib/kernelValidation.ts` so the same logic
// drives both the disabled-reason text here and the upstream submit
// button. Form-level reason text is referenced by the caller via
// the ``reasonElementId`` prop so the submit button can wire
// ``aria-describedby`` to it (codex round-1.b2.ui amendment 5: not
// just a disabled-button tooltip).
//
// PR-C0.b2.tests: all user-visible strings now flow through i18n.

import type { ProjectLanguage } from "../lib/api";
import { useT } from "../lib/i18n";
import {
  intakeSubmitDisabledReason,
  modeSpecOrFallback,
  PUZZLE_MIN_CHARS,
  SCOPE_MAX_CHARS,
  type KernelIntakeFormState,
  type PaperModeSpec,
} from "../lib/kernelValidation";

import { ModeAvailability } from "./ModeAvailability";

type Props = {
  state: KernelIntakeFormState;
  onChange: (next: KernelIntakeFormState) => void;
  modes: PaperModeSpec[] | null;
  language: ProjectLanguage;
  /** Read-only mode picker (backend mode-change guard kicks in
   * after proposal_version >= 1). */
  readOnlyMode?: boolean;
  /** Stable testid prefix shared with ModeAvailability. */
  testIdPrefix: string;
  /** ID for the form-level disabled-reason element so callers can
   * wire ``aria-describedby`` on their submit button. */
  reasonElementId: string;
};

const inputClasses =
  "min-h-11 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-normal text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20";
const textareaClasses =
  "min-h-24 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-normal text-slate-950 outline-none transition focus:border-[#114b5f] focus:ring-2 focus:ring-[#114b5f]/20";
const labelClasses = "grid gap-1 text-sm font-semibold text-slate-800";
const hintClasses = "text-xs font-normal text-slate-500";

export function KernelIntakeForm(props: Props) {
  const {
    state,
    onChange,
    modes,
    language,
    readOnlyMode,
    testIdPrefix,
    reasonElementId,
  } = props;
  const t = useT();

  const setField = (
    name: keyof KernelIntakeFormState,
    value: string | boolean,
  ): void => {
    onChange({ ...state, [name]: value } as KernelIntakeFormState);
  };

  const modeSpec = modeSpecOrFallback(modes, state.paper_mode);
  const reason = intakeSubmitDisabledReason(state, modeSpec);
  const reasonText = reason ? t(reason.key, reason.vars) : "";

  return (
    <div className="grid gap-4" data-testid={`${testIdPrefix}-form`}>
      <ModeAvailability
        modes={modes}
        selectedModeId={state.paper_mode}
        onModeChange={(id) =>
          onChange({
            ...state,
            paper_mode: id,
            // Reset ack when mode changes so user re-confirms for
            // the new mode.
            accept_developer_preview: false,
          })
        }
        acceptDeveloperPreview={state.accept_developer_preview}
        onAckChange={(b) => setField("accept_developer_preview", b)}
        language={language}
        readOnly={readOnlyMode}
        testIdPrefix={testIdPrefix}
      />

      <label className={labelClasses}>
        <span>
          {t("kernel.form.observed_puzzle_label")}
          <span className="ml-1 font-normal text-red-700">*</span>
        </span>
        <textarea
          data-testid={`${testIdPrefix}-observed-puzzle`}
          className={textareaClasses}
          value={state.observed_puzzle}
          onChange={(e) => setField("observed_puzzle", e.target.value)}
          placeholder={t("kernel.form.observed_puzzle_placeholder")}
        />
        <span className={hintClasses}>
          {t("kernel.form.observed_puzzle_hint", {
            min: PUZZLE_MIN_CHARS,
            count: state.observed_puzzle.trim().length,
          })}
        </span>
      </label>

      <label className={labelClasses}>
        <span>
          {t("kernel.form.tentative_question_label")}
          <span className="ml-1 font-normal text-red-700">*</span>
        </span>
        <input
          data-testid={`${testIdPrefix}-tentative-question`}
          type="text"
          className={inputClasses}
          value={state.tentative_question}
          onChange={(e) => setField("tentative_question", e.target.value)}
          placeholder={t("kernel.form.tentative_question_placeholder")}
        />
      </label>

      <label className={labelClasses}>
        <span>
          {t("kernel.form.scope_label")}
          <span className="ml-1 font-normal text-red-700">*</span>
        </span>
        <input
          data-testid={`${testIdPrefix}-scope`}
          type="text"
          className={inputClasses}
          value={state.scope}
          onChange={(e) => setField("scope", e.target.value)}
          placeholder={t("kernel.form.scope_placeholder")}
          maxLength={SCOPE_MAX_CHARS + 50}
        />
        <span className={hintClasses}>
          {t("kernel.form.scope_hint", {
            max: SCOPE_MAX_CHARS,
            count: state.scope.length,
          })}
        </span>
      </label>

      <label className={labelClasses}>
        <span>{t("kernel.form.method_preference_label")}</span>
        <input
          data-testid={`${testIdPrefix}-method-preference`}
          type="text"
          className={inputClasses}
          value={state.method_preference}
          onChange={(e) => setField("method_preference", e.target.value)}
          placeholder={t("kernel.form.method_preference_placeholder")}
        />
        <span className={hintClasses}>{t("kernel.form.optional_hint")}</span>
      </label>

      <label className={labelClasses}>
        <span>{t("kernel.form.theory_preference_label")}</span>
        <input
          data-testid={`${testIdPrefix}-theory-preference`}
          type="text"
          className={inputClasses}
          value={state.theory_preference}
          onChange={(e) => setField("theory_preference", e.target.value)}
          placeholder={t("kernel.form.theory_preference_placeholder")}
        />
        <span className={hintClasses}>{t("kernel.form.optional_hint")}</span>
      </label>

      <fieldset className="grid gap-2">
        <legend className="text-sm font-semibold text-slate-800">
          {t("kernel.form.primary_materials_legend")}
          <span className="ml-1 font-normal text-red-700">*</span>
        </legend>
        {(
          [
            { value: "yes", labelKey: "kernel.form.primary.yes" },
            {
              value: "will_upload_later",
              labelKey: "kernel.form.primary.will_upload_later",
            },
            { value: "none", labelKey: "kernel.form.primary.none" },
          ] as const
        ).map((opt) => (
          <label
            key={opt.value}
            data-testid={`${testIdPrefix}-primary-${opt.value}`}
            className="flex cursor-pointer items-center gap-2 text-sm font-normal text-slate-700"
          >
            <input
              type="radio"
              name={`${testIdPrefix}-primary-materials-status`}
              value={opt.value}
              checked={state.primary_materials_status === opt.value}
              onChange={() =>
                onChange({
                  ...state,
                  primary_materials_status: opt.value,
                })
              }
            />
            {t(opt.labelKey)}
          </label>
        ))}
      </fieldset>

      {/* Form-level disabled-reason text. Caller wires
          aria-describedby on the submit button to this element. */}
      {reasonText ? (
        <p
          id={reasonElementId}
          role="status"
          data-testid={`${testIdPrefix}-disabled-reason`}
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs leading-5 text-amber-900"
        >
          {reasonText}
        </p>
      ) : null}
      {/* Suppress unused-prop warning when language is not used here
          (passed through to ModeAvailability). */}
      {language ? null : null}
    </div>
  );
}
