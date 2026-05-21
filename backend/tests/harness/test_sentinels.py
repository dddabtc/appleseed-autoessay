"""Tests for the harness output-sanity sentinels."""

from __future__ import annotations

from autoessay.harness.sentinels import (
    DEFAULT_FORBIDDEN_SUBSTRINGS,
    SentinelViolation,
    check_value,
    format_violations,
)


def test_clean_text_passes() -> None:
    assert check_value("This is a normal paragraph about banking history.") == []


def test_uncited_literal_blocked() -> None:
    violations = check_value({"prose": "This claim has [UNCITED] markers."})
    assert violations
    assert any("[UNCITED]" in v.message() for v in violations)


def test_todo_evidence_literal_blocked() -> None:
    violations = check_value({"prose": "需要补充资料 TODO_EVIDENCE 然后定稿。"})
    assert violations


def test_stub_extracted_text_blocked() -> None:
    violations = check_value(
        {"prose": "本节以 stub-extracted-text 的方式标示其论述基础。"},
    )
    assert violations


def test_chinese_placeholder_blocked() -> None:
    """Stylist's "本节原始草稿为空…占位文本" filler must trigger."""
    violations = check_value(
        {"prose": "本节原始草稿为空，因此无法在不增加新内容的前提下进行实质性润色。"},
    )
    assert violations


def test_paragraph_id_literal_blocked() -> None:
    violations = check_value(
        {"prose": "首先，结果表明（discussion-p001），该发现与相关研究相一致。"},
    )
    assert violations
    assert any("p\\d" in v.pattern or "discussion" in v.sample for v in violations)


def test_body_n_heading_blocked() -> None:
    violations = check_value({"section_title": "Body 1"})
    assert violations
    # The matching is on "## Body N" as a markdown heading line; if the
    # title is just the word "Body 1" without "##", the substring still
    # has no match. So accept either pattern. But typically in prose:
    violations = check_value({"prose": "## Body 1\n\nThis section..."})
    assert violations


def test_safe_keys_skipped() -> None:
    """source_id with placeholder-looking content must not flag — it's
    an identifier."""
    assert check_value({"source_id": "crossref:10.5/8"}) == []
    assert check_value({"doi": "10.5/[UNCITED]"}) == []
    assert check_value({"url": "https://example.com/paragraph_id-p001"}) == []


def test_paired_critic_audit_fields_can_discuss_sentinels() -> None:
    """The v3 paired critic is allowed to say a candidate has no
    ``[UNCITED]`` / ``TODO_EVIDENCE`` markers in evaluator metadata.
    This exemption must not apply to manuscript prose fields.
    """
    assert (
        check_value(
            {
                "candidate_reports": [
                    {
                        "candidate_id": "A",
                        "score_breakdown": {
                            "compliance": {
                                "evidence": ["全文未见 [UNCITED] 或 TODO_EVIDENCE。"],
                            },
                        },
                        "repair_plan_to_full_score": [
                            {
                                "specific_action": "移除 TODO_EVIDENCE 标记。",
                                "acceptance_test": "不再出现 [UNCITED]。",
                            },
                        ],
                    },
                ],
            },
        )
        == []
    )
    assert check_value(
        {
            "candidate_reports": [
                {
                    "candidate_id": "A",
                    "prose": "正文仍含 [UNCITED]。",
                },
            ],
        },
    )


def test_north_star_gate_item_ledger_can_discuss_sentinels() -> None:
    """The independent gate can mention marker names in item evidence
    without weakening manuscript-prose sentinel coverage."""
    assert (
        check_value(
            {
                "scores": {
                    "A": {
                        "items": [
                            {
                                "id": "no_sentinels",
                                "evidence": ["未见 [UNCITED] / TODO_EVIDENCE。"],
                                "brief_reason": "无内部标记。",
                            },
                        ],
                    },
                },
            },
        )
        == []
    )
    assert check_value({"scores_summary": "正文仍含 [UNCITED]。"})


def test_extra_substrings_extend() -> None:
    violations = check_value(
        {"prose": "phrase X should not appear"},
        extra_substrings=("phrase X",),
    )
    assert violations


def test_pydantic_model_walked() -> None:
    """Pydantic models with .dict() are walked transparently."""

    class _Fake:
        def dict(self) -> dict[str, str]:
            return {"prose": "包含 [UNCITED] 字面"}

    violations = check_value(_Fake())
    assert violations


def test_format_violations_round_trip() -> None:
    v = SentinelViolation(field_path="prose", pattern="[UNCITED]", sample="abc")
    assert "[UNCITED]" in format_violations([v])[0]
    assert "prose" in format_violations([v])[0]


def test_default_pattern_list_is_non_empty() -> None:
    """Sanity-check that the default list does not regress to empty."""
    assert "[UNCITED]" in DEFAULT_FORBIDDEN_SUBSTRINGS
    assert "stub-extracted-text" in DEFAULT_FORBIDDEN_SUBSTRINGS
    assert "本节原始草稿为空" in DEFAULT_FORBIDDEN_SUBSTRINGS
