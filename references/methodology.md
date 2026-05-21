# Automatic Research Methodology

## 0) Overall Logic

This methodology aligns with the high-level logic of autoresearch:

- iterate quickly,
- measure strictly,
- make hypotheses explicit,
- and update direction from evidence.

The extension here is operational discipline:

- baseline-first thinking,
- telescope method for efficient search,
- direction over early results,
- deep post-round analysis,
- controlled SOTA intake,
- acceptance gates,
- LLM-first design,
- and durable state for resumability.

---

## 1) Baseline Must Be Trustworthy

Before optimization begins:

- lock the task definition,
- lock the dataset/split/version,
- verify reproducibility,
- document failure modes,
- and confirm baseline stability across reruns.

**Rule:** if the baseline is unreliable, downstream conclusions are unreliable.

---

## 2) Telescope Method: Coarse → Fine-Tune → Scale

Like using a telescope: coarse adjustment finds the star field, fine adjustment resolves the target, then you observe.

1. **Coarse Sweep** — Scan all functions/categories at once with a small sample. Goal: find where the signal IS and where it IS NOT.
   - Example: Run a handful of test items across all categories → discover which categories show improvement and which show zero effect.
2. **Fine-Tune** — Zoom into the weak spot with the narrowest possible test to diagnose root cause.
   - Example: Take 3-5 failures from the weak category → test alternative approaches → confirm which component is the bottleneck.
3. **Scale** — Only after all basic functions are good enough. Scaling amplifies signal, it does not create it.
   - If broken at n=3, still broken at n=500.

**Critical rule:** Do NOT skip fine-tuning by jumping from coarse to scale.

---

## 3) Direction Over Results

Theoretically right direction matters more than early test numbers — until evidence proves the theory wrong.

- A low score with right direction can be improved (implementation fix).
- A high score with wrong direction is a dead end.
- When a sound approach tests poorly, ask: is the THEORY wrong, or is the IMPLEMENTATION incomplete?
- Only abandon when the theory itself is disproven.

**Guideline:** If an approach addresses a real failure mode but underperforms the current best, refine it — the theory may be right while the implementation is incomplete. But if N experiments consistently show the theory doesn't hold, abandon it.

---

## 4) Every Round Must End in Analysis

Each round should answer at least five questions:

1. Did the primary metric improve?
2. Which slices improved or regressed?
3. What changed in the failure distribution?
4. How strong is the signal?
5. What is the best next move?

**Rule:** no "next step by vibe". Every directional update should cite evidence.

---

### §4a. Diagnose-and-Fix (Telescope Mid-Phase Repair)

When smoke or POC phases reveal failures, the system diagnoses each failure before proceeding:

#### Failure Classification

Each failed task is classified into one of four categories:

| Category | Description | Action |
|---|---|---|
| `fixable_deterministic` | Deterministic bugs: wrong parameter types, tool name errors, schema mismatches | Auto-fix and retry |
| `fixable_prompt` | Agent misunderstanding due to unclear prompt: incorrect action calls, premature termination | Adjust prompt suffix/few-shot and retry |
| `random_llm` | Stochastic LLM failures: occasional wrong reasoning, hallucinated parameters | Accept as noise, do not retry |
| `fundamental` | Structural issues: task design conflicts, impossible constraints | Log and skip, do not retry |

#### Retry Protocol

1. After smoke (or POC) completes, run `diagnose_failures()` on all failed tasks
2. If any failures are classified as `fixable_*`:
   - Generate targeted fix (prompt adjustment, parameter correction, etc.)
   - Re-run ONLY the failed tasks with the fix applied
   - Merge retry results with original results
   - Recalculate phase score
3. Maximum 1 retry per phase (no infinite loops)
4. Retry consumes step budget from the round's allocation

#### Why This Matters

At baseline=91% with smoke_size=5, a single fixable failure drops score from 100% to 80%. Without diagnose-and-fix, this triggers unnecessary POC/full runs or false rejections. The mechanism ensures:

- Fixable issues are resolved before scaling up evaluation
- Token budget is not wasted on full-134 runs with known bugs
- Random noise is accepted rather than over-fitted

#### Flow Integration

```
smoke → diagnose_failures → [fix + re-smoke if fixable] → smoke_gate
  → poc → diagnose_failures → [fix + re-poc if fixable] → poc_gate
    → full
```

## 5) Keep Changes Attributable

When possible:

- one hypothesis per round,
- one focused code/config change,
- one stable evaluation path,
- one clear decision.

This keeps the loop interpretable and makes rollback easy.

