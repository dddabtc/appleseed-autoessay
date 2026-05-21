"""PR-D4 diff — compare a candidate ``evaluator.json`` against a frozen
baseline ``evaluator.json``, applying field-specific tolerance rules.

Codex round-1 A4: NOT a uniform ±15%. Per-field rules:

  exact-zero (``integrity_p0`` / ``fabricated_citations`` /
              ``fallback_events``)
    Any positive value in candidate = regression (severity = blocker).
  higher-is-better with hard floor (``manuscript_bytes``):
    Hard floor: ``MANUSCRIPT_HARD_FLOOR`` (25k bytes; the same baseline
    real-paper.spec.ts uses).
    Soft drop: 5% below baseline = regression.
  higher-is-better with soft drop (``claim_density`` /
                                    ``stop_slop_total`` /
                                    ``manuscript_citations``)
    Drop more than ``SOFT_DROP_TOLERANCE`` = regression.

The diff emits a PR-comment-friendly Markdown table to stdout; the
acceptance.yml workflow concatenates these into ``acceptance_report.md``
and posts a single PR comment.

Usage:

    python backend/scripts/diff_baselines.py \
        --baseline backend/baselines/case_analysis/<id>/evaluator.json \
        --candidate /tmp/eval_pr.json \
        [--label "PR #N case_analysis"]
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MANUSCRIPT_HARD_FLOOR = 25_000  # bytes — matches real-paper.spec.ts baseline
MANUSCRIPT_SOFT_DROP = 0.05  # 5% drop below baseline triggers warn / regression
SOFT_DROP_TOLERANCE = 0.15  # 15% drop on claim density / stop-slop / citations

EXACT_ZERO_FIELDS: set[str] = {"integrity_p0", "fabricated_citations", "fallback_events"}
SOFT_DROP_FIELDS: set[str] = {"claim_density", "stop_slop_total", "manuscript_citations"}


def diff_evaluator(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Compare the two evaluator payloads; return a dict with
    ``status`` ("pass" / "warn" / "fail"), per-field deltas, and a
    Markdown summary.

    PR-I2 retro fix #2 (codex retrospective B1): when the baseline is
    ``baseline_confirmed``, soft-drop verdicts (manuscript_bytes 5%
    drop / claim_density / stop_slop_total / manuscript_citations 15%
    drop) are promoted to ``fail`` — confirmed baselines are the
    regression floor and a 15% drop on a confirmed baseline IS a
    regression that must block. ``baseline_candidate`` keeps the
    advisory-only ``warn`` semantics so the skeleton stage stays
    non-blocking.
    """
    fields = baseline.get("vector_fields") or candidate.get("vector_fields") or []
    base_vec = dict(zip(fields, baseline.get("vector", []), strict=False))
    cand_vec = dict(zip(fields, candidate.get("vector", []), strict=False))
    confirmed = baseline.get("baseline_status") == "baseline_confirmed"
    deltas: list[dict[str, Any]] = []
    overall = "pass"

    for field in fields:
        base = base_vec.get(field)
        cand = cand_vec.get(field)
        verdict = _verdict_for(field, base, cand, confirmed=confirmed)
        deltas.append({"field": field, "baseline": base, "candidate": cand, **verdict})
        if verdict["status"] == "fail":
            overall = "fail"
        elif verdict["status"] == "warn" and overall != "fail":
            overall = "warn"

    return {
        "status": overall,
        "deltas": deltas,
        "baseline_label": baseline.get("baseline_label"),
        "baseline_status": baseline.get("baseline_status"),
        "markdown": _render_markdown(overall, deltas, baseline, candidate),
    }


