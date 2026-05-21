"""PR-J7: shared ``research_kernel_for_prompt`` helper that
drafter / ideator / critic / synthesizer / scout all use to project
the opaque ``runs.research_kernel_json`` blob into prompts.
"""

from __future__ import annotations

from autoessay.agents._research_kernel_prompt import (
    KERNEL_INJECTION_GUARD,
    research_kernel_for_prompt,
)


def test_extracts_known_string_fields_only() -> None:
    out = research_kernel_for_prompt(
        {
            "kernel_schema_version": 1,
            "tentative_question": "How did Jiangnan literati circulate?",
            "observed_puzzle": "Cross-field mobility durable under Qing reform.",
            "scope": "1890-1911 Jiangnan",
            "method_preference": "archival + prosopography",
            "theory_preference": "Bourdieu / Polanyi",
            "rogue_unknown_field": "ignored",
        }
    )
    assert out == {
        "tentative_question": "How did Jiangnan literati circulate?",
        "observed_puzzle": "Cross-field mobility durable under Qing reform.",
        "scope": "1890-1911 Jiangnan",
        "method_preference": "archival + prosopography",
        "theory_preference": "Bourdieu / Polanyi",
    }


def test_returns_empty_for_missing_or_empty_or_blank() -> None:
    assert research_kernel_for_prompt(None) == {}
    assert research_kernel_for_prompt({}) == {}
    assert research_kernel_for_prompt({"tentative_question": "  "}) == {}
    assert research_kernel_for_prompt({"tentative_question": None}) == {}


def test_returns_empty_for_non_mapping() -> None:
    assert research_kernel_for_prompt(["not a mapping"]) == {}  # type: ignore[arg-type]
    assert research_kernel_for_prompt("nonsense") == {}  # type: ignore[arg-type]


def test_caps_each_field_at_documented_limit() -> None:
    huge = "x" * 5000
    out = research_kernel_for_prompt(
        {
            "tentative_question": huge,
            "observed_puzzle": huge,
            "scope": huge,
            "method_preference": huge,
            "theory_preference": huge,
        }
    )
    assert len(out["tentative_question"]) == 400
    assert len(out["observed_puzzle"]) == 500
    assert len(out["scope"]) == 200
    assert len(out["method_preference"]) == 200
    assert len(out["theory_preference"]) == 200


def test_kernel_injection_guard_string_is_directive_not_instruction() -> None:
    """``KERNEL_INJECTION_GUARD`` is the standard system-prompt line
    agents append when they include kernel content. It must mention
    the prompt-injection class explicitly so the LLM treats kernel
    text as data, not commands."""
    assert "USER-PROVIDED CONTENT" in KERNEL_INJECTION_GUARD
    assert "ignore previous" in KERNEL_INJECTION_GUARD.lower()
    assert "data" in KERNEL_INJECTION_GUARD.lower()
