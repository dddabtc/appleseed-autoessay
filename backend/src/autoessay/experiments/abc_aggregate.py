"""Aggregation and threshold application for ABC experiment judge outputs."""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoessay.experiments.abc_architecture import EXPERIMENT_ID
from autoessay.experiments.abc_blinder import ARMS as SECTION8_ARMS
from autoessay.experiments.abc_judge_schema import (
    DIMENSIONS,
    JUDGE_IDS,
    validate_judge_output,
)

TIE_BAND = 0.3
KERNEL_WIN_DELTA = 0.5
HIGH_DISAGREEMENT_DELTA = 2.0
EXTRA_AGGREGATE_ARMS: tuple[str, ...] = ("E", "F", "G")
VALID_AGGREGATE_ARMS: tuple[str, ...] = (*SECTION8_ARMS, *EXTRA_AGGREGATE_ARMS)


@dataclass(frozen=True)
class SubmissionAggregate:
    submission_uuid: str
    kernel_id: str
    arm: str
    provider: str | None
    provider_model: str | None
    judgeable: bool
    valid_judge_count: int
    dimension_scores: dict[str, float]
    overall_score: float | None
    disagreement: dict[str, dict[str, object]]
    judge_scores: list[dict[str, object]]
    budget_exceeded: bool


@dataclass(frozen=True)
class ArmSummary:
    arm: str
    arm_median: float | None
    dimension_medians: dict[str, float | None]
    judgeable_kernels: tuple[str, ...]
    unjudgeable_kernels: tuple[str, ...]
    budget_exceeded_kernels: tuple[str, ...]


@dataclass(frozen=True)
class ThresholdDecision:
    order: int
    condition: str
    conclusion: str
    roadmap_action: str

    def to_json(self) -> dict[str, object]:
        return {
            "order": self.order,
            "condition": self.condition,
            "conclusion": self.conclusion,
            "roadmap_action": self.roadmap_action,
        }


THRESHOLD_TABLE: tuple[ThresholdDecision, ...] = (
    ThresholdDecision(
        1,
        "B fails to produce judgeable manuscripts for >= 4 kernels for implementation "
        "reasons unrelated to model quality",
        "Experiment implementation failed",
        "Fix B runner and rerun; do not decide architecture",
    ),
    ThresholdDecision(
        2,
        "B' fails to produce judgeable manuscripts for >= 4 kernels for implementation "
        "reasons unrelated to model quality while B is judgeable",
        "Critic-control implementation failed",
        "Fix B' runner and rerun B' against the same frozen B manuscripts before deciding "
        "whether A's lead is architectural or critic-only",
    ),
    ThresholdDecision(
        3,
        "A fails to reach a judgeable manuscript for >= 4 kernels while B, B', and C are judgeable",
        "Production path is operationally too brittle for this workload",
        "Start evidence-first refactor, keeping A artifacts for failure analysis",
    ),
    ThresholdDecision(
        4,
        "C has arm_median(C) >= arm_median(A), arm_median(C) >= arm_median(B), "
        "and arm_median(C) >= arm_median(B')",
        "Retrieval/evidence value is not visible under this judge protocol, or all "
        "evidence-aware arms underuse sources",
        "Pause architecture cuts; audit source-use scoring and judge design before refactor",
    ),
    ThresholdDecision(
        5,
        "arm_median(B) >= arm_median(A)",
        "B is equal or better in overall quality without self-critique",
        "Cut the middle and late phases from the default roadmap; start evidence-first "
        "refactor without a required critic loop",
    ),
    ThresholdDecision(
        6,
        "arm_median(B) > arm_median(A) - 0.3 and no dimension_median(B, dim) is "
        "worse than A by >= 0.5",
        "B is non-inferior within the tie band without self-critique",
        "Cut the middle and late phases; cost and complexity decide against A",
    ),
    ThresholdDecision(
        7,
        "B wins exactly 1 dimension over A by >= 0.5, and the other 2 dimensions "
        "are tie-band or small-loss",
        "B carries at least one real quality advantage without a decisive quality loss",
        "Cut the middle and late phases; keep deterministic compliance repair",
    ),
    ThresholdDecision(
        8,
        "arm_median(B') >= arm_median(A)",
        "One bounded self-critique pass closes or reverses A's advantage",
        "Cut the middle and late phases; roadmap becomes evidence-first composer plus "
        "one self-critique pass",
    ),
    ThresholdDecision(
        9,
        "arm_median(B') > arm_median(A) - 0.3 and no dimension_median(B', dim) is "
        "worse than A by >= 0.5",
        "B' is non-inferior within the tie band",
        "Cut the middle and late phases; keep one bounded self-critique pass as the "
        "quality-control candidate",
    ),
    ThresholdDecision(
        10,
        "A has kernel_win(A, B', k) on >= 5 kernels and arm_median(A) - arm_median(B') >= 0.5",
        "Current architecture has measurable value beyond one self-critique pass",
        "Keep 13-phase architecture for now; focus optimization on evidence handoff and "
        "middle/late phase quality",
    ),
    ThresholdDecision(
        11,
        "A, B, B', and C are all pairwise within the tie band on arm_median, "
        "and no arm has a stable win on >= 4 kernels",
        "The evaluation system is not discriminating enough",
        "Fix evaluation first; add source-use and evidence-grounding checks; rerun",
    ),
    ThresholdDecision(
        12,
        "None of the above",
        "Inconclusive mixed result",
        "Write kernel-level diagnosis; do not change default architecture until a narrower "
        "follow-up resolves the split",
    ),
)

