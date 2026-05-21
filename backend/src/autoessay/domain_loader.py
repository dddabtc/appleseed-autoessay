from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from autoessay.clients.registry import VALID_SOURCE_IDS


class DomainConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LoadedDomain:
    path: Path
    data: dict[str, Any]
    warnings: tuple[str, ...]


def load_domain(path: str | Path) -> LoadedDomain:
    domain_path = Path(path)
    with domain_path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle)
    if not isinstance(parsed, dict):
        raise DomainConfigError(f"{domain_path} must contain a YAML mapping")
    data = cast(dict[str, Any], parsed)
    warnings = tuple(validate_domain_config(data))
    return LoadedDomain(path=domain_path, data=data, warnings=warnings)


def load_domains(directory: str | Path) -> dict[str, LoadedDomain]:
    domain_dir = Path(directory)
    loaded: dict[str, LoadedDomain] = {}
    for path in sorted(domain_dir.glob("*.yaml")):
        domain = load_domain(path)
        domain_id = str(domain.data["id"])
        loaded[domain_id] = domain
    return loaded


def validate_domain_config(data: dict[str, Any]) -> list[str]:
    _require_non_empty_string(data, "id")
    _require_non_empty_string(data, "display_name")
    _require_path(data, ("search", "sources"), list)
    _require_path(data, ("journals", "targets"), list)
    _require_path(data, ("citation", "style"), str)
    warnings: list[str] = []
    search = cast(dict[str, Any], data["search"])
    sources = cast(list[Any], search["sources"])
    if not any(isinstance(source, dict) and source.get("enabled") is True for source in sources):
        warnings.append("no search source is enabled")
    for index, source in enumerate(sources):
        _validate_source_config(index, source)
    evidence = data.get("evidence", {})
    minimum = 0
    if isinstance(evidence, dict):
        minimum_raw = evidence.get("minimum_consensus_sample", 0)
        if isinstance(minimum_raw, int):
            minimum = minimum_raw
    if minimum < 30:
        warnings.append("minimum_consensus_sample is below 30")
    targets = cast(list[Any], cast(dict[str, Any], data["journals"])["targets"])
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            raise DomainConfigError(f"journals.targets[{index}] must be a mapping")
        length_range = target.get("expected_length_words")
        if not _is_two_integer_range(length_range):
            raise DomainConfigError(
                f"journals.targets[{index}].expected_length_words must contain two integers",
            )
    return warnings


def _validate_source_config(index: int, source: object) -> None:
    if not isinstance(source, dict):
        raise DomainConfigError(f"search.sources[{index}] must be a mapping")
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id:
        raise DomainConfigError(f"search.sources[{index}].id must be a non-empty string")
    if source_id not in VALID_SOURCE_IDS:
        raise DomainConfigError(f"unknown search source_id: {source_id}")
    enabled = source.get("enabled")
    if not isinstance(enabled, bool):
        raise DomainConfigError(f"search.sources[{index}].enabled must be a bool")
    query_templates = source.get("query_templates")
    if query_templates is not None and (
        not isinstance(query_templates, list)
        or not all(isinstance(template, str) for template in query_templates)
    ):
        raise DomainConfigError(f"search.sources[{index}].query_templates must be a string list")


def _require_non_empty_string(data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise DomainConfigError(f"missing required string key: {key}")


def _require_path(data: dict[str, Any], path: tuple[str, ...], expected_type: type[object]) -> None:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            raise DomainConfigError(f"missing required key: {'.'.join(path)}")
        current = current[key]
    if not isinstance(current, expected_type):
        raise DomainConfigError(f"{'.'.join(path)} must be {expected_type.__name__}")
    if isinstance(current, (list, str)) and len(current) == 0:
        raise DomainConfigError(f"{'.'.join(path)} must not be empty")


def _is_two_integer_range(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 2:
        return False
    return all(isinstance(item, int) for item in value)
