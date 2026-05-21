from typing import Any

from pydantic import BaseModel

from autoessay.harness.validator import validate_response


class QueryPayload(BaseModel):
    queries: list[str]
    rationale: str


def test_validate_response_pydantic_positive() -> None:
    result = validate_response(
        '{"queries": ["banking crisis"], "rationale": "domain coverage"}',
        QueryPayload,
    )

    assert result.valid is True
    assert isinstance(result.parsed, QueryPayload)
    assert result.parsed.queries == ["banking crisis"]
    assert result.errors == []


def test_validate_response_pydantic_negative() -> None:
    result = validate_response('{"queries": "bad"}', QueryPayload)

    assert result.valid is False
    assert result.parsed is None
    assert result.errors


def test_validate_response_json_schema_positive() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["queries", "rationale"],
        "properties": {
            "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "rationale": {"type": "string"},
        },
    }

    result = validate_response(
        '{"queries": ["banking crisis"], "rationale": "domain coverage"}',
        schema,
    )

    assert result.valid is True
    assert result.parsed["queries"] == ["banking crisis"]


def test_validate_response_json_schema_negative() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "required": ["queries", "rationale"],
        "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    }

    result = validate_response('{"queries": [1]}', schema)

    assert result.valid is False
    assert "$.rationale: missing required field" in result.errors
    assert "$.queries[0]: expected string, got integer" in result.errors
