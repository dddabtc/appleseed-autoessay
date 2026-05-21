# A/B/B'/C Architecture Comparison Protocol

> Amended by `docs/adr/0002-experiment-execution-revisions.md` (2026-05-16):
> - GENERATION_MODEL_ID is now an abstract label; actual model recorded in provenance
> - PROVIDER_FALLBACK_ALLOWED = True (was False) per user directive
> - Driver runs in isolated dev stack; production environment is not touched
> - Concurrency: kernel-level max 3, default 1
>
> P0 reset amendment (2026-05-16):
> - All results generated before this amendment are exploratory-only.
> - Non-dry-run execution must pin both `AUTOESSAY_EXPERIMENT_ABC_PRODUCTION_SHA`
>   and `AUTOESSAY_EXPERIMENT_ABC_SCRIPT_SHA`.
> - Token cap is hard-enforced at `1800000`; an over-cap arm is not accepted
>   into the decision set.
> - The 3-judge quorum is codex `gpt-5.5 xhigh`, apiport `gpt-5.4`,
>   and minimax `MiniMax-M2.7`. Anthropic and Gemini keys were unavailable
>   in the executable environment, so the P0 rerun uses three available
>   independent model/provider paths instead of manual fallback.
> - 2026-05-17 rejudge amendment: the apiport judge must use full `gpt-5.4`
>   or full `gpt-5.5`; mini variants are invalid for P0 scoring.

Status: P0 reset protocol, binding for rerun

This protocol is binding for the first architecture comparison. If the protocol changes after manuscript generation begins, the experiment must be restarted or marked invalid.

## 1. Experiment Question

Does the current 13-phase production pipeline produce better manuscripts than evidence-first composer variants using the same front-half evidence package?

The key comparisons are:

- A vs B tests whether the middle and late prose-production phases beat a no-critic evidence-first single-shot manuscript.
- B vs B' estimates the value of one bounded self-critique pass after the single-shot manuscript.
- A vs B' tests whether A's advantage, if any, is more than "one self-critique pass."

C remains a control arm:

- If C matches A, B, and B', either retrieval/evidence is not adding visible value, or the judge protocol cannot measure evidence use.
- C does not decide the evidence-first refactor by itself. It diagnoses whether evidence handling is measurable.

For the user-facing binary decision "13-phase optimization vs direct LLM
output", the direct-LLM comparator is Arm C's no-retrieval single-shot prompt.
The final P0 verdict must report this as `Direct` even if the artifact path
keeps the historical `C` label.

## 2. Arms

### Arm A: Production 13-phase Pipeline

A is the incumbent.

Implementation rules:

- Use the production run path unchanged.
- Use the same phase sequence and current production defaults:

```text
proposal -> scout -> curator -> synthesizer -> tension_extraction
-> framework_lens -> ideator -> drafter -> stylist -> final_rewrite
-> critic -> integrity -> exports
```

- Do not disable `final_rewrite`.
- Do not disable `polish_loop`.
- Do not disable `critic_loop`.
- Do not change prompts, schemas, state transitions, retry counts, or phase order for the experiment.
- Pin A's production commit SHA at experiment start. All A runs must use that exact SHA. Any production-path PR merged during the experiment window does not apply to A's experiment runs.
- `auto_advance` may be enabled to avoid manual gates, but it must be recorded in `provenance.json`.
- A's submitted manuscript is the Markdown text at `run_dir/exports/manuscript.md` after the full pipeline reaches `EXPORTS_DONE`.
- If `exports` fails but `critic` selected a best candidate, A's submitted manuscript is the critic-selected candidate text. This fallback must be recorded as `submitted_manuscript_source = "critic_selected"`.
- If neither `exports_done` nor `critic_selected` is available, A is not judgeable for that kernel and the failure is recorded in provenance.

Allowed operational setup:

- Pin model/provider and token accounting through environment configuration.
- Run on a staging database or local experiment database.
- Use existing production code paths for run creation, phase execution, integrity, and export.

