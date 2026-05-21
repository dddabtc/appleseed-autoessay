"""PR-J9b: standalone LLM 4-axis rerank + OpenAlex dual-verify tests.

Covers:
  * 4-axis weighted-sum formula in ``_rank_sources``
  * Hard-penalty cap (scope_fit < 0.30 OR retain_decision = False)
  * 4-axis blend with legacy formula (RERANK_BLEND / LEGACY_BLEND)
  * Persistence of ``rerank_axes`` / ``rerank_rationale`` on
    ``NormalizedSource``
  * ``CuratorRankedSource`` 4-axis schema validation
  * ``_scores_from_curator_ranking`` 4-axis extraction
  * ``ScoreResult`` field defaults + ``rerank_active`` flag
  * Stub flag (``AUTOESSAY_CURATOR_RERANK_STUB``) drops 4-axis fields
  * ``_normalized_title_fuzzy`` handles long-subtitle monographs
  * ``verify_canonical_dual_source`` falls back from Crossref to
    OpenAlex when Crossref drops a monograph
  * Combined drop-warning when both verifiers fail
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from autoessay.agents._canonical_mining import (
    _combine_drop_warnings,
    _normalize_title_for_fuzzy,
    _normalized_title_fuzzy,
    verify_canonical_dual_source,
)
from autoessay.agents._canonical_sources_schema import CanonicalWork
from autoessay.agents.curator import (
    HARD_PENALTY_CAP,
    RERANK_AXIS_WEIGHTS,
    RERANK_BLEND,
    CuratorRankedSource,
    CuratorRanking,
    ScoreResult,
    _rank_sources,
    _scores_from_curator_ranking,
)
from autoessay.clients.common import AccessStatus, NormalizedSource

# ----------------------------------------------------------------------
# CuratorRankedSource — 4-axis schema
# ----------------------------------------------------------------------


def _valid_axis_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "source_id": "openalex:W123",
        "scope_fit": 0.85,
        "relevance": 0.80,
        "impact": 0.70,
        "frontier_currency": 0.20,
        "rationale": "Foundational work on Korean late industrialization",
        "retain_decision": True,
        "risk_flags": [],
    }
    payload.update(overrides)
    return payload


def test_curator_ranked_source_accepts_4axis_payload() -> None:
    parsed = CuratorRankedSource.parse_obj(_valid_axis_payload())
    assert parsed.scope_fit == 0.85
    assert parsed.relevance == 0.80
    assert parsed.impact == 0.70
    assert parsed.frontier_currency == 0.20
    assert parsed.retain_decision is True


def test_curator_ranked_source_rationale_truncates_to_200_chars() -> None:
    parsed = CuratorRankedSource.parse_obj(_valid_axis_payload(rationale="x" * 500))
    assert len(parsed.rationale) == 200


def test_curator_ranked_source_rejects_axis_out_of_unit_interval() -> None:
    with pytest.raises(ValidationError):
        CuratorRankedSource.parse_obj(_valid_axis_payload(scope_fit=1.5))
    with pytest.raises(ValidationError):
        CuratorRankedSource.parse_obj(_valid_axis_payload(impact=-0.1))


def test_curator_ranked_source_legacy_payload_synthesizes_axes() -> None:
    legacy_payload = {
        "source_id": "openalex:W999",
        "rank_score": 0.7,
        "relevance": 0.8,
        "recency": 0.4,
        "venue_authority": 0.5,
        "diversity_bonus": 0.3,
        "retain_decision": True,
        "risk_flags": [],
    }
    parsed = CuratorRankedSource.parse_obj(legacy_payload)
    assert parsed.relevance == 0.8
    # Legacy fixtures synthesize all axes from `relevance` so the
    # legacy single-axis stub path keeps parsing.
    assert parsed.scope_fit == 0.8
    assert parsed.impact == 0.8
    assert parsed.frontier_currency == 0.8


def test_curator_ranking_rejects_empty_root() -> None:
    with pytest.raises(ValidationError):
        CuratorRanking.parse_obj([])


# ----------------------------------------------------------------------
# _scores_from_curator_ranking — 4-axis extraction
# ----------------------------------------------------------------------


def test_scores_from_curator_ranking_extracts_4axes_and_retain() -> None:
    parsed = CuratorRanking.parse_obj(
        [
            _valid_axis_payload(source_id="s1", scope_fit=0.9),
            _valid_axis_payload(source_id="s2", scope_fit=0.2, retain_decision=False),
        ]
    )
    result = _scores_from_curator_ranking(parsed)
    assert set(result.relevance_scores.keys()) == {"s1", "s2"}
    assert result.rerank_axes["s1"]["scope_fit"] == 0.9
    assert result.rerank_axes["s2"]["scope_fit"] == 0.2
    assert result.rerank_retain["s1"] is True
    assert result.rerank_retain["s2"] is False
    assert result.rerank_rationales["s1"]


def test_scores_from_curator_ranking_returns_empty_on_garbage() -> None:
    result = _scores_from_curator_ranking("not a ranking")
    assert result.relevance_scores == {}
    assert result.rerank_axes == {}
    assert result.rerank_retain == {}


# ----------------------------------------------------------------------
# _rank_sources — 4-axis blend formula + hard penalty
# ----------------------------------------------------------------------


def _src(source_id: str, *, year: int = 2020, source_client: str = "crossref") -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Title {source_id}",
        authors=["Author A"],
        year=year,
        venue="Venue X",
        doi=None,
        url=None,
        pdf_url=None,
        abstract="Abstract",
        source_client=source_client,
        access_status=AccessStatus.OPEN,
        license=None,
        risk_flags=[],
    )


def test_rank_sources_applies_4axis_blend_when_axes_present() -> None:
    sources = [_src("good"), _src("ok")]
    relevance_scores = {"good": 0.8, "ok": 0.6}
    rerank_axes = {
        "good": {"scope_fit": 0.95, "relevance": 0.9, "impact": 0.85, "frontier_currency": 0.4},
        "ok": {"scope_fit": 0.6, "relevance": 0.6, "impact": 0.5, "frontier_currency": 0.4},
    }
    rerank_retain = {"good": True, "ok": True}
    domain_data = {"id": "demo"}
    ranked = _rank_sources(
        sources,
        domain_data,
        relevance_scores,
        fallback_recency_only=False,
        rerank_axes=rerank_axes,
        rerank_retain=rerank_retain,
    )
    by_id = {item.source_id: item for item in ranked}
    # Final = 0.85*rerank + 0.15*legacy. Both > 0.30 so no hard penalty.
    expected_good_rerank = sum(
        RERANK_AXIS_WEIGHTS[k] * rerank_axes["good"][k] for k in RERANK_AXIS_WEIGHTS
    )
    assert by_id["good"].rank_score > by_id["ok"].rank_score
    assert by_id["good"].rank_score > expected_good_rerank * RERANK_BLEND
    assert by_id["good"].rerank_axes is not None
    assert by_id["good"].rerank_axes["scope_fit"] == 0.95


def test_rank_sources_keeps_kernel_aligned_fulltext_inside_deep_dive() -> None:
    sterling = _src("sterling", source_client="openalex").copy(
        update={
            "title": "Zombie International Currency: The Pound Sterling 1945-1971",
            "abstract": "Sterling reserve role in the Bretton Woods era.",
            "pdf_url": "https://example.test/sterling.pdf",
        },
    )
    dollar_gold = _src("dollar_gold", year=2025).copy(
        update={
            "title": "The End of Dollar Convertibility. 15 August 1971",
            "abstract": "Nixon ended dollar convertibility into gold and the Bretton Woods system.",
            "pdf_url": "https://example.test/dollar-gold.pdf",
        },
    )
    ranked = _rank_sources(
        [sterling, dollar_gold],
        {"id": "financial_history"},
        {"sterling": 0.9, "dollar_gold": 0.4},
        fallback_recency_only=False,
        rerank_axes={
            "sterling": {
                "scope_fit": 0.91,
                "relevance": 0.93,
                "impact": 0.79,
                "frontier_currency": 0.86,
            },
            "dollar_gold": {
                "scope_fit": 0.12,
                "relevance": 0.43,
                "impact": 0.41,
                "frontier_currency": 0.78,
            },
        },
        rerank_retain={"sterling": True, "dollar_gold": True},
        research_kernel={
            "kernel_schema_version": 1,
            "tentative_question": (
                "布雷顿森林金本位承诺的实际约束力应如何依据美元—黄金兑换证据重估？"
            ),
            "scope": (
                "1960-1971 年美元—黄金兑换通道、IMF 备忘录、美联储会议纪要"
                "与 London Gold Pool 记录。"
            ),
        },
    )

    assert ranked[0].source_id == "dollar_gold"
    assert ranked[0].rank_score >= 0.68
    assert ranked[1].source_id == "sterling"
    assert ranked[1].rank_score <= 0.64


def test_rank_sources_hard_penalty_caps_low_scope_fit() -> None:
    """Scope_fit < 0.30 forces final rank_score ≤ HARD_PENALTY_CAP,
    even when impact + frontier_currency are sky-high. This is the
    RCEP-2025-vs-Amsden-1989 protection (codex round-1 A3 + A4).
    """
    sources = [_src("rcep"), _src("amsden", year=1989)]
    relevance_scores = {"rcep": 0.9, "amsden": 0.6}
    rerank_axes = {
        # RCEP has high impact + frontier but is OUT of scope.
        "rcep": {"scope_fit": 0.20, "relevance": 0.95, "impact": 0.9, "frontier_currency": 0.95},
        # Amsden is foundational + in scope; old canon scores 0 frontier.
        "amsden": {"scope_fit": 0.95, "relevance": 0.85, "impact": 0.95, "frontier_currency": 0.05},
    }
    rerank_retain = {"rcep": True, "amsden": True}
    ranked = _rank_sources(
        sources,
        {"id": "economic_history"},
        relevance_scores,
        fallback_recency_only=False,
        rerank_axes=rerank_axes,
        rerank_retain=rerank_retain,
    )
    by_id = {item.source_id: item for item in ranked}
    assert by_id["rcep"].rank_score <= HARD_PENALTY_CAP + 1e-6
    assert by_id["amsden"].rank_score > HARD_PENALTY_CAP
    # Amsden must rank above RCEP.
    assert ranked[0].source_id == "amsden"
    assert ranked[1].source_id == "rcep"


def test_rank_sources_hard_penalty_caps_retain_decision_false() -> None:
    """retain_decision = False also forces the hard-penalty cap, even
    when scope_fit ≥ 0.30 (e.g., LLM flagged a content-tangential
    source that happens to mention the period)."""
    sources = [_src("borderline")]
    rerank_axes = {
        "borderline": {
            "scope_fit": 0.50,
            "relevance": 0.60,
            "impact": 0.60,
            "frontier_currency": 0.60,
        }
    }
    rerank_retain = {"borderline": False}
    ranked = _rank_sources(
        sources,
        {"id": "demo"},
        {"borderline": 0.6},
        fallback_recency_only=False,
        rerank_axes=rerank_axes,
        rerank_retain=rerank_retain,
    )
    assert ranked[0].rank_score <= HARD_PENALTY_CAP + 1e-6


def test_rank_sources_falls_back_to_legacy_when_axes_missing() -> None:
    """When rerank_axes is empty (rerank_stub on or 4-axis failed),
    `_rank_sources` returns the legacy formula score (no blend)."""
    sources = [_src("a"), _src("b")]
    ranked_with_axes = _rank_sources(
        sources,
        {"id": "demo"},
        {"a": 0.9, "b": 0.5},
        fallback_recency_only=False,
        rerank_axes={},
    )
    by_id = {item.source_id: item for item in ranked_with_axes}
    # Legacy formula: 0.65*rel + 0.20*rec + 0.10*venue + 0.05*div
    # rerank_axes attribute should be None on each source.
    assert by_id["a"].rerank_axes is None
    assert by_id["b"].rerank_axes is None
    assert by_id["a"].rank_score > by_id["b"].rank_score


def test_rank_sources_recency_only_fallback_short_circuits() -> None:
    sources = [_src("recent", year=2023), _src("old", year=2000)]
    ranked = _rank_sources(
        sources,
        {"id": "demo"},
        {},
        fallback_recency_only=True,
    )
    assert ranked[0].source_id == "recent"


def test_score_result_has_rerank_field_defaults() -> None:
    sr = ScoreResult(relevance_scores={}, warnings=[], fallback_recency_only=False)
    assert sr.rerank_axes == {}
    assert sr.rerank_rationales == {}
    assert sr.rerank_retain == {}
    assert sr.rerank_active is False


# ----------------------------------------------------------------------
# _normalized_title_fuzzy — long-subtitle handling
# ----------------------------------------------------------------------


def test_normalize_title_strips_punctuation_and_lowercases() -> None:
    assert _normalize_title_for_fuzzy("Asia's Next Giant!") == "asia s next giant"
    assert _normalize_title_for_fuzzy("Foo: Bar — Baz") == "foo bar baz"


def test_normalized_title_fuzzy_high_for_subtitle_extension() -> None:
    """A long-subtitle monograph (``"Asia's Next Giant: South Korea
    and Late Industrialization"``) must clear ≥0.90 fuzzy against a
    bare ``"Asia's Next Giant"`` listing — codex round-1 A5: bare
    SequenceMatcher would drop too far on the subtitle suffix."""
    score = _normalized_title_fuzzy(
        "Asia's Next Giant: South Korea and Late Industrialization",
        "Asia's Next Giant",
    )
    assert score >= 0.90


def test_normalized_title_fuzzy_low_for_unrelated() -> None:
    assert (
        _normalized_title_fuzzy(
            "Asia's Next Giant",
            "Something Entirely Different",
        )
        < 0.50
    )


# ----------------------------------------------------------------------
# verify_canonical_dual_source — Crossref → OpenAlex fallback
# ----------------------------------------------------------------------


def _work(**overrides: Any) -> CanonicalWork:
    payload = {
        "title": "Asia's Next Giant: South Korea and Late Industrialization",
        "first_author": "Alice Amsden",
        "year": 1989,
        "doi": None,
        "rationale": "Foundational state-led-growth Korean monograph " * 3,
    }
    payload.update(overrides)
    return CanonicalWork.parse_obj(payload)


def _candidate(**overrides: Any) -> NormalizedSource:
    payload = {
        "source_id": "openalex:W42",
        "title": "Asia's Next Giant",
        "authors": ["Alice Amsden"],
        "year": 1989,
        "venue": "Oxford UP",
        "doi": None,
        "url": None,
        "pdf_url": None,
        "abstract": "Korean late industrialization classic",
        "source_client": "openalex",
        "access_status": AccessStatus.METADATA_ONLY,
        "license": None,
        "risk_flags": [],
    }
    payload.update(overrides)
    return NormalizedSource(**payload)


class _FakeAsyncClient:
    """Drop-in for either CrossrefClient or OpenAlexClient. Returns a
    fixed list of candidates regardless of query (tests pin behavior
    by stubbing the search method)."""

    def __init__(self, candidates: list[NormalizedSource] | None = None) -> None:
        self._candidates = candidates or []
        self.search_calls: list[str] = []

    async def search(self, *, query: str, year_window: Any, limit: int) -> list[NormalizedSource]:
        self.search_calls.append(query)
        return list(self._candidates)


@pytest.mark.asyncio
async def test_dual_verify_uses_crossref_when_available() -> None:
    crossref = _FakeAsyncClient([_candidate(source_client="crossref")])
    openalex = _FakeAsyncClient([_candidate(source_client="openalex")])
    work = _work()
    verified, warnings = await verify_canonical_dual_source(
        [("consensus", "rationale", work)],
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=openalex,  # type: ignore[arg-type]
    )
    assert len(verified) == 1
    assert warnings == []
    assert verified[0].verified_by == "crossref"
    assert verified[0].provenance == "llm_canon"
    # OpenAlex SHOULD NOT have been queried (Crossref short-circuits).
    assert openalex.search_calls == []


@pytest.mark.asyncio
async def test_dual_verify_enriches_crossref_metadata_with_openalex_abstract() -> None:
    crossref = _FakeAsyncClient(
        [
            _candidate(
                source_client="crossref",
                source_id="crossref:10.1/example",
                doi="10.1/example",
                abstract=None,
                pdf_url=None,
                access_status=AccessStatus.METADATA_ONLY,
            )
        ]
    )
    openalex = _FakeAsyncClient(
        [
            _candidate(
                source_client="openalex",
                source_id="https://openalex.org/W1",
                doi="10.1/example",
                abstract="OpenAlex inverted-index abstract text.",
                pdf_url="https://example.test/paper.pdf",
                access_status=AccessStatus.OPEN,
            )
        ]
    )

    verified, warnings = await verify_canonical_dual_source(
        [("consensus", "rationale", _work(doi="10.1/example"))],
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=openalex,  # type: ignore[arg-type]
    )

    assert warnings == []
    assert len(verified) == 1
    assert verified[0].source_id == "crossref:10.1/example"
    assert verified[0].source_client == "crossref"
    assert verified[0].verified_by == "crossref"
    assert verified[0].abstract == "OpenAlex inverted-index abstract text."
    assert verified[0].pdf_url == "https://example.test/paper.pdf"
    assert openalex.search_calls


@pytest.mark.asyncio
async def test_dual_verify_falls_back_to_openalex_when_crossref_drops() -> None:
    """The driving J9b scenario: Crossref doesn't index the monograph
    (Amsden 1989 / Wade 1990 / Cumings 1981). OpenAlex does. Dual-
    verify should return the OpenAlex result with verified_by="openalex"."""
    crossref = _FakeAsyncClient([])  # no Crossref hits
    openalex = _FakeAsyncClient([_candidate(source_client="openalex")])
    work = _work()
    verified, warnings = await verify_canonical_dual_source(
        [("consensus", "rationale", work)],
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=openalex,  # type: ignore[arg-type]
    )
    assert len(verified) == 1
    assert warnings == []
    assert verified[0].verified_by == "openalex"
    assert verified[0].source_client == "openalex"
    # Both backends called.
    assert len(crossref.search_calls) == 1
    assert len(openalex.search_calls) == 1


@pytest.mark.asyncio
async def test_dual_verify_combined_warning_when_both_legs_drop() -> None:
    crossref = _FakeAsyncClient([])
    openalex = _FakeAsyncClient([])
    verified, warnings = await verify_canonical_dual_source(
        [("consensus", "rationale", _work())],
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=openalex,  # type: ignore[arg-type]
    )
    assert verified == []
    assert len(warnings) == 1
    drop = warnings[0]
    assert drop["reason"] == "dual_verify_below_threshold"
    assert drop["crossref_reason"] == "no_crossref_match"
    assert drop["openalex_reason"] == "no_openalex_match"


@pytest.mark.asyncio
async def test_dual_verify_crossref_only_mode_preserves_v1_semantics() -> None:
    """Backward-compat: ``openalex_client=None`` keeps J9 v1 Crossref-
    only behavior — no OpenAlex call, no combined warning."""
    crossref = _FakeAsyncClient([])
    verified, warnings = await verify_canonical_dual_source(
        [("consensus", "rationale", _work())],
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=None,
    )
    assert verified == []
    assert len(warnings) == 1
    drop = warnings[0]
    assert drop["reason"] == "no_crossref_match"
    assert "openalex_reason" not in drop


def test_combine_drop_warnings_picks_max_score() -> None:
    cr_drop = {
        "work_title": "T",
        "first_author": "A",
        "year": 1989,
        "reason": "crossref_below_fuzzy_threshold",
        "best_match_score": 0.78,
    }
    oa_drop = {
        "work_title": "T",
        "first_author": "A",
        "year": 1989,
        "reason": "openalex_below_fuzzy_threshold",
        "best_match_score": 0.85,
    }
    combined = _combine_drop_warnings(cr_drop, oa_drop)
    assert combined["best_match_score"] == 0.85
    assert combined["crossref_reason"] == "crossref_below_fuzzy_threshold"
    assert combined["openalex_reason"] == "openalex_below_fuzzy_threshold"
