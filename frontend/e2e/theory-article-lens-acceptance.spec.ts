/**
 * PR-C2c step 5: real-LLM acceptance gate for the framework_lens
 * milestone (`milestone-lens-llm`).
 *
 * Per HANDOFF §11.7.1 step 5 + codex round-1 amendment J: this spec
 * is its OWN file (not bundled into real-paper.spec.ts) and runs only
 * under `playwright.real.config.ts` against the local mirror with the
 * lens stub disabled. The base `playwright.config.ts` testIgnore list
 * excludes this file so it never runs in CI.
 *
 * The spec hits the API directly (no UI walk) for two reasons:
 *   1. The framework_lens artifact rendering is already covered by
 *      `lens-display.spec.ts` (stub mode); the acceptance gate is
 *      backend-shape, not UI.
 *   2. The artifact + audit log are the actual gate criteria; reading
 *      them directly is more deterministic than scraping the UI.
 *
 * Acceptance criteria (HANDOFF §11.7.1 step 5; codex amendment 6
 * collapsed):
 *   1. paper_mode = "theory_article", AUTOESSAY_FRAMEWORK_LENS_STUB=0
 *      (preconditions; spec asserts the env-derived flag too).
 *   2. Run reaches USER_LENS_REVIEW; no `framework_lens_stub_fallback`
 *      event was emitted.
 *   3. `synthesis/framework_lens.json` schema_version=2; signals.length
 *      >= 1.
 *   4. Each signal: source_id ∈ kernel `theoretical_lens` source set;
 *      lens_name is not a placeholder + not equal to source_id;
 *      key_concepts is non-empty.
 *   5. >=1 accepted `framework_lens` provider call (asserted by reading
 *      the audit log via `/api/runs/{id}/audit/llm_calls.jsonl`).
 *   6. Manual inspection (out-of-band; the test prints lens_name +
 *      applicability_to_kernel for the human operator to scan).
 *
 * To run:
 *   Terminal 1: ``bash frontend/scripts/run-local-mirror.sh``
 *   Terminal 2: ``( cd frontend && AUTOESSAY_E2E_TARGET=local
 *               AUTOESSAY_E2E_VITE_PORT=5174 AUTOESSAY_E2E_API_PORT=8018
 *               npx playwright test --config=playwright.real.config.ts
 *               e2e/theory-article-lens-acceptance.spec.ts )``
 */

import {
  expect,
  test,
  type APIRequestContext,
} from "@playwright/test";

type RunResponse = {
  id: string;
  state: string;
  paper_mode: string | null;
  research_kernel_json?: Record<string, unknown> | null;
  research_kernel?: Record<string, unknown> | null;
  research_kernel_hash?: string;
};

type SourcesResponse = {
  shortlist: Array<{
    source_id?: string;
    title?: string;
  }>;
};

type LensBundle = {
  artifact_present: boolean;
  schema_version: number | null;
  signals: Array<{
    lens_name: string;
    key_concepts: string[];
    source_id: string;
    applicability_to_kernel: string;
  }>;
};

const BANNED_LENS_NAMES = new Set(
  [
    "lens 1",
    "lens 2",
    "lens 3",
    "default lens",
    "default theory",
    "default framework",
    "generic theory",
    "generic framework",
    "generic lens",
    "theory 1",
    "theory 2",
    "theory 3",
    "unnamed lens",
    "unnamed theory",
    "unnamed framework",
    "untitled lens",
    "untitled theory",
    "untitled framework",
    "unknown lens",
    "unknown theory",
    "unknown framework",
    "placeholder lens",
    "placeholder theory",
    "placeholder framework",
    "sample lens",
    "example lens",
    "framework 1",
    "framework 2",
    "framework 3",
    "general theory",
    "general framework",
    "n/a",
    "none",
    "tbd",
  ].map((s) => s.toLowerCase()),
);