---

## 6) Separate Fast Validation from Strong Confirmation

Use at least two quality layers when stakes are meaningful:

- **fast validation path** — cheaper, faster, more frequent
- **strong confirmation path** — slower, more trustworthy, used before major acceptance

This prevents local overfitting to a weak internal judge.

**Warning:** A large metric improvement can come from fixing the evaluation judge, not from actual system improvement. Always distinguish judge calibration from true gains.

---

## 7) LLM-First Design

Don't build what AI will do 10x better tomorrow. When a heuristic/rule-based method shows no results in testing, try the LLM path before diagnosing further.

- Always leave an LLM entry point even when building a heuristic version.
- Heuristics are speed optimizations, not architecture.
- Use a `USE_LLM_PRIMARY` flag pattern for easy toggling.
- Build for the future capability curve: if tomorrow's model is 10x better, your system should benefit with zero code changes.

**Proven pattern:** A rule-based extraction method scored 0pp improvement while an LLM-based approach on the same task scored +16pp. The heuristic was simply too dumb for the task's complexity.

---

## 8) Small Sample Warning

n<30 category-level conclusions are unreliable.

- A "+8pp" category improvement can be a single flipped item in n=12 (noise).
- Always report sample size alongside percentages.
- Use n≥30 for category-level decisions, n≥100 for overall conclusions.

---

## 9) Continuous SOTA Scanning, Controlled Adoption

In every round or every few rounds:

- scan new papers,
- scan relevant repos,
- scan benchmark reports,
- identify promising ideas,
- deep-dive only when the expected value is high.

Specifically search for:

- adversarial/unanswerable defense strategies,
- hallucination guard / faithfulness checking,
- abstention mechanisms,
- grounding verification.

**Rule:** novelty enters through controlled evaluation, not blind adoption.

---

## 10) Cross-Benchmark Validation

When improvements are developed on one benchmark:

1. Smoke test on ALL benchmarks (telescope step 1).
2. POC on all (telescope step 2).
3. Full scale on all (telescope step 3).

Never assume improvements transfer without validation.

---

## 11) Durable Files Beat Transient Session Memory

A serious autoresearch loop should survive:

- chat resets,
- process restarts,
- machine reboots,
- human handoff,
- long-running experiment gaps.

Recommended durable files:

- `program.md`
- `ledger.jsonl`
- `CURRENT_STATUS.md`
- `experiments/NNN/round-log.md`

**Rule:** if a new session cannot resume the work from files alone, the loop is under-documented.

---

## 12) Suggested Operating Rhythm

### Per experiment round
- verify baseline when needed
- run one controlled change (single variable)
- record metrics
- perform deep 5-question analysis
- write decision and next step

### Per day
- summarize what was learned
- re-rank backlog
- scan external developments
- decide whether current line still has gradient

### On plateau
- stop brute-force looping
- re-open assumptions
- inspect eval validity
- widen search space or redesign the problem decomposition

---

## 13) Typical Failure Modes

### Baseline drift
What looked like improvement was only a changed baseline.

### Multi-variable confusion
Too many simultaneous changes prevent attribution.

### Weak-judge overfitting
The system learns how to satisfy a cheap proxy but not the true objective.

### Shallow reporting
Headline metric improves, but important slices regress.

### Transcript-only state
The research loop becomes unrecoverable after interruption.

### Novelty chasing
External ideas are adopted before being tested against local constraints.

### Small-sample mirage
Large percentage improvements on tiny subsets turn out to be noise.

### Premature abandonment
Approaches with sound theory abandoned due to poor early implementation, before the theory itself is disproven.

---

## 14) Multi-agent Coordination: Single Ledger Principle

When multiple agents run experiments concurrently:

- One single ledger file is the **ONLY** authority for experiment numbers.
- **NO** split or secondary ledger files allowed.
- Every experiment MUST write a ledger entry BEFORE starting execution.
- Every experiment MUST commit after completion, abort, or kill.
- Old experiments MUST be marked before being superseded.
- All agents read and write the SAME ledger.

**Why:** Without a single source of truth, agents independently evolve their own view of reality → divergent numbering, zombie processes, conflicting status reports.

---

## 15) Incremental Validation

Large benchmark runs must be split into segments with **decision points** between each.

1. Run segment A (smallest, fastest) → analyze results
2. Decision point: metrics OK → continue. Metrics bad → stop, diagnose, fix.
3. Run segment B → analyze.
4. Decision point: consistent with A → continue. Diverged → investigate.
5. Repeat until all segments pass or a fix is needed.

