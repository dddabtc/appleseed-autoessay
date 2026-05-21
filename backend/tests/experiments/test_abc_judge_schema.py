from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from autoessay.experiments.abc_judge_schema import (
    ABC_ARCHITECTURE_JUDGE_SCHEMA,
    JUDGE_SCHEMA_VERSION,
    LEDGER_ITEMS,
    validate_judge_output,
)


def _valid_payload() -> dict[str, object]:
    return {
        "schema_version": JUDGE_SCHEMA_VERSION,
        "judge_id": "codex-gpt-5.5-xhigh",
        "submission_uuid": str(uuid4()),
        "validity": {"can_score": True, "reason": None},
        "overall_scores": {
            "compliance": 7.0,
            "novelty": 6.5,
            "completeness": 8.0,
        },
        "ledger": [
            {
                "id": item["id"],
                "dimension": item["dimension"],
                "max": item["max"],
                "points": item["max"] / 2,
                "reason_code": "PARTIAL",
                "evidence": ["anchor"],
                "brief_reason": "Brief reason.",
            }
            for item in LEDGER_ITEMS
        ],
        "residual_risks": [],
        "confidence": "high",
    }


def test_judge_schema_constant_contains_13_item_ledger() -> None:
    assert ABC_ARCHITECTURE_JUDGE_SCHEMA["properties"]
    assert len(LEDGER_ITEMS) == 13
    assert [item["id"] for item in LEDGER_ITEMS] == [
        "citation_alignment",
        "no_sentinels",
        "cnki_format",
        "academic_voice",
        "new_material",
        "new_perspective",
        "new_method",
        "new_question",
        "new_argument",
        "eight_sections",
        "claim_evidence_conclusion",
        "abstract_keywords_refs",
        "cross_section_coherence",
    ]


def test_validate_judge_output_accepts_valid_payload() -> None:
    ok, errors = validate_judge_output(_valid_payload())

    assert ok is True
    assert errors == []


def test_validate_judge_output_rejects_bad_top_level_fields() -> None:
    payload = _valid_payload()
    payload["schema_version"] = "wrong"
    payload["judge_id"] = "unknown"
    payload["submission_uuid"] = "not-a-uuid"

    ok, errors = validate_judge_output(payload)

    assert ok is False
    assert any("schema_version" in error for error in errors)
    assert any("judge_id" in error for error in errors)
    assert any("submission_uuid" in error for error in errors)


def test_validate_judge_output_rejects_invalid_scores_and_validity() -> None:
    payload = _valid_payload()
    payload["validity"] = {"can_score": False, "reason": ""}
    payload["overall_scores"] = {
        "compliance": 0.5,
        "novelty": 11.0,
        "completeness": True,
    }

    ok, errors = validate_judge_output(payload)

    assert ok is False
    assert any("validity.reason" in error for error in errors)
    assert any("overall_scores.compliance" in error for error in errors)
    assert any("overall_scores.novelty" in error for error in errors)
    assert any("overall_scores.completeness" in error for error in errors)


def test_validate_judge_output_rejects_bad_ledger() -> None:
    payload = _valid_payload()
    ledger = deepcopy(payload["ledger"])
    assert isinstance(ledger, list)
    ledger[0]["points"] = 4.25
    ledger[1]["points"] = 99
    ledger[2]["evidence"] = []
    ledger[3]["max"] = 99
    ledger.pop()
    payload["ledger"] = ledger

    ok, errors = validate_judge_output(payload)

    assert ok is False
    assert any("whole or half points" in error for error in errors)
    assert any("between 0 and 2" in error for error in errors)
    assert any("evidence" in error for error in errors)
    assert any("max must be 2" in error for error in errors)
    assert any("missing" in error for error in errors)