NON_SECTION8_THRESHOLD_DECISION = ThresholdDecision(
    0,
    "Selected arms do not include the full Section 8 A/B/B'/C threshold set",
    "Section 8 threshold table not applied",
    "Use the experiment-specific aggregate comparisons and verdict for this arm set",
)


def aggregate_results(
    *,
    results_dir: str | Path,
    write_files: bool = True,
) -> dict[str, object]:
    """Aggregate completed judge outputs and optionally write result files."""
    root = Path(results_dir)
    judged_payloads = _load_completed_judge_outputs(root)
    blind_entries = _load_blind_map_after_scoring_complete(root)
    submissions = _aggregate_submissions(root, blind_entries, judged_payloads)
    report = build_aggregate_report(submissions)
    if write_files:
        _write_json(root / "aggregate.json", report)
        _write_text(root / "aggregate.md", render_aggregate_markdown(report))
    return report


def build_aggregate_report(submissions: Sequence[SubmissionAggregate]) -> dict[str, object]:
    kernel_ids = tuple(sorted({submission.kernel_id for submission in submissions}))
    arms_order = _ordered_arms(submission.arm for submission in submissions)
    by_kernel_arm = _index_by_kernel_arm(submissions)
    arm_summaries = _summarize_arms(kernel_ids, by_kernel_arm, arms_order)
    threshold = apply_thresholds(
        kernel_ids=kernel_ids, by_kernel_arm=by_kernel_arm, arms=arm_summaries
    )
    return {
        "experiment_id": EXPERIMENT_ID,
        "generated_at": _utc_now(),
        "arms_order": list(arms_order),
        "judge_ids": list(JUDGE_IDS),
        "dimensions": list(DIMENSIONS),
        "formulas": {
            "dimension_score": "median of 3 judges' overall_scores[dim]",
            "overall_score": "mean of compliance, novelty, completeness dimension_scores",
            "arm_median": "median of overall_score across kernels",
            "dimension_median": "median of dimension_score across kernels",
            "kernel_win": "overall_score(x) - overall_score(y) >= 0.5",
            "tie_band": "abs(diff) < 0.3",
            "high_disagreement": "max_judge_score - min_judge_score >= 2.0",
        },
        "sensitivity": {
            "provider_model_distribution": _provider_model_distribution(submissions, arms_order),
            "b_b_prime_provider_mismatch_kernels": _b_b_prime_provider_mismatch_kernels(
                submissions
            ),
        },
        "submissions": [_submission_to_json(submission) for submission in submissions],
        "kernels": _kernels_to_json(kernel_ids, by_kernel_arm, arms_order),
        "arms": {
            arm: {
                "arm_median": summary.arm_median,
                "dimension_medians": summary.dimension_medians,
                "judgeable_kernels": list(summary.judgeable_kernels),
                "unjudgeable_kernels": list(summary.unjudgeable_kernels),
                "budget_exceeded_kernels": list(summary.budget_exceeded_kernels),
            }
            for arm, summary in arm_summaries.items()
        },
        "threshold_decision": threshold.to_json(),
    }


