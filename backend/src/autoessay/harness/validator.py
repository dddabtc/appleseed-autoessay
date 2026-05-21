from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeGuard, cast

from pydantic import BaseModel, ValidationError

from autoessay.harness.types import ValidationResult


def validate_response(
    response_content: str,
    schema: dict[str, Any] | type[BaseModel],
) -> ValidationResult:
    if _is_pydantic_model(schema):
        try:
            parsed = schema.parse_raw(response_content)
        except ValidationError as exc:
            return ValidationResult(
                valid=False,
                parsed=None,
                errors=[_format_pydantic_error(error) for error in exc.errors()],
            )
        except ValueError as exc:
            return ValidationResult(valid=False, parsed=None, errors=[str(exc)])
        return ValidationResult(valid=True, parsed=parsed, errors=[])

    try:
        parsed_json = json.loads(response_content)
    except json.JSONDecodeError as exc:
        return ValidationResult(valid=False, parsed=None, errors=[f"invalid JSON: {exc.msg}"])

    json_schema = cast(dict[str, Any], schema)
    errors = _validate_json_schema(parsed_json, json_schema, "$")
    return ValidationResult(valid=not errors, parsed=parsed_json, errors=errors)


def _is_pydantic_model(schema: object) -> TypeGuard[type[BaseModel]]:
    return isinstance(schema, type) and issubclass(schema, BaseModel)


def _format_pydantic_error(error: Mapping[str, Any]) -> str:
    location = ".".join(str(part) for part in error.get("loc", ())) or "$"
    message = str(error.get("msg", "validation error"))
    return f"{location}: {message}"


def _validate_json_schema(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_matches_type(value, item) for item in expected_type if isinstance(item, str)):
            errors.append(f"{path}: expected one of {expected_type}, got {_json_type(value)}")
            return errors
    elif isinstance(expected_type, str) and not _matches_type(value, expected_type):
        errors.append(f"{path}: expected {expected_type}, got {_json_type(value)}")
        return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']!r}")

    if expected_type == "object" or isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and isinstance(value, dict) and key not in value:
                    errors.append(f"{path}.{key}: missing required field")
        if isinstance(properties, dict) and isinstance(value, dict):
            for key, child_schema in properties.items():
                if key in value and isinstance(child_schema, dict):
                    errors.extend(_validate_json_schema(value[key], child_schema, f"{path}.{key}"))

    if expected_type == "array" or isinstance(value, list):
        if not isinstance(value, list):
            return errors
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path}: expected at least {min_items} items")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path}: expected at most {max_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_validate_json_schema(item, item_schema, f"{path}[{index}]"))

    return errors


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return type(value).__name__