Not allowed:

- Any kernel-specific prompt override.
- Any post-hoc manual edit.
- Any code change that only benefits A.

### Arm B: Evidence-first Single-shot from A Front-half Outputs

B tests whether the front half of the workflow plus one global writing call beats the current middle and late phase chain without any self-critique quality repair.

B input package:

- Project title.
- Research kernel.
- Target journal.
- A's `scout` output.
- A's `curator` shortlist.
- A's `synthesizer` claims and source notes.
- A's `tension_extraction` output if present.
- A's `framework_lens` output if present.

The source portion of B's package must be the same approved source pool A made available to downstream drafting: no extra sources, no manual substitutions, and no post-final citations copied from A's manuscript.

B must not consume:

- A's `ideator` output.
- A's `drafter` output.
- A's `stylist` output.
- A's `final_rewrite` output.
- A's `critic` output.
- A's `integrity` report.
- A's `exports` output.
- Any phase logs, state names, failure reasons, or judge outputs.

B generation flow:

```text
A front-half artifacts -> evidence package -> single manuscript prompt
-> one strong LLM call -> deterministic compliance repair pass
-> submitted manuscript freeze -> non-mutating integrity + external_scan audit
```

Compliance repair scope:

- One pass only.
- Allowed repairs: citation marker normalization, reference-list alignment, removal of unsupported sentinel markers, and mechanical CNKI-structure completion if headings are malformed.
- Compliance repair must be deterministic code: regex, parser, structured validator, or other rule-based normalization.
- No LLM call is allowed in the B compliance repair pass. If a malformed structure cannot be deterministically fixed, submit the manuscript as produced and record the blocker in `provenance.json`.
- Not allowed: holistic quality rewrite, polish loop, paired critic loop, multi-iteration repair plan, or judge-driven revision.
- B's `integrity` and `external_scan` steps are audits only. They may record blockers, but they must not rewrite the manuscript or feed findings into another revision call.

The B prompt must be global:

- The model sees the whole evidence package and writes the full manuscript in one call.
- The prompt must ask for a complete Chinese academic paper body plus abstract, keywords, and references.
- The prompt must require citations to use only the provided source ids.
- The prompt must include the same humanizer directive used by A.

### Arm B': Evidence-first Single-shot plus One Self-critique Pass

B' tests the reviewer confound: if A beats B, is the gap caused by A's multi-phase architecture or simply by A receiving a self-review loop?

B' input package:

- The exact B evidence package for the same kernel.
- The B manuscript after deterministic compliance repair.
- The same humanizer directive used by A and B.

B' must not consume:

- A's `ideator`, `drafter`, `stylist`, `final_rewrite`, `critic`, `integrity`, or `exports` output.
- B's `integrity` or `external_scan` audit output.
- Any judge output, score, threshold result, or manual review note.

B' generation flow:

```text
B evidence package + B repaired manuscript
-> one self-critique-and-targeted-revision LLM call
-> deterministic compliance repair pass
-> submitted manuscript freeze -> non-mutating integrity + external_scan audit
```

B' self-critique constraints:

- Exactly one post-manuscript LLM call is allowed.
- The prompt must ask for targeted local fixes based on the same evidence package, not a fresh whole-paper rewrite.
- The model may revise claims, citations, section ordering, or connective tissue only when the change is supported by the provided source package.
- No iterative critic loop, repair-plan loop, paired candidate selection, external judge feedback, or manual edit is allowed.
- B' provenance must record `base_b_manuscript_sha256`, `self_critique_prompt_sha256`, token usage for the self-critique call, and whether deterministic post-repair changed bytes.

### Arm C: Naked Single-shot Baseline

C measures what a strong model can do without retrieval.

C input:

- Project title.
- Research kernel.
- Target journal style.
- Same humanizer directive.

C generation flow:

```text
title + research kernel + target journal -> one strong LLM call
-> submitted manuscript
```