def apply_thresholds(
    *,
    kernel_ids: Sequence[str],
    by_kernel_arm: Mapping[tuple[str, str], SubmissionAggregate],
    arms: Mapping[str, ArmSummary],
) -> ThresholdDecision:
    """Apply the Section 8 threshold table in first-match order."""
    if not set(SECTION8_ARMS).issubset(arms):
        return NON_SECTION8_THRESHOLD_DECISION

    b_failures = arms["B"].unjudgeable_kernels
    if len(b_failures) >= 4:
        return THRESHOLD_TABLE[0]

    b_prime_failures_while_b_judgeable = [
        kernel_id
        for kernel_id in kernel_ids
        if not _is_judgeable(by_kernel_arm, kernel_id, "B_prime")
        and _is_judgeable(by_kernel_arm, kernel_id, "B")
    ]
    if len(b_prime_failures_while_b_judgeable) >= 4:
        return THRESHOLD_TABLE[1]

    a_failures_while_others_judgeable = [
        kernel_id
        for kernel_id in kernel_ids
        if not _is_judgeable(by_kernel_arm, kernel_id, "A")
        and all(_is_judgeable(by_kernel_arm, kernel_id, arm) for arm in ("B", "B_prime", "C"))
    ]
    if len(a_failures_while_others_judgeable) >= 4:
        return THRESHOLD_TABLE[2]

    medians = {arm: arms[arm].arm_median for arm in SECTION8_ARMS}
    if _all_scores_present(medians.values()):
        c_median = _require_score(medians["C"])
        if all(c_median >= _require_score(medians[arm]) for arm in ("A", "B", "B_prime")):
            return THRESHOLD_TABLE[3]

    if _score_pair_present(medians.get("B"), medians.get("A")):
        b_median = _require_score(medians["B"])
        a_median = _require_score(medians["A"])
        if b_median >= a_median:
            return THRESHOLD_TABLE[4]
        if b_median > a_median - TIE_BAND and not _dimension_worse_by(arms["B"], arms["A"]):
            return THRESHOLD_TABLE[5]
        if _b_dimension_advantage_condition(arms["B"], arms["A"]):
            return THRESHOLD_TABLE[6]

    if _score_pair_present(medians.get("B_prime"), medians.get("A")):
        bp_median = _require_score(medians["B_prime"])
        a_median = _require_score(medians["A"])
        if bp_median >= a_median:
            return THRESHOLD_TABLE[7]
        if bp_median > a_median - TIE_BAND and not _dimension_worse_by(arms["B_prime"], arms["A"]):
            return THRESHOLD_TABLE[8]

    if _score_pair_present(medians.get("A"), medians.get("B_prime")):
        a_median = _require_score(medians["A"])
        bp_median = _require_score(medians["B_prime"])
        a_kernel_wins = sum(
            1
            for kernel_id in kernel_ids
            if _kernel_diff(by_kernel_arm, kernel_id, "A", "B_prime") >= KERNEL_WIN_DELTA
        )
        if a_kernel_wins >= 5 and a_median - bp_median >= KERNEL_WIN_DELTA:
            return THRESHOLD_TABLE[9]

    if (
        _all_scores_present(medians.values())
        and _all_arm_medians_pairwise_tied(medians, SECTION8_ARMS)
        and not _has_stable_kernel_win(kernel_ids, by_kernel_arm, SECTION8_ARMS)
    ):
        return THRESHOLD_TABLE[10]

    return THRESHOLD_TABLE[11]


