/**
 * HelpPage must use plain user-facing language only.
 *
 * Codex-AGREEd #7 banned the following technical terms from any
 * user-visible Help page string in zh / en / ja: agent, checkpoint,
 * harness, state machine, RQ, sandbox, baseline_hash, claim_map,
 * source_id, re-trigger, LLM, LLMs, permanent purge.
 *
 * The test renders HelpPage three times (one per UI language) and
 * asserts the rendered HTML contains none of those terms.
 */
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

import { setUILanguage, type UILanguage } from "../lib/i18n";

import HelpPage from "./HelpPage";

const BANNED_TERMS: RegExp[] = [
  /\bagent\b/i,
  /\bagents\b/i,
  /\bcheckpoint\b/i,
  /\bcheckpoints\b/i,
  /\bharness\b/i,
  /\bstate machine\b/i,
  /\bRQ\b/,
  /\bsandbox\b/i,
  /\bbaseline_hash\b/i,
  /\bclaim_map\b/i,
  /\bsource_id\b/i,
  /\bre-?trigger\b/i,
  /\bLLM\b/,
  /\bLLMs\b/,
  /\bpermanent purge\b/i,
];

const LANGUAGES: UILanguage[] = ["en", "zh", "ja"];

afterEach(() => {
  setUILanguage("en");
});

function renderHelp(): string {
  return renderToString(
    <MemoryRouter initialEntries={["/help"]}>
      <HelpPage />
    </MemoryRouter>,
  );
}

describe("HelpPage plain-language guarantee", () => {
  for (const lang of LANGUAGES) {
    it(`uses no banned technical terms in ${lang}`, () => {
      setUILanguage(lang);
      const html = renderHelp();
      // Strip HTML tags so a regex like /\bagent\b/ won't match a
      // class name like "data-agent" embedded in markup.
      const text = html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
      for (const pattern of BANNED_TERMS) {
        expect(
          text,
          `HelpPage rendered in ${lang} should not contain ${pattern}`,
        ).not.toMatch(pattern);
      }
    });
  }
});