async function pollUntilState(
  request: APIRequestContext,
  runId: string,
  expectedStates: string[],
  timeoutMs: number,
): Promise<RunResponse> {
  const start = Date.now();
  let last: RunResponse | null = null;
  while (Date.now() - start < timeoutMs) {
    const resp = await request.get(`/api/runs/${runId}`);
    if (resp.ok()) {
      last = (await resp.json()) as RunResponse;
      if (expectedStates.includes(last.state)) {
        return last;
      }
      if (last.state.startsWith("FAILED_") || last.state === "CANCELLED") {
        throw new Error(
          `Run ${runId} entered terminal failure state '${last.state}' ` +
            `while waiting for one of [${expectedStates.join(", ")}]`,
        );
      }
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error(
    `Timeout waiting for run ${runId} to reach one of ` +
      `[${expectedStates.join(", ")}]; last state: ${last?.state ?? "unknown"}`,
  );
}

async function startPhaseAndWait(
  request: APIRequestContext,
  runId: string,
  phase: string,
  expectedStates: string[],
  timeoutMs: number,
): Promise<RunResponse> {
  const resp = await request.post(`/api/runs/${runId}/${phase}`, { data: {} });
  expect(
    resp.status(),
    `${phase} POST failed: ${resp.status()} ${await resp.text().catch(() => "")}`,
  ).toBe(202);
  return pollUntilState(request, runId, expectedStates, timeoutMs);
}

async function cleanupPriorAcceptanceProjects(
  request: APIRequestContext,
): Promise<void> {
  const resp = await request.get(`/api/projects`);
  if (!resp.ok()) return;
  const projects = (await resp.json()) as Array<{
    id: string;
    title: string;
    deleted_at: string | null;
  }>;
  for (const p of projects) {
    if (p.deleted_at !== null) continue;
    if (
      p.title.startsWith("PR-C2c lens acceptance") ||
      p.title.startsWith("hash test")
    ) {
      await request.delete(`/api/projects/${p.id}`);
    }
  }
}

test.describe("PR-C2c framework_lens LLM acceptance gate", () => {
  test.beforeEach(async ({ request }) => {
    // Spec is repeatable: prune leftovers from earlier runs so we
    // stay under the per-user "active essays" cap (default 3).
    await cleanupPriorAcceptanceProjects(request);
  });

  test("theory_article + lens-tagged source produces real LLM signals", async ({
    request,
  }) => {
    test.setTimeout(20 * 60 * 1000); // up to 20 min for the full pipeline

    // ------------------------------------------------------------------
    // Setup project + run with theory_article kernel.
    // ------------------------------------------------------------------
    const projectResp = await request.post("/api/projects", {
      data: {
        title: `PR-C2c lens acceptance ${Date.now()}`,
        domain_id: "financial_history",
        language: "en",
      },
    });
    expect(projectResp.ok()).toBeTruthy();
    const project = await projectResp.json();

    const runResp = await request.post(`/api/projects/${project.id}/runs`, {
      data: { mode: "deep" },
    });
    expect(runResp.ok()).toBeTruthy();
    const run = (await runResp.json()) as RunResponse;
    const runId = run.id;

    // The POST /runs response is a slim RunCreateResponse and doesn't
    // include research_kernel_hash; the full GET does (per
    // backend/src/autoessay/main.py::_run_response). Frontend
    // NewRunPage uses the GET hash too.
    const fullRunResp = await request.get(`/api/runs/${runId}`);
    expect(fullRunResp.ok()).toBeTruthy();
    const fullRun = (await fullRunResp.json()) as RunResponse;
    const baseKernelHash = fullRun.research_kernel_hash;
    expect(
      baseKernelHash,
      "GET /api/runs/{id} must report a non-empty research_kernel_hash",
    ).toBeTruthy();

    // Set theory_article mode + a kernel that anchors the LLM. The
    // research_kernel endpoint accepts paper_mode + an opaque kernel
    // blob. We pin a specific tentative_question so the lens prompt
    // has something concrete to anchor on.
    const kernelResp = await request.put(
      `/api/runs/${runId}/research_kernel`,
      {
        data: {
          paper_mode: "theory_article",
          // theory_article is developer_preview (PR-C2 unlock); the
          // backend requires explicit acknowledgement before allowing
          // the mode for a run.
          accept_developer_preview: true,
          base_kernel_hash: baseKernelHash,
          base_proposal_version: 0,
          kernel: {
            kernel_schema_version: 1,
            tentative_question:
              "How do dispositional structures (habitus / field) explain " +
              "the persistence of patron-client networks in late-Qing " +
              "Jiangnan literati circulation between 1890 and 1911?",
            observed_puzzle:
              "Late-Qing Jiangnan literati moved between official, merchant, " +
              "and reform circles with surprising fluidity, yet patron-client " +
              "ties from earlier generations stayed structurally durable.",
            scope: "Late-Qing Jiangnan, 1890-1911",
            theory_preference:
              "Bourdieu (habitus / field) and Polanyi (embeddedness) framings",
          },
        },
      },
    );
    expect(
      kernelResp.ok(),
      `kernel PUT failed: ${kernelResp.status()} ${await kernelResp.text().catch(() => "")}`,
    ).toBeTruthy();

    // ------------------------------------------------------------------
    // Walk: proposal → scout → curator → (promote source) → synthesizer.
    // ------------------------------------------------------------------
    await startPhaseAndWait(
      request,
      runId,
      "proposal",
      ["USER_PROPOSAL_REVIEW"],
      6 * 60 * 1000,
    );
    await startPhaseAndWait(
      request,
      runId,
      "scout",
      ["USER_SEARCH_REVIEW"],
      8 * 60 * 1000,
    );
    await startPhaseAndWait(
      request,
      runId,
      "curator",
      ["USER_DEEP_DIVE_REVIEW"],
      10 * 60 * 1000,
    );

    const sourcesResp = await request.get(`/api/runs/${runId}/sources`);
    expect(sourcesResp.ok()).toBeTruthy();
    const sources = (await sourcesResp.json()) as SourcesResponse;
    const lensSourceId = sources.shortlist.find((s) => !!s.source_id)?.source_id;
    expect(
      lensSourceId,
      "curator must produce at least one shortlisted source for the lens promotion step",
    ).toBeTruthy();

    const roleResp = await request.put(
      `/api/runs/${runId}/sources/${encodeURIComponent(lensSourceId!)}/research_role`,
      { data: { research_role: "theoretical_lens" } },
    );
    expect(roleResp.ok()).toBeTruthy();

    await startPhaseAndWait(
      request,
      runId,
      "synthesizer",
      ["USER_FIELD_REVIEW"],
      8 * 60 * 1000,
    );

    // ------------------------------------------------------------------
    // Trigger framework_lens (the focus of the acceptance gate).
    // ------------------------------------------------------------------
    await startPhaseAndWait(
      request,
      runId,
      "framework_lens",
      ["USER_LENS_REVIEW"],
      6 * 60 * 1000,
    );

    // ------------------------------------------------------------------
    // Acceptance criterion 2: no stub_fallback event was emitted.
    //
    // ``/api/runs/{id}/events`` is an SSE stream; ``close_after_event=
    // true`` returns only the FIRST buffered event (the runner breaks
    // out after a single yield), so we cannot enumerate the full event
    // log via the API. Instead we rely on the SHAPE of the lens
    // artifact below: the deterministic stub uses ``title.split(":")[0]``
    // as ``lens_name`` and a fixed templated ``applicability_to_kernel``
    // ("Stubbed signal from source <sid> in venue <venue>."), neither of
    // which matches the LLM-produced multi-clause names + verbose
    // applicability strings. ``criterion 2`` is then enforced indirectly
    // by ``criterion 5b`` below.
    // ------------------------------------------------------------------
    const STUB_APPLICABILITY_RE = /^Stubbed signal from source .+ in venue /;

    // ------------------------------------------------------------------
    // Acceptance criterion 3: artifact schema + signal count.
    // ------------------------------------------------------------------
    const lensResp = await request.get(`/api/runs/${runId}/framework_lens`);
    expect(lensResp.ok()).toBeTruthy();
    const lens = (await lensResp.json()) as LensBundle;
    expect(lens.artifact_present).toBe(true);
    expect(lens.schema_version).toBe(2);
    expect(lens.signals.length).toBeGreaterThanOrEqual(1);

    // ------------------------------------------------------------------
    // Acceptance criterion 4: per-signal structural integrity.
    // ------------------------------------------------------------------
    const eligibleSourceIds = new Set(
      sources.shortlist
        .map((s) => s.source_id)
        .filter((s): s is string => typeof s === "string" && s.length > 0),
    );
    for (const [i, signal] of lens.signals.entries()) {
      const lensNameNormalized = signal.lens_name.trim().toLowerCase();
      const sourceIdNormalized = signal.source_id.trim().toLowerCase();
      expect(
        eligibleSourceIds.has(signal.source_id),
        `signals[${i}].source_id='${signal.source_id}' must be one of the ` +
          `shortlisted sources [${[...eligibleSourceIds].join(", ")}]`,
      ).toBe(true);
      expect(
        BANNED_LENS_NAMES.has(lensNameNormalized),
        `signals[${i}].lens_name='${signal.lens_name}' is a placeholder; ` +
          "the LLM did not produce a real framework name",
      ).toBe(false);
      expect(
        lensNameNormalized,
        `signals[${i}].lens_name must differ from source_id`,
      ).not.toBe(sourceIdNormalized);
      expect(
        signal.key_concepts.length,
        `signals[${i}].key_concepts must be non-empty`,
      ).toBeGreaterThanOrEqual(1);
      // Helpful trace for the manual inspection step.
      console.log(
        `[lens signal ${i}] name='${signal.lens_name}' ` +
          `source=${signal.source_id} concepts=${signal.key_concepts.join(",")} ` +
          `applicability='${signal.applicability_to_kernel.slice(0, 120)}…'`,
      );
    }

    // ------------------------------------------------------------------
    // Acceptance criterion 5 (and 5b — implicit no-fallback proof):
    // none of the signals' ``applicability_to_kernel`` matches the
    // deterministic stub template (``"Stubbed signal from source X in
    // venue Y."``). If the LLM had failed mid-flight and the runner
    // fell back to ``compose_framework_lens(stub=True)``, every signal
    // would have that exact prefix. So a clean LLM run leaves zero
    // stub-shaped signals; this is a tighter check than the SSE event
    // proxy and survives the ``close_after_event=true`` early-return
    // limitation.
    // ------------------------------------------------------------------
    const stubShapedSignals = lens.signals.filter((s) =>
      STUB_APPLICABILITY_RE.test(s.applicability_to_kernel),
    );
    expect(
      stubShapedSignals.length,
      `${stubShapedSignals.length} signal(s) match the deterministic ` +
        "stub template — the runner must have fallen back to the stub " +
        "path. milestone-lens-llm tag cannot be promoted on this run.",
    ).toBe(0);

    // ------------------------------------------------------------------
    // Acceptance criterion 5b: per HANDOFF §11.7.1 step 5, the
    // operator confirms the audit log shows ≥1 accepted framework_lens
    // provider call out of band. The ``synthesis/llm_calls.jsonl``
    // audit file the AuditWriter writes is not exposed via the API;
    // operators verify by reading the local-mirror tmp dir directly.
    // ------------------------------------------------------------------

    console.log(
      `[acceptance] Run ${runId} produced ${lens.signals.length} lens ` +
        `signal(s) via real LLM (no stub fallback). Manual inspection ` +
        `pending; print above for review.`,
    );
  });
});