def render_aggregate_markdown(report: Mapping[str, object]) -> str:
    threshold = _mapping(report["threshold_decision"])
    arms_order = _report_arms(report)
    lines = [
        "# ABC Architecture Aggregate",
        "",
        "## Threshold Decision",
        "",
        f"- Order: {threshold['order']}",
        f"- Conclusion: {threshold['conclusion']}",
        f"- Roadmap action: {threshold['roadmap_action']}",
        f"- Condition: {threshold['condition']}",
        "",
        "## Arm Medians",
        "",
        "| Arm | Overall median | Compliance | Novelty | Completeness | Unjudgeable kernels |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    arms = _mapping(report["arms"])
    for arm in arms_order:
        summary = _mapping(arms[arm])
        dimensions = _mapping(summary["dimension_medians"])
        unjudgeable = _sequence(summary["unjudgeable_kernels"])
        lines.append(
            "| "
            f"{arm} | "
            f"{_fmt_score(summary['arm_median'])} | "
            f"{_fmt_score(dimensions['compliance'])} | "
            f"{_fmt_score(dimensions['novelty'])} | "
            f"{_fmt_score(dimensions['completeness'])} | "
            f"{len(unjudgeable)} |"
        )

    lines.extend(
        [
            "",
            "## Per Kernel",
            "",
            "| Kernel | " + " | ".join(arms_order) + " |",
            "|---" + "|---:" * len(arms_order) + "|",
        ]
    )
    kernels = _mapping(report["kernels"])
    for kernel_id in sorted(kernels):
        kernel = _mapping(kernels[kernel_id])
        kernel_arms = _mapping(kernel["arms"])
        lines.append(
            "| "
            f"{kernel_id} | "
            + " | ".join(_kernel_arm_score(kernel_arms, arm) for arm in arms_order)
            + " |"
        )

    lines.extend(["", "## Provider/Model Sensitivity", ""])
    lines.extend(_provider_model_sensitivity_lines(report))
    lines.extend(["", "## High Disagreement", ""])
    high_lines = _high_disagreement_lines(report)
    lines.extend(high_lines or ["None."])
    lines.append("")
    return "\n".join(lines)


def _load_completed_judge_outputs(root: Path) -> dict[str, list[dict[str, object]]]:
    if not root.is_dir():
        raise FileNotFoundError(f"Results directory does not exist: {root}")
    manuscript_paths = sorted(root.glob("*/blinded/*/manuscript.md"))
    if not manuscript_paths:
        raise ValueError(f"No blinded submissions found below {root}")
    judged_payloads: dict[str, list[dict[str, object]]] = {}
    for manuscript_path in manuscript_paths:
        submission_uuid = manuscript_path.parent.name
        payloads: list[dict[str, object]] = []
        for judge_id in JUDGE_IDS:
            judge_path = manuscript_path.parent / f"judge-{judge_id}.json"
            if not judge_path.exists():
                raise ValueError(
                    "Scoring is incomplete; refusing to read blind_map.json before all "
                    f"3 judge outputs exist. Missing {judge_path}"
                )
            payload = _load_json_object(judge_path)
            ok, errors = validate_judge_output(payload)
            if not ok:
                raise ValueError(f"Invalid judge output at {judge_path}: {errors}")
            if payload.get("judge_id") != judge_id:
                raise ValueError(f"Judge id mismatch in {judge_path}")
            if payload.get("submission_uuid") != submission_uuid:
                raise ValueError(f"Submission UUID mismatch in {judge_path}")
            payloads.append(payload)
        judged_payloads[submission_uuid] = payloads
    return judged_payloads


def _load_blind_map_after_scoring_complete(root: Path) -> list[dict[str, str]]:
    path = root / "blind_map.json"
    payload = _load_json_object(path)
    if payload.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError(f"blind_map.json experiment_id must be {EXPERIMENT_ID}")
    raw_submissions = payload.get("submissions")
    if not isinstance(raw_submissions, list):
        raise ValueError("blind_map.json submissions must be an array")
    entries: list[dict[str, str]] = []
    for index, raw_entry in enumerate(raw_submissions):
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"blind_map.json submissions[{index}] must be an object")
        submission_uuid = raw_entry.get("submission_uuid")
        kernel_id = raw_entry.get("kernel_id")
        arm = raw_entry.get("arm")
        if (
            not isinstance(submission_uuid, str)
            or not isinstance(kernel_id, str)
            or arm not in VALID_AGGREGATE_ARMS
        ):
            raise ValueError(f"Invalid blind_map.json submissions[{index}]")
        entries.append(
            {"submission_uuid": submission_uuid, "kernel_id": kernel_id, "arm": str(arm)}
        )
    return entries


