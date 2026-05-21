"""Hybrid stop-slop scoring with static checks and optional LLM grading."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Mapping, Sequence
from statistics import pstdev
from typing import cast

import httpx

from autoessay.config import get_settings
from autoessay.llm_client import LLMClient
from autoessay.stop_slop.rules import StructuralPattern

DIMENSIONS = ("directness", "rhythm", "trust", "authenticity", "density")
DEFAULT_DIMENSIONS = {
    "directness": 8,
    "rhythm": 8,
    "trust": 8,
    "authenticity": 8,
    "density": 8,
}


def score_text(
    text: str,
    phrases_set: set[str],
    structures: Sequence[StructuralPattern | Mapping[str, object]],
    *,
    llm_enabled: bool = True,
) -> dict[str, object]:
    """Score text for stop-slop signals. The default ``llm_enabled=True``
    asks the LLM grader for the 5 dimensions and falls back to defaults
    when the LLM is unreachable; ``llm_enabled=False`` is a hard
    deterministic mode (PR-D4 evaluator path) that skips the LLM
    entirely so CI / acceptance gate runs are reproducible without
    needing a gateway."""
    findings = _static_findings(text, phrases_set, structures)
    if llm_enabled:
        dimensions = _llm_dimensions(text) or dict(DEFAULT_DIMENSIONS)
    else:
        dimensions = dict(DEFAULT_DIMENSIONS)
    _apply_static_deductions(dimensions, findings)
    total = sum(dimensions.values())
    return {
        "dimensions": dimensions,
        "total": total,
        "findings": findings,
    }


def score_text_static(
    text: str,
    phrases_set: set[str],
    structures: Sequence[StructuralPattern | Mapping[str, object]],
) -> dict[str, object]:
    """Deterministic stop-slop scorer (PR-D4 codex round-1 A2).
    Equivalent to ``score_text(..., llm_enabled=False)`` — kept as a
    public shorthand so the evaluator's intent is obvious at the
    call site.

    PR-I2 retro fix #4 (codex retrospective B4): the ``structures``
    argument is currently a no-op — ``_structure_findings`` ignores
    its input and runs hardcoded checks. Callers should still pass
    ``rules.structures`` so the call site reads correctly when the
    structures wiring is restored. We document the gap rather than
    delete the parameter so the contract stays stable."""
    return score_text(text, phrases_set, structures, llm_enabled=False)


def _static_findings(
    text: str,
    phrases_set: set[str],
    structures: Sequence[StructuralPattern | Mapping[str, object]],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    findings.extend(_banned_phrase_findings(text, phrases_set))
    findings.extend(_structure_findings(text, structures))
    findings.extend(_em_dash_findings(text))
    findings.extend(_opener_findings(text))
    rhythm = _rhythm_findings(text)
    if rhythm is not None:
        findings.append(rhythm)
    return _dedupe_findings(findings)


def _banned_phrase_findings(text: str, phrases_set: set[str]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for phrase in sorted(phrases_set, key=lambda item: (-len(item), item)):
        if len(phrase) < 4:
            continue
        pattern = re.compile(_phrase_pattern(phrase), re.IGNORECASE)
        for match in pattern.finditer(text):
            findings.append(
                _finding(
                    "banned_phrase",
                    match.span(),
                    f"Remove stop-slop phrase: {match.group(0)}",
                    "medium",
                ),
            )
    return findings


def _structure_findings(
    text: str,
    structures: Sequence[StructuralPattern | Mapping[str, object]],
) -> list[dict[str, object]]:
    del structures
    checks = [
        (
            "not_just_form",
            re.compile(
                r"\b(?:it(?:'|’)?s\s+)?not\s+just\b.{0,120}?\b"
                r"(?:but\s+also|but|it(?:'|’)?s)\b",
                re.IGNORECASE | re.DOTALL,
            ),
            "Replace the 'not just X, it is Y' construction with the direct claim.",
        ),
        (
            "binary_contrast",
            re.compile(
                r"\bnot\s+because\b.{0,120}?\bbecause\b|"
                r"\b(?:it(?:'|’)?s\s+)?not\s+just\b.{0,120}?\b"
                r"(?:but\s+also|but|it(?:'|’)?s)\b|"
                r"\b(?:answer|question|problem)\s+isn(?:'|’)?t\b.{0,120}?\b(?:it(?:'|’)?s|is)\b|"
                r"\bisn(?:'|’)?t\b.{0,80}?,\s*(?:but\s+)?(?:it(?:'|’)?s\s+)?",
                re.IGNORECASE | re.DOTALL,
            ),
            "Avoid binary contrast. State the positive claim directly.",
        ),
        (
            "negative_listing",
            re.compile(r"\bnot\s+a\b.{0,60}?\bnot\s+a\b", re.IGNORECASE | re.DOTALL),
            "Avoid listing what the claim is not before naming what it is.",
        ),
    ]
    findings: list[dict[str, object]] = []
    for finding_type, pattern, message in checks:
        for match in pattern.finditer(text):
            findings.append(_finding(finding_type, match.span(), message, "high"))
    return findings


def _em_dash_findings(text: str) -> list[dict[str, object]]:
    spans = [match.span() for match in re.finditer(r"[—–]", text)]
    findings = [
        _finding("em_dash", span, "Replace em dashes with commas or periods.", "medium")
        for span in spans
    ]
    if len(spans) >= 2:
        first_start = spans[0][0]
        last_end = spans[-1][1]
        findings.append(
            _finding(
                "em_dash_overuse",
                (first_start, last_end),
                "Multiple em dashes create a stop-slop rhythm pattern.",
                "high",
            ),
        )
    return findings


def _opener_findings(text: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    opener_pattern = re.compile(
        r"(^|(?<=[.!?]\s))(?P<opener>(?:here(?:'|’)s|so\b|look,|"
        r"what\b|when\b|where\b|which\b|who\b|why\b|how\b)[^.!?]{0,80})",
        re.IGNORECASE,
    )
    for match in opener_pattern.finditer(text):
        opener = match.group("opener")
        findings.append(
            _finding(
                "opener_cliche",
                (match.start("opener"), match.end("opener")),
                f"Lead with the subject instead of opener: {opener[:60]}",
                "medium",
            ),
        )
    return findings


def _rhythm_findings(text: str) -> dict[str, object] | None:
    lengths = [_word_count(sentence) for sentence in _sentences(text)]
    useful_lengths = [length for length in lengths if length > 0]
    if len(useful_lengths) < 4:
        return None
    if pstdev(useful_lengths) <= 2.0:
        return _finding(
            "rhythm_monotony",
            (0, min(len(text), 240)),
            "Sentence lengths are too similar; vary rhythm.",
            "medium",
        )
    return None


def _llm_dimensions(text: str) -> dict[str, int] | None:
    settings = get_settings()
    if not settings.stop_slop_llm_enabled or not text.strip():
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return None
    try:
        return asyncio.run(_llm_dimensions_async(text))
    except Exception:  # noqa: BLE001 - deterministic static fallback keeps scoring available.
        return None


async def _llm_dimensions_async(text: str) -> dict[str, int] | None:
    settings = get_settings()
    http_client = httpx.AsyncClient(base_url=settings.one_api_base_url, timeout=8.0)
    client = LLMClient(http_client=http_client)
    try:
        response = await client.chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "You grade prose quality using five stop-slop dimensions. "
                        "Return only JSON with integer scores from 0 to 10."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Rubric dimensions: directness, rhythm, trust, authenticity, density. "
                        "Score the following manuscript excerpt. "
                        'Return JSON like {"dimensions":{"directness":8,...}}.\n\n'
                        f"{text[:9000]}"
                    ),
                },
            ],
            model=settings.one_api_model,
            temperature=0.0,
            max_tokens=240,
            retries=0,
            response_format={"type": "json_object"},
            validate_json_content=True,
        )
    finally:
        await client.aclose()
    return _parse_llm_dimensions(str(response.get("content", "")))


def _parse_llm_dimensions(value: str) -> dict[str, int] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    raw_dimensions = decoded.get("dimensions", decoded)
    if not isinstance(raw_dimensions, dict):
        return None
    dimensions: dict[str, int] = {}
    for dimension in DIMENSIONS:
        raw_score = raw_dimensions.get(dimension)
        if isinstance(raw_score, int | float):
            dimensions[dimension] = _clamp_score(int(round(raw_score)))
    if set(dimensions) != set(DIMENSIONS):
        return None
    return dimensions


def _apply_static_deductions(
    dimensions: dict[str, int],
    findings: Sequence[Mapping[str, object]],
) -> None:
    counts: dict[str, int] = {}
    for finding in findings:
        finding_type = str(finding.get("type", ""))
        counts[finding_type] = counts.get(finding_type, 0) + 1
    _deduct(dimensions, "directness", min(4, counts.get("banned_phrase", 0)))
    _deduct(dimensions, "density", min(4, counts.get("banned_phrase", 0)))
    _deduct(dimensions, "authenticity", min(3, counts.get("banned_phrase", 0)))
    binary_count = counts.get("binary_contrast", 0) + counts.get("not_just_form", 0)
    _deduct(dimensions, "directness", min(5, binary_count * 3))
    _deduct(dimensions, "trust", min(4, binary_count * 2))
    _deduct(dimensions, "authenticity", min(4, binary_count * 2))
    _deduct(dimensions, "rhythm", min(3, counts.get("em_dash", 0)))
    if counts.get("em_dash_overuse", 0):
        _deduct(dimensions, "rhythm", 4)
        _deduct(dimensions, "authenticity", 1)
    opener_count = counts.get("opener_cliche", 0)
    _deduct(dimensions, "directness", min(4, opener_count * 2))
    _deduct(dimensions, "density", min(3, opener_count))
    if counts.get("rhythm_monotony", 0):
        _deduct(dimensions, "rhythm", 3)
    for dimension in DIMENSIONS:
        dimensions[dimension] = _clamp_score(dimensions[dimension])


def _deduct(dimensions: dict[str, int], dimension: str, amount: int) -> None:
    dimensions[dimension] = _clamp_score(dimensions.get(dimension, 0) - amount)


def _finding(
    finding_type: str,
    span: tuple[int, int],
    message: str,
    severity: str,
) -> dict[str, object]:
    return {
        "type": finding_type,
        "span": [span[0], span[1]],
        "message": message,
        "severity": severity,
    }


def _dedupe_findings(findings: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[str, int, int]] = set()
    deduped: list[dict[str, object]] = []
    for finding in findings:
        span = finding.get("span")
        if not isinstance(span, list) or len(span) != 2:
            continue
        start = span[0]
        end = span[1]
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        finding_type = str(finding.get("type", ""))
        key = (finding_type, start, end)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cast(dict[str, object], dict(finding)))
    return deduped


def _phrase_pattern(phrase: str) -> str:
    pieces = [re.escape(piece) for piece in phrase.split()]
    return r"(?<!\w)" + r"\s+".join(pieces) + r"(?!\w)"


def _sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", value))


def _clamp_score(value: int) -> int:
    if math.isnan(float(value)):
        return 0
    return max(0, min(10, value))
