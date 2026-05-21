"""PR-J9 v1: scout LLM canonical / frontier mining + Crossref dual-
verify + merge into skim_candidates with provenance.

Pins the new behaviors:
  * Pydantic schemas accept the codex-amended shape (CanonicalWork
    with monograph-friendly DOI + journal_or_publisher; consensus 0-5
    + disagreement 0-3 + frontier 0-5 buckets)
  * `_year_range` validator rejects out-of-range years
  * Crossref verification accepts ≥0.90 fuzzy match + family-name +
    year proximity (codex amendment 3.2)
  * Crossref verification short-circuits on DOI exact
  * Provenance fields land on NormalizedSource correctly
    (provenance="llm_canon" + canonical_bucket + canonical_rationale)
  * source_client stays the real verifier (codex amendment 3.3 — does
    NOT become "llm_canon")
  * merge_canonical_with_search dedups by source_id; canonical wins on
    tie + tags vendor entries with provenance="search"
  * is_stub_enabled gates the chain (Settings.canonical_mining_stub)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from autoessay.agents._canonical_mining import (
    _family_name,
    _verify_via_crossref,
    is_stub_enabled,
    merge_canonical_with_search,
)
from autoessay.agents._canonical_sources_schema import (
    CanonicalSourcesOutput,
    CanonicalWork,
    ConsensusItem,
    DisagreementItem,
    FrontierItem,
    FrontierSourcesOutput,
    iter_canonical_works,
    iter_frontier_works,
)
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings

# ----------------------------------------------------------------------
# Pydantic schemas
# ----------------------------------------------------------------------


def _valid_work() -> dict[str, object]:
    return {
        "title": "Asia's Next Giant: South Korea and Late Industrialization",
        "first_author": "Alice Amsden",
        "year": 1989,
        "doi": "10.1093/oso/9780195076035.001.0001",
        "journal_or_publisher": "Oxford University Press",
        "rationale": "Foundational state-led-growth account.",
    }


def test_canonical_work_round_trip() -> None:
    w = CanonicalWork.parse_obj(_valid_work())
    assert w.first_author == "Alice Amsden"
    assert w.year == 1989


def test_canonical_work_year_out_of_range() -> None:
    bad = _valid_work()
    bad["year"] = 1499
    with pytest.raises(ValidationError):
        CanonicalWork.parse_obj(bad)
    bad["year"] = 2200
    with pytest.raises(ValidationError):
        CanonicalWork.parse_obj(bad)


def test_canonical_work_extra_field_rejected() -> None:
    payload = _valid_work()
    payload["rogue_field"] = "x"
    with pytest.raises(ValidationError):
        CanonicalWork.parse_obj(payload)


def test_canonical_work_doi_optional_blank_allowed_via_omission() -> None:
    payload = _valid_work()
    del payload["doi"]
    w = CanonicalWork.parse_obj(payload)
    assert w.doi is None


def test_consensus_item_requires_one_or_two_works() -> None:
    with pytest.raises(ValidationError):
        ConsensusItem.parse_obj({"statement": "x" * 50, "representative_works": []})
    with pytest.raises(ValidationError):
        ConsensusItem.parse_obj(
            {"statement": "x" * 50, "representative_works": [_valid_work()] * 3}
        )


def test_disagreement_item_requires_exactly_two_works() -> None:
    with pytest.raises(ValidationError):
        DisagreementItem.parse_obj(
            {"axis_description": "x" * 50, "representative_works": [_valid_work()]}
        )


def test_frontier_item_requires_why_frontier() -> None:
    with pytest.raises(ValidationError):
        FrontierItem.parse_obj(
            {
                "direction": "x" * 50,
                "representative_works": [_valid_work()],
                # missing why_frontier
            }
        )


def test_iter_canonical_works_yields_bucket_rationale_pairs() -> None:
    output = CanonicalSourcesOutput.parse_obj(
        {
            "consensus_findings": [
                {"statement": "S1" * 30, "representative_works": [_valid_work()]},
                {
                    "statement": "S2" * 30,
                    "representative_works": [_valid_work(), _valid_work()],
                },
            ],
            "major_disagreements": [
                {
                    "axis_description": "A1" * 30,
                    "representative_works": [_valid_work(), _valid_work()],
                },
            ],
        }
    )
    pairs = list(iter_canonical_works(output))
    # 1 + 2 (consensus) + 2 (disagreement) = 5 works
    assert len(pairs) == 5
    buckets = [bucket for bucket, _, _ in pairs]
    assert buckets.count("consensus") == 3
    assert buckets.count("disagreement") == 2


def test_iter_frontier_works_uses_why_frontier_as_seed() -> None:
    output = FrontierSourcesOutput.parse_obj(
        {
            "frontier_hotspots": [
                {
                    "direction": "Direction X" * 5,
                    "representative_works": [_valid_work()],
                    "why_frontier": "WHY_FRONTIER_SEED" * 3,
                },
            ],
        }
    )
    pairs = list(iter_frontier_works(output))
    assert len(pairs) == 1
    bucket, seed, _ = pairs[0]
    assert bucket == "frontier"
    assert "WHY_FRONTIER_SEED" in seed


# ----------------------------------------------------------------------
# Stub gate
# ----------------------------------------------------------------------


def test_is_stub_enabled_default_false() -> None:
    get_settings.cache_clear()
    # Conftest may have set the flag; clear env first.
    import os

    os.environ.pop("AUTOESSAY_CANONICAL_MINING_STUB", None)
    get_settings.cache_clear()
    assert is_stub_enabled() is False


def test_is_stub_enabled_true_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CANONICAL_MINING_STUB", "1")
    get_settings.cache_clear()
    assert is_stub_enabled() is True


# ----------------------------------------------------------------------
# Crossref verification — _verify_one_work
# ----------------------------------------------------------------------


class _FakeCrossrefClient:
    """Test double matching CrossrefClient.search shape."""

    def __init__(self, results_by_query: dict[str, list[NormalizedSource]]) -> None:
        self._results = results_by_query
        self.calls: list[str] = []

    async def search(
        self,
        *,
        query: str,
        year_window: object | None,
        limit: int,
    ) -> list[NormalizedSource]:
        del year_window, limit
        self.calls.append(query)
        return self._results.get(query, [])


def _candidate(
    *,
    source_id: str = "crossref:10.x",
    title: str = "Asia's Next Giant: South Korea and Late Industrialization",
    authors: list[str] | None = None,
    year: int | None = 1989,
    doi: str | None = "10.1093/oso/9780195076035.001.0001",
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=authors if authors is not None else ["Alice Amsden"],
        year=year,
        venue="Oxford UP",
        doi=doi,
        url=None,
        pdf_url=None,
        abstract="Abstract",
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        risk_flags=[],
    )


@pytest.mark.asyncio
async def test_verify_doi_exact_match_accepts() -> None:
    work = CanonicalWork.parse_obj(_valid_work())
    client = _FakeCrossrefClient(
        {
            "Asia's Next Giant: South Korea and Late Industrialization Alice Amsden 1989": [
                _candidate()
            ]
        },
    )
    result = await _verify_via_crossref(
        client,  # type: ignore[arg-type]
        "consensus",
        "consensus rationale " * 5,
        work,
        fuzzy_threshold=0.9,
    )
    assert isinstance(result, NormalizedSource)
    assert result.provenance == "llm_canon"
    assert result.canonical_bucket == "consensus"
    assert result.source_client == "crossref"  # codex amendment 3.3
    # Work's own rationale takes precedence over parent rationale_seed.
    assert "Foundational state-led-growth" in (result.canonical_rationale or "")


@pytest.mark.asyncio
async def test_verify_title_fuzzy_below_threshold_rejects() -> None:
    work = CanonicalWork.parse_obj(_valid_work())
    # Candidate title is completely different — fuzzy ratio will be low
    bad_candidate = _candidate(
        title="Something Entirely Unrelated to Korean Economy",
        doi="10.1234/different.doi",
        authors=["Different Author"],
    )
    client = _FakeCrossrefClient(
        {
            "Asia's Next Giant: South Korea and Late Industrialization Alice Amsden 1989": [
                bad_candidate
            ]
        },
    )
    result = await _verify_via_crossref(
        client,  # type: ignore[arg-type]
        "consensus",
        "rationale",
        work,
        fuzzy_threshold=0.9,
    )
    assert isinstance(result, dict)
    # PR-J9b: drop reason prefixed by verifier name (e.g.
    # ``crossref_below_fuzzy_threshold``) so dual-source warnings can
    # carry per-leg attribution.
    assert result["reason"] == "crossref_below_fuzzy_threshold"


@pytest.mark.asyncio
async def test_verify_no_candidates_drops_with_reason() -> None:
    work = CanonicalWork.parse_obj(_valid_work())
    client = _FakeCrossrefClient({})
    result = await _verify_via_crossref(
        client,  # type: ignore[arg-type]
        "consensus",
        "rationale",
        work,
        fuzzy_threshold=0.9,
    )
    assert isinstance(result, dict)
    assert result["reason"] == "no_crossref_match"


# ----------------------------------------------------------------------
# _family_name
# ----------------------------------------------------------------------


def test_family_name_western_takes_last_token() -> None:
    assert _family_name("Alice Amsden") == "Amsden"
    assert _family_name("J. K. Rowling") == "Rowling"


def test_family_name_single_token_returns_whole() -> None:
    assert _family_name("毛泽东") == "毛泽东"
    assert _family_name("钱穆") == "钱穆"


# ----------------------------------------------------------------------
# merge_canonical_with_search
# ----------------------------------------------------------------------


def test_merge_dedups_by_source_id_canonical_wins() -> None:
    canonical = _candidate(source_id="crossref:10.amsden")
    canonical = canonical.copy(
        update={
            "provenance": "llm_canon",
            "canonical_bucket": "consensus",
            "canonical_rationale": "canon-side rationale",
        }
    )
    vendor_dup = _candidate(source_id="crossref:10.amsden")  # same id
    vendor_unique = _candidate(source_id="crossref:10.different")
    out = merge_canonical_with_search([canonical], [vendor_dup, vendor_unique])
    # Length: 1 canon + 1 unique vendor = 2 (vendor_dup deduped)
    assert len(out) == 2
    # Canon entry preserves canonical_bucket + canonical_rationale
    canon_in_out = next(s for s in out if s.source_id == "crossref:10.amsden")
    assert canon_in_out.provenance == "llm_canon"
    assert canon_in_out.canonical_bucket == "consensus"
    # Unique vendor gets provenance="search"
    unique_in_out = next(s for s in out if s.source_id == "crossref:10.different")
    assert unique_in_out.provenance == "search"


def test_merge_canonical_first_then_vendor() -> None:
    canonical = _candidate(source_id="crossref:10.canon").copy(
        update={"provenance": "llm_canon", "canonical_bucket": "frontier"}
    )
    vendor = _candidate(source_id="crossref:10.vendor")
    out = merge_canonical_with_search([canonical], [vendor])
    assert out[0].source_id == "crossref:10.canon"
    assert out[1].source_id == "crossref:10.vendor"
