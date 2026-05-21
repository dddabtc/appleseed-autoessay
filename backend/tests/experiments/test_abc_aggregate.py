from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_DNS, uuid5

import pytest

from autoessay.experiments.abc_aggregate import aggregate_results
from autoessay.experiments.abc_blinder import ARMS
from autoessay.experiments.abc_judge_schema import JUDGE_IDS, JUDGE_SCHEMA_VERSION, LEDGER_ITEMS

KERNELS = tuple(f"k{i:02d}" for i in range(10))


def _dims(
    compliance: float,
    novelty: float | None = None,
    completeness: float | None = None,
) -> dict[str, float]:
    return {
        "compliance": compliance,
        "novelty": compliance if novelty is None else novelty,
        "completeness": compliance if completeness is None else completeness,
    }


@pytest.mark.parametrize(
    ("arm_scores", "invalid_pairs", "expected_order"),
    [
        (
            {"A": _dims(7.0), "B": _dims(6.0), "B_prime": _dims(6.5), "C": _dims(5.0)},
            {(kernel_id, "B") for kernel_id in KERNELS[:4]},
            1,
        ),
        (
            {"A": _dims(7.0), "B": _dims(6.0), "B_prime": _dims(6.5), "C": _dims(5.0)},
            {(kernel_id, "B_prime") for kernel_id in KERNELS[:4]},
            2,
        ),
        (
            {"A": _dims(7.0), "B": _dims(6.0), "B_prime": _dims(6.5), "C": _dims(5.0)},
            {(kernel_id, "A") for kernel_id in KERNELS[:4]},
            3,
        ),
        (
            {"A": _dims(7.0), "B": _dims(6.0), "B_prime": _dims(6.5), "C": _dims(7.1)},
            set(),
            4,
        ),
        (
            {"A": _dims(7.0), "B": _dims(7.0), "B_prime": _dims(6.0), "C": _dims(5.0)},
            set(),
            5,
        ),
        (
            {"A": _dims(7.0), "B": _dims(6.8), "B_prime": _dims(6.0), "C": _dims(5.0)},
            set(),
            6,
        ),
        (
            {
                "A": _dims(7.0, 7.0, 7.0),
                "B": _dims(7.6, 6.5, 6.6),
                "B_prime": _dims(6.0),
                "C": _dims(5.0),
            },
            set(),
            7,
        ),
        (
            {"A": _dims(7.0), "B": _dims(5.0), "B_prime": _dims(7.1), "C": _dims(4.0)},
            set(),
            8,
        ),
        (
            {"A": _dims(7.0), "B": _dims(5.0), "B_prime": _dims(6.8), "C": _dims(4.0)},
            set(),
            9,
        ),
        (
            {"A": _dims(7.0), "B": _dims(5.0), "B_prime": _dims(6.4), "C": _dims(4.0)},
            set(),
            10,
        ),
        (
            {
                "A": _dims(7.0, 7.0, 7.0),
                "B": _dims(6.4, 7.15, 7.15),
                "B_prime": _dims(6.4, 7.15, 7.15),
                "C": _dims(6.85),
            },
            set(),
            11,
        ),
        (
            {"A": _dims(7.0), "B": _dims(5.8), "B_prime": _dims(6.69), "C": _dims(4.0)},
            set(),
            12,
        ),
    ],
)
def test_aggregate_applies_each_threshold_order_from_fake_judge_json(
    tmp_path: Path,
    arm_scores: dict[str, dict[str, float]],
    invalid_pairs: set[tuple[str, str]],
    expected_order: int,
) -> None:
    results_dir = _write_fake_results(
        tmp_path,
        arm_scores=arm_scores,
        invalid_pairs=invalid_pairs,
    )

    report = aggregate_results(results_dir=results_dir, write_files=False)

    threshold = report["threshold_decision"]
    assert isinstance(threshold, dict)
    assert threshold["order"] == expected_order


def test_aggregate_writes_outputs_and_reports_disagreement(tmp_path: Path) -> None:
    score_overrides: dict[tuple[str, str, str], dict[str, float]] = {}
    for judge_id, score in zip(JUDGE_IDS, (5.0, 7.0, 9.0), strict=True):
        score_overrides[(KERNELS[0], "A", judge_id)] = _dims(score)
    results_dir = _write_fake_results(
        tmp_path,
        arm_scores={
            "A": _dims(7.0),
            "B": _dims(5.0),
            "B_prime": _dims(6.4),
            "C": _dims(4.0),
        },
        score_overrides=score_overrides,
    )

    report = aggregate_results(results_dir=results_dir, write_files=True)

    assert (results_dir / "aggregate.json").exists()
    assert (results_dir / "aggregate.md").exists()
    kernels = report["kernels"]
    assert isinstance(kernels, dict)
    first_a = kernels[KERNELS[0]]["arms"]["A"]
    assert first_a["dimension_scores"]["compliance"] == 7.0
    assert first_a["disagreement"]["compliance"]["spread"] == 4.0
    assert first_a["disagreement"]["compliance"]["high_disagreement"] is True
    sensitivity = report["sensitivity"]
    assert isinstance(sensitivity, dict)
    distribution = sensitivity["provider_model_distribution"]
    assert isinstance(distribution, dict)
    assert distribution["B"] == [
        {
            "provider": "rightcode",
            "provider_model": "gpt-5.4-mini",
            "count": 10,
            "kernels": list(KERNELS),
        }
    ]
    assert report["threshold_decision"]["order"] == 10


