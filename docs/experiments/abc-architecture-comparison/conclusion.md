# ABC Architecture Comparison Conclusion

Status: **Exploratory-only, superseded by the P0 reset rerun.**

This document records the first 2026-05-16 pilot result. It is frozen for audit
history and must not be used as the roadmap verdict because it used only one
completed judge instead of the required 3-judge quorum.

Date: 2026-05-16

Input artifacts:

- Results root: `docs/experiments/abc-architecture-comparison/results`
- Aggregate JSON: `results/aggregate.json`
- Aggregate summary: `results/aggregate.md`
- Active judge set: `codex-gpt-5.5-xhigh` only
- Completed judge JSON: 40/120 total planned judge files, 40/40 for the active codex judge
- Blinded submissions in `blind_map.json`: 40/40, 10 kernels x 4 arms

## Decision

Triggered threshold: **Order 4**

Condition:

> C has `arm_median(C) >= arm_median(A)`, `arm_median(C) >= arm_median(B)`, and `arm_median(C) >= arm_median(B')`.

Conclusion:

> Retrieval/evidence value is not visible under this judge protocol, or all evidence-aware arms underuse sources.

Roadmap action:

> Pause architecture cuts; audit source-use scoring and judge design before refactor.

This is a first-match decision under protocol Section 8. The result should not be used to cut or preserve the 13-phase architecture yet, because the no-retrieval C arm is the top arm by median score.

## Caveats

- This is single-judge data. Protocol Section 6 specifies median of 3 judges; here the median is the codex judge score and disagreement is trivially 0. All threshold results are provisional until Claude and Gemini/manual fallback scores are added or the protocol is explicitly revised for single-judge operation.
- The original execution history included partial-state notes such as 38/40 vs 40/40 manuscripts and `soc-01` partial. The final aggregate inputs are 40/40 blinded submissions and 40/40 codex judge JSON. Treat the earlier partial state as run-history context, not as an aggregate exclusion.
- `manifest.json` lists 9 kernel ids, while `driver_state.json`, `blind_map.json`, and the blinded result tree contain all 10 kernels. The aggregator used the blinded submissions and `blind_map.json`; this bookkeeping mismatch should be cleaned up before publishing the experiment as final.
- No high-disagreement rows were reported, but this is a single-judge artifact rather than evidence of inter-judge agreement.
- No arm exceeded the configured token budget in the aggregate report.

## Aggregate Scores

| Arm | Overall mean | Overall median | Compliance mean | Compliance median | Novelty mean | Novelty median | Completeness mean | Completeness median |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 5.85 | 5.92 | 5.55 | 5.50 | 5.10 | 5.00 | 6.90 | 7.00 |
| B | 5.53 | 5.50 | 5.25 | 5.25 | 4.90 | 5.00 | 6.45 | 6.75 |
| B_prime | 5.48 | 5.33 | 5.15 | 5.00 | 4.80 | 5.00 | 6.50 | 6.75 |
| C | 6.13 | 6.17 | 5.65 | 5.50 | 5.65 | 5.50 | 7.10 | 7.00 |

Key median comparisons:

- A vs B: A leads B by 0.42 overall median points. B does not satisfy Orders 5-7.
- B vs B_prime: B leads B_prime by 0.17 overall median points. The bounded self-critique pass does not show aggregate uplift in this single-judge run.
- C vs B: C leads B by 0.67 overall median points. Since C has no retrieval package, this is the decisive evaluation warning that triggers Order 4.
- A vs B_prime: A leads B_prime by 0.58 median points, but A has only 4 kernel wins over B_prime at the >= 0.5 margin, so Order 10 does not match even aside from earlier Order 4.

## Per-Kernel Overall Scores