def _aggregate_submissions(
    root: Path,
    blind_entries: Sequence[Mapping[str, str]],
    judged_payloads: Mapping[str, list[dict[str, object]]],
) -> tuple[SubmissionAggregate, ...]:
    blind_uuids = {entry["submission_uuid"] for entry in blind_entries}
    extra_judged = set(judged_payloads) - blind_uuids
    if extra_judged:
        raise ValueError(
            f"Judge outputs exist for submissions missing from blind_map.json: {extra_judged}"
        )
    submissions: list[SubmissionAggregate] = []
    seen_kernel_arms: set[tuple[str, str]] = set()
    for entry in blind_entries:
        submission_uuid = entry["submission_uuid"]
        kernel_id = entry["kernel_id"]
        arm = entry["arm"]
        key = (kernel_id, arm)
        if key in seen_kernel_arms:
            raise ValueError(f"Duplicate blind_map entry for kernel/arm {kernel_id}/{arm}")
        seen_kernel_arms.add(key)
        payloads = judged_payloads.get(submission_uuid)
        if payloads is None:
            raise ValueError(
                f"blind_map submission {submission_uuid} has no completed judge outputs"
            )
        submissions.append(
            _aggregate_one_submission(
                root=root,
                submission_uuid=submission_uuid,
                kernel_id=kernel_id,
                arm=arm,
                payloads=payloads,
            )
        )
    return tuple(submissions)


def _aggregate_one_submission(
    *,
    root: Path,
    submission_uuid: str,
    kernel_id: str,
    arm: str,
    payloads: Sequence[Mapping[str, object]],
) -> SubmissionAggregate:
    valid_payloads = tuple(
        payload for payload in payloads if _mapping(payload["validity"]).get("can_score") is True
    )
    valid_judge_count = len(valid_payloads)
    judgeable = valid_judge_count >= 2
    dimension_scores: dict[str, float] = {}
    disagreement: dict[str, dict[str, object]] = {}
    for dimension in DIMENSIONS:
        scores = [
            float(_mapping(payload["overall_scores"])[dimension]) for payload in valid_payloads
        ]
        if judgeable:
            dimension_scores[dimension] = float(statistics.median(scores))
        spread = max(scores) - min(scores) if scores else None
        disagreement[dimension] = {
            "spread": spread,
            "high_disagreement": spread is not None and spread >= HIGH_DISAGREEMENT_DELTA,
        }
    overall_score = (
        sum(dimension_scores[dimension] for dimension in DIMENSIONS) / len(DIMENSIONS)
        if judgeable
        else None
    )
    provider, provider_model = _provider_model(root, kernel_id, arm)
    return SubmissionAggregate(
        submission_uuid=submission_uuid,
        kernel_id=kernel_id,
        arm=arm,
        provider=provider,
        provider_model=provider_model,
        judgeable=judgeable,
        valid_judge_count=valid_judge_count,
        dimension_scores=dimension_scores,
        overall_score=overall_score,
        disagreement=disagreement,
        judge_scores=[
            {
                "judge_id": payload["judge_id"],
                "validity": payload["validity"],
                "overall_scores": payload["overall_scores"],
            }
            for payload in payloads
        ],
        budget_exceeded=_budget_exceeded(root, kernel_id, arm),
    )