def test_aggregate_supports_non_section8_arm_sets(tmp_path: Path) -> None:
    results_dir = _write_fake_results(
        tmp_path,
        arm_scores={
            "A": _dims(5.0),
            "E": _dims(7.0),
            "G": _dims(6.5),
        },
        arms=("A", "E", "G"),
    )

    report = aggregate_results(results_dir=results_dir, write_files=False)

    assert report["arms_order"] == ["A", "E", "G"]
    arms = report["arms"]
    assert isinstance(arms, dict)
    assert arms["E"]["arm_median"] == 7.0
    assert arms["G"]["arm_median"] == 6.5
    assert report["threshold_decision"]["order"] == 0


def test_aggregate_rejects_complete_single_judge_data(tmp_path: Path) -> None:
    results_dir = _write_fake_results(
        tmp_path,
        arm_scores={
            "A": _dims(7.0),
            "B": _dims(5.0),
            "B_prime": _dims(6.4),
            "C": _dims(4.0),
        },
    )
    for judge_id in JUDGE_IDS[1:]:
        for judge_path in results_dir.glob(f"*/blinded/*/judge-{judge_id}.json"):
            judge_path.unlink()

    with pytest.raises(ValueError, match="all 3 judge outputs"):
        aggregate_results(results_dir=results_dir, write_files=False)


def test_aggregate_reports_b_b_prime_provider_model_mismatch(tmp_path: Path) -> None:
    results_dir = _write_fake_results(
        tmp_path,
        arm_scores={
            "A": _dims(7.0),
            "B": _dims(5.0),
            "B_prime": _dims(6.4),
            "C": _dims(4.0),
        },
        provider_overrides={
            (KERNELS[0], "B_prime"): ("minimax", "MiniMax-M2.7"),
        },
    )

    report = aggregate_results(results_dir=results_dir, write_files=False)

    sensitivity = report["sensitivity"]
    assert isinstance(sensitivity, dict)
    mismatches = sensitivity["b_b_prime_provider_mismatch_kernels"]
    assert mismatches == [
        {
            "kernel_id": KERNELS[0],
            "interpretation": "self-critique_with_provider_model_confound",
            "B": {"provider": "rightcode", "provider_model": "gpt-5.4-mini"},
            "B_prime": {"provider": "minimax", "provider_model": "MiniMax-M2.7"},
        }
    ]


def test_aggregate_refuses_blind_map_until_scoring_complete(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    submission_uuid = str(uuid5(NAMESPACE_DNS, "missing-judge"))
    manuscript = results_dir / "k00" / "blinded" / submission_uuid / "manuscript.md"
    manuscript.parent.mkdir(parents=True)
    manuscript.write_text("正文\n", encoding="utf-8")
    (results_dir / "blind_map.json").write_text(
        "not json",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Scoring is incomplete"):
        aggregate_results(results_dir=results_dir, write_files=False)


def _write_fake_results(
    tmp_path: Path,
    *,
    arm_scores: dict[str, dict[str, float]],
    arms: tuple[str, ...] = ARMS,
    invalid_pairs: set[tuple[str, str]] | None = None,
    score_overrides: dict[tuple[str, str, str], dict[str, float]] | None = None,
    provider_overrides: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> Path:
    results_dir = tmp_path / "results"
    invalid_pairs = invalid_pairs or set()
    score_overrides = score_overrides or {}
    provider_overrides = provider_overrides or {}
    blind_entries: list[dict[str, str]] = []
    for kernel_id in KERNELS:
        for arm in arms:
            submission_uuid = str(uuid5(NAMESPACE_DNS, f"{kernel_id}-{arm}"))
            submission_dir = results_dir / kernel_id / "blinded" / submission_uuid
            submission_dir.mkdir(parents=True)
            submission_dir.joinpath("manuscript.md").write_text("正文\n", encoding="utf-8")
            _write_provenance(results_dir, kernel_id, arm, provider_overrides)
            blind_entries.append(
                {
                    "submission_uuid": submission_uuid,
                    "kernel_id": kernel_id,
                    "arm": arm,
                }
            )
            for judge_id in JUDGE_IDS:
                scores = score_overrides.get((kernel_id, arm, judge_id), arm_scores[arm])
                payload = _judge_payload(
                    submission_uuid=submission_uuid,
                    judge_id=judge_id,
                    scores=scores,
                    can_score=(kernel_id, arm) not in invalid_pairs,
                )
                submission_dir.joinpath(f"judge-{judge_id}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
    (results_dir / "blind_map.json").write_text(
        json.dumps(
            {
                "experiment_id": "abc-architecture-comparison-v1",
                "created_at": "2026-05-16T00:00:00Z",
                "submissions": blind_entries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return results_dir


def _write_provenance(
    results_dir: Path,
    kernel_id: str,
    arm: str,
    provider_overrides: dict[tuple[str, str], tuple[str, str]],
) -> None:
    provider, provider_model = provider_overrides.get(
        (kernel_id, arm),
        ("production", "production-configured") if arm == "A" else ("rightcode", "gpt-5.4-mini"),
    )
    path = results_dir / kernel_id / arm / "provenance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "provider": provider,
                "provider_model": provider_model,
                "token_usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "budget_exceeded": False,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _judge_payload(
    *,
    submission_uuid: str,
    judge_id: str,
    scores: dict[str, float],
    can_score: bool,
) -> dict[str, object]:
    return {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "judge_id": judge_id,
        "submission_uuid": submission_uuid,
        "validity": {
            "can_score": can_score,
            "reason": None if can_score else "MISSING: corrupted submission",
        },
        "overall_scores": scores,
        "ledger": [
            {
                "id": item["id"],
                "dimension": item["dimension"],
                "max": item["max"],
                "points": item["max"],
                "reason_code": "SUPPORTED",
                "evidence": ["anchor"],
                "brief_reason": "Brief reason.",
            }
            for item in LEDGER_ITEMS
        ],
        "residual_risks": [],
        "confidence": "high",
    }
