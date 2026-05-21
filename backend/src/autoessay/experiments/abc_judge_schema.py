"""Judge JSON schema and validation helpers for the ABC experiment."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TypedDict
from uuid import UUID

JUDGE_SCHEMA_VERSION = "abc_architecture_judge_v1"
JUDGE_IDS: tuple[str, ...] = (
    "codex-gpt-5.5-xhigh",
    "codex-gpt-5.4",
    "apiport-gpt-5.4",
)
DIMENSIONS: tuple[str, ...] = ("compliance", "novelty", "completeness")


class LedgerItem(TypedDict):
    id: str
    dimension: str
    max: int


LEDGER_ITEMS: list[LedgerItem] = [
    {"id": "citation_alignment", "dimension": "compliance", "max": 4},
    {"id": "no_sentinels", "dimension": "compliance", "max": 2},
    {"id": "cnki_format", "dimension": "compliance", "max": 2},
    {"id": "academic_voice", "dimension": "compliance", "max": 2},
    {"id": "new_material", "dimension": "novelty", "max": 2},
    {"id": "new_perspective", "dimension": "novelty", "max": 2},
    {"id": "new_method", "dimension": "novelty", "max": 2},
    {"id": "new_question", "dimension": "novelty", "max": 2},
    {"id": "new_argument", "dimension": "novelty", "max": 2},
    {"id": "eight_sections", "dimension": "completeness", "max": 3},
    {"id": "claim_evidence_conclusion", "dimension": "completeness", "max": 2},
    {"id": "abstract_keywords_refs", "dimension": "completeness", "max": 2},
    {"id": "cross_section_coherence", "dimension": "completeness", "max": 3},
]

LEDGER_ITEMS_BY_ID: dict[str, LedgerItem] = {item["id"]: item for item in LEDGER_ITEMS}

ABC_ARCHITECTURE_JUDGE_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://autoessay.local/schemas/abc_architecture_judge_v1.json",
    "title": "ABC architecture blind judge output",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "judge_id",
        "submission_uuid",
        "validity",
        "overall_scores",
        "ledger",
        "residual_risks",
        "confidence",
    ],
    "properties": {
        "schema_version": {"const": JUDGE_SCHEMA_VERSION},
        "judge_id": {"type": "string", "enum": list(JUDGE_IDS)},
        "submission_uuid": {
            "type": "string",
            "format": "uuid",
        },
        "validity": {
            "type": "object",
            "additionalProperties": False,
            "required": ["can_score", "reason"],
            "properties": {
                "can_score": {"type": "boolean"},
                "reason": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "null"},
                    ]
                },
            },
        },
        "overall_scores": {
            "type": "object",
            "additionalProperties": False,
            "required": list(DIMENSIONS),
            "properties": {
                dimension: {"type": "number", "minimum": 1, "maximum": 10}
                for dimension in DIMENSIONS
            },
        },
        "ledger": {
            "type": "array",
            "minItems": len(LEDGER_ITEMS),
            "maxItems": len(LEDGER_ITEMS),
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["id", "max", "points", "reason_code", "evidence", "brief_reason"],
                "properties": {
                    "id": {"type": "string", "enum": [item["id"] for item in LEDGER_ITEMS]},
                    "dimension": {"type": "string", "enum": list(DIMENSIONS)},
                    "max": {"type": "integer", "minimum": 0},
                    "points": {"type": "number", "minimum": 0},
                    "reason_code": {"type": "string", "minLength": 1},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "brief_reason": {"type": "string", "minLength": 1},
                },
            },
        },
        "residual_risks": {
            "type": "array",
            "items": {"type": "string"},
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
    },
}


def validate_judge_output(payload: object) -> tuple[bool, list[str]]:
    """Validate a judge JSON payload against the experiment contract.

    The project does not currently depend on a JSON Schema validator, so this
    function enforces the protocol-critical constraints directly.
    """
    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return False, ["payload must be a JSON object"]

    _validate_top_level(payload, errors)
    _validate_validity(payload.get("validity"), errors)
    _validate_overall_scores(payload.get("overall_scores"), errors)
    _validate_ledger(payload.get("ledger"), errors)
    _validate_residual_risks(payload.get("residual_risks"), errors)
    _validate_confidence(payload.get("confidence"), errors)
    return not errors, errors


def ledger_max(item_id: str) -> int:
    """Return the protocol max points for a ledger item id."""
    return LEDGER_ITEMS_BY_ID[item_id]["max"]


def ledger_dimension(item_id: str) -> str:
    """Return the protocol dimension for a ledger item id."""
    return LEDGER_ITEMS_BY_ID[item_id]["dimension"]


def _validate_top_level(payload: Mapping[object, object], errors: list[str]) -> None:
    schema_version = payload.get("schema_version")
    if schema_version != JUDGE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {JUDGE_SCHEMA_VERSION!r}")

    judge_id = payload.get("judge_id")
    if judge_id not in JUDGE_IDS:
        errors.append(f"judge_id must be one of {', '.join(JUDGE_IDS)}")

    submission_uuid = payload.get("submission_uuid")
    if not isinstance(submission_uuid, str):
        errors.append("submission_uuid must be a UUID string")
    else:
        try:
            UUID(submission_uuid)
        except ValueError:
            errors.append("submission_uuid must be a valid UUID")


def _validate_validity(value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("validity must be an object")
        return
    can_score = value.get("can_score")
    reason = value.get("reason")
    if not isinstance(can_score, bool):
        errors.append("validity.can_score must be a boolean")
    if reason is not None and not isinstance(reason, str):
        errors.append("validity.reason must be a string or null")
    if can_score is False and not _non_empty_string(reason):
        errors.append("validity.reason is required when validity.can_score is false")


def _validate_overall_scores(value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("overall_scores must be an object")
        return
    keys = {key for key in value if isinstance(key, str)}
    missing = set(DIMENSIONS) - keys
    extra = keys - set(DIMENSIONS)
    for dimension in sorted(missing):
        errors.append(f"overall_scores.{dimension} is required")
    for dimension in sorted(extra):
        errors.append(f"overall_scores.{dimension} is not allowed")
    for dimension in DIMENSIONS:
        score = value.get(dimension)
        numeric = _number_value(score)
        if numeric is None:
            errors.append(f"overall_scores.{dimension} must be a finite number")
            continue
        if numeric < 1 or numeric > 10:
            errors.append(f"overall_scores.{dimension} must be between 1 and 10")


def _validate_ledger(value: object, errors: list[str]) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        errors.append("ledger must be an array")
        return
    if len(value) != len(LEDGER_ITEMS):
        errors.append(f"ledger must contain exactly {len(LEDGER_ITEMS)} items")

    seen: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            errors.append(f"ledger[{index}] must be an object")
            continue
        raw_id = entry.get("id")
        if not isinstance(raw_id, str):
            errors.append(f"ledger[{index}].id must be a string")
            continue
        item_id = raw_id
        if item_id in seen:
            errors.append(f"ledger item {item_id!r} is duplicated")
        seen.add(item_id)
        definition = LEDGER_ITEMS_BY_ID.get(item_id)
        if definition is None:
            errors.append(f"ledger item {item_id!r} is not in the protocol ledger")
            continue

        raw_max = entry.get("max")
        if raw_max != definition["max"]:
            errors.append(f"ledger.{item_id}.max must be {definition['max']}")
        dimension = entry.get("dimension")
        if dimension is not None and dimension != definition["dimension"]:
            errors.append(f"ledger.{item_id}.dimension must be {definition['dimension']}")
        _validate_ledger_points(item_id, entry.get("points"), definition["max"], errors)
        if not _non_empty_string(entry.get("reason_code")):
            errors.append(f"ledger.{item_id}.reason_code must be a non-empty string")
        _validate_evidence(item_id, entry.get("evidence"), errors)
        if not _non_empty_string(entry.get("brief_reason")):
            errors.append(f"ledger.{item_id}.brief_reason must be a non-empty string")

    missing_ids = set(LEDGER_ITEMS_BY_ID) - seen
    for item_id in sorted(missing_ids):
        errors.append(f"ledger item {item_id!r} is missing")


def _validate_ledger_points(
    item_id: str,
    value: object,
    max_points: int,
    errors: list[str],
) -> None:
    numeric = _number_value(value)
    if numeric is None:
        errors.append(f"ledger.{item_id}.points must be a finite number")
        return
    if numeric < 0 or numeric > max_points:
        errors.append(f"ledger.{item_id}.points must be between 0 and {max_points}")
    if not math.isclose(numeric * 2, round(numeric * 2), abs_tol=1e-9):
        errors.append(f"ledger.{item_id}.points must use whole or half points")


def _validate_evidence(item_id: str, value: object, errors: list[str]) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        errors.append(f"ledger.{item_id}.evidence must be a non-empty array")
        return
    if not value:
        errors.append(f"ledger.{item_id}.evidence must include at least one anchor")
        return
    for index, anchor in enumerate(value):
        if not _non_empty_string(anchor):
            errors.append(f"ledger.{item_id}.evidence[{index}] must be a non-empty string")


def _validate_residual_risks(value: object, errors: list[str]) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        errors.append("residual_risks must be an array")
        return
    for index, risk in enumerate(value):
        if not isinstance(risk, str):
            errors.append(f"residual_risks[{index}] must be a string")


def _validate_confidence(value: object, errors: list[str]) -> None:
    if value not in {"low", "medium", "high"}:
        errors.append("confidence must be one of low, medium, high")


def _number_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