C must not use:

- Retrieval.
- Source shortlist.
- Synthesizer claims.
- Tension output.
- Framework lens output.
- Critic output.
- Integrity repair.
- External scan.
- A, B, or B' manuscripts.

C may include references generated from model knowledge, but judges will score citation alignment and evidence quality from the manuscript only. If C hallucinates citations, that is part of the measurement.

## 3. Control Variables

| Variable | Committed setting |
|---|---|
| Generation model id | `provider-configured-fallback-chain`; actual provider/model recorded in provenance |
| Production commit SHA | Must be explicitly pinned in `AUTOESSAY_EXPERIMENT_ABC_PRODUCTION_SHA`; record it in every provenance file and in `results/manifest.json` |
| Experiment script SHA | Must be explicitly pinned in `AUTOESSAY_EXPERIMENT_ABC_SCRIPT_SHA`; record it in every provenance file and in `results/manifest.json` |
| Provider pinning | Use the configured rightcode -> apiport -> minimax provider chain per ADR 0002; record actual provider/model for each arm |
| Token cap | `1800000` provider-reported total tokens per kernel per arm, counting prompt plus completion |
| Manuscript call completion cap | `25000` max completion tokens for B and C manuscript calls; B' uses one additional self-critique call capped at `25000`; A uses existing per-phase caps but total usage must stay within the same `1800000` cap |
| Time window | Generate all 40 manuscripts within 1-2 calendar days |
| Source pool | A uses native retrieval; B and B' use the exact A front-half source package; C uses no source pool |
| Humanizer directive | Use `autoessay.agents._humanizer.humanizer_directive("zh")` for all manuscript-generating calls |
| Paper language | Chinese |
| Mathematical mode | Off unless every arm can use the same mode and the kernel explicitly requires it; v1 default is off |
| Manual edits | Forbidden before judging |
| Prompt tuning on kernels | Forbidden |

Token cap handling:

- Token usage must be recorded from provider response usage when available.
- For B, cost accounting includes the shared front-half A token usage through `framework_lens` plus B-specific manuscript and repair usage. This estimates what B would cost if deployed as the default evidence-first workflow.
- For B', cost accounting includes the shared front-half A token usage through `framework_lens`, the B manuscript call, and the B' self-critique call. This estimates what an evidence-first workflow with one self-critique pass would cost if deployed.
- The original `400000` cap was invalidated by exploratory A runs near or above 1M tokens. The P0 rerun uses `1800000` so A can be quality-tested while still preventing runaway cost.
- If an arm exceeds `1800000` tokens on a kernel, the runner must fail that arm and keep it out of the accepted decision set.
- Accepted submissions must have `token_usage.budget_exceeded = false`; budget failures are reported as operational failures, not scored as quality manuscripts.

Provider failure handling:

- Use the ADR 0002 provider fallback chain during generation.
- Record actual provider and model in every provenance file.
- If B and B' use different provider/model pairs for the same kernel, downgrade that kernel's B vs B' reading as a provider/model confound.

Time-window handling:

- Run order should be randomized by kernel and arm where practical.
- A must finish its front-half package before B for the same kernel.
- B must finish before B' for the same kernel.
- C should be interleaved with A/B/B' generation to reduce model-service drift.

## 4. Implementation Interface

### Package Layout

Create a new experiment-only subpackage:

```text
backend/src/autoessay/experiments/
  __init__.py
  abc_architecture.py
  abc_extract.py
  abc_prompts.py
  abc_judge_schema.py
```

Rationale:

- Keep experiment code out of production phase modules.
- Avoid adding experiment flags to production `Settings` unless the implementation genuinely needs an env override.
- Make deletion or promotion easy after the result.

### Settings

Use production `Settings` for gateway, provider, database, and data directory.

Add experiment-specific constants in `autoessay.experiments.abc_architecture` rather than expanding `Settings` for v1:

```python
EXPERIMENT_ID = "abc-architecture-comparison-v1"
GENERATION_MODEL_ID = "gpt-5.5"
TOKEN_CAP_TOTAL = 400_000
MANUSCRIPT_MAX_TOKENS = 25_000
SELF_CRITIQUE_MAX_TOKENS = 25_000
PROVIDER_FALLBACK_ALLOWED = False
```

If implementation needs env overrides, use namespaced env vars only:

```text
AUTOESSAY_EXPERIMENT_ABC_MODEL
AUTOESSAY_EXPERIMENT_ABC_TOKEN_CAP
AUTOESSAY_EXPERIMENT_ABC_RESULTS_DIR
```

Do not change production defaults for A.

### Database

Do not add a database schema migration for v1.

Decision:

- A uses the existing `runs` table because it is a real production run.
- B, B', and C are file-artifact experiment submissions, not production runs.
- Experiment metadata lives in `results/manifest.json` and per-arm `provenance.json`.

Reason:

- This experiment must not mutate production schema before the architecture decision.
- The current `Run` model does not have a `phase_outputs` JSON column; artifacts live in run directories and `phase_versions`.
- A schema change would add review surface without improving the experiment.

Promotion rule:

- If evidence-first wins and becomes a product path, add a real DB model in a later refactor PR.
- At that point, prefer a separate `experiment_runs` or successor `manuscript_runs` table over adding temporary columns to `runs`.

### Extracting A Front-half Outputs for B and B'

Use a dump script, not DB JSON columns.

Expected script:

```text
scripts/abc-run.py dump-front-half --run-id {a_run_id} --kernel-id {kernel_id}
```

The dump reads canonical artifacts from A's `run_dir`:

```text
discovery/scout_report.md
sources/shortlist.json
synthesis/claims.jsonl
synthesis/synthesizer.json
synthesis/tension_extraction.json
synthesis/framework_lens.json
```

If an optional artifact does not exist because the phase is disabled or skipped, record:

```json
{"present": false, "reason": "missing_or_skipped"}
```

The dump must write:

```text
results/{kernel_id}/front_half/package.json
results/{kernel_id}/front_half/package.md
results/{kernel_id}/front_half/package.sha256
```

The B and B' prompts must consume `package.md` and must cite `package.sha256` in provenance.

### Running B, B', and C

Expected script:

```text
scripts/abc-run.py generate --kernel-id {kernel_id} --arm B
scripts/abc-run.py generate --kernel-id {kernel_id} --arm B_prime
scripts/abc-run.py generate --kernel-id {kernel_id} --arm C
```

B output:

```text
results/{kernel_id}/B/manuscript.md
results/{kernel_id}/B/provenance.json
results/{kernel_id}/B/prompt.redacted.txt
```

B' output:

```text
results/{kernel_id}/B_prime/manuscript.md
results/{kernel_id}/B_prime/provenance.json
results/{kernel_id}/B_prime/prompt.redacted.txt
```

C output:

```text
results/{kernel_id}/C/manuscript.md
results/{kernel_id}/C/provenance.json
results/{kernel_id}/C/prompt.redacted.txt
```

A output should be copied into the same structure after export:

```text
results/{kernel_id}/A/manuscript.md
results/{kernel_id}/A/provenance.json
```

The A copy rule is binding:

- If A reaches `EXPORTS_DONE`, copy the exact bytes from `run_dir/exports/manuscript.md` and set `submitted_manuscript_source = "exports_done"`.
- If `exports` fails but `critic` selected a best candidate, copy that candidate text and set `submitted_manuscript_source = "critic_selected"`.
- Any other source must be recorded as `submitted_manuscript_source = "other"` with a one-sentence reason; this should be treated as a protocol exception in aggregation.

### Blinding

Expected script:

```text
scripts/abc-build-blindset.py --results-dir docs/experiments/abc-architecture-comparison/results
```

The script creates random UUID filenames:

```text
results/{kernel_id}/blinded/{submission_uuid}/manuscript.md
results/blind_map.json
```

