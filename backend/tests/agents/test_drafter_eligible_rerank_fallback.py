"""PR-G-Sources Stage 3 Q3 amendment (codex round-1):
``_is_rerank_eligible_fallback`` predicate + extension of
``_eligible_diversity_count`` to admit curator-reranked sources
that fail keyword-overlap but have strong 4-axis LLM scores.

Real-paper round-1 produced ``eligible_count = 3`` out of 24
shortlist sources because the kernel keywords (江南刊本/断代)
didn't substring-match most curator-recommended abstracts (which
were translated into English). The fallback fixes this without
weakening the citation-stuffing safeguard: a source must clear
all four 4-axis gates to be admitted.
"""

from __future__ import annotations

from autoessay.agents.drafter import (
    _eligible_diversity_count,
    _is_rerank_eligible_fallback,
)
from autoessay.clients.common import AccessStatus, NormalizedSource


def _src(
    source_id: str,
    *,
    title: str = "Generic title",
    abstract: str = "",
    rerank_axes: dict[str, float] | None = None,
    rank_score: float = 0.5,
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=["Author"],
        year=2020,
        venue="Venue",
        doi=None,
        url=None,
        pdf_url=None,
        abstract=abstract,
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=rank_score,
        risk_flags=[],
        rerank_axes=rerank_axes,
    )


# ----- _is_rerank_eligible_fallback predicate ---------------------


def test_fallback_false_when_rerank_axes_missing() -> None:
    """Sources without 4-axis rerank (legacy single-axis path or
    rerank_stub mode) should NOT trigger the fallback — we only
    trust 4-axis evidence."""
    src = _src("s1", rerank_axes=None, rank_score=0.9)
    assert _is_rerank_eligible_fallback(src) is False


def test_fallback_false_when_scope_fit_too_low() -> None:
    """scope_fit < 0.55 → reject (codex Q3 threshold)."""
    src = _src("s1", rerank_axes={"scope_fit": 0.50, "relevance": 0.70}, rank_score=0.8)
    assert _is_rerank_eligible_fallback(src) is False


def test_fallback_false_when_relevance_too_low() -> None:
    """relevance < 0.50 → reject."""
    src = _src("s1", rerank_axes={"scope_fit": 0.70, "relevance": 0.45}, rank_score=0.8)
    assert _is_rerank_eligible_fallback(src) is False


def test_fallback_false_when_rank_score_too_low() -> None:
    """rank_score < 0.6 → reject (final blended; weak rank means
    even the reranker was unsure)."""
    src = _src("s1", rerank_axes={"scope_fit": 0.80, "relevance": 0.70}, rank_score=0.55)
    assert _is_rerank_eligible_fallback(src) is False


def test_fallback_true_when_all_four_gates_clear() -> None:
    """All four conditions met → admit even when keyword-overlap
    would mark this source low."""
    src = _src(
        "s1",
        rerank_axes={"scope_fit": 0.65, "relevance": 0.60},
        rank_score=0.75,
    )
    assert _is_rerank_eligible_fallback(src) is True


def test_fallback_true_at_exact_threshold() -> None:
    """Boundary check: scope_fit=0.55, relevance=0.50,
    rank_score=0.6 — exactly at threshold → admit."""
    src = _src(
        "s1",
        rerank_axes={"scope_fit": 0.55, "relevance": 0.50},
        rank_score=0.6,
    )
    assert _is_rerank_eligible_fallback(src) is True


# ----- _eligible_diversity_count integration ---------------------


def test_eligible_count_includes_rerank_fallback_sources() -> None:
    """Realistic scenario from real-paper round-1: kernel keywords
    don't match abstracts, but several sources have strong 4-axis
    rerank. Without fallback, eligible=0; with fallback, those
    rerank-strong sources count."""
    research_kernel = {
        "observed_puzzle": "断代张力",
        "tentative_question": "如何重建年代依据",
        "scope": "晚清江南刊本",
    }
    sources = [
        # Rerank-strong but keyword-overlap "low" (English abstract,
        # no Chinese keyword hit)
        _src(
            "rerank_strong",
            abstract="A study of digital tools in publishing",
            rerank_axes={"scope_fit": 0.65, "relevance": 0.60},
            rank_score=0.75,
        ),
        # Rerank-weak: should NOT be admitted
        _src(
            "rerank_weak",
            abstract="Cooking recipes",
            rerank_axes={"scope_fit": 0.30, "relevance": 0.40},
            rank_score=0.50,
        ),
        # No rerank axes at all (legacy path)
        _src("legacy", abstract="Other unrelated topic", rerank_axes=None),
    ]
    count, ids = _eligible_diversity_count(
        shortlist=sources,
        project_title="江南刊本断代",
        research_kernel=research_kernel,
    )
    assert "rerank_strong" in ids
    assert "rerank_weak" not in ids
    assert "legacy" not in ids
    assert count == 1


def test_eligible_count_keyword_high_relevance_still_works() -> None:
    """A source that DOES match keywords stays eligible without
    needing the fallback path (preserves Stage 2 contract)."""
    research_kernel = {
        "observed_puzzle": "banking crisis",
        "tentative_question": "lender of last resort",
        "scope": "central banking",
    }
    sources = [
        _src(
            "keyword_hit",
            abstract="Banking crisis lender of last resort regulation evidence",
            rerank_axes=None,
        ),
    ]
    count, ids = _eligible_diversity_count(
        shortlist=sources,
        project_title="banking history",
        research_kernel=research_kernel,
    )
    # Keyword-overlap → "high" (3+ hits) → eligible without fallback
    assert "keyword_hit" in ids
    assert count == 1
