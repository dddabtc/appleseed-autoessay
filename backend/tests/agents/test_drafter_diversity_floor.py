"""PR-G-Sources Stage 2 (codex round-2 amendment Q2):
diversity-floor diagnostic for drafter.

Validates the post-LLM check that the cited_source_ids count
meets ``min(Settings.cited_sources_diversity_floor,
eligible_source_count)``. Below floor → diagnostic dict (caller
emits ``cited_sources_below_floor`` event); at or above → None.
"""

from __future__ import annotations

from autoessay.agents.drafter import (
    DraftedSection,
    _check_diversity_floor,
    _eligible_diversity_count,
)
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings


def _source(
    source_id: str,
    title: str,
    abstract: str = "",
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=["Author"],
        year=2020,
        venue="Venue",
        doi=None,
        url="https://example.test/" + source_id,
        pdf_url=None,
        abstract=abstract,
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=0.5,
        risk_flags=[],
    )


def _section(
    section_id: str,
    cited_source_ids: list[str],
) -> DraftedSection:
    """Build a stub DraftedSection whose claim_map has one claim
    citing each source in ``cited_source_ids``."""
    return DraftedSection(
        section_id=section_id,
        title=f"Title {section_id}",
        prose="prose",
        claim_map=[
            {
                "claim_id": f"{section_id}_c{idx}",
                "paragraph_id": f"{section_id}_p1",
                "claim_text": f"claim {idx}",
                "source_ids": [sid],
            }
            for idx, sid in enumerate(cited_source_ids)
        ],
        failed=False,
        warnings=[],
        word_count=500,
        target_words=1500,
    )


# ----- _eligible_diversity_count ----------------------------------


def test_eligible_count_zero_when_keywords_empty() -> None:
    """research_kernel + project_title yielding empty keyword set →
    eligible_count = 0 (every source counts as 'low' relevance —
    consistent with ``_score_source_topic_relevance`` semantics)."""
    sources = [_source("s1", "Some Paper")]
    count, ids = _eligible_diversity_count(
        shortlist=sources,
        project_title="",
        research_kernel=None,
    )
    assert count == 0
    assert ids == set()


def test_eligible_count_includes_only_non_low_sources() -> None:
    """Only sources whose ``topic_relevance`` ≠ ``"low"`` count
    toward the eligible pool."""
    get_settings.cache_clear()
    sources = [
        _source(
            "s1",
            "Banking crises in nineteenth century",
            abstract="Banking, crises, lender, last, resort, century",
        ),
        _source("s2", "Unrelated paper", abstract="cooking recipes"),
        _source("s3", "Crisis lending in banking", abstract="Banking crises lender regulation"),
    ]
    # Project title + research_kernel produce keywords that hit s1
    # and s3 (banking, crisis, etc.) but not s2 (cooking).
    research_kernel = {
        "observed_puzzle": "banking crises and lender of last resort",
        "tentative_question": "How did banking regulation evolve?",
        "scope": "nineteenth century banking",
    }
    count, ids = _eligible_diversity_count(
        shortlist=sources,
        project_title="banking crises",
        research_kernel=research_kernel,
    )
    assert "s2" not in ids  # cooking is unrelated → low
    # s1 / s3 both have multiple banking-related keyword hits.
    assert count >= 1
    assert count == len(ids)


# ----- _check_diversity_floor -------------------------------------


def test_check_returns_none_when_floor_zero(monkeypatch) -> None:
    """``Settings.cited_sources_diversity_floor=0`` is the operator
    opt-out: the helper short-circuits to None without running any
    eligibility computation."""
    monkeypatch.setenv("AUTOESSAY_CITED_SOURCES_DIVERSITY_FLOOR", "0")
    get_settings.cache_clear()
    sections = [_section("intro", [])]
    result = _check_diversity_floor(
        drafted_sections=sections,
        shortlist=[],
        project_title="any",
        research_kernel={"observed_puzzle": "x"},
        draft_version="v001",
    )
    assert result is None


def test_check_returns_none_when_eligible_pool_empty(monkeypatch) -> None:
    """If ``eligible_count == 0`` (e.g. shortlist empty or all
    sources low-relevance) ``effective_floor`` collapses to 0 and
    no diagnostic fires — we'd be asking for citations from a pool
    that doesn't exist."""
    monkeypatch.setenv("AUTOESSAY_CITED_SOURCES_DIVERSITY_FLOOR", "12")
    get_settings.cache_clear()
    sections = [_section("intro", [])]
    result = _check_diversity_floor(
        drafted_sections=sections,
        shortlist=[],  # no eligible sources
        project_title="banking",
        research_kernel={"observed_puzzle": "banking"},
        draft_version="v001",
    )
    assert result is None


