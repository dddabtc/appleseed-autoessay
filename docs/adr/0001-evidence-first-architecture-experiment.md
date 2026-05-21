# ADR-0001: Evidence-first Architecture Experiment

Date: 2026-05-16

Status: Draft for review

Decision owner: codex drafts, reviewer provides adversarial review, user signs off after experiment conclusion

## Context

`appleseed-autoessay` is currently a workflow-dominant LLM application. The production path is a fixed state-machine pipeline, not an agentic system where the model decides the next action:

```text
proposal -> scout -> curator -> synthesizer -> tension_extraction
-> framework_lens -> ideator -> drafter -> stylist -> final_rewrite
-> critic -> integrity -> exports
```

The code decides phase order, retry boundaries, review gates, loop counts, state transitions, and persistence. LLM calls produce bounded artifacts for the next phase. The LLM does not call tools, query databases, choose which phase to run next, or decide when to stop the overall workflow.

The architecture has also accumulated three evaluation or repair loops:

- `final_rewrite` polish loop, used for full-manuscript academic revision.
- `critic` paired-runner loop, using a structured repair plan and selecting the best candidate.
- `north_star_gate`, using `paired_blind_box_ledger_v1` to measure selected pipeline output against a shadow baseline.

The quality evidence is mixed and does not justify another round of prompt tuning without an architecture experiment.

Known data points:

- R10 verdict on 2026-05-08: pipeline scored `2.2 / 4.1 / 3.0` against single-shot baseline `8.6 / 8.4 / 8.7`, a gap of `4.3-6.4` across compliance, novelty, and completeness.
- R11 baseline after shadow-baseline activation: single-shot baseline scored `7.3 / 6.8 / 7.6` by codex; pipeline could fail honestly when the source pool was thin, but that did not prove the later writing phases added value.
- North-star v3 gate on 2026-05-10 passed `3/3` kernels, but the pass was boundary-level, not decisive. `bretton_woods` passed with `gate max_loss = -1.0`; the median item delta was `citation_alignment = -1.0` and the other 12 ledger items were `0.0`. `jiangnan_publishing` and `wang_yangming_turn` were `0.0`.
- The three successful gate kernels were not a broad random sample. They include topics already used in the quality push and should not be treated as evidence that the 13-phase architecture generalizes.

The current hypothesis is that the early phases may still carry durable value:

- find real sources;
- rank and normalize source metadata;
- synthesize claims;
- surface tensions and theoretical lenses;
- preserve compliance, reviewability, audit trail, and export UX.

The risky part is the middle and late prose-production chain:

```text
drafter -> stylist -> final_rewrite -> critic loop
```

The suspected failure mode is fragmentation loss: the system cuts one article into many phase-local tasks and then tries to recover global argument, prose rhythm, and evidence alignment through later repair loops. If a strong model can write a better full manuscript from the same evidence package in one call, the middle and late phases are net negative despite being engineered carefully.

## Two Perspectives

### Perspective A: Continue Optimizing the Current Pipeline

This position treats the 13-phase pipeline as directionally correct but under-tuned.

Arguments for this view:

- The pipeline gives us observability, audit artifacts, resumability, phase-level failure recovery, and compliance hooks.
- Source acquisition and synthesis are hard enough to justify workflow decomposition.
- The north-star v3 gate did pass `3/3`, which means the current system can reach a non-regression boundary under some conditions.
- More focused fixes to `drafter`, `stylist`, `final_rewrite`, and `critic_loop` may close the quality gap without discarding workflow infrastructure.

Weaknesses:

- R10 was not a small miss. A `4.3-6.4` quality gap is too large to explain away as ordinary prompt drift.
- A boundary pass on three tuned or semi-tuned kernels does not prove the architecture adds value.
- More loops may keep increasing complexity while producing mostly zero deltas against the baseline.
- The strongest model improvements over the next 1-2 years are likely to improve long-context single-pass writing more than phase-local repair.

### Perspective D: Evidence-first Workflow with Narrow Agents

This position keeps the workflow where it creates real product value, but removes the prose-production chain if the experiment shows it is not helping.

The proposed future shape:

- Keep a workflow trunk for source discovery, source ranking, synthesis, compliance, audit trail, review gates, export, and UX.
- Replace `drafter -> stylist -> final_rewrite -> critic_loop` with a smaller evidence-first manuscript composer if it is non-inferior or better, and separately test whether one bounded self-critique pass is enough quality control.
- Use narrowly scoped agents only where local autonomy is justified, such as source triage, citation repair, or targeted evidence-gap diagnosis.
- Keep blind evaluation as a workflow, not as an always-on rewriting loop.

Arguments for this view:

- The core defensible asset is evidence handling, not a long prose assembly line.
- A single strong model call can preserve global argument better than many local calls.
- Reducing the middle and late phases lowers state-machine, queue, test, prompt, and operations complexity.
- If B can match A with the same evidence package, A's extra phases are carrying cost without quality benefit.

Weaknesses:

- Single-shot composition can hallucinate, flatten nuance, or underuse source evidence.
- The current pipeline's audit artifacts are valuable; removing phases without replacing their observability would be a regression.
- Some kernels may genuinely benefit from staged drafting if evidence is sparse, contradictory, or structurally complex.

## Decision

We will not choose either roadmap now. We will run an A/B/B'/C architecture comparison first.

The experiment uses 10 fresh kernels, not the previously tuned or quality-push topics. The excluded topics include `bretton_woods`, `jiangnan_publishing`, `wang_yangming_turn`, and any prompt-tuned descendants of those domains.

The four arms are:

- A: current production 13-phase pipeline, unchanged.
- B: evidence-first single-shot manuscript from A's front-half evidence package, followed by deterministic compliance repair and non-mutating audit scans. No polish loop and no critic loop.
- B': B plus exactly one bounded self-critique-and-targeted-revision pass using the same evidence package. No iterative critic loop and no paired candidate selection.
- C: naked strong-model single-shot from title, research kernel, and target journal style. No retrieval, no evidence package, no critic, no polish.

The experiment decides the roadmap with pre-committed thresholds in `docs/experiments/abc-architecture-comparison/protocol.md`.

Expected roadmap outcomes:

| Experiment result | Interpretation | Roadmap |
|---|---|---|
| B is equal to or better than A, or B is near-equal with materially lower complexity | The middle and late pipeline phases are not earning their cost | Cut `drafter -> stylist -> final_rewrite -> critic_loop` from the default architecture and start evidence-first refactor |
| B loses to A, but B' is equal, better, or non-inferior | A's visible advantage is likely critic/self-review value rather than the full multi-phase prose chain | Cut the middle and late phases, but carry forward one bounded self-critique pass as the evidence-first quality-control candidate |
| A decisively beats B' on at least 5 of 10 kernels by the committed margin | The current architecture still has recoverable quality value beyond one self-critique pass | Keep the 13-phase architecture for now, but focus all optimization on the middle and late phases and their evidence handoff |
| A, B, B', and C are all effectively tied, or C matches evidence-aware arms too often | The evaluation design or evidence-use measurement is not discriminating enough | Fix evaluation, source-use checks, and judge protocol before making architecture cuts |

## Consequences

Before the experiment:

- Do not change the production default pipeline to evidence-first.
- Do not remove, disable, or rewrite `drafter`, `stylist`, `final_rewrite`, or `critic_loop`.
- Do not tune prompts on the 10 selected experiment kernels.
- Limit code work to experiment harness, artifact extraction, blinded judging, aggregation, and documentation.

During the experiment:

- A must use the production path unchanged.
- B may reuse A's front-half artifacts but must not inspect A's manuscript, style output, rewrite output, critic output, integrity report, or export artifacts.
- B' may reuse B's repaired manuscript and the same front-half evidence package, but must not inspect A's late artifacts, B audit outputs, judge outputs, or manual notes.
- C must not use retrieval or any source package.
- Judges must see only blinded manuscript text and references, not phase logs or state-machine metadata.

After the experiment:

- The pre-committed protocol decides the default roadmap.
- If B wins, or if B' shows that one self-critique pass is enough, the next PR series should be an evidence-first refactor design, not another prompt polish pass.
- If A wins decisively against B', the next PR series should narrow the current architecture's failure modes and remove only demonstrably redundant loops.
- If the result is inconclusive, the next PR should improve the measurement system before touching architecture.
