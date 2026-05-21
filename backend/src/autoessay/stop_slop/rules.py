"""Load stop-slop rules from the configured runtime bundle."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from autoessay.config import get_settings


@dataclass(frozen=True)
class StructuralPattern:
    name: str
    examples: tuple[str, ...]


@dataclass(frozen=True)
class StopSlopRules:
    source_dir: Path
    phrases: set[str]
    structures: list[StructuralPattern]


def resolve_stop_slop_dir() -> Path:
    configured = get_settings().stop_slop_dir
    candidates = [
        configured,
        Path(os.path.expanduser("~/.codex/skills/stop-slop")),
        Path(os.path.expanduser("~/.claude/skills/stop-slop")),
    ]
    for candidate in candidates:
        if (candidate / "references" / "phrases.md").is_file() and (
            candidate / "references" / "structures.md"
        ).is_file():
            return candidate
    return configured


@lru_cache(maxsize=1)
def load_stop_slop_rules() -> StopSlopRules:
    source_dir = resolve_stop_slop_dir()
    phrases = parse_phrases(source_dir / "references" / "phrases.md")
    structures = parse_structures(source_dir / "references" / "structures.md")
    return StopSlopRules(source_dir=source_dir, phrases=phrases, structures=structures)


def parse_phrases(path: Path) -> set[str]:
    if not path.exists():
        return set()
    phrases: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        phrases.update(_quoted_phrases(line))
        table_phrase = _table_avoid_phrase(line)
        if table_phrase:
            phrases.add(table_phrase)
    return {phrase for phrase in phrases if phrase}


def parse_structures(path: Path) -> list[StructuralPattern]:
    if not path.exists():
        return []
    current_name = ""
    examples: list[str] = []
    patterns: list[StructuralPattern] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            if current_name and examples:
                patterns.append(StructuralPattern(name=current_name, examples=tuple(examples)))
            current_name = line.removeprefix("## ").strip()
            examples = []
            continue
        if not line.startswith("|") or line.startswith("|---") or "Pattern" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells:
            example = _normalize_rule_phrase(cells[0])
            if example:
                examples.append(example)
    if current_name and examples:
        patterns.append(StructuralPattern(name=current_name, examples=tuple(examples)))
    return patterns


def _quoted_phrases(line: str) -> set[str]:
    found: set[str] = set()
    for phrase in re.findall(r'"([^"]+)"', line):
        for part in phrase.split(" / "):
            normalized = _normalize_rule_phrase(part)
            if normalized:
                found.add(normalized)
    return found


def _table_avoid_phrase(line: str) -> str | None:
    if not line.startswith("|") or line.startswith("|---") or "Avoid" in line:
        return None
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    if len(cells) < 2:
        return None
    return _normalize_rule_phrase(cells[0])


def _normalize_rule_phrase(value: str) -> str:
    normalized = value.casefold()
    normalized = normalized.replace("’", "'")
    normalized = re.sub(r"\[[^\]]+\]", "", normalized)
    normalized = re.sub(r"\([^)]*\)", "", normalized)
    normalized = normalized.replace("...", " ")
    normalized = normalized.strip(" .,:;")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized
