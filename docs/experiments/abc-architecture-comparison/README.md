# A/B/B'/C Architecture Comparison

Status: P0 reset protocol active

The first 2026-05-16 result set is frozen as exploratory-only. It used a
single codex judge and is not a roadmap decision artifact.

Experiment window: approximately 2 weeks after design sign-off

Primary question: how much quality is lost by splitting manuscript production across the current middle and late phases, after separating that question from the value of one self-critique pass?

## Goal

This experiment measures the real size of fragmentation loss in `appleseed-autoessay`.

The current production workflow may still be valuable in the front half: source discovery, source ranking, synthesis, tensions, theoretical lens, compliance, audit, review gates, and export. The uncertain part is whether the prose-production chain adds value:

```text
drafter -> stylist -> final_rewrite -> critic loop
```

The experiment compares the current production path against three simpler baselines:

- same evidence, fewer writing phases;
- same evidence, one bounded self-critique pass;
- no evidence, one strong model call.

For the P0 binary question "13-phase optimization vs direct LLM output",
the direct-LLM comparator is the no-retrieval single-shot arm (`C` in the
historical ABC artifact layout, reported as `Direct` in the final verdict).

The result should decide whether the next architecture roadmap is continued 13-phase optimization, evidence-first refactor, or evaluation repair.

## Experiment Arms

| Arm | Definition | Uses retrieval? | Uses A front-half outputs? | Uses polish loop? | Uses critic/self-critique? |
|---|---|---:|---:|---:|---:|
| A | Current 13-phase production pipeline, unchanged | Yes | Native path | Yes | Production critic loop |
| B | Evidence-first single-shot manuscript from A's front-half evidence package, then deterministic compliance repair and audit-only scans | Yes | Yes, only through `framework_lens` | No | No |
| B' | B plus exactly one self-critique-and-targeted-revision pass using the same evidence package | Yes | Yes, only through `framework_lens` | No | One bounded pass |
| C | Naked strong-model single-shot from title, research kernel, and target journal style | No | No | No | No |

Shared constraints:

- Same generation model id for all arms: `gpt-5.5`.
- Same per-kernel hard token cap for all arms: `1800000`.
- Frozen production and experiment-script commit SHAs.
- ADR 0002 provider fallback chain enabled during generation; actual
  provider/model recorded per submission.
- Same humanizer directive.
- Same 10 fresh kernels.
- Same run window, ideally 40 manuscripts generated in 1-2 days.
- Same blinded judge protocol.

Intentional differences:

- B shares A's source pool because the experiment asks whether the later writing phases add value after evidence has been gathered.
- B' shares B's draft and evidence package because it isolates whether one self-critique pass, rather than the full middle/late phase chain, explains A's advantage.
- C does not use sources because it is the no-retrieval baseline.
- A keeps the production loops because it represents the incumbent architecture.

## Kernels

The experiment uses 10 fresh kernels:

- 2 history;
- 2 literature;
- 2 philosophy;
- 2 economics;
- 2 sociology.

Excluded topics:

- `bretton_woods`;
- `jiangnan_publishing`;
- `wang_yangming_turn`;
- direct variants of the above;
- any topic that has already been prompt-tuned during the north-star or real-paper quality push.

The proposed list is in `kernels.md`.

## Timeline

| Days | Work | Output |
|---:|---|---|
| Day 1-2 | ADR, experiment design PR, kernel review | this directory plus ADR |
| Day 3-4 | Implement B/B'/C runners, artifact dump, judge input builder, aggregation skeleton | experiment scripts and tests |
| Day 5-8 | Generate 40 manuscripts | `results/{kernel_id}/{arm}/manuscript.md` and manifests |
| Day 9-11 | Run 3 independent blinded judges | `judge-{judge_id}.json` per blinded submission |
| Day 12-14 | Aggregate scores, write conclusion, choose roadmap | `conclusion.md` and roadmap decision |

The 40 generation runs should be completed in a tight 1-2 day window inside Day 5-8 to reduce model-service drift.

## File Structure

```text
docs/
  adr/
    0001-evidence-first-architecture-experiment.md
  experiments/
    abc-architecture-comparison/
      README.md
      kernels.md
      protocol.md
      conclusion.md                         # created after scoring
      results/
        manifest.json                       # created by runner
        blind_map.json                      # private until scoring complete
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

Expected code locations for the implementation PR:

```text
backend/src/autoessay/experiments/
  __init__.py
  abc_architecture.py
  abc_prompts.py
  abc_extract.py
  abc_judge_schema.py

scripts/
  abc-run.py
  abc-build-blindset.py
  abc-judge.py
  abc-aggregate.py
```

The design PR creates the docs only. The implementation PR should add the package and scripts above unless review finds a cleaner local pattern.

## Artifacts

Each arm submission must produce:

- `manuscript.md`: the exact text sent to judges.
- `provenance.json`: model id, provider, frozen commit SHAs, token usage, run time, source package hash, prompt hash, submitted manuscript source, and any compliance, self-critique, or scan status.
- `prompt.txt` or `prompt.redacted.txt`: stored for audit, not shown to judges.

Each kernel must produce:

- A production `run_id` for arm A.
- A front-half package hash used by B and B'.
- A blinded submission UUID for each arm.
- Three judge JSON files per submission.
- Aggregated kernel-level scores.

## Review and Sign-off

Roles:

- codex: primary author for ADR, protocol, experiment code, aggregation, and first conclusion draft.
- Reviewer: adversarial review. Reviewer should challenge kernels, confound controls, thresholds, and interpretation.
- User: final sign-off on the roadmap after the 40 judged manuscripts and `conclusion.md` are complete.

Design sign-off:

- The experiment design is ready when codex and reviewer both mark the ADR, README, kernels, and protocol as `AGREE`.
- Until then, no B/B'/C runner or judge automation code should be merged.

Result sign-off:

- The architecture roadmap is not decided by rhetoric after the runs.
- It is decided by the threshold table in `protocol.md`, with any unresolved judge disagreement reported explicitly in `conclusion.md`.
