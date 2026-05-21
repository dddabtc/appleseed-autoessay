"""Shared helpers for round-0 holistic revision in polish loop + critic loop.

Round 0 is the "整体性改稿" pass inserted before each loop's structured rounds:
the LLM integrates the critic's revision items into one full rewrite before
per-item repair runs. Round 0 is unconditional (no monotone / take-best),
but its output must pass a deterministic sanity gate so it cannot wreck the
manuscript — codex AGREE-WITH-AMENDMENTS 2026-05-12 was explicit that no
sanity check is unsafe because critic_loop round 0 directly replaces the
manuscript with no downstream compliance review.

Sanity gate (`round0_sanity_check`): non-empty + length ≥ 70% of incumbent
+ no TODO/UNCITED sentinels + citation marker multiset preserved + CNKI
structural blocks intact (zh/ja only). The 70% floor is looser than polish
loop's 95% hard validator on purpose: structured rounds run after and re-run
their own validators, so round 0 only has to avoid catastrophic regressions
(empty, mostly-deleted, sentinels left in, citations stripped).

Context preflight (`compact_critique_payload`): the critique JSON returned
by the V2 (`ExpertCritiqueOutput`) and V3 (`POLISH_BLIND_EVAL_V3`) prompts
runs 6-13k completion tokens. Stuffing the full JSON back as the assistant
turn in a 4-turn chat-style round-0 prompt plus the manuscript can push input
past the endpoint window. When estimated input + max_output exceeds 85% of
``window_tokens``, the critique JSON is replaced with a compact form keeping
only the fields the LLM needs to integrate items: scores, top verdict,
revision_items, deletion_or_compression_plan, fatal blockers, deduction_ledger.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Prompt directives
# ---------------------------------------------------------------------------

ROUND0_DIRECTIVE_USER_TURN = (
    "请你按照你上面提出的修改意见，给一个完整的修改版本。"
    '只返回严格 JSON：{"manuscript": "..."}，'
    "其中 manuscript 字段是修订后的完整中文论文 markdown 正文。"
    "不要返回额外字段、不要重新打分、不要解释你的修改。"
)


# ---------------------------------------------------------------------------
# Token estimation + context preflight
# ---------------------------------------------------------------------------

# Conservative heuristic: 1 token ≈ 2 chars for CJK-heavy text (real rate is
# 1.5-2 chars/token for Chinese on GPT-class tokenizers; we round down for
# safety so preflight errs toward "compact"). Latin text is ~4 chars/token
# but our critic payloads are CJK-dominant, so we use the tighter estimate.
_CHARS_PER_TOKEN = 2.0


def estimate_tokens(text: str) -> int:
    """Conservative char→token estimate suitable for context preflight only."""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def should_compact_critique(
    *,
    system_text: str,
    user_turn_1_text: str,
    critique_json_text: str,
    user_turn_2_text: str,
    max_output_tokens: int,
    window_tokens: int,
    safety_ratio: float = 0.85,
) -> bool:
    """Return True when the 4-turn chat-style request would exceed safety_ratio
    of the endpoint's context window. Caller should then swap the full critique
    JSON for a compact payload via ``compact_critique_payload``.
    """
    if window_tokens <= 0:
        return False
    input_tokens = (
        estimate_tokens(system_text)
        + estimate_tokens(user_turn_1_text)
        + estimate_tokens(critique_json_text)
        + estimate_tokens(user_turn_2_text)
    )
    total = input_tokens + max_output_tokens
    return total > window_tokens * safety_ratio


_COMPACT_CRITIQUE_KEEP_FIELDS = (
    "scores",
    "top_journal_readiness",
    "editorial_decision_if_submitted_now",
    "value_assessment",
    "revision_items",
    "deletion_or_compression_plan",
    "deduction_ledger",
    "repair_plan_to_full_score",
    "fatal_issues",
    "needs_revision",
)


def compact_critique_payload(critique_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip critique JSON down to fields the LLM needs to integrate revision
    items in round 0. Drops verbose narrative fields (per-item reasoning, etc.)
    while keeping scores, verdicts, and the revision/repair list itself.
    """
    return {
        key: critique_payload[key]
        for key in _COMPACT_CRITIQUE_KEEP_FIELDS
        if key in critique_payload
    }


# ---------------------------------------------------------------------------
# Sanity gate
# ---------------------------------------------------------------------------

_SENTINEL_RE = re.compile(
    r"\[UNCITED\]|TODO(?:_EVIDENCE)?|FIXME|<\s*TODO\b",
    flags=re.IGNORECASE,
)


def _length_floor_chars(incumbent_text: str, ratio: float) -> int:
    return int(len(incumbent_text) * ratio)


