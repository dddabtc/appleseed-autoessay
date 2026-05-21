"""PR-C2c step 2: tests for the strict Pydantic schema +
``framework_lens_integrity`` structural gate.

Covers (matrix from HANDOFF §11.7.1 step 2 + codex round-1 amendments
C / D / E / I):

- ``extra="forbid"`` rejects unknown fields at model + nested level
- whitespace normalize + blank reject on every string field
- min/max bounds (lens_name 2..120, applicability 10..600,
  key_concepts 1..8, signals 0..8)
- banned ``lens_name`` literal list (case-insensitive, normalized)
- ``lens_name == source_id`` rejection
- pure-non-alpha lens_name rejection
- source_id must be in eligible (the actually-prompt-truncated set)
- duplicate lens_name within one payload
- paper_mode=theory_article + eligible inputs + empty signals →
  reject (codex amendment E)
- paper_mode=theory_article + zero eligible inputs + empty signals →
  accept (the upstream FAILED_FIXABLE guidance handles that case)
- paper_mode=case_analysis + empty signals → accept
- paper_mode=case_analysis + 1 valid signal → accept
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from autoessay.agents._framework_lens_schema import (
    LENS_NAME_BANNED_LITERAL,
    FrameworkLensSignalsOutput,
    LensSignalOutput,
    framework_lens_integrity,
)


def _valid_signal(**overrides: object) -> dict[str, object]:
    base = {
        "lens_name": "Bourdieu: habitus",
        "key_concepts": ["habitus", "field"],
        "source_id": "openalex_W123",
        "applicability_to_kernel": (
            "Habitus explains how class-bound dispositions reproduce "
            "scholarly preferences across generations of Jiangnan literati."
        ),
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------
# Pydantic schema enforcement (extra=forbid + bounds + normalize)
# ----------------------------------------------------------------------


def test_valid_signal_round_trips() -> None:
    sig = LensSignalOutput.parse_obj(_valid_signal())
    assert sig.lens_name == "Bourdieu: habitus"
    assert sig.key_concepts == ["habitus", "field"]
    assert sig.source_id == "openalex_W123"
    assert sig.applicability_to_kernel.startswith("Habitus")


def test_extra_field_rejected_on_signal() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(rogue_field="x"))


def test_extra_field_rejected_on_payload() -> None:
    with pytest.raises(ValidationError):
        FrameworkLensSignalsOutput.parse_obj(
            {"signals": [_valid_signal()], "extra_top_level": True}
        )


def test_lens_name_min_length() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(lens_name="x"))


def test_lens_name_max_length() -> None:
    too_long = "a" * 121
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(lens_name=too_long))


def test_applicability_min_length() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(applicability_to_kernel="too short"))


def test_applicability_max_length() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(applicability_to_kernel="x" * 601))


def test_key_concepts_min_items() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(key_concepts=[]))


def test_key_concepts_max_items() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(key_concepts=["c"] * 9))


def test_signals_max_items() -> None:
    with pytest.raises(ValidationError):
        FrameworkLensSignalsOutput.parse_obj({"signals": [_valid_signal()] * 9})


def test_signals_empty_accepted_at_schema_level() -> None:
    # Pydantic schema allows 0 signals; ``framework_lens_integrity``
    # is what enforces the paper_mode-aware ≥1 rule.
    payload = FrameworkLensSignalsOutput.parse_obj({"signals": []})
    assert payload.signals == []


def test_lens_name_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(lens_name="   "))


def test_source_id_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(source_id="   "))


def test_applicability_blank_rejected() -> None:
    # 10 spaces still violates min_length=10 because validator runs
    # AFTER min_length, but the more important pin is whitespace
    # normalize: a 600-char whitespace string should be rejected.
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(applicability_to_kernel=" " * 60))


def test_key_concept_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        LensSignalOutput.parse_obj(_valid_signal(key_concepts=["   "]))


def test_whitespace_normalized_on_lens_name() -> None:
    sig = LensSignalOutput.parse_obj(_valid_signal(lens_name="Bourdieu:   habitus  \n  "))
    assert sig.lens_name == "Bourdieu: habitus"


def test_whitespace_normalized_on_source_id() -> None:
    sig = LensSignalOutput.parse_obj(_valid_signal(source_id="  openalex_W123  "))
    assert sig.source_id == "openalex_W123"


# ----------------------------------------------------------------------
# framework_lens_integrity structural gate
# ----------------------------------------------------------------------


def _payload(*signals: dict[str, object]) -> FrameworkLensSignalsOutput:
    return FrameworkLensSignalsOutput.parse_obj({"signals": list(signals)})


def test_integrity_accepts_clean_payload() -> None:
    errors = framework_lens_integrity(
        _payload(_valid_signal()),
        eligible_source_ids=["openalex_W123"],
        paper_mode="case_analysis",
        eligible_lens_input_present=True,
    )
    assert errors == []


def test_integrity_rejects_banned_lens_name() -> None:
    for bad in ("Lens 1", "Default Lens", "GENERIC theory", "  unnamed   theory  "):
        errors = framework_lens_integrity(
            _payload(_valid_signal(lens_name=bad)),
            eligible_source_ids=["openalex_W123"],
            paper_mode="case_analysis",
            eligible_lens_input_present=True,
        )
        assert any("banned" in e for e in errors), (bad, errors)


def test_integrity_extended_banned_phrases_round1_amendment_d() -> None:
    """Codex round-1 amendment D additions must all be banned."""
    new_terms = [
        "untitled lens",
        "untitled theory",
        "unknown framework",
        "placeholder lens",
        "sample lens",
        "example lens",
        "framework 1",
        "general theory",
        "general framework",
    ]
    for term in new_terms:
        assert term in LENS_NAME_BANNED_LITERAL, term


def test_integrity_rejects_lens_name_equal_to_source_id() -> None:
    errors = framework_lens_integrity(
        _payload(_valid_signal(lens_name="openalex_W123", source_id="openalex_W123")),
        eligible_source_ids=["openalex_W123"],
        paper_mode="case_analysis",
        eligible_lens_input_present=True,
    )
    assert any("differ from source_id" in e for e in errors), errors


def test_integrity_rejects_pure_non_alpha_lens_name() -> None:
    # Pydantic schema requires min_length=2, so "12" is structurally
    # valid but the integrity gate flags it.
    errors = framework_lens_integrity(
        _payload(_valid_signal(lens_name="123 456")),
        eligible_source_ids=["openalex_W123"],
        paper_mode="case_analysis",
        eligible_lens_input_present=True,
    )
    assert any("alphabetic" in e for e in errors), errors


def test_integrity_rejects_unknown_source_id() -> None:
    errors = framework_lens_integrity(
        _payload(_valid_signal(source_id="rogue_W999")),
        eligible_source_ids=["openalex_W123"],
        paper_mode="case_analysis",
        eligible_lens_input_present=True,
    )
    assert any("not in eligible" in e for e in errors), errors


def test_integrity_rejects_dup_lens_name_in_one_payload() -> None:
    errors = framework_lens_integrity(
        _payload(
            _valid_signal(),
            _valid_signal(source_id="openalex_W456"),
        ),
        eligible_source_ids=["openalex_W123", "openalex_W456"],
        paper_mode="case_analysis",
        eligible_lens_input_present=True,
    )
    assert any("duplicates" in e for e in errors), errors


def test_integrity_rejects_empty_signals_when_theory_article_with_inputs() -> None:
    """Codex round-1 amendment E: theory_article + non-empty eligible
    inputs MUST produce ≥1 signal."""
    errors = framework_lens_integrity(
        _payload(),
        eligible_source_ids=["openalex_W123"],
        paper_mode="theory_article",
        eligible_lens_input_present=True,
    )
    assert any("must produce at least one signal" in e for e in errors), errors


def test_integrity_accepts_empty_signals_when_theory_article_no_inputs() -> None:
    """When there are zero eligible lens inputs, the upstream agent
    runner has already routed to FAILED_FIXABLE before LLM. The
    integrity gate must NOT also reject — it would double-fail."""
    errors = framework_lens_integrity(
        _payload(),
        eligible_source_ids=[],
        paper_mode="theory_article",
        eligible_lens_input_present=False,
    )
    assert errors == []


def test_integrity_accepts_empty_signals_when_case_analysis() -> None:
    """case_analysis allows 0 signals (lens is optional)."""
    errors = framework_lens_integrity(
        _payload(),
        eligible_source_ids=["openalex_W123"],
        paper_mode="case_analysis",
        eligible_lens_input_present=True,
    )
    assert errors == []


def test_integrity_normalizes_source_id_whitespace_for_eligibility() -> None:
    """Codex round-1 amendment D: source_id eligibility check must
    normalize whitespace + casefold both sides before compare."""
    errors = framework_lens_integrity(
        _payload(_valid_signal(source_id="OPENALEX_w123")),
        eligible_source_ids=["openalex_W123"],
        paper_mode="case_analysis",
        eligible_lens_input_present=True,
    )
    assert errors == [], errors
