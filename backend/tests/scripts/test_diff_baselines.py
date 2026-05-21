"""PR-D4 diff_baselines unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "backend" / "scripts"))

import diff_baselines  # noqa: E402  isort:skip


def _baseline_payload(**overrides) -> dict:
    payload = {
        "vector_fields": [
            "integrity_p0",
            "fabricated_citations",
            "fallback_events",
            "manuscript_bytes",
            "claim_density",
            "stop_slop_total",
            "manuscript_citations",
        ],
        "vector": [0, 0, 0, 30000, 1.5, 38, 8],
        "baseline_label": "smoke-baseline",
        "baseline_status": "baseline_candidate",
    }
    payload.update(overrides)
    return payload


def _candidate_payload(**overrides) -> dict:
    payload = {
        "vector_fields": [
            "integrity_p0",
            "fabricated_citations",
            "fallback_events",
            "manuscript_bytes",
            "claim_density",
            "stop_slop_total",
            "manuscript_citations",
        ],
        "vector": [0, 0, 0, 30100, 1.55, 38, 9],
    }
    payload.update(overrides)
    return payload


def test_diff_passes_when_candidate_matches_or_improves() -> None:
    result = diff_baselines.diff_evaluator(_baseline_payload(), _candidate_payload())
    assert result["status"] == "pass"
    assert all(d["status"] == "pass" for d in result["deltas"])


def test_exact_zero_violation_fails_integrity_p0() -> None:
    candidate = _candidate_payload(vector=[1, 0, 0, 30000, 1.5, 38, 8])
    result = diff_baselines.diff_evaluator(_baseline_payload(), candidate)
    assert result["status"] == "fail"
    p0 = next(d for d in result["deltas"] if d["field"] == "integrity_p0")
    assert p0["status"] == "fail"
    assert "exact-zero violation" in p0["reason"]


def test_exact_zero_violation_fails_fabricated_citations() -> None:
    candidate = _candidate_payload(vector=[0, 1, 0, 30000, 1.5, 38, 8])
    result = diff_baselines.diff_evaluator(_baseline_payload(), candidate)
    assert result["status"] == "fail"


def test_exact_zero_violation_fails_fallback_events() -> None:
    candidate = _candidate_payload(vector=[0, 0, 1, 30000, 1.5, 38, 8])
    result = diff_baselines.diff_evaluator(_baseline_payload(), candidate)
    assert result["status"] == "fail"


def test_manuscript_hard_floor_below_25k_fails() -> None:
    candidate = _candidate_payload(vector=[0, 0, 0, 24999, 1.5, 38, 8])
    result = diff_baselines.diff_evaluator(_baseline_payload(), candidate)
    assert result["status"] == "fail"
    bytes_d = next(d for d in result["deltas"] if d["field"] == "manuscript_bytes")
    assert "below hard floor" in bytes_d["reason"]


def test_manuscript_5pct_drop_warns() -> None:
    """Manuscript = 28000 vs baseline 30000 → 6.7% drop > 5% tolerance
    but ≥ hard floor 25000 → warn (not fail)."""
    candidate = _candidate_payload(vector=[0, 0, 0, 28000, 1.5, 38, 8])
    result = diff_baselines.diff_evaluator(_baseline_payload(), candidate)
    assert result["status"] == "warn"
    bytes_d = next(d for d in result["deltas"] if d["field"] == "manuscript_bytes")
    assert bytes_d["status"] == "warn"
    assert "dropped" in bytes_d["reason"]


def test_claim_density_15pct_drop_warns() -> None:
    candidate = _candidate_payload(vector=[0, 0, 0, 30000, 1.0, 38, 8])  # 33% drop
    result = diff_baselines.diff_evaluator(_baseline_payload(), candidate)
    assert result["status"] == "warn"
    cd = next(d for d in result["deltas"] if d["field"] == "claim_density")
    assert cd["status"] == "warn"


def test_stop_slop_within_tolerance_passes() -> None:
    candidate = _candidate_payload(vector=[0, 0, 0, 30000, 1.5, 36, 8])  # 5% drop
    result = diff_baselines.diff_evaluator(_baseline_payload(), candidate)
    assert result["status"] == "pass"


def test_markdown_emits_skeleton_advisory_note_for_candidate() -> None:
    result = diff_baselines.diff_evaluator(_baseline_payload(), _candidate_payload())
    assert "Skeleton stage" in result["markdown"]
    assert "advisory" in result["markdown"]


def test_markdown_omits_skeleton_note_for_confirmed() -> None:
    baseline = _baseline_payload(baseline_status="baseline_confirmed")
    result = diff_baselines.diff_evaluator(baseline, _candidate_payload())
    assert "Skeleton stage" not in result["markdown"]


def test_confirmed_baseline_manuscript_5pct_drop_fails() -> None:
    """PR-I2 retro fix #2: when baseline_status=baseline_confirmed,
    soft-drop on manuscript_bytes promotes from warn to fail so the
    --exit-fail-on-fail CI gate actually blocks regressions."""
    confirmed = _baseline_payload(baseline_status="baseline_confirmed")
    candidate = _candidate_payload(vector=[0, 0, 0, 28000, 1.5, 38, 8])
    result = diff_baselines.diff_evaluator(confirmed, candidate)
    assert result["status"] == "fail"
    bytes_d = next(d for d in result["deltas"] if d["field"] == "manuscript_bytes")
    assert bytes_d["status"] == "fail"


def test_confirmed_baseline_claim_density_drop_fails() -> None:
    confirmed = _baseline_payload(baseline_status="baseline_confirmed")
    candidate = _candidate_payload(vector=[0, 0, 0, 30000, 1.0, 38, 8])  # 33% drop
    result = diff_baselines.diff_evaluator(confirmed, candidate)
    assert result["status"] == "fail"


def test_confirmed_baseline_within_tolerance_passes() -> None:
    confirmed = _baseline_payload(baseline_status="baseline_confirmed")
    # 3% drop on manuscript_bytes — within the 5% tolerance.
    candidate = _candidate_payload(vector=[0, 0, 0, 29100, 1.5, 38, 8])
    result = diff_baselines.diff_evaluator(confirmed, candidate)
    assert result["status"] == "pass"


def test_candidate_baseline_soft_drop_still_warns_not_fails() -> None:
    """Regression guard: the candidate-baseline (skeleton-stage) path
    must keep ``warn`` semantics for soft drops. Otherwise the D4
    skeleton's "advisory only until confirmed" promise breaks."""
    candidate_baseline = _baseline_payload(baseline_status="baseline_candidate")
    candidate = _candidate_payload(vector=[0, 0, 0, 28000, 1.0, 38, 8])
    result = diff_baselines.diff_evaluator(candidate_baseline, candidate)
    assert result["status"] == "warn"


def test_no_baseline_field_passes_silently() -> None:
    """When baseline lacks a field present in candidate (e.g. evaluator
    schema added a field), default verdict = pass with reason."""
    baseline = _baseline_payload(vector=[0, 0, 0, 30000, 1.5, 38, 8])
    candidate = _candidate_payload(vector=[0, 0, 0, 30000, 1.5, 38, 8])
    result = diff_baselines.diff_evaluator(baseline, candidate)
    assert result["status"] == "pass"