| Kernel | A | B | B_prime | C | Winner/tie |
|---|---:|---:|---:|---:|---|
| econ-01 | 6.00 | 5.83 | 5.67 | 6.00 | A, C |
| econ-02 | 5.83 | 6.17 | 5.50 | 6.17 | B, C |
| hist-01 | 5.67 | 5.33 | 5.33 | 6.67 | C |
| hist-02 | 6.17 | 4.67 | 5.33 | 6.17 | A, C |
| lit-01 | 5.67 | 5.33 | 5.00 | 6.50 | C |
| lit-02 | 5.33 | 5.50 | 6.17 | 5.33 | B_prime |
| phil-01 | 5.67 | 5.50 | 5.33 | 6.50 | C |
| phil-02 | 6.17 | 6.33 | 5.33 | 5.33 | B |
| soc-01 | 6.00 | 4.83 | 5.00 | 6.67 | C |
| soc-02 | 6.00 | 5.83 | 6.17 | 6.00 | B_prime |

Kernel-level reading:

- C is top or tied top on 7/10 kernels.
- A is top or tied top on 3/10 kernels.
- B is top or tied top on 2/10 kernels.
- B_prime is top on 2/10 kernels.
- A beats B_prime by >= 0.5 on 4 kernels: `hist-02`, `lit-01`, `phil-02`, `soc-01`.

## Provider and Model Sensitivity

ADR 0002 changed the estimate from strict same-model control to production-like provider fallback. The aggregate therefore must be read as provider-fallback behavior, not as an isolated architecture-only comparison.

| Arm | Provider | Provider model | Count | Kernels |
|---|---|---|---:|---|
| A | production | production-configured | 10 | econ-01, econ-02, hist-01, hist-02, lit-01, lit-02, phil-01, phil-02, soc-01, soc-02 |
| B | apiport | gpt-5.4-mini | 3 | econ-01, econ-02, phil-02 |
| B | rightcode | gpt-5.4-mini | 7 | hist-01, hist-02, lit-01, lit-02, phil-01, soc-01, soc-02 |
| B_prime | apiport | gpt-5.4-mini | 4 | econ-01, econ-02, phil-02, soc-02 |
| B_prime | rightcode | gpt-5.4-mini | 6 | hist-01, hist-02, lit-01, lit-02, phil-01, soc-01 |
| C | rightcode | gpt-5.4-mini | 10 | econ-01, econ-02, hist-01, hist-02, lit-01, lit-02, phil-01, phil-02, soc-01, soc-02 |

B/B_prime provider-model mismatch:

- `soc-02`: B used `rightcode/gpt-5.4-mini`; B_prime used `apiport/gpt-5.4-mini`. Per ADR 0002, the B vs B_prime comparison on this kernel is downgraded to self-critique with provider/model confound.

## North-Star Gate Context

The historical north-star gate data does not override this ABC threshold decision:

- R10 on 2026-05-08 showed the pipeline far behind a single-shot baseline: pipeline `2.2 / 4.1 / 3.0` vs baseline `8.6 / 8.4 / 8.7` for compliance, novelty, and completeness.
- R11 after shadow-baseline activation had codex baseline `7.3 / 6.8 / 7.6`; honest failure on thin source pools did not prove later writing phases add value.
- North-star v3 on 2026-05-10 passed 3/3 kernels, but only at a boundary level. `bretton_woods` had `gate max_loss = -1.0` with median item delta `citation_alignment = -1.0` and the other 12 items at `0.0`; `jiangnan_publishing` and `wang_yangming_turn` were `0.0`.

Interpretation: the north-star gate showed the current system can reach a non-regression boundary on some known kernels. It did not establish that the 13-phase architecture generalizes, and the current ABC result says the judge protocol is not currently distinguishing evidence-aware writing from naked single-shot writing.

## Roadmap Recommendation

Do not cut production phases from the default roadmap based on this run.

Recommended next steps:

1. Audit judge design for source-use and evidence-grounding sensitivity. The current scoring let C, the no-retrieval arm, beat all evidence-aware arms by median.
2. Add or strengthen source-use checks before judging: citation alignment, source specificity, unsupported-reference detection, and claim-to-source grounding.
3. Complete the missing judge set or formally rerun as a declared single-judge pilot. If using single judge, keep the result labeled as provisional.
4. Clean result bookkeeping: reconcile `manifest.json` with the 10-kernel `blind_map.json` and final result tree.
5. After judge/source-use audit, rerun the aggregate and reapply the Section 8 threshold table in order.