`blind_map.json` must not be shown to judges until all judging is complete.

The blinded manuscript must remove:

- arm label;
- run id;
- phase names;
- state names;
- prompt names;
- provenance;
- generation timestamp;
- any system marker that reveals A/B/B'/C.

The blinded manuscript may keep:

- the manuscript title if it is part of the generated text;
- abstract;
- keywords;
- body;
- footnotes if generated;
- references.

### Judging

Expected script:

```text
scripts/abc-judge.py --judge codex-gpt-5.5-xhigh --submission {submission_uuid}
scripts/abc-judge.py --judge apiport-gpt-5.4 --submission {submission_uuid}
scripts/abc-judge.py --judge minimax-m2.7 --submission {submission_uuid}
```

If a live judge credential or provider adapter is unavailable, the script must
raise and fail the run. Explicit `--manual-only` may be used only to prepare
debug inputs outside the P0 decision run.

Manual judge-input files are non-decision debug artifacts only.

## 5. Judge Protocol

Judges:

- codex, model label `gpt-5.5-xhigh`, reasoning effort `xhigh`.
- apiport, model label `gpt-5.4` full; mini variants are not accepted for P0 scoring.
- minimax, model label `MiniMax-M2.7`.

Judge independence:

- Each judge scores each submission independently.
- Judges do not discuss scores before all JSON files are submitted.
- Judges do not see other judges' outputs.
- Judges do not see `blind_map.json`.

Judge input:

- One blinded manuscript at a time.
- The judge sees only the manuscript text and references.
- The judge does not see the original kernel, arm, phase logs, state machine, source package, prompt, provenance, or run timing.

Judge prompt template:

System prompt:

```text
You are an independent blind reviewer for a Chinese academic manuscript
quality experiment. Score only the manuscript shown to you. Do not infer
which experimental arm produced it. Do not reward plausible intent that is
not visible in the text. Use the JSON schema exactly.

Evidence rule: score citation and evidence quality from manuscript-internal
claims, citation markers, and reference entries only. A claim is supported
only when the manuscript gives enough citation context for a reader to trace
the claim to a listed reference. Hallucinated, missing, or mismatched
references lower citation_alignment even if the prose is fluent.
```

User prompt:

```text
Submission UUID: {submission_uuid}

Read the blinded manuscript below and return one JSON object matching
abc_architecture_judge_v1. Score compliance, novelty, and completeness on
1-10 scales. Then score all 13 ledger items. Use short manuscript anchors
as evidence. If the manuscript is unreadable or too incomplete to score, set
validity.can_score=false and explain why.

<MANUSCRIPT>
{blinded_manuscript_markdown}
</MANUSCRIPT>
```

Ledger boundary conditions:

- `citation_alignment` max 4: 4 means citations consistently support substantive claims and match the reference list; 2 means mixed support with several weak, vague, or mismatched citations; 0 means citation use is mostly missing, hallucinated, or unusable.
- Max-3 items: 3 means the requirement is fully present and coherent; 2 means usable but incomplete; 1 means fragmentary; 0 means absent or misleading.
- Max-2 items: 2 means the requirement is clearly satisfied; 1 means partial or inconsistent; 0 means absent, contradicted, or only asserted without visible textual evidence.
- `validity.can_score=false` is reserved for empty, corrupted, non-Chinese, or severely truncated submissions. Do not use it merely because the paper is weak.

Judge output has two layers.

Layer 1: overall quality scores, each `1-10`:

- `compliance`: citation alignment, format, no sentinels, academic voice.
- `novelty`: visible contribution beyond generic summary.
- `completeness`: full paper structure, argument continuity, evidence-to-conclusion closure.

Layer 2: item ledger, reusing the `paired_blind_box_ledger_v1` item ids and max points:

| Dimension | Item id | Max |
|---|---|---:|
| compliance | `citation_alignment` | 4 |
| compliance | `no_sentinels` | 2 |
| compliance | `cnki_format` | 2 |
| compliance | `academic_voice` | 2 |
| novelty | `new_material` | 2 |
| novelty | `new_perspective` | 2 |
| novelty | `new_method` | 2 |
| novelty | `new_question` | 2 |
| novelty | `new_argument` | 2 |
| completeness | `eight_sections` | 3 |
| completeness | `claim_evidence_conclusion` | 2 |
| completeness | `abstract_keywords_refs` | 2 |
| completeness | `cross_section_coherence` | 3 |

Required judge JSON:

```json
{
  "schema_version": "abc_architecture_judge_v1",
  "judge_id": "codex-gpt-5.5-xhigh",
  "submission_uuid": "00000000-0000-0000-0000-000000000000",
  "validity": {"can_score": true, "reason": null},
  "overall_scores": {
    "compliance": 1.0,
    "novelty": 1.0,
    "completeness": 1.0
  },
  "ledger": [
    {
      "id": "citation_alignment",
      "max": 4,
      "points": 0,
      "reason_code": "SUPPORTED",
      "evidence": ["short manuscript anchor"],
      "brief_reason": "one sentence"
    }
  ],
  "residual_risks": ["optional short notes"],
  "confidence": "high"
}
```

Ledger constraints:

- Every judge must score all 13 items.
- `points` may use half points.
- Every item needs at least one evidence anchor or `MISSING: ...`.
- Overall scores are independent `1-10` judgments, not a mechanical conversion from ledger points.

## 6. Aggregation

Definitions:

- `dimension_score(k, arm, dim)`: median of the 3 judges' overall score for a kernel, arm, and dimension.
- `overall_score(k, arm)`: arithmetic mean of the three `dimension_score` values.
- `arm_median(arm)`: median of `overall_score(k, arm)` across the 10 kernels.
- `dimension_median(arm, dim)`: median of `dimension_score(k, arm, dim)` across the 10 kernels.
- `kernel_win(arm_x, arm_y, k)`: `overall_score(k, arm_x) - overall_score(k, arm_y) >= 0.5`.
- `tie_band`: absolute difference `< 0.3`.
- `small_loss`: difference from `-0.5` inclusive to `< -0.3`.
- `stable_win`: difference `>= 0.5`.

Disagreement reporting:

- For each submission and dimension, report `max_judge_score - min_judge_score`.
- Mark `high_disagreement = true` when that spread is `>= 2.0`.
- `conclusion.md` must list every kernel where any arm has high disagreement.
- High disagreement does not invalidate the experiment by itself, but it weakens any roadmap claim based on that kernel.

## 7. Confounding-factor Defense Checklist

Before generation:

- [ ] Kernel list excludes bretton, jiangnan, wang, and prompt-tuned variants.
- [ ] No kernel-specific prompt edits exist.
- [ ] Generation model id is pinned to `gpt-5.5`.
- [ ] A production commit SHA and B/B'/C experiment script SHA are frozen and recorded.
- [ ] Provider fallback follows ADR 0002 and actual provider/model is recorded.
- [ ] Token cap is configured at `1800000` and token usage is recorded.
- [ ] Humanizer directive is identical across A/B/B'/C manuscript-generation calls.
- [ ] A runs in production path without experiment-only phase changes.
- [ ] A submitted manuscript source is recorded as `exports_done`, `critic_selected`, or `other`.

Before B generation:

- [ ] B package hash is written before any A manuscript, rewrite, critic, integrity, or export artifact is copied into experiment results.
- [ ] B package contains only allowed front-half artifacts.
- [ ] B source package is the same source pool A made available to downstream drafting.
- [ ] A's final cited sources are audited against the same source pool; any extra source use is recorded as a production-path confound.
- [ ] B prompt does not contain phase names or logs beyond neutral artifact labels.
- [ ] B cannot inspect A final manuscript.
- [ ] B compliance repair is deterministic code with no LLM call.

Before B' generation:

- [ ] B' input uses only the B evidence package and B repaired manuscript.
- [ ] B' cannot inspect A late artifacts, B audit outputs, judge outputs, or manual notes.
- [ ] B' has exactly one self-critique-and-targeted-revision LLM call.
- [ ] B' deterministic post-repair is no-LLM and byte changes are recorded.

Before C generation:

- [ ] C prompt contains no retrieved source list.
- [ ] C prompt contains no A, B, or B' artifact.
- [ ] C prompt contains title, research kernel, target journal style, and humanizer only.

Before judging:

- [ ] Blinded files use random UUIDs.
- [ ] Judge prompts do not reveal arm labels.
- [ ] Judges cannot see phase logs, state-machine states, provenance, token usage, run time, or prompt text.
- [ ] Judges see manuscript text and references only.
- [ ] `blind_map.json` is not accessible to judges.
- [ ] `docx` rendering success is not included in judge input.
- [ ] Export speed is not included in judge input.

During aggregation:

- [ ] Use median across judges, not mean, for dimension scores.
- [ ] Report disagreement.
- [ ] Report budget-exceeded arms.
- [ ] Report completion failures separately from quality scores.
- [ ] Apply Section 8 thresholds in order.

## 8. Pre-committed Decision Thresholds

Apply this table in order. The first matching condition decides the roadmap.

| Order | Condition | Conclusion | Roadmap action |
|---:|---|---|---|
| 1 | B fails to produce judgeable manuscripts for `>= 4` kernels for implementation reasons unrelated to model quality | Experiment implementation failed | Fix B runner and rerun; do not decide architecture |
| 2 | B' fails to produce judgeable manuscripts for `>= 4` kernels for implementation reasons unrelated to model quality while B is judgeable | Critic-control implementation failed | Fix B' runner and rerun B' against the same frozen B manuscripts before deciding whether A's lead is architectural or critic-only |
| 3 | A fails to reach a judgeable manuscript for `>= 4` kernels while B, B', and C are judgeable | Production path is operationally too brittle for this workload | Start evidence-first refactor, keeping A artifacts for failure analysis |
| 4 | C has `arm_median(C) >= arm_median(A)`, `arm_median(C) >= arm_median(B)`, and `arm_median(C) >= arm_median(B')` | Retrieval/evidence value is not visible under this judge protocol, or all evidence-aware arms underuse sources | Pause architecture cuts; audit source-use scoring and judge design before refactor |
| 5 | `arm_median(B) >= arm_median(A)` | B is equal or better in overall quality without self-critique | Cut the middle and late phases from the default roadmap; start evidence-first refactor without a required critic loop |
| 6 | `arm_median(B) > arm_median(A) - 0.3` and no `dimension_median(B, dim)` is worse than A by `>= 0.5` | B is non-inferior within the tie band without self-critique | Cut the middle and late phases; cost and complexity decide against A |
| 7 | B wins exactly 1 dimension over A by `>= 0.5`, and the other 2 dimensions are tie-band or small-loss | B carries at least one real quality advantage without a decisive quality loss | Cut the middle and late phases; keep deterministic compliance repair |
| 8 | `arm_median(B') >= arm_median(A)` | One bounded self-critique pass closes or reverses A's advantage | Cut the middle and late phases; roadmap becomes evidence-first composer plus one self-critique pass |
| 9 | `arm_median(B') > arm_median(A) - 0.3` and no `dimension_median(B', dim)` is worse than A by `>= 0.5` | B' is non-inferior within the tie band | Cut the middle and late phases; keep one bounded self-critique pass as the quality-control candidate |
| 10 | A has `kernel_win(A, B', k)` on `>= 5` kernels and `arm_median(A) - arm_median(B') >= 0.5` | Current architecture has measurable value beyond one self-critique pass | Keep 13-phase architecture for now; focus optimization on evidence handoff and middle/late phase quality |
| 11 | A, B, B', and C are all pairwise within the tie band on `arm_median`, and no arm has a stable win on `>= 4` kernels | The evaluation system is not discriminating enough | Fix evaluation first; add source-use and evidence-grounding checks; rerun |
| 12 | None of the above | Inconclusive mixed result | Write kernel-level diagnosis; do not change default architecture until a narrower follow-up resolves the split |