def _summarize_arms(
    kernel_ids: Sequence[str],
    by_kernel_arm: Mapping[tuple[str, str], SubmissionAggregate],
    arms_order: Sequence[str],
) -> dict[str, ArmSummary]:
    summaries: dict[str, ArmSummary] = {}
    for arm in arms_order:
        judgeable_submissions = [
            submission
            for kernel_id in kernel_ids
            if (submission := by_kernel_arm.get((kernel_id, arm))) is not None
            and submission.judgeable
        ]
        dimension_medians: dict[str, float | None] = {}
        for dimension in DIMENSIONS:
            dimension_medians[dimension] = _median_or_none(
                submission.dimension_scores[dimension] for submission in judgeable_submissions
            )
        summaries[arm] = ArmSummary(
            arm=arm,
            arm_median=_median_or_none(
                submission.overall_score
                for submission in judgeable_submissions
                if submission.overall_score is not None
            ),
            dimension_medians=dimension_medians,
            judgeable_kernels=tuple(submission.kernel_id for submission in judgeable_submissions),
            unjudgeable_kernels=tuple(
                kernel_id
                for kernel_id in kernel_ids
                if not _is_judgeable(by_kernel_arm, kernel_id, arm)
            ),
            budget_exceeded_kernels=tuple(
                submission.kernel_id
                for submission in judgeable_submissions
                if submission.budget_exceeded
            ),
        )
    return summaries


def _index_by_kernel_arm(
    submissions: Sequence[SubmissionAggregate],
) -> dict[tuple[str, str], SubmissionAggregate]:
    indexed: dict[tuple[str, str], SubmissionAggregate] = {}
    for submission in submissions:
        indexed[(submission.kernel_id, submission.arm)] = submission
    return indexed


def _ordered_arms(arms: Sequence[str] | Any) -> tuple[str, ...]:
    seen = {str(arm) for arm in arms}
    ordered = [arm for arm in VALID_AGGREGATE_ARMS if arm in seen]
    ordered.extend(sorted(seen - set(ordered)))
    return tuple(ordered)


def _kernels_to_json(
    kernel_ids: Sequence[str],
    by_kernel_arm: Mapping[tuple[str, str], SubmissionAggregate],
    arms_order: Sequence[str],
) -> dict[str, object]:
    kernels: dict[str, object] = {}
    for kernel_id in kernel_ids:
        arms = {
            arm: _submission_to_json(submission)
            for arm in arms_order
            if (submission := by_kernel_arm.get((kernel_id, arm))) is not None
        }
        comparisons: dict[str, object] = {}
        for left in arms_order:
            for right in arms_order:
                if left == right:
                    continue
                diff = _kernel_diff(by_kernel_arm, kernel_id, left, right)
                comparisons[f"{left}_vs_{right}"] = {
                    "diff": None if diff == float("-inf") else diff,
                    "kernel_win": diff >= KERNEL_WIN_DELTA,
                    "tie_band": abs(diff) < TIE_BAND if diff != float("-inf") else False,
                }
        kernels[kernel_id] = {
            "arms": arms,
            "comparisons": comparisons,
        }
    return kernels


def _submission_to_json(submission: SubmissionAggregate) -> dict[str, object]:
    return {
        "submission_uuid": submission.submission_uuid,
        "kernel_id": submission.kernel_id,
        "arm": submission.arm,
        "provider": submission.provider,
        "provider_model": submission.provider_model,
        "judgeable": submission.judgeable,
        "valid_judge_count": submission.valid_judge_count,
        "dimension_scores": submission.dimension_scores,
        "overall_score": submission.overall_score,
        "disagreement": submission.disagreement,
        "judge_scores": submission.judge_scores,
        "budget_exceeded": submission.budget_exceeded,
    }


def _provider_model_distribution(
    submissions: Sequence[SubmissionAggregate],
    arms_order: Sequence[str],
) -> dict[str, list[dict[str, object]]]:
    by_arm: dict[str, dict[tuple[str, str], dict[str, object]]] = {arm: {} for arm in arms_order}
    for submission in submissions:
        provider = submission.provider or "unknown"
        provider_model = submission.provider_model or "unknown"
        bucket = by_arm[submission.arm].setdefault(
            (provider, provider_model),
            {
                "provider": provider,
                "provider_model": provider_model,
                "count": 0,
                "kernels": [],
            },
        )
        count = bucket.get("count")
        bucket["count"] = (count if isinstance(count, int) else 0) + 1
        kernels = bucket.get("kernels")
        if isinstance(kernels, list):
            kernels.append(submission.kernel_id)
    return {
        arm: sorted(
            arm_buckets.values(),
            key=lambda item: (str(item["provider"]), str(item["provider_model"])),
        )
        for arm, arm_buckets in by_arm.items()
    }


