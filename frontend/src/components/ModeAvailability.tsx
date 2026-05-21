// PR-C0.b2.ui: paper-mode picker. Radio cards driven by
// GET /api/paper_modes. Renders ``available`` modes selectable,
// ``developer_preview`` modes also selectable but with required ack
// (PR-C0.b2.tests UX change), ``coming_soon`` disabled with
// accessible reason text (codex round-1.b2.ui amendment 6:
// aria-describedby, not just tooltip).
//
// Pure-decision logic lives in `lib/kernelValidation.ts`
// (modeAvailabilityState, modeSpecOrFallback) so vitest covers it
// without React/jsdom rendering. This component is a thin render
// of those decisions; user-visible text comes from i18n.

import type { ProjectLanguage } from "../lib/api";
import { useT } from "../lib/i18n";
import {
  FALLBACK_MODE_SPEC,
  modeAvailabilityState,
  type PaperModeSpec,
} from "../lib/kernelValidation";

type Props = {
  modes: PaperModeSpec[] | null;
  selectedModeId: string;
  onModeChange: (id: string) => void;
  acceptDeveloperPreview: boolean;
  onAckChange: (b: boolean) => void;
  language: ProjectLanguage;
  /** When true, render the current mode as a static read-only pill
   * instead of the radio picker. Used by the workspace edit modal
   * after proposal_version >= 1 (mode-change blocked by backend). */
  readOnly?: boolean;
  /** Stable testid prefix, so NewRunPage and the workspace modal
   * can share this component without testid collisions. */
  testIdPrefix: string;
};

export function ModeAvailability(props: Props) {
  const {
    modes,
    selectedModeId,
    onModeChange,
    acceptDeveloperPreview,
    onAckChange,
    language,
    readOnly,
    testIdPrefix,
  } = props;
  const t = useT();

  // Round-1.b2.ui amendment 1: when API returned no modes, fall
  // back to a single case_analysis pseudo-spec so the form is
  // still submittable in degraded state.
  const visibleModes = modes && modes.length > 0 ? modes : [FALLBACK_MODE_SPEC];
  const isDegraded = !modes || modes.length === 0;

  const labelOf = (m: PaperModeSpec): string =>
    language === "zh"
      ? m.label_zh
      : language === "ja"
        ? m.label_ja
        : m.label_en;
  const descriptionOf = (m: PaperModeSpec): string =>
    language === "zh"
      ? m.description_zh
      : language === "ja"
        ? m.description_ja
        : m.description_en;

  if (readOnly) {
    const current =
      visibleModes.find((m) => m.mode_id === selectedModeId) ??
      FALLBACK_MODE_SPEC;
    return (
      <div className="grid gap-2">
        <p className="text-sm font-semibold text-slate-800">
          {t("paper_mode.legend")}
        </p>
        <span
          data-testid={`${testIdPrefix}-readonly-pill`}
          className="inline-flex w-fit items-center rounded-full border border-slate-300 bg-slate-50 px-3 py-1 text-sm font-semibold text-slate-700"
        >
          {labelOf(current)}
        </span>
        <p className="text-xs text-slate-500">
          {t("paper_mode.readonly_hint")}
        </p>
      </div>
    );
  }

  return (
    <fieldset className="grid gap-3" data-testid={`${testIdPrefix}-fieldset`}>
      <legend className="text-sm font-semibold text-slate-800">
        {t("paper_mode.legend")}
      </legend>
      {isDegraded ? (
        <p
          className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900"
          role="status"
          data-testid={`${testIdPrefix}-degraded-banner`}
        >
          {t("paper_mode.degraded_banner")}
        </p>
      ) : null}
      <div className="grid gap-2 md:grid-cols-2">
        {visibleModes.map((mode) => {
          const avail = modeAvailabilityState(mode, acceptDeveloperPreview);
          const checked = mode.mode_id === selectedModeId;
          const disabled = !avail.selectable;
          const reasonId = `${testIdPrefix}-${mode.mode_id}-reason`;
          const reasonText = avail.reason
            ? t(avail.reason.key, avail.reason.vars)
            : "";
          return (
            <label
              key={mode.mode_id}
              data-testid={`${testIdPrefix}-mode-${mode.mode_id}`}
              data-status={mode.status}
              className={
                "grid cursor-pointer gap-1 rounded-md border p-3 text-sm transition " +
                (checked
                  ? "border-[#114b5f] bg-[#114b5f]/5 ring-2 ring-[#114b5f]/20"
                  : disabled
                    ? "cursor-not-allowed border-slate-200 bg-slate-50 text-slate-500"
                    : "border-slate-300 bg-white hover:border-slate-400")
              }
            >
              <span className="flex items-baseline gap-2">
                <input
                  type="radio"
                  name={`${testIdPrefix}-mode`}
                  value={mode.mode_id}
                  checked={checked}
                  disabled={disabled}
                  onChange={() => onModeChange(mode.mode_id)}
                  aria-describedby={reasonText ? reasonId : undefined}
                  className="mt-0.5"
                  data-testid={`${testIdPrefix}-radio-${mode.mode_id}`}
                />
                <span className="font-bold">{labelOf(mode)}</span>
                {mode.status === "developer_preview" ? (
                  <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-bold text-amber-900">
                    {t("paper_mode.status.preview")}
                  </span>
                ) : null}
                {mode.status === "coming_soon" ? (
                  <span className="rounded bg-slate-200 px-1.5 py-0.5 text-xs font-bold text-slate-600">
                    {t("paper_mode.status.coming_soon")}
                  </span>
                ) : null}
              </span>
              <p className="ml-6 text-xs leading-5 text-slate-700">
                {descriptionOf(mode)}
              </p>
              {reasonText ? (
                <p
                  id={reasonId}
                  className="ml-6 text-xs italic text-slate-500"
                  role="status"
                >
                  {reasonText}
                </p>
              ) : null}
            </label>
          );
        })}
      </div>
      {/* Developer-preview ack — only render when the currently
          selected mode requires it. With the PR-C0.b2.tests UX
          change, the preview mode is now always selectable, so
          ack-row appears as soon as the user clicks the preview
          radio. */}
      {(() => {
        const selectedSpec = visibleModes.find(
          (m) => m.mode_id === selectedModeId,
        );
        if (!selectedSpec || selectedSpec.status !== "developer_preview") {
          return null;
        }
        return (
          <label
            className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900"
            data-testid={`${testIdPrefix}-ack-row`}
          >
            <input
              type="checkbox"
              checked={acceptDeveloperPreview}
              onChange={(e) => onAckChange(e.target.checked)}
              className="mt-0.5"
              data-testid={`${testIdPrefix}-ack-checkbox`}
            />
            <span>{t("paper_mode.ack_text")}</span>
          </label>
        );
      })()}
    </fieldset>
  );
}