Cost and complexity note:

- If A wins quality but exceeds the token cap on `>= 3` more kernels than B', `conclusion.md` must state that A's win is quality-only and not cost-adjusted.
- This note does not override Order 10, but it constrains the roadmap to middle/late phase simplification rather than open-ended loop expansion.

## 9. Required Result Files

Final result tree:

```text
docs/experiments/abc-architecture-comparison/results/
  manifest.json
  blind_map.json
  aggregate.json
  aggregate.md
  {kernel_id}/
    A/
      manuscript.md
      provenance.json
    B/
      manuscript.md
      provenance.json
    B_prime/
      manuscript.md
      provenance.json
    C/
      manuscript.md
      provenance.json
    blinded/
      {submission_uuid}/
        manuscript.md
        judge-codex-gpt-5.5-xhigh.json
        judge-apiport-gpt-5.4.json
        judge-minimax-m2.7.json
```

`manifest.json` minimum fields:

```json
{
  "experiment_id": "abc-architecture-comparison-v1",
  "generation_model_id": "provider-configured-fallback-chain",
  "production_commit_sha": "hex",
  "experiment_script_sha": "hex",
  "provider": "provider-name",
  "provider_fallback_allowed": true,
  "token_cap_total": 1800000,
  "arms": ["A", "B", "B_prime", "C"],
  "kernel_ids": ["hist-01"],
  "generation_window_utc": {"started_at": "ISO-8601", "ended_at": "ISO-8601"}
}
```

`provenance.json` minimum fields:

```json
{
  "experiment_id": "abc-architecture-comparison-v1",
  "kernel_id": "hist-01",
  "arm": "B",
  "model_id": "gpt-5.5",
  "provider": "provider-name-or-chain",
  "provider_fallback_allowed": true,
  "production_commit_sha": "hex-or-null",
  "experiment_script_sha": "hex",
  "submitted_manuscript_source": "exports_done|critic_selected|other|null",
  "prompt_sha256": "hex",
  "source_package_sha256": "hex-or-null",
  "token_usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "budget_exceeded": false
  },
  "generated_at": "2026-05-16T00:00:00Z",
  "compliance_repair": {"attempted": true, "mode": "deterministic", "status": "passed"},
  "self_critique": {"attempted": false, "prompt_sha256": null, "base_b_manuscript_sha256": null},
  "external_scan": {"attempted": true, "mode": "audit_only", "status": "passed_or_skipped_or_failed"}
}
```

## 10. Timeline

| Days | Work |
|---:|---|
| Day 1-2 | ADR, design PR, kernel selection, Claude adversarial review, codex revisions |
| Day 3-4 | Implement B/B'/C script, A front-half dump, blindset builder, judge schema, aggregation skeleton |
| Day 5-8 | Run 40 manuscripts: A=10 production runs, B=10 evidence-first manuscripts, B'=10 self-critique manuscripts, C=10 naked single-shot manuscripts |
| Day 9-11 | Run 3 independent blinded judges for all submissions |
| Day 12-14 | Aggregate scores, write `conclusion.md`, apply threshold table, choose roadmap |

The design PR ends at Day 2. Code work starts only after ADR plus this protocol are agreed.

## 11. Non-goals

This experiment does not:

- measure export reliability;
- measure `docx` rendering;
- measure wall-clock speed as a quality score;
- tune prompts for the selected kernels;
- compare different generation models;
- decide final product UI;
- remove production phases before evidence exists.

It only answers whether the current middle and late prose-production pipeline improves manuscript quality over evidence-first composition under controlled conditions, with B' isolating the value of one bounded self-critique pass.