def _b_b_prime_provider_mismatch_kernels(
    submissions: Sequence[SubmissionAggregate],
) -> list[dict[str, object]]:
    by_kernel_arm = {
        (submission.kernel_id, submission.arm): submission for submission in submissions
    }
    kernel_ids = sorted({submission.kernel_id for submission in submissions})
    mismatches: list[dict[str, object]] = []
    for kernel_id in kernel_ids:
        b = by_kernel_arm.get((kernel_id, "B"))
        b_prime = by_kernel_arm.get((kernel_id, "B_prime"))
        if b is None or b_prime is None:
            continue
        b_pair = (b.provider or "unknown", b.provider_model or "unknown")
        b_prime_pair = (b_prime.provider or "unknown", b_prime.provider_model or "unknown")
        if b_pair != b_prime_pair:
            mismatches.append(
                {
                    "kernel_id": kernel_id,
                    "interpretation": "self-critique_with_provider_model_confound",
                    "B": {"provider": b_pair[0], "provider_model": b_pair[1]},
                    "B_prime": {
                        "provider": b_prime_pair[0],
                        "provider_model": b_prime_pair[1],
                    },
                }
            )
    return mismatches


def _b_dimension_advantage_condition(candidate: ArmSummary, baseline: ArmSummary) -> bool:
    diffs = _dimension_diffs(candidate, baseline)
    if len(diffs) != len(DIMENSIONS):
        return False
    wins = [diff for diff in diffs if diff >= KERNEL_WIN_DELTA]
    others = [diff for diff in diffs if diff < KERNEL_WIN_DELTA]
    return len(wins) == 1 and all(_is_tie_or_small_loss(diff) for diff in others)


def _dimension_worse_by(
    candidate: ArmSummary,
    baseline: ArmSummary,
    delta: float = KERNEL_WIN_DELTA,
) -> bool:
    return any(diff <= -delta for diff in _dimension_diffs(candidate, baseline))


def _dimension_diffs(candidate: ArmSummary, baseline: ArmSummary) -> list[float]:
    diffs: list[float] = []
    for dimension in DIMENSIONS:
        candidate_score = candidate.dimension_medians.get(dimension)
        baseline_score = baseline.dimension_medians.get(dimension)
        if candidate_score is None or baseline_score is None:
            return []
        diffs.append(candidate_score - baseline_score)
    return diffs


def _is_tie_or_small_loss(diff: float) -> bool:
    return abs(diff) < TIE_BAND or (-KERNEL_WIN_DELTA <= diff < -TIE_BAND)


def _all_arm_medians_pairwise_tied(
    medians: Mapping[str, float | None], arms_order: Sequence[str]
) -> bool:
    for index, left in enumerate(arms_order):
        for right in arms_order[index + 1 :]:
            left_score = medians.get(left)
            right_score = medians.get(right)
            if left_score is None or right_score is None:
                return False
            if abs(left_score - right_score) >= TIE_BAND:
                return False
    return True


def _has_stable_kernel_win(
    kernel_ids: Sequence[str],
    by_kernel_arm: Mapping[tuple[str, str], SubmissionAggregate],
    arms_order: Sequence[str],
) -> bool:
    for left in arms_order:
        for right in arms_order:
            if left == right:
                continue
            wins = sum(
                1
                for kernel_id in kernel_ids
                if _kernel_diff(by_kernel_arm, kernel_id, left, right) >= KERNEL_WIN_DELTA
            )
            if wins >= 4:
                return True
    return False


def _kernel_diff(
    by_kernel_arm: Mapping[tuple[str, str], SubmissionAggregate],
    kernel_id: str,
    left: str,
    right: str,
) -> float:
    left_submission = by_kernel_arm.get((kernel_id, left))
    right_submission = by_kernel_arm.get((kernel_id, right))
    if (
        left_submission is None
        or right_submission is None
        or left_submission.overall_score is None
        or right_submission.overall_score is None
    ):
        return float("-inf")
    return left_submission.overall_score - right_submission.overall_score