def test_check_returns_diagnostic_when_below_floor(monkeypatch) -> None:
    """Drafter cited only 2 of 5 eligible sources, configured floor
    is 12 → effective_floor = min(12, 5) = 5; cited_count 2 < 5 →
    diagnostic returned."""
    monkeypatch.setenv("AUTOESSAY_CITED_SOURCES_DIVERSITY_FLOOR", "12")
    get_settings.cache_clear()
    keywords_kernel = {
        "observed_puzzle": "banking crises lender",
        "tentative_question": "How banking crisis lender",
        "scope": "banking crisis lender regulation",
    }
    # Build 5 sources all with banking/crisis/lender hits.
    shortlist = [
        _source(
            f"s{i}",
            f"Banking crisis lender paper {i}",
            abstract="Banking crises lender regulation discussion",
        )
        for i in range(1, 6)
    ]
    # Drafter only cited s1 and s2.
    sections = [
        _section("intro", ["s1"]),
        _section("body", ["s2"]),
    ]
    result = _check_diversity_floor(
        drafted_sections=sections,
        shortlist=shortlist,
        project_title="banking crises",
        research_kernel=keywords_kernel,
        draft_version="v001",
    )
    assert result is not None
    assert result["cited_count"] == 2
    assert result["cited_eligible_count"] == 2  # both cited are eligible
    assert result["eligible_count"] == 5
    assert result["configured_floor"] == 12
    assert result["effective_floor"] == 5  # min(12, 5)
    assert result["shortlist_count"] == 5


def test_check_returns_none_when_at_or_above_floor(monkeypatch) -> None:
    """Drafter met the floor → no diagnostic fires."""
    monkeypatch.setenv("AUTOESSAY_CITED_SOURCES_DIVERSITY_FLOOR", "3")
    get_settings.cache_clear()
    keywords_kernel = {
        "observed_puzzle": "banking crises lender",
        "tentative_question": "How banking crisis lender",
        "scope": "banking crisis lender regulation",
    }
    shortlist = [
        _source(
            f"s{i}",
            f"Banking crisis lender paper {i}",
            abstract="Banking crises lender regulation discussion",
        )
        for i in range(1, 6)
    ]
    sections = [
        _section("intro", ["s1"]),
        _section("body", ["s2", "s3", "s4"]),
    ]
    result = _check_diversity_floor(
        drafted_sections=sections,
        shortlist=shortlist,
        project_title="banking crises",
        research_kernel=keywords_kernel,
        draft_version="v001",
    )
    assert result is None  # 4 cited >= floor 3


def test_check_excludes_uncited_marker_from_count(monkeypatch) -> None:
    """``[UNCITED]`` placeholder must not inflate the cited_count
    above the floor (fixes the regression where stub fallback
    sections used [UNCITED] and incorrectly satisfied the floor)."""
    monkeypatch.setenv("AUTOESSAY_CITED_SOURCES_DIVERSITY_FLOOR", "5")
    get_settings.cache_clear()
    keywords_kernel = {
        "observed_puzzle": "banking crises lender",
        "tentative_question": "How banking crisis lender",
        "scope": "banking crisis lender regulation",
    }
    shortlist = [
        _source(
            f"s{i}",
            f"Banking crisis lender paper {i}",
            abstract="Banking crises lender regulation discussion",
        )
        for i in range(1, 6)
    ]
    sections = [
        DraftedSection(
            section_id="intro",
            title="Intro",
            prose="prose",
            claim_map=[
                {
                    "claim_id": "c1",
                    "paragraph_id": "p1",
                    "claim_text": "uncited claim",
                    "source_ids": ["[UNCITED]"],
                },
                {
                    "claim_id": "c2",
                    "paragraph_id": "p2",
                    "claim_text": "real claim",
                    "source_ids": ["s1"],
                },
            ],
            failed=False,
            warnings=[],
            word_count=200,
            target_words=1500,
        ),
    ]
    result = _check_diversity_floor(
        drafted_sections=sections,
        shortlist=shortlist,
        project_title="banking",
        research_kernel=keywords_kernel,
        draft_version="v001",
    )
    assert result is not None
    assert result["cited_count"] == 1  # only s1, not [UNCITED]