**Rule:** Never launch a full run without first validating on a fast subset.

---

## 16) Mandatory Experiment Logging

Every experiment MUST write a persistent log file.

- At startup: timestamp, hypothesis, config/parameters.
- Per item: item number, result, score.
- Progress checkpoint: every N items — cumulative stats.
- At completion: summary stats, total runtime, final verdict.
- Use unbuffered output so `tail -f` works in real time.
- The log file is the PRIMARY record; session transcripts may be lost.
- Log file goes in `experiments/NNN/blob/run.log`.

### Execution Environment

- **Use tmux (or screen) for all experiment runs.** Never run experiments in a session that can time out or disconnect.
- Session naming convention: `exp-NNN` (e.g., `tmux new -s exp-001`).
- This ensures: (1) experiments survive disconnects, (2) progress can be checked anytime via `tmux attach`, (3) experiments can be stopped with Ctrl-C and resumed.

### Stop and Resume

Every experiment MUST support stop-and-resume:

- Write checkpoint after every item (or every N items for fast loops).
- On startup, check for existing checkpoint and resume from last completed item.
- Checkpoint file goes in `experiments/NNN/blob/checkpoint.jsonl` (or similar).
- Stopping an experiment (Ctrl-C, kill, machine reboot) must NOT corrupt the checkpoint.
- Resuming must produce identical results to a clean run (no duplicate items, no skipped items).

**Rule:** If an experiment cannot be stopped and resumed without data loss, it is not production-ready.

### Progress Visibility

At any time, an observer must be able to determine:

1. **Is it running?** → `tmux ls` or `ps aux | grep exp-NNN`
2. **How far along?** → `tail -1 experiments/NNN/blob/run.log` or `wc -l experiments/NNN/blob/checkpoint.jsonl`
3. **Current accuracy?** → cumulative stats in the log (printed every N items)
4. **ETA?** → items/sec × remaining items (printed in log)

### Experiment Monitor

A monitor process should watch running experiments and report progress at regular intervals.

**What the monitor does:**

1. Check which experiments are running (tmux sessions, process list)
2. Read the latest checkpoint/log to get current progress (items done, accuracy so far, ETA)
3. Report to the configured channel (Telegram, Discord, etc.) at set intervals
4. Detect anomalies: stalled experiments (no progress for N minutes), crashes (tmux session gone but not completed), error spikes

**Implementation options:**

- **Cron job** — runs every N minutes, reads log/checkpoint, sends summary if there's progress
- **Heartbeat integration** — add experiment monitoring to the agent's heartbeat checks
- **Dedicated watcher script** — `scripts/monitor.py` that tails logs and reports

**Report format (minimal):**

```
Exp-001 [S1 temporal] 47/105 QA (44.8%) | acc: 72.3% | ETA: 12min
Exp-002 [S3 iterative] not started
```

**Report triggers:**

- At fixed intervals (e.g., every 10 minutes while an experiment is running)
- On experiment completion (final results)
- On experiment failure/crash (alert immediately)
- On milestone (25%, 50%, 75%, 100%)

**Rule:** Long-running experiments (>10 minutes) MUST have monitoring. The researcher should never need to manually check — progress comes to them.

---

## 17) Experiment Directory Structure

Every experiment MUST have its own directory with a standard layout:

```
experiments/<benchmark>/R{n}/
├── scripts/          # Run scripts (reproducible entry points)
├── code/             # Code snapshot, patches, or diffs
├── logs/             # Compressed execution logs (.log.gz)
├── results/          # Result files (JSON, CSV, etc.)
├── design.md         # Architecture design and technical approach
├── plan.md           # Experiment plan: objectives, hypotheses, expected outcomes
├── thoughts.md       # Decision log: reasoning, trade-offs, pivots
├── report.md         # Results analysis, comparisons, conclusions
└── current_status.md # Ledger: current state, progress, blockers
```

### File descriptions

