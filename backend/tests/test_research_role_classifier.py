"""PR-C1.a: research_role classifier unit tests."""

from __future__ import annotations

from dataclasses import dataclass

from autoessay.agents import research_role_classifier as classifier


@dataclass
class _StubSource:
    source_id: str


def test_default_role_is_secondary_argument() -> None:
    assert classifier.DEFAULT_RESEARCH_ROLE == "secondary_argument"


def test_research_roles_are_four_tier() -> None:
    assert set(classifier.RESEARCH_ROLES) == {
        "primary_source",
        "secondary_argument",
        "theoretical_lens",
        "methodological_reference",
    }


def test_stub_classifies_archive_prefix_as_primary() -> None:
    sources = [
        _StubSource("archive_qing_dynasty_001"),
        _StubSource("archive_field_002"),
    ]
    out = classifier.classify_sources(
        sources, paper_mode="case_analysis", research_kernel={}, stub=True
    )
    assert out["archive_qing_dynasty_001"] == "primary_source"
    assert out["archive_field_002"] == "primary_source"


def test_stub_classifies_primary_prefix_as_primary() -> None:
    sources = [_StubSource("primary_letter_smith_1860")]
    out = classifier.classify_sources(
        sources, paper_mode="case_analysis", research_kernel={}, stub=True
    )
    assert out["primary_letter_smith_1860"] == "primary_source"


def test_stub_classifies_theory_prefix_as_theoretical_lens() -> None:
    sources = [
        _StubSource("theory_bourdieu_field"),
        _StubSource("lens_skinner_intent"),
    ]
    out = classifier.classify_sources(
        sources, paper_mode="case_analysis", research_kernel={}, stub=True
    )
    assert out["theory_bourdieu_field"] == "theoretical_lens"
    assert out["lens_skinner_intent"] == "theoretical_lens"


def test_stub_classifies_method_prefix_as_methodological() -> None:
    sources = [_StubSource("method_archival_research")]
    out = classifier.classify_sources(
        sources, paper_mode="case_analysis", research_kernel={}, stub=True
    )
    assert out["method_archival_research"] == "methodological_reference"


def test_stub_falls_back_to_secondary_argument() -> None:
    sources = [
        _StubSource("openalex_W42"),
        _StubSource("doi_10.1234.5678"),
        _StubSource("user_upload_3"),
    ]
    out = classifier.classify_sources(
        sources, paper_mode="case_analysis", research_kernel={}, stub=True
    )
    for sid in out:
        assert out[sid] == "secondary_argument"


def test_is_valid_role_filters_unknown() -> None:
    assert classifier.is_valid_role("primary_source")
    assert classifier.is_valid_role("secondary_argument")
    assert classifier.is_valid_role("theoretical_lens")
    assert classifier.is_valid_role("methodological_reference")
    assert not classifier.is_valid_role("primary")
    assert not classifier.is_valid_role("")
    assert not classifier.is_valid_role("anything_else")


def test_classify_sources_passes_through_default_when_no_stub_flag() -> None:
    """When stub flag is off and the LLM-driven path is not yet
    implemented, the fallback still populates the column (codex
    AGREE-with-amendments: never leave Japanese UI / role column
    rendering depend on undefined / null values)."""
    sources = [_StubSource("archive_a"), _StubSource("openalex_b")]
    out = classifier.classify_sources(
        sources,
        paper_mode="case_analysis",
        research_kernel={"observed_puzzle": "x"},
        stub=False,
    )
    assert out["archive_a"] == "primary_source"
    assert out["openalex_b"] == "secondary_argument"


def test_classify_sources_is_per_run_context_signature() -> None:
    """Even though the stub ignores paper_mode + kernel, the API
    accepts them so a non-stub LLM implementation drops in
    without callers re-plumbing arguments."""
    sources = [_StubSource("archive_x")]
    out_case = classifier.classify_sources(
        sources, paper_mode="case_analysis", research_kernel={}, stub=True
    )
    out_empirical = classifier.classify_sources(
        sources, paper_mode="empirical", research_kernel={}, stub=True
    )
    assert out_case == out_empirical
