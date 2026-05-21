import { useT, useUILanguage } from "../lib/i18n";

// Each `screenshot` is the *filename* under
// `public/help-assets/<lang>/`. The active UI language picks the
// per-locale variant at render time so a Chinese user sees Chinese
// screenshots, etc. Files are produced by
// `npm run capture:help` (see e2e/capture-screenshots.spec.ts).
const SECTIONS: { id: string; screenshot?: string }[] = [
  { id: "getting-started", screenshot: "05-runs-list.png" },
  { id: "creating-essay", screenshot: "06-new-run-form.png" },
  {
    id: "review-steps",
    screenshot: "10-01-after-Generate-Initial-Proposal.png",
  },
  { id: "workspace-states", screenshot: "workspace-states.png" },
  { id: "mid-flight-edits", screenshot: "08-workspace-loaded.png" },
  {
    id: "final-review",
    screenshot: "10-10-after-Accept-integrity-findings.png",
  },
  { id: "exports-downloads", screenshot: "11-exports-done-cta.png" },
  { id: "managing-essays" },
  { id: "authors" },
  { id: "troubleshooting" },
  { id: "privacy-data" },
];

export default function HelpPage() {
  const t = useT();
  const [lang] = useUILanguage();
  return (
    <section className="grid gap-6">
      <div className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm sm:p-6">
        <h1 className="text-2xl font-bold text-slate-950 sm:text-3xl">
          {t("help.heading")}
        </h1>
        <p className="mt-2 leading-7 text-slate-700">{t("help.subtitle")}</p>
      </div>

      <ul className="grid list-none gap-2 p-0">
        {SECTIONS.map((section) => (
          <li key={section.id}>
            <a
              href={`#${section.id}`}
              className="text-sm font-semibold text-[#114b5f] no-underline hover:underline"
            >
              {t(`help.${section.id}.title`)}
            </a>
          </li>
        ))}
      </ul>

      {SECTIONS.map((section) => {
        const callout = t(`help.${section.id}.callout`);
        const hasCallout = callout && callout !== `help.${section.id}.callout`;
        return (
          <article
            key={section.id}
            id={section.id}
            className="rounded-lg border border-slate-200 bg-white p-5 shadow-sm sm:p-6"
          >
            <h2 className="text-xl font-bold text-slate-950">
              {t(`help.${section.id}.title`)}
            </h2>
            <p className="mt-2 whitespace-pre-line leading-7 text-slate-700">
              {t(`help.${section.id}.body`)}
            </p>
            {hasCallout ? (
              <p className="mt-3 rounded-md border-l-4 border-[#114b5f] bg-slate-50 px-3 py-2 leading-7 text-slate-700">
                {callout}
              </p>
            ) : null}
            {section.screenshot ? (
              <img
                src={`/help-assets/${lang}/${section.screenshot}`}
                alt={t(`help.${section.id}.screenshot_alt`)}
                className="mt-4 w-full rounded-md border border-slate-200"
              />
            ) : null}
          </article>
        );
      })}
    </section>
  );
}