def _verdict_for(field: str, base: Any, cand: Any, *, confirmed: bool = False) -> dict[str, Any]:
    """Returns ``{"status": pass|warn|fail, "reason": str}`` for a
    single vector field. Status semantics:

      * pass — within tolerance
      * warn — outside tolerance but baseline is unconfirmed
        (skeleton stage); shown but doesn't block
      * fail — exact-zero violation OR confirmed-baseline drop beyond
        tolerance. The acceptance.yml job (skeleton) reports fail
        as warn until ``baseline_status`` reaches
        ``baseline_confirmed``.
    """
    if base is None and cand is None:
        return {"status": "pass", "reason": "field absent in both"}
    if cand is None:
        return {"status": "fail", "reason": "candidate missing field"}
    if base is None:
        return {"status": "pass", "reason": "no baseline yet"}

    if field in EXACT_ZERO_FIELDS:
        if isinstance(cand, (int, float)) and cand <= 0:
            return {"status": "pass", "reason": "exact-zero held"}
        return {
            "status": "fail",
            "reason": f"exact-zero violation: candidate={cand}",
        }

    # higher-is-better. PR-I2 retro fix #2: ``confirmed`` flips
    # soft-drop verdicts from ``warn`` to ``fail`` so D4.1's
    # required-check semantics actually block regressions.
    soft_status = "fail" if confirmed else "warn"
    if field == "manuscript_bytes":
        if cand < MANUSCRIPT_HARD_FLOOR:
            return {
                "status": "fail",
                "reason": f"manuscript_bytes={cand} below hard floor {MANUSCRIPT_HARD_FLOOR}",
            }
        if base > 0 and cand < base * (1 - MANUSCRIPT_SOFT_DROP):
            drop = (base - cand) / base * 100.0
            tolerance_pct = MANUSCRIPT_SOFT_DROP * 100
            return {
                "status": soft_status,
                "reason": f"dropped {drop:.1f}% below baseline (tolerance {tolerance_pct:.0f}%)",
            }
        return {"status": "pass", "reason": "within tolerance"}

    if field in SOFT_DROP_FIELDS:
        if base > 0 and cand < base * (1 - SOFT_DROP_TOLERANCE):
            drop = (base - cand) / base * 100.0
            tolerance_pct = SOFT_DROP_TOLERANCE * 100
            return {
                "status": soft_status,
                "reason": f"dropped {drop:.1f}% below baseline (tolerance {tolerance_pct:.0f}%)",
            }
        return {"status": "pass", "reason": "within tolerance"}

    # Unknown field — pass-through
    return {"status": "pass", "reason": "no rule for field"}


def _render_markdown(
    overall: str,
    deltas: list[dict[str, Any]],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> str:
    label = baseline.get("baseline_label") or "(unlabeled)"
    status = baseline.get("baseline_status", "unknown")
    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(overall, "❓")
    lines: list[str] = [
        f"### {icon} acceptance gate vs `{label}` ({status})",
        "",
        "| field | baseline | candidate | status | reason |",
        "|---|---|---|---|---|",
    ]
    for d in deltas:
        s_icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}.get(d["status"], "❓")
        row = (
            f"| `{d['field']}` | {d['baseline']} | {d['candidate']} | "
            f"{s_icon} {d['status']} | {d['reason']} |"
        )
        lines.append(row)
    if status != "baseline_confirmed":
        lines.append("")
        lines.append(
            "> **Skeleton stage** — baseline is `baseline_candidate`; this comment is "
            "advisory and does not block merge."
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="path to the frozen baseline's evaluator.json",
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        required=True,
        help="path to the PR-side evaluator.json to compare",
    )
    parser.add_argument(
        "--label",
        type=str,
        default=None,
        help="override the baseline_label in the markdown header",
    )
    parser.add_argument(
        "--exit-fail-on-fail",
        action="store_true",
        help=(
            "exit code 1 if overall status is fail (default: always 0; CI uses "
            "--exit-fail-on-fail only after baseline_confirmed)"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    if args.label:
        baseline = {**baseline, "baseline_label": args.label}

    result = diff_evaluator(baseline, candidate)
    print(result["markdown"])
    if args.exit_fail_on_fail and result["status"] == "fail":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
