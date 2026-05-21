from __future__ import annotations

import random

from autoessay.agents._north_star_gate import (
    NORTH_STAR_GATE_ITEM_IDS,
    NORTH_STAR_GATE_ITEM_MAX,
    NORTH_STAR_GATE_SYSTEM_PROMPT,
    NorthStarGateOutput,
    aggregate_gate_samples,
    coin_flip_slots,
    evaluate_gate_sample,
    should_resample_gate,
)


def _item(item_id: str, points: float | None = None) -> dict[str, object]:
    max_points = NORTH_STAR_GATE_ITEM_MAX[item_id]
    return {
        "id": item_id,
        "max": max_points,
        "points": max_points if points is None else points,
        "reason_code": "SUPPORTED",
        "evidence": ["S01_P01"],
        "brief_reason": "supported",
    }


def _side(**overrides: float) -> dict[str, object]:
    items = [_item(item_id, overrides.get(item_id)) for item_id in NORTH_STAR_GATE_ITEM_IDS]
    return {
        "score": sum(float(item["points"]) for item in items),
        "items": items,
    }


def _output(
    *,
    a_overrides: dict[str, float] | None = None,
    b_overrides: dict[str, float] | None = None,
    a_score_offset: float = 0.0,
    b_score_offset: float = 0.0,
) -> NorthStarGateOutput:
    a_side = _side(**(a_overrides or {}))
    b_side = _side(**(b_overrides or {}))
    a_side["score"] = float(a_side["score"]) + a_score_offset
    b_side["score"] = float(b_side["score"]) + b_score_offset
    return NorthStarGateOutput.parse_obj(
        {
            "schema_version": "paired_blind_box_ledger_v1",
            "validity": {"can_score": True, "reason": None},
            "scores": {"A": a_side, "B": b_side},
        }
    )


def test_north_star_gate_prompt_pins_hybrid_schema() -> None:
    assert "paired_blind_box_ledger_v1" in NORTH_STAR_GATE_SYSTEM_PROMPT
    assert "citation_alignment" in NORTH_STAR_GATE_SYSTEM_PROMPT
    assert "reason_code: SUPPORTED | PARTIAL | WEAK | INVALID" in NORTH_STAR_GATE_SYSTEM_PROMPT
    assert "不输出比较结论" in NORTH_STAR_GATE_SYSTEM_PROMPT
    assert "顶层 score 必须严格等于" in NORTH_STAR_GATE_SYSTEM_PROMPT


def test_checksum_failure_with_complete_ledger_is_corrected_and_valid() -> None:
    parsed = _output(a_score_offset=1.0)
    sample = evaluate_gate_sample(output=parsed, pipeline_slot="A", baseline_slot="B")
    assert sample["can_score"] is True
    assert sample["checksum_failed"] is True
    assert sample["checksum_corrected"] is True
    assert any("checksum" in err for err in sample["validation_errors"])
    assert sample["reported_pipeline_score"] == sample["pipeline_score"] + 1.0
    assert sample["total_delta"] == 0.0
    assert should_resample_gate(sample) is True


def test_structural_failure_remains_unscorable() -> None:
    payload = _output().dict()
    payload["scores"]["A"]["items"] = payload["scores"]["A"]["items"][:-1]
    parsed = NorthStarGateOutput.parse_obj(payload)
    sample = evaluate_gate_sample(output=parsed, pipeline_slot="A", baseline_slot="B")
    assert sample["can_score"] is False
    assert sample["checksum_corrected"] is False
    assert any("missing_items" in err for err in sample["validation_errors"])


def test_coin_flip_can_assign_both_blind_orders() -> None:
    orders = {coin_flip_slots(random.Random(seed)) for seed in range(20)}
    assert orders == {("A", "B"), ("B", "A")}


def test_valid_sample_uses_coin_flip_to_compute_pipeline_delta() -> None:
    parsed = _output(a_overrides={"citation_alignment": 2.0})
    sample = evaluate_gate_sample(output=parsed, pipeline_slot="B", baseline_slot="A")
    assert sample["can_score"] is True
    assert sample["pipeline_slot"] == "B"
    assert sample["baseline_slot"] == "A"
    assert sample["item_deltas"]["citation_alignment"] == 2.0
    assert sample["max_loss"] == 0.0


def test_resample_band_and_total_delta_threshold() -> None:
    borderline = {
        "can_score": True,
        "checksum_failed": False,
        "max_loss": -1.0,
        "total_delta": 5.0,
    }
    assert should_resample_gate(borderline) is True
    tiny_total = {
        "can_score": True,
        "checksum_failed": False,
        "max_loss": 0.0,
        "total_delta": 1.0,
    }
    assert should_resample_gate(tiny_total) is True
    clear = {
        "can_score": True,
        "checksum_failed": False,
        "max_loss": -2.0,
        "total_delta": 4.0,
    }
    assert should_resample_gate(clear) is False


def test_multi_sample_median_item_delta_aggregation() -> None:
    samples = [
        {
            "can_score": True,
            "item_deltas": {
                item_id: (-2.0 if item_id == "new_material" else 0.0)
                for item_id in NORTH_STAR_GATE_ITEM_IDS
            },
            "max_loss": -2.0,
            "total_delta": -2.0,
        },
        {
            "can_score": True,
            "item_deltas": {
                item_id: (0.0 if item_id == "new_material" else 0.0)
                for item_id in NORTH_STAR_GATE_ITEM_IDS
            },
            "max_loss": 0.0,
            "total_delta": 0.0,
        },
        {
            "can_score": True,
            "item_deltas": {
                item_id: (-1.0 if item_id == "new_material" else 0.0)
                for item_id in NORTH_STAR_GATE_ITEM_IDS
            },
            "max_loss": -1.0,
            "total_delta": -1.0,
        },
    ]
    result = aggregate_gate_samples(samples)
    assert result["n_samples"] == 3
    assert result["n_valid_samples"] == 3
    assert result["n_required_valid_samples"] == 1
    assert result["median_item_delta"]["new_material"] == -1.0
    assert result["max_loss"] == -1.0
    assert result["pass"] is True


def test_all_invalid_samples_return_gate_unscorable() -> None:
    result = aggregate_gate_samples(
        [
            {"can_score": False, "checksum_failed": True},
            {"can_score": False, "checksum_failed": False},
        ]
    )
    assert result["pass"] is False
    assert result["reason"] == "gate_unscorable"
    assert result["max_loss"] is None


def test_forced_gate_requires_majority_valid_samples() -> None:
    valid = {
        "can_score": True,
        "item_deltas": {item_id: 0.0 for item_id in NORTH_STAR_GATE_ITEM_IDS},
        "max_loss": 0.0,
        "total_delta": 0.0,
    }
    result = aggregate_gate_samples(
        [
            valid,
            {"can_score": False, "checksum_failed": False},
            {"can_score": False, "checksum_failed": False},
        ],
        forced_samples=3,
    )
    assert result["pass"] is False
    assert result["reason"] == "insufficient_valid_gate_samples"
    assert result["n_valid_samples"] == 1
    assert result["n_required_valid_samples"] == 2
    assert result["max_loss"] is None