def round0_sanity_check(
    *,
    candidate_text: str,
    incumbent_text: str,
    language: str,
    length_floor_ratio: float = 0.70,
) -> dict[str, Any]:
    """Deterministic sanity gate for round-0 holistic rewrite outputs.

    Returns a dict ``{"ok": bool, "reasons": [str], "details": {...}}``.
    Callers treat ``ok=False`` as "skip round 0, keep incumbent" — never
    raise. The reasons + details land in audit.round0_holistic.sanity.

    Checks:
    1. ``empty_candidate`` — manuscript text stripped to empty.
    2. ``length_below_floor`` — candidate length < ``length_floor_ratio``
       (default 70%) of incumbent.
    3. ``unresolved_marker_or_todo`` — any [UNCITED] / TODO / FIXME left.
    4. ``citation_multiset_changed`` — inline citation marker multiset
       not equal to incumbent's. Caller supplies ``extract_citations`` so
       this module stays import-free from agents/drafter.
    5. ``cnki_structure_incomplete`` — for zh/ja: 摘要/关键词/参考文献
       headings or 一、…八、 body section ordering missing. Optional —
       caller passes ``cnki_errors`` from ``_controlled_polish_cnki_structure_errors``.

    The caller wires extractors in to avoid circular imports between
    final_rewrite.py / critic_loop.py / drafter.py.
    """
    # Caller-provided dependencies are reserved for the rich-context path;
    # this minimal core lets unit tests exercise the gate without monkey-
    # patching the whole agents module. Real callers should wrap this with
    # ``round0_sanity_check_with_deps`` below.
    raise NotImplementedError(
        "Use round0_sanity_check_with_deps for production callers; this stub "
        "exists only to document the contract."
    )


def round0_sanity_check_with_deps(
    *,
    candidate_text: str,
    incumbent_text: str,
    language: str,
    extract_citations: Any,
    cnki_structure_errors: Any,
    length_floor_ratio: float = 0.70,
) -> dict[str, Any]:
    """See ``round0_sanity_check``. ``extract_citations`` and
    ``cnki_structure_errors`` are callables passed in by the caller to keep
    this helper free of circular imports.
    """
    reasons: list[str] = []
    details: dict[str, Any] = {}

    candidate_clean = candidate_text.strip()
    if not candidate_clean:
        reasons.append("empty_candidate")
        details["empty_candidate"] = True
        return {"ok": False, "reasons": reasons, "details": details}

    floor = _length_floor_chars(incumbent_text, length_floor_ratio)
    if len(candidate_clean) < floor:
        reasons.append("length_below_floor")
        details["length"] = {
            "before": len(incumbent_text),
            "after": len(candidate_clean),
            "floor_ratio": length_floor_ratio,
            "floor_chars": floor,
        }

    sentinel_matches = _SENTINEL_RE.findall(candidate_clean)
    if sentinel_matches:
        reasons.append("unresolved_marker_or_todo")
        details["sentinels"] = sentinel_matches[:10]

    expected_citations = extract_citations(incumbent_text)
    actual_citations = extract_citations(candidate_clean)
    if Counter(actual_citations) != Counter(expected_citations):
        reasons.append("citation_multiset_changed")
        details["citations"] = {
            "expected": list(expected_citations),
            "actual": list(actual_citations),
        }

    cnki_errors = cnki_structure_errors(candidate_clean, language)
    if cnki_errors:
        reasons.append("cnki_structure_incomplete")
        details["cnki_errors"] = list(cnki_errors)

    return {"ok": not reasons, "reasons": reasons, "details": details}


# ---------------------------------------------------------------------------
# 4-turn message builder (chat-style critique replay)
# ---------------------------------------------------------------------------


def build_round0_messages(
    *,
    critique_system_prompt: str,
    critique_user_turn_1: str,
    critique_assistant_payload: Mapping[str, Any] | str,
    user_turn_2_directive: str = ROUND0_DIRECTIVE_USER_TURN,
) -> list[dict[str, str]]:
    """Build the chat-style 4-turn message list for round-0 LLM call.

    Layout:
      [0] system   — the critique's system prompt (reused verbatim)
      [1] user     — the critique's user prompt (reused verbatim;
                     contains the manuscript)
      [2] assistant — the prior critique output as JSON string
      [3] user     — round-0 directive ("请按你上面提出的修改意见…")

    ``critique_assistant_payload`` may be a dict (will be json.dumps'd
    with ensure_ascii=False, sort_keys=True for stable hashing) or an
    already-serialized JSON string.
    """
    if isinstance(critique_assistant_payload, str):
        assistant_text = critique_assistant_payload
    else:
        assistant_text = json.dumps(
            dict(critique_assistant_payload),
            ensure_ascii=False,
            sort_keys=True,
        )
    return [
        {"role": "system", "content": critique_system_prompt},
        {"role": "user", "content": critique_user_turn_1},
        {"role": "assistant", "content": assistant_text},
        {"role": "user", "content": user_turn_2_directive},
    ]
