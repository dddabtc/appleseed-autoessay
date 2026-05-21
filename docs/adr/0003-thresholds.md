# ADR-0003 Production Thresholds

This file defines the production evidence thresholds for ADR-0003 P3.
It is a framework for post-ship review; it does not claim that enough
30-day or 60-day data exists at P3 merge time.

## Data Sources

- `run_telemetry` is the source for completed or stopped run evidence:
  mode, total tokens, wall-clock latency, audit status, manuscript
  length, finish time, and failure code.
- `runs.generation_mode` is the source for user selection share,
  including runs that have not finished yet.
- The report generator is:

```bash
python -m autoessay.scripts.mode_telemetry_report --since=30d
```

## Express Long-Term Default

Express may remain or become the long-term default only if the same
30-day production window satisfies all of these conditions:

- Sample size: at least 50 express telemetry rows.
- Audit pass rate: at least 0.70 among express rows whose audit status
  is `pass` or `fail`.
- Failure rate: at most 0.10, where any non-null `failure_code` counts
  as a failed production run.
- Median token usage: no more than 60 percent of deep median token usage
  when deep has at least 10 telemetry rows in the same window.
- Median latency: no more than 60 percent of deep median latency when
  deep has at least 10 telemetry rows in the same window.

If deep has too few rows for a cost or latency comparison, the express
default decision can still proceed on sample size, audit pass rate, and
failure rate, but the missing comparison must be called out in the ADR
delta.

## Deep First-Class Retention

Deep remains first-class if any of these conditions is true in the same
30-day production window:

- User selection share for deep is at least 10 percent of non-deleted
  created runs.
- Deep has at least 10 telemetry rows and its audit pass rate is at
  least 0.85.
- At least one documented paper category, customer workflow, or support
  incident requires phase preview, review gates, or paired-runner critic
  behavior that express intentionally does not provide.

Deep can lose default status without losing first-class status. Removing
deep as a first-class mode requires a separate ADR delta and explicit
deprecation plan.

## F Arm Reconsider Trigger

The F arm remains excluded from the shipped dual-mode plan unless
production telemetry shows express quality is materially weak in a
stable sub-domain. Reconsider F only when all of these conditions are
met:

- A stable sub-domain has at least 15 express runs in the review window.
- That sub-domain has a median quality score below 6.0.
- The low score is not explained primarily by setup errors, missing
  source access, or user-abandoned runs.

Meeting this trigger authorizes a new production experiment proposal for
F. It does not automatically add F to runtime generation modes.

## Review Cadence

- 30-day review: decide whether evidence is strong enough to keep
  express as default, switch default, or extend observation.
- 60-day review: confirm or revise the 30-day decision using a larger
  window.
- Any default-mode change must cite the report JSON and update
  `docs/adr/0003-delta-decision-criteria.md`.