def _is_judgeable(
    by_kernel_arm: Mapping[tuple[str, str], SubmissionAggregate],
    kernel_id: str,
    arm: str,
) -> bool:
    submission = by_kernel_arm.get((kernel_id, arm))
    return submission is not None and submission.judgeable


def _budget_exceeded(root: Path, kernel_id: str, arm: str) -> bool:
    provenance_path = root / kernel_id / arm / "provenance.json"
    if not provenance_path.exists():
        return False
    try:
        payload = _load_json_object(provenance_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    token_usage = payload.get("token_usage")
    if not isinstance(token_usage, Mapping):
        return False
    return token_usage.get("budget_exceeded") is True


def _provider_model(root: Path, kernel_id: str, arm: str) -> tuple[str | None, str | None]:
    provenance_path = root / kernel_id / arm / "provenance.json"
    if not provenance_path.exists():
        return None, None
    try:
        payload = _load_json_object(provenance_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None
    provider = payload.get("provider")
    provider_model = payload.get("provider_model")
    return (
        provider.strip() if isinstance(provider, str) and provider.strip() else None,
        provider_model.strip()
        if isinstance(provider_model, str) and provider_model.strip()
        else None,
    )


def _median_or_none(values: Sequence[float] | Any) -> float | None:
    materialized = [float(value) for value in values if value is not None]
    if not materialized:
        return None
    return float(statistics.median(materialized))


def _all_scores_present(values: Sequence[float | None] | Any) -> bool:
    return all(value is not None for value in values)


def _score_pair_present(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None


def _require_score(value: float | None) -> float:
    if value is None:
        raise ValueError("Expected score to be present")
    return value


def _load_json_object(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return data


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fmt_score(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    return str(value)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected mapping")
    return value


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("expected sequence")
    return value


def _report_arms(report: Mapping[str, object]) -> tuple[str, ...]:
    raw = report.get("arms_order")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return tuple(str(arm) for arm in raw)
    arms = _mapping(report["arms"])
    return _ordered_arms(tuple(str(arm) for arm in arms))


def _kernel_arm_score(kernel_arms: Mapping[str, Any], arm: str) -> str:
    raw = kernel_arms.get(arm)
    if not isinstance(raw, Mapping):
        return "n/a"
    return _fmt_score(raw.get("overall_score"))


def _provider_model_sensitivity_lines(report: Mapping[str, object]) -> list[str]:
    arms_order = _report_arms(report)
    sensitivity = _mapping(report["sensitivity"])
    distribution = _mapping(sensitivity["provider_model_distribution"])
    lines = [
        "| Arm | Provider | Provider model | Count | Kernels |",
        "|---|---|---|---:|---|",
    ]
    for arm in arms_order:
        for raw_entry in _sequence(distribution[arm]):
            entry = _mapping(raw_entry)
            kernels = ", ".join(str(kernel) for kernel in _sequence(entry["kernels"]))
            lines.append(
                "| "
                f"{arm} | "
                f"{entry['provider']} | "
                f"{entry['provider_model']} | "
                f"{entry['count']} | "
                f"{kernels} |"
            )
    mismatches = _sequence(sensitivity["b_b_prime_provider_mismatch_kernels"])
    if mismatches:
        lines.extend(["", "B/B' provider-model mismatches:"])
        for raw_entry in mismatches:
            entry = _mapping(raw_entry)
            lines.append(f"- {entry['kernel_id']}: self-critique with provider/model confound")
    return lines


def _high_disagreement_lines(report: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    for submission in _sequence(report["submissions"]):
        submission_map = _mapping(submission)
        disagreement = _mapping(submission_map["disagreement"])
        high_dims = [
            dimension
            for dimension in DIMENSIONS
            if _mapping(disagreement[dimension]).get("high_disagreement") is True
        ]
        if high_dims:
            lines.append(
                "- "
                f"{submission_map['kernel_id']} {submission_map['arm']} "
                f"({submission_map['submission_uuid']}): {', '.join(high_dims)}"
            )
    return lines
