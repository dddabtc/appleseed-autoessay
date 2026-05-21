"""Unit tests for ``backend/src/autoessay/agents/_round0_helpers.py``.

Sanity gate + context preflight + 4-turn message builder. These tests run
without agents / DB / harness; they exercise the deterministic helpers that
the round-0 integration in polish loop + critic loop both depend on.
"""

from __future__ import annotations

import json

from autoessay.agents._round0_helpers import (
    ROUND0_DIRECTIVE_USER_TURN,
    build_round0_messages,
    compact_critique_payload,
    estimate_tokens,
    round0_sanity_check_with_deps,
    should_compact_critique,
)


def _fake_extract_citations(text: str) -> list[str]:
    # Pull "[N]" tokens out of the text so tests can mimic drafter._extract_inline_citations
    import re

    return re.findall(r"\[\d+\]", text)


def _fake_cnki_no_errors(_text: str, _language: str) -> list[str]:
    return []


def _fake_cnki_missing(_text: str, _language: str) -> list[str]:
    return ["missing_摘要"]


# ---------------------------------------------------------------------------
# round0_sanity_check_with_deps
# ---------------------------------------------------------------------------


def test_sanity_pass_returns_ok_with_no_reasons() -> None:
    candidate = "# Paper\n\n论点 [1] [2] 充分。\n" * 50
    incumbent = "# Paper\n\n论点 [1] [2] 不够。\n" * 50
    result = round0_sanity_check_with_deps(
        candidate_text=candidate,
        incumbent_text=incumbent,
        language="zh",
        extract_citations=_fake_extract_citations,
        cnki_structure_errors=_fake_cnki_no_errors,
    )
    assert result["ok"] is True
    assert result["reasons"] == []


def test_sanity_empty_candidate_short_circuits() -> None:
    result = round0_sanity_check_with_deps(
        candidate_text="   \n  ",
        incumbent_text="anything",
        language="zh",
        extract_citations=_fake_extract_citations,
        cnki_structure_errors=_fake_cnki_no_errors,
    )
    assert result["ok"] is False
    assert "empty_candidate" in result["reasons"]


def test_sanity_length_below_70pct_floor_fails() -> None:
    incumbent = "x" * 1000
    candidate = "x" * 600  # 60% — below default 70%
    result = round0_sanity_check_with_deps(
        candidate_text=candidate,
        incumbent_text=incumbent,
        language="zh",
        extract_citations=_fake_extract_citations,
        cnki_structure_errors=_fake_cnki_no_errors,
    )
    assert result["ok"] is False
    assert "length_below_floor" in result["reasons"]
    assert result["details"]["length"]["after"] == 600


def test_sanity_unresolved_todo_marker_fails() -> None:
    candidate = "# Paper\n\n论点 [1]。TODO_EVIDENCE: 补充数据。\n" * 50
    result = round0_sanity_check_with_deps(
        candidate_text=candidate,
        incumbent_text=candidate,
        language="zh",
        extract_citations=_fake_extract_citations,
        cnki_structure_errors=_fake_cnki_no_errors,
    )
    assert result["ok"] is False
    assert "unresolved_marker_or_todo" in result["reasons"]


def test_sanity_uncited_sentinel_fails() -> None:
    candidate = "# Paper\n\n论点 [UNCITED] 待补 [1]。\n" * 50
    result = round0_sanity_check_with_deps(
        candidate_text=candidate,
        incumbent_text=candidate,
        language="zh",
        extract_citations=_fake_extract_citations,
        cnki_structure_errors=_fake_cnki_no_errors,
    )
    assert result["ok"] is False
    assert "unresolved_marker_or_todo" in result["reasons"]


def test_sanity_citation_multiset_change_fails() -> None:
    incumbent = "Refs [1] [2] [2] [3]" + " padding" * 200
    # Same length but one [2] dropped → multiset different
    candidate = "Refs [1] [2] [3]" + " padding" * 200
    result = round0_sanity_check_with_deps(
        candidate_text=candidate,
        incumbent_text=incumbent,
        language="zh",
        extract_citations=_fake_extract_citations,
        cnki_structure_errors=_fake_cnki_no_errors,
    )
    assert result["ok"] is False
    assert "citation_multiset_changed" in result["reasons"]


def test_sanity_cnki_structure_failure_propagates() -> None:
    candidate = "# Paper" + " padding" * 500
    result = round0_sanity_check_with_deps(
        candidate_text=candidate,
        incumbent_text=candidate,
        language="zh",
        extract_citations=_fake_extract_citations,
        cnki_structure_errors=_fake_cnki_missing,
    )
    assert result["ok"] is False
    assert "cnki_structure_incomplete" in result["reasons"]
    assert "missing_摘要" in result["details"]["cnki_errors"]


def test_sanity_collects_multiple_reasons() -> None:
    incumbent = "Refs [1] [2]" + " padding" * 500
    candidate = "Refs [1] TODO" + " padding" * 100  # short + sentinel + multiset
    result = round0_sanity_check_with_deps(
        candidate_text=candidate,
        incumbent_text=incumbent,
        language="zh",
        extract_citations=_fake_extract_citations,
        cnki_structure_errors=_fake_cnki_no_errors,
    )
    assert result["ok"] is False
    assert "length_below_floor" in result["reasons"]
    assert "unresolved_marker_or_todo" in result["reasons"]
    assert "citation_multiset_changed" in result["reasons"]