- **scripts/**: Executable run scripts that reproduce the experiment. Must be self-contained with all parameters documented.
- **code/**: Git diffs or patches capturing the code changes for this round. Use `git diff <prev_commit>..<this_commit> > code/round.patch`.
- **logs/**: Execution logs compressed with gzip. Raw logs should not be committed uncompressed.
- **results/**: Structured result files (JSON preferred). Include per-task breakdowns, not just aggregate scores.
- **design.md**: Technical specification of what this round changes and why. Include architecture diagrams if applicable.
- **plan.md**: Pre-experiment plan stating the hypothesis, expected impact, and success criteria.
- **thoughts.md**: Running decision log capturing reasoning during the experiment. Written as work progresses, not retroactively. Store failure case sampling notes (§19), 5-question post-round analysis (§4), seesaw analysis (if cross-benchmark), and comparisons with prior experiments here.
- **report.md**: Post-experiment analysis and experiment summary. Must include experiment ID and timestamp, hypothesis, change summary, baseline, results (including per-category breakdown and sample size), verdict (ACCEPT / REJECT / PENDING_REVIEW / BLOCKED), reason (with evidence), next step, score comparison with previous rounds, failure breakdown, and conclusions.
- **current_status.md**: Living document tracking experiment state (planned/running/completed/abandoned), blockers, dependencies, and current progress.

### Rules

1. Every round gets its own directory, even if changes are parameter-only.
2. All artifacts must be committed before starting the next round.
3. Logs must be compressed before commit (no raw .log files in git).
4. Results JSON must include per-task pass/fail, not just aggregate scores.
5. design.md and plan.md should be written BEFORE running the experiment.
6. report.md and current_status.md should be updated AFTER results are available.

### Naming

- Use zero-padded numbers: `001`, `002`, ..., `099`, `100`
- Sub-experiments: `001.1`, `001.2` (see §17b below)
- Directory name = experiment ID from the ledger

### Why this structure

1. **Reproducibility** — everything needed to understand and reproduce is in one place
2. **Resumability** — a new session can read the experiment files and know exactly what happened
3. **Auditability** — thoughts.md preserves the reasoning chain, not just the numbers
4. **Separation of concerns** — scripts/code/logs/results for artifacts, thoughts.md for analysis, report.md for summary, current_status.md for live state

---

## 17b) Sub-experiment Structure

Experiments may have sub-experiments when testing variants of the same hypothesis.

- Naming: `NNN.M` (e.g., 005.1, 005.2, 005.3).
- Sub-experiments live inside the parent directory: `experiments/005/005.1/`, `experiments/005/005.2/`
- Each sub-experiment has its own blob/ and thoughts/ if needed, or shares the parent's.
- Each sub-experiment is independently scored.
- Main experiment verdict based on the best sub-experiment result.
- Use for: multiple prompt variants, parameter settings, implementation approaches.
- Do NOT use for: fundamentally different hypotheses or sequential refinements.

---

## 18) Data Quality Audit: Pre-experiment Gate

Before any experiment that depends on a data pipeline, MUST audit:

1. Schema check: all expected tables/columns present and populated?
2. Sample inspection: pull 10 random records, manually verify.
3. Format consistency: dates, entity names, type labels.
4. Coverage check: what % of source data made it through?
5. Link integrity: can you trace from structured record → source → original text?

**Rule:** No downstream experiment may begin until audit passes. This is a gate, not a suggestion.

---

## 19) Failure Case Sampling: Post-REJECT Mandatory

After every REJECT, sample 5-10 failures and classify:

1. **Retrieval miss** — correct answer exists but wasn't retrieved.
2. **Retrieval noise** — correct answer retrieved but buried.
3. **Generation error** — correct context retrieved, model answered wrong.
4. **Data gap** — information doesn't exist in stored data.
5. **Grounding failure** — system hallucinated instead of abstaining.

Record the distribution. This tells you WHERE in the pipeline to focus.

**Rule:** No next-experiment design after a REJECT without failure sampling.

---

## 20) Acceptance Gate

- No experiment accepted without external re-evaluation confirmation.
- Internal evaluation is a hypothesis; external confirmation is evidence.
- Every 3 experiments or 2+ consecutive failures → mandatory SOTA scan.
- Plateau protocol: 2+ consecutive failures → stop → SOTA scan → re-open assumptions → inspect eval → resume.

---

## 21) Practical Summary

A good autoresearch loop is not just:

> generate experiment → run eval → repeat

It is:

> establish truth → telescope into signal → test carefully → analyze deeply → update direction → preserve state → repeat

---

_This document is living. Update it as new principles are learned._


## Dual Validation (Design Spec ↔ External Research)

When your project has a design specification, treat it as equal-weight evidence alongside external research. Analyze both in the same framework to find convergence or divergence.

- Design spec = hypothesis from first principles
- External research (SOTA papers, prior art) = independent evidence
- **Convergence** (both agree) → strong signal, move fast
- **Only one source** → gather more evidence before committing
- **Conflict** → investigate which is wrong

Put both in the SAME analysis framework. Side-by-side analysis reveals convergence faster than treating them sequentially. Don't treat external research as the sole source of direction while ignoring your own design, or vice versa.


## Dual-Lane Research System (Auto + Manual)

Split research into autonomous and human-directed lanes sharing a single experiment ledger. The autonomous lane runs mechanical experiments continuously and flags results for review. The human-directed lane sets research direction and makes final accept/reject decisions. Neither lane blocks the other.

### Auto Lane (autonomous/cron-driven)
- Runs mechanical experiments: parameter sweeps, baseline validation, simple variants
- Operates on fixed schedule without human intervention
- Cannot make final ACCEPT decisions — only PENDING_REVIEW
- Checks for manual lane activity before starting (no conflicts)
- Runs SOTA scans when triggered by plateau protocol

### Manual Lane (human-directed)
- Sets research direction and architecture decisions
- Designs novel experiments based on deeper analysis
- Makes final ACCEPT/REJECT verdicts
- Can adopt auto lane results by confirming verdict

### Shared Infrastructure
- Single ledger (one source of truth for all experiment numbers)
- Lane field distinguishes origin
- Both lanes write to same status files
- Merge protocol: auto results are provisional until manual review

### Why it works
- Human creativity + machine throughput
- Auto lane discovers dead ends cheaply (burns compute, not human attention)
- Manual lane focuses on direction, not grinding
- Neither blocks the other — truly parallel

### When to use
Any research project where (1) many experiments need running, (2) some are mechanical (parameter sweeps) and some need creative design, (3) you want continuous progress even when the researcher isn't actively working.


## 22) Multi-Agent Peer Review: Consensus Gate

Every experiment round must pass a peer review before its conclusions are accepted. The executing agent writes the experiment report; an independent LLM (different model or endpoint) reviews the entire experiment — not just answer correctness, but methodology, reasoning, and conclusions.

### Why This Matters

A single agent running experiments has blind spots:
- **Confirmation bias** — the agent wants its hypothesis to succeed and interprets results favorably
- **Methodological gaps** — confounding variables, insufficient controls, small-sample overconfidence
- **Missed patterns** — regression analysis skipped, alternative explanations not considered
- **Premature conclusions** — data doesn't fully support the claim but agent moves on

An independent reviewer catches these because it has no investment in the outcome.

### Process

After each experiment round:

1. **Executing agent** writes the full experiment report: hypothesis, method, results, analysis, conclusion, next steps.

2. **Report is sent to an independent reviewer** (different LLM model/endpoint). The reviewer's role:
   - "You are an independent research reviewer. Examine this experiment report critically."
   - Evaluate: Is the methodology sound? Do the results support the conclusions? What are alternative explanations? What was missed?

3. **Reviewer outputs a structured assessment:**
   - `verdict`: AGREE / CHALLENGE / REJECT
   - `methodology_issues`: list of concerns about experimental design
   - `conclusion_supported`: whether data actually supports the stated conclusion
   - `alternative_explanations`: what else could explain the results
   - `missed_points`: observations the executing agent overlooked
   - `recommendations`: what should be done before accepting

4. **If CHALLENGE or REJECT:** the executing agent responds to each point. The exchange continues (max 3 rounds) until:
   - Reviewer changes to AGREE (consensus reached), or
   - Agent modifies conclusions based on reviewer's valid points, or
   - Disagreement is logged with both perspectives for human decision

5. **Consensus is recorded** in the experiment's `report.md` under a "## Peer Review" section, including:
   - Reviewer's assessment
   - Points of discussion
   - Final consensus or recorded disagreement
   - Any conclusion modifications

### Configuration

```
reviewer:
  provider: right.codes-codex
  model: gpt-5.4
  role: "independent research reviewer"
  max_rounds: 3
  gate: AGREE required before ACCEPT verdict
```

### What the Reviewer Should Check

1. **Methodology**: single variable? baseline stable? sample size adequate? controls in place?
2. **Data interpretation**: do the numbers actually say what the agent claims? statistical significance?
3. **Confounds**: could something other than the variable explain the results?
4. **Regressions**: were losses/regressions analyzed, not just gains?
5. **Generalizability**: does a result on subset X transfer to the full benchmark?
6. **Completeness**: was failure analysis done? Were edge cases considered?

### Integration with Experiment Flow

```
experiment execution → report → peer review → [consensus or iterate] → final verdict
                                                                          ↓
                                                              update ledger + status
```

The peer review gate sits between experiment completion and final ACCEPT/REJECT. No experiment verdict is final without passing this gate.

**Rule:** An experiment can be REJECTED without peer review (clear failure), but cannot be ACCEPTED without peer review consensus.
