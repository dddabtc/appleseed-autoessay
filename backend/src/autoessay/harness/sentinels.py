"""Output-quality sentinels.

A long-term mechanism baked into the harness: every ``run_llm_step``
call automatically inspects the parsed LLM output for forbidden
patterns that have leaked into manuscripts in the past. If anything
matches, the harness treats the response as a schema violation and
fires the corrective-retry loop, exactly as if the JSON had failed
Pydantic validation.

This is **default-deny**: anything that looks like internal
placeholder text — debug strings, stub fallbacks, prompt-template
variables, dev-template section names — must not reach a downstream
phase. Agents can extend the list with phase-specific patterns by
passing ``extra_sentinels`` to ``run_llm_step``.

The module is intentionally tiny and side-effect free so it can be
imported from runner / agents / exporter / tests without circularity.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from re import Pattern
from typing import Any

# Substrings that should NEVER appear inside any human-readable string
# value coming out of an agent. They are markers from earlier failure
# modes that we have explicitly observed leaking into manuscripts.
DEFAULT_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "[UNCITED]",
    "TODO_EVIDENCE",
    "stub-extracted-text",
    "stub_extracted_text",
    "本节原始草稿为空",
    "占位文本",  # generic Chinese "placeholder text"
    "<<placeholder>>",
    "<<TODO>>",
    "<placeholder>",
    "{{placeholder}}",
    # Empty-content boilerplate the LLM emits when it has no real material.
    # These are content-vacant sentences that disguise lack of evidence as
    # "scholarly summarising" — banning them forces the writer to either
    # cite something specific or drop the sentence.
    "系统性的总结与归纳",
    "本文围绕相关主题给出了系统性",
    "研究结果表明所识别的模式具有一致性",
    "本文不仅补充了该领域的经验认识",
    "为后续研究提供了可供延展的基础",
)

# Regex patterns. Matched case-insensitively. Use raw strings.
DEFAULT_FORBIDDEN_REGEXES: tuple[Pattern[str], ...] = (
    # Variable-name leaks: e.g. "discussion-p001" or "claim_id-abc123"
    # appearing as literal text instead of being substituted. We match
    # the surface form, requiring the whole token (separated by word
    # boundaries) to look like an unsubstituted identifier.
    re.compile(r"\bparagraph_id\b", re.IGNORECASE),
    re.compile(r"\bclaim_id-[a-z0-9_]+", re.IGNORECASE),
    # Round-bracketed identifier-only stand-ins like "（discussion-p001）"
    # or "(discussion-p002)" — purely the section_id-pNNN pattern.
    re.compile(r"[（(]\s*[a-z]+-p\d+\s*[）)]", re.IGNORECASE),
    # Generic dev-template body section labels. These should be
    # replaced by domain-specific topic names before reaching prose.
    # Match either a markdown heading line or a standalone full-string
    # "Body N" (which is what ``section_title`` would carry).
    re.compile(r"^\s*##\s*Body\s+\d+\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\A\s*Body\s+\d+\s*\Z", re.IGNORECASE),
    # Mustache / Jinja-style variable that wasn't substituted.
    re.compile(r"\{\{\s*[a-z_][a-z0-9_]*\s*\}\}", re.IGNORECASE),
)

# Keys whose values are identifiers / paths / URLs and should NOT be
# scanned. Extending this list is the right way to silence false
# positives, never weakening the substring/regex lists.
SAFE_KEY_SUFFIXES: tuple[str, ...] = (
    "_id",
    "_ids",
    "_path",
    "_url",
    "_hash",
    "_doi",
    "_sha256",
    "_uri",
    # PR-G-CriticScores wireup follow-up (round 5 evidence): the
    # polish blind-eval LLM emits ``a_compliance_justification`` /
    # ``b_completeness_justification`` etc. that legitimately
    # discuss whether the manuscripts contain ``[UNCITED]`` /
    # ``TODO_EVIDENCE`` (e.g. "未见 [UNCITED]"). Drafter / synthesizer
    # / stylist don't have any field named ``*_justification``,
    # so this exemption is surgical: critic evaluator output skips
    # the substring/regex scan, every other agent still gets the
    # default-deny coverage. Round 4 + 5 had ALL polish_blind_eval
    # calls bouncing on this; result was no polish_quality.json.
    "_justification",
    "id",
    "url",
    "path",
    "hash",
    "doi",
    "key",
    "uri",
)

# v3 paired blind critic output is evaluator metadata, not manuscript prose.
# It must be allowed to mention sentinels such as "[UNCITED]" and
# "TODO_EVIDENCE" while explaining whether a candidate contains them. Keep this
# exemption scoped to the paired candidate report audit fields so normal agent
# prose remains default-deny.
SAFE_FIELD_PATH_SEGMENTS: tuple[str, ...] = (
    ".score_breakdown.",
    ".deduction_ledger",
    ".repair_plan_to_full_score",
    ".full_score_revision_contract.",
    ".frozen_issue_registry.",
)


@dataclass(frozen=True)
class SentinelViolation:
    """A specific violation found in a parsed agent output."""

    field_path: str
    pattern: str
    sample: str

    def message(self) -> str:
        return (
            f"sentinel violation at {self.field_path or '<root>'}: "
            f"forbidden pattern {self.pattern!r}; "
            f"sample={self.sample[:120]!r}"
        )


def check_value(
    value: Any,
    *,
    extra_substrings: Iterable[str] = (),
    extra_regexes: Iterable[Pattern[str]] = (),
) -> list[SentinelViolation]:
    """Walk ``value`` recursively and return any sentinel violations.

    ``value`` may be a dict, list, Pydantic model (with ``.dict()``),
    or a primitive. Identifier-like keys (per ``SAFE_KEY_SUFFIXES``)
    are skipped so that ``source_id="crossref:10..."`` is never
    flagged as containing forbidden bracket expressions.
    """
    substrings = (*DEFAULT_FORBIDDEN_SUBSTRINGS, *tuple(extra_substrings))
    regexes = (*DEFAULT_FORBIDDEN_REGEXES, *tuple(extra_regexes))
    violations: list[SentinelViolation] = []
    for path, sample in _walk_strings(value):
        for needle in substrings:
            if needle in sample:
                violations.append(
                    SentinelViolation(field_path=path, pattern=needle, sample=sample),
                )
                break
        else:
            for pattern in regexes:
                m = pattern.search(sample)
                if m is not None:
                    violations.append(
                        SentinelViolation(
                            field_path=path,
                            pattern=pattern.pattern,
                            sample=sample,
                        ),
                    )
                    break
    return violations


def _walk_strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if _is_safe_path(path):
            return
        if value.strip():
            yield (path, value)
        return
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            if _is_safe_key(key):
                continue
            child_path = f"{path}.{key}" if path else key
            yield from _walk_strings(child, child_path)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{path}[{index}]")
        return
    # Pydantic models expose .dict() — fall back to that for any
    # arbitrary object so we can scan their fields.
    dump = getattr(value, "dict", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:  # noqa: BLE001 - sentinel must never crash the run
            return
        yield from _walk_strings(data, path)
        return
    # Anything else (bytes, custom types) is ignored.


def _is_safe_key(key: str) -> bool:
    lower = key.lower()
    return any(lower == suffix or lower.endswith(suffix) for suffix in SAFE_KEY_SUFFIXES)


def _is_safe_path(path: str) -> bool:
    if path.startswith("candidate_reports["):
        return any(segment in path for segment in SAFE_FIELD_PATH_SEGMENTS)
    # North-star gate output is evaluator metadata, not manuscript prose.
    # Its boxed ledger must be allowed to mention sentinel marker names while
    # judging whether a candidate contains them.
    return path.startswith("scores.") and ".items[" in path


def format_violations(violations: Sequence[SentinelViolation]) -> list[str]:
    """Format violations as one error string per violation."""
    return [v.message() for v in violations]
