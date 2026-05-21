"""Deterministic compliance repair for ABC experiment manuscripts.

This module deliberately performs no LLM calls. It only applies mechanical
normalization that can be expressed as local parsing and regex rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SENTINEL_RE = re.compile(
    r"\{\{[^{}\n]*(?:TODO|todo|待填|placeholder|PLACEHOLDER)[^{}\n]*\}\}"
    r"|<\s*(?:placeholder|todo)\s*>"
    r"|【\s*(?:待填|TODO|todo)\s*】"
    r"|\[\s*(?:TODO|todo|待填)\s*\]",
)

NUMERIC_CITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"【\s*(\d{1,3}(?:\s*[-,，、]\s*\d{1,3})*)\s*】"),
    re.compile(r"［\s*(\d{1,3}(?:\s*[-,，、]\s*\d{1,3})*)\s*］"),
    re.compile(r"（\s*(\d{1,3}(?:\s*[-,，、]\s*\d{1,3})*)\s*）"),
    re.compile(r"(?<!\w)\(\s*(\d{1,3}(?:\s*[-,，、]\s*\d{1,3})*)\s*\)"),
)

AUTHOR_YEAR_RE = re.compile(r"（\s*([^（）\[\]]{1,60}?\s*[，,]?\s*(?:19|20)\d{2}[a-z]?)\s*）")
REFERENCE_HEADING_RE = re.compile(r"^\s{0,3}#{0,6}\s*参考文献\s*$")
NUMBERED_REF_RE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.、．])\s*(.+?)\s*$")
CITATION_RE = re.compile(r"\[(\d{1,3})(?:\s*[-,，、]\s*\d{1,3})*\]")


@dataclass(frozen=True)
class ComplianceRepairResult:
    manuscript: str
    changed: bool
    status: str
    blockers: tuple[str, ...]
    operations: tuple[str, ...]


def repair_manuscript(manuscript: str) -> ComplianceRepairResult:
    """Run one deterministic repair pass.

    If reference-list alignment cannot be determined safely, the current
    manuscript text is returned with a blocker. The caller still submits that
    text, matching the protocol's "submit as-is and record blocker" rule.
    """
    original = manuscript
    operations: list[str] = []
    blockers: list[str] = []

    repaired = _remove_sentinels(manuscript)
    if repaired != manuscript:
        operations.append("sentinel_markers_removed")
    manuscript = repaired

    repaired = _normalize_citation_markers(manuscript)
    if repaired != manuscript:
        operations.append("citation_markers_normalized")
    manuscript = repaired

    repaired = _repair_heading_levels(manuscript)
    if repaired != manuscript:
        operations.append("cnki_headings_repaired")
    manuscript = repaired

    repaired, ref_operations, ref_blockers = _align_reference_list(manuscript)
    operations.extend(ref_operations)
    blockers.extend(ref_blockers)
    manuscript = repaired

    status = "blocked" if blockers else "passed"
    return ComplianceRepairResult(
        manuscript=manuscript,
        changed=manuscript != original,
        status=status,
        blockers=tuple(blockers),
        operations=tuple(operations),
    )


def _remove_sentinels(text: str) -> str:
    return SENTINEL_RE.sub("", text)


def _normalize_citation_markers(text: str) -> str:
    normalized = text
    for pattern in NUMERIC_CITATION_PATTERNS:
        normalized = pattern.sub(
            lambda match: _normalize_numeric_marker(match.group(1)),
            normalized,
        )
    normalized = AUTHOR_YEAR_RE.sub(
        lambda match: f"({match.group(1).replace('，', ',').strip()})",
        normalized,
    )
    return normalized


def _normalize_numeric_marker(raw: str) -> str:
    parts = re.split(r"\s*([-,，、])\s*", raw.strip())
    normalized_parts: list[str] = []
    for part in parts:
        if not part:
            continue
        if part in {",", "，", "、"}:
            normalized_parts.append(",")
        elif part == "-":
            normalized_parts.append("-")
        else:
            normalized_parts.append(part)
    marker = "".join(normalized_parts).replace(",,", ",")
    return f"[{marker}]"


def _repair_heading_levels(text: str) -> str:
    lines = text.splitlines()
    repaired: list[str] = []
    seen_h1 = False
    for line in lines:
        stripped = line.strip()
        match = re.match(r"^(#{1,})\s*(.+?)\s*$", stripped)
        if match:
            hashes, title = match.groups()
            title = title.strip()
            if len(hashes) == 1 and not seen_h1:
                seen_h1 = True
                repaired.append(f"# {title}")
            elif _is_top_level_cnki_heading(title):
                repaired.append(f"## {title}")
            else:
                level = min(max(len(hashes), 2), 3)
                repaired.append(f"{'#' * level} {title}")
            continue
        if _is_plain_cnki_heading(stripped):
            repaired.append(f"## {stripped}")
            continue
        repaired.append(line.rstrip())
    return "\n".join(repaired).rstrip() + ("\n" if text.endswith("\n") else "")


def _is_top_level_cnki_heading(title: str) -> bool:
    return bool(
        re.match(
            r"^(摘要|关键词|引言|结语|结论|参考文献|[一二三四五六七八九十]+[、.．].+)$",
            title,
        )
    )


def _is_plain_cnki_heading(line: str) -> bool:
    return bool(
        line
        and re.match(
            r"^(摘要|关键词|引言|结语|结论|参考文献|[一二三四五六七八九十]+[、.．].+)$",
            line,
        )
    )


def _align_reference_list(text: str) -> tuple[str, list[str], list[str]]:
    lines = text.splitlines()
    split_index = _find_reference_heading(lines)
    body_text = "\n".join(lines if split_index is None else lines[:split_index])
    body_citations = _citation_numbers(body_text)
    if split_index is None:
        if body_citations:
            return text, [], ["reference_list_missing_for_numeric_citations"]
        return text, [], []

    heading = "## 参考文献"
    raw_ref_lines = lines[split_index + 1 :]
    ref_entries = _parse_reference_entries(raw_ref_lines)
    if not body_citations:
        return text, [], []
    if body_citations and not ref_entries:
        return text, [], ["reference_entries_missing_for_numeric_citations"]

    operations: list[str] = []
    ref_numbers = {number for number, _entry in ref_entries}
    if body_citations - ref_numbers:
        body_text = _remove_missing_reference_citations(body_text, body_citations - ref_numbers)
        operations.append("orphan_citations_removed")
        body_citations = _citation_numbers(body_text)

    if ref_entries:
        aligned_refs = [
            (number, entry) for number, entry in ref_entries if number in body_citations
        ]
        if len(aligned_refs) != len(ref_entries):
            operations.append("uncited_reference_entries_removed")
        ref_lines = [f"[{number}] {entry}" for number, entry in aligned_refs]
    else:
        ref_lines = []

    rebuilt_lines = body_text.splitlines()
    if rebuilt_lines and rebuilt_lines[-1].strip():
        rebuilt_lines.append("")
    rebuilt_lines.append(heading)
    rebuilt_lines.extend(ref_lines)
    rebuilt = "\n".join(rebuilt_lines).rstrip() + ("\n" if text.endswith("\n") else "")
    if rebuilt != text and "reference_list_aligned" not in operations:
        operations.append("reference_list_aligned")
    return rebuilt, operations, []


def _find_reference_heading(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if REFERENCE_HEADING_RE.match(line):
            return index
    return None


def _parse_reference_entries(lines: list[str]) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    next_number = 1
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = NUMBERED_REF_RE.match(stripped)
        if match:
            raw_number = match.group(1) or match.group(2)
            number = int(raw_number)
            entry = match.group(3).strip()
        else:
            number = next_number
            entry = stripped
        entries.append((number, entry))
        next_number = max(next_number, number + 1)
    return entries


def _citation_numbers(text: str) -> set[int]:
    numbers: set[int] = set()
    for match in CITATION_RE.finditer(text):
        marker = match.group(0).strip("[]")
        for item in re.split(r"\s*[,，、-]\s*", marker):
            if item.isdigit():
                numbers.add(int(item))
    return numbers


def _remove_missing_reference_citations(text: str, missing_numbers: set[int]) -> str:
    def replace(match: re.Match[str]) -> str:
        marker = match.group(0).strip("[]")
        kept = [
            item
            for item in re.split(r"\s*[,，、]\s*", marker)
            if item.isdigit() and int(item) not in missing_numbers
        ]
        return f"[{','.join(kept)}]" if kept else ""

    return CITATION_RE.sub(replace, text)