# ---------------------------------------------------------------------------
# Context preflight
# ---------------------------------------------------------------------------


def test_should_compact_critique_returns_false_when_well_under_window() -> None:
    assert (
        should_compact_critique(
            system_text="x" * 1000,
            user_turn_1_text="y" * 1000,
            critique_json_text="z" * 1000,
            user_turn_2_text="w" * 100,
            max_output_tokens=4000,
            window_tokens=100000,
        )
        is False
    )


def test_should_compact_critique_returns_true_near_window() -> None:
    # 2 chars per token → 200k chars ≈ 100k tokens, with 25k output = 125k > 85k floor
    big_text = "x" * 200000
    assert (
        should_compact_critique(
            system_text="",
            user_turn_1_text="",
            critique_json_text=big_text,
            user_turn_2_text="",
            max_output_tokens=25000,
            window_tokens=100000,
        )
        is True
    )


def test_should_compact_critique_zero_window_disables() -> None:
    assert (
        should_compact_critique(
            system_text="x" * 1000000,
            user_turn_1_text="",
            critique_json_text="",
            user_turn_2_text="",
            max_output_tokens=10000,
            window_tokens=0,
        )
        is False
    )


def test_estimate_tokens_increases_with_length() -> None:
    assert estimate_tokens("") == 0
    short = estimate_tokens("hello")
    long = estimate_tokens("hello" * 100)
    assert long > short
    assert short > 0


# ---------------------------------------------------------------------------
# compact_critique_payload
# ---------------------------------------------------------------------------


def test_compact_critique_keeps_listed_fields_only() -> None:
    full = {
        "scores": {"compliance": 8.0},
        "top_journal_readiness": "ok",
        "editorial_decision_if_submitted_now": "revise",
        "value_assessment": "fine",
        "revision_items": [{"id": "r1"}],
        "deletion_or_compression_plan": ["chunk a"],
        "deduction_ledger": [{"id": "d1"}],
        "repair_plan_to_full_score": [{"id": "p1"}],
        "fatal_issues": [],
        "needs_revision": True,
        # These should be dropped:
        "value_assessment_long_form": "x" * 5000,
        "internal_reasoning_traces": [{"step": 1}],
        "model_thinking": "drop me",
    }
    compact = compact_critique_payload(full)
    assert "value_assessment_long_form" not in compact
    assert "internal_reasoning_traces" not in compact
    assert "model_thinking" not in compact
    assert compact["scores"] == {"compliance": 8.0}
    assert compact["revision_items"] == [{"id": "r1"}]
    assert compact["needs_revision"] is True


def test_compact_critique_handles_missing_fields() -> None:
    full = {"scores": {"compliance": 7.0}}
    compact = compact_critique_payload(full)
    assert compact == {"scores": {"compliance": 7.0}}


# ---------------------------------------------------------------------------
# build_round0_messages
# ---------------------------------------------------------------------------


def test_build_round0_messages_yields_4_turn_structure() -> None:
    messages = build_round0_messages(
        critique_system_prompt="SYSTEM",
        critique_user_turn_1="USER1",
        critique_assistant_payload={"a": 1, "b": [2, 3]},
    )
    assert len(messages) == 4
    assert messages[0] == {"role": "system", "content": "SYSTEM"}
    assert messages[1] == {"role": "user", "content": "USER1"}
    assert messages[2]["role"] == "assistant"
    # JSON dump is deterministic (sort_keys=True), ensure_ascii=False
    assert messages[2]["content"] == json.dumps(
        {"a": 1, "b": [2, 3]}, ensure_ascii=False, sort_keys=True
    )
    assert messages[3] == {
        "role": "user",
        "content": ROUND0_DIRECTIVE_USER_TURN,
    }


def test_build_round0_messages_passthrough_str_assistant() -> None:
    messages = build_round0_messages(
        critique_system_prompt="SYS",
        critique_user_turn_1="U1",
        critique_assistant_payload='{"already":"json"}',
    )
    assert messages[2]["content"] == '{"already":"json"}'


def test_directive_contains_user_literal_phrase() -> None:
    # Codex amendment 2026-05-12: user's literal phrasing must survive.
    # If a refactor strips this phrase, the round-0 prompt no longer
    # matches the user's stated intent ("按你上面提出的修改意见…").
    assert "请你按照你上面提出的修改意见" in ROUND0_DIRECTIVE_USER_TURN
    assert "完整的修改版本" in ROUND0_DIRECTIVE_USER_TURN


def test_directive_requires_strict_json_output() -> None:
    # Output format guard: harness validates JSON, so the directive
    # must instruct the model to return strict JSON with manuscript field.
    assert "manuscript" in ROUND0_DIRECTIVE_USER_TURN
    assert "JSON" in ROUND0_DIRECTIVE_USER_TURN
