"""PR-263a — source enrichment from shadow baseline.

Tests focus on the adapter logic (``_candidate_to_canonical_work``
+ ``_shadow_to_verifier_input``) and one happy-path
``enrich_with_shadow_baseline`` integration. The actual Crossref /
OpenAlex verification semantics are covered by existing
``test_canonical_mining_v1.py`` — we don't re-test them here, just
prove the shadow baseline shape feeds through correctly.
"""

from __future__ import annotations

import pytest

from autoessay.agents._canonical_sources_schema import CanonicalWork
from autoessay.agents.shadow_baseline import (
    ReferenceCandidate,
    ShadowBaselineOutput,
)
from autoessay.agents.source_enrichment import (
    _candidate_to_canonical_work,
    _shadow_to_verifier_input,
    enrich_with_shadow_baseline,
)
from autoessay.clients.common import AccessStatus, NormalizedSource

# ----- _candidate_to_canonical_work -------------------------------


def test_canonical_work_full_metadata() -> None:
    cand = ReferenceCandidate(
        author="Joseph McDermott",
        year="2006",
        title="A Social History of the Chinese Book",
        venue="Hong Kong University Press",
        type="book",
        doi_or_isbn="10.x/y",
        why_relevant="late-imperial book history",
    )
    work = _candidate_to_canonical_work(cand)
    assert work is not None
    assert work.first_author == "Joseph McDermott"
    assert work.year == 2006
    assert work.title.startswith("A Social History")
    assert work.doi == "10.x/y"
    assert work.journal_or_publisher == "Hong Kong University Press"


def test_canonical_work_isbn_left_as_null_doi() -> None:
    """ISBN strings (``978-...``) shouldn't be passed as DOI — the
    verifier short-circuits on DOI exact match and would mis-route.
    Codex Q6 verdict: only ``10.``-prefix DOIs go into ``doi``;
    everything else falls through to title+author+year fuzzy."""
    cand = ReferenceCandidate(
        author="Some Author",
        year="2020",
        title="Some Book Title Long Enough",
        doi_or_isbn="978-0-19-507603-5",
    )
    work = _candidate_to_canonical_work(cand)
    assert work is not None
    assert work.doi is None


def test_canonical_work_unparseable_year_becomes_null() -> None:
    cand = ReferenceCandidate(
        author="Some Author",
        year="late 19th century",
        title="Some Book Title",
    )
    work = _candidate_to_canonical_work(cand)
    assert work is not None
    assert work.year is None


def test_canonical_work_drops_too_short_title() -> None:
    """CanonicalWork enforces min_length=4 on title; we drop the
    candidate entirely so we don't surface a verifier exception
    upstream."""
    cand = ReferenceCandidate(author="X" * 5, year="2020", title="abc")
    assert _candidate_to_canonical_work(cand) is None


def test_canonical_work_drops_too_short_author() -> None:
    """CanonicalWork enforces min_length=2 on first_author."""
    cand = ReferenceCandidate(
        author="A",
        year="2020",
        title="Title Long Enough",
    )
    assert _candidate_to_canonical_work(cand) is None


def test_canonical_work_caps_long_fields() -> None:
    """Fields exceeding CanonicalWork max_length get truncated, not
    rejected — we want enrichment to be tolerant of long titles."""
    cand = ReferenceCandidate(
        author="X" * 250,
        year="2020",
        title="T" * 500,
        venue="V" * 400,
        why_relevant="W" * 500,
    )
    work = _candidate_to_canonical_work(cand)
    assert work is not None
    assert len(work.title) <= 400
    assert len(work.first_author) <= 200
    assert work.journal_or_publisher is not None
    assert len(work.journal_or_publisher) <= 300


# ----- _shadow_to_verifier_input ----------------------------------


def test_shadow_to_verifier_input_skips_invalid_candidates() -> None:
    """A shadow baseline with mixed-valid candidates yields only
    valid (bucket, rationale, CanonicalWork) triples — invalid
    candidates are silently dropped (the warnings end up in audit
    later)."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[
            ReferenceCandidate(author="Valid Author", year="2020", title="Valid Title Here"),
            ReferenceCandidate(author="A", year="2020", title="Too Short Author"),  # drop
            ReferenceCandidate(author="X" * 5, year="2020", title="abc"),  # drop, title too short
            ReferenceCandidate(author="Another Author", year="2021", title="Another Valid Title"),
        ],
    )
    triples = _shadow_to_verifier_input(out)
    assert len(triples) == 2
    # All triples land in the frontier bucket.
    assert all(triple[0] == "frontier" for triple in triples)
    # CanonicalWork instances came through.
    assert all(isinstance(triple[2], CanonicalWork) for triple in triples)


def test_shadow_to_verifier_input_empty_candidates_returns_empty() -> None:
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[],
    )
    assert _shadow_to_verifier_input(out) == []


# ----- enrich_with_shadow_baseline (integration) -------------------


class _FakeCrossrefClient:
    """Same shape as CrossrefClient.search; mirrors the test double
    used by test_canonical_mining_v1.py so we re-use a known-good
    pattern."""

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


def _normalized(
    *,
    source_id: str = "crossref:10.x/y",
    title: str = "A Social History of the Chinese Book",
    authors: list[str] | None = None,
    year: int | None = 2006,
    doi: str | None = "10.x/y",
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=authors if authors is not None else ["Joseph McDermott"],
        year=year,
        venue="Hong Kong University Press",
        doi=doi,
        url=None,
        pdf_url=None,
        abstract="A social history of the Chinese book.",
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        risk_flags=[],
    )


@pytest.mark.asyncio
async def test_enrich_returns_verified_source_for_doi_exact_match() -> None:
    """End-to-end: shadow baseline emits a candidate with a real
    DOI that the (mocked) Crossref client knows about, and the
    enrichment returns a verified NormalizedSource."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[
            ReferenceCandidate(
                author="Joseph McDermott",
                year="2006",
                title="A Social History of the Chinese Book",
                venue="Hong Kong University Press",
                type="book",
                doi_or_isbn="10.x/y",
                why_relevant="late-imperial book history",
            ),
        ],
    )
    crossref = _FakeCrossrefClient(
        {
            "A Social History of the Chinese Book Joseph McDermott 2006": [_normalized()],
        },
    )
    verified, warnings = await enrich_with_shadow_baseline(
        out,
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=None,
    )
    assert len(verified) == 1
    assert verified[0].title.startswith("A Social History")
    assert verified[0].provenance == "llm_canon"
    assert verified[0].canonical_bucket == "frontier"
    assert verified[0].source_client == "crossref"
    assert warnings == []


@pytest.mark.asyncio
async def test_enrich_drops_unverifiable_candidate() -> None:
    """When Crossref returns a result whose title doesn't fuzzy-
    match the candidate, the candidate ends up in warnings rather
    than the verified list."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[
            ReferenceCandidate(
                author="Hallucinated Author",
                year="2020",
                title="A Book That Does Not Exist In Crossref",
                doi_or_isbn=None,
            ),
        ],
    )
    # Crossref returns a totally unrelated result for the query →
    # fuzzy threshold drops it.
    crossref = _FakeCrossrefClient(
        {
            "A Book That Does Not Exist In Crossref Hallucinated Author 2020": [
                _normalized(title="Something Completely Different"),
            ],
        },
    )
    verified, warnings = await enrich_with_shadow_baseline(
        out,
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=None,
    )
    assert verified == []
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_enrich_empty_candidates_returns_empty() -> None:
    """Shadow baseline with no reference_candidates → no LLM
    overhead, no Crossref calls, returns empty result."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[],
    )
    crossref = _FakeCrossrefClient({})
    verified, warnings = await enrich_with_shadow_baseline(
        out,
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=None,
    )
    assert verified == []
    assert warnings == []
    # Crossref should not have been queried at all.
    assert crossref.calls == []


# ----- OpenLibrary fallback integration ---------------------------


class _FakeOpenLibraryClient:
    """Test double matching ``OpenLibraryClient.lookup_isbn`` shape.
    Tracks which ISBNs were queried so tests can assert call patterns
    (e.g. that we don't re-query an already-verified ISBN)."""

    def __init__(
        self,
        isbn_to_metadata: dict[str, object | None],
    ) -> None:
        self._results = isbn_to_metadata
        self.calls: list[str] = []

    async def lookup_isbn(self, isbn: str) -> object | None:
        self.calls.append(isbn)
        return self._results.get(isbn)


@pytest.mark.asyncio
async def test_enrich_openlibrary_fallback_recovers_chinese_book() -> None:
    """Real-paper validator finding: 100% of shadow-baseline candidates
    on the 江南刊本 kernel were Chinese books with ISBN-13 (978-7
    prefix) that Crossref+OpenAlex don't index. OpenLibrary fallback
    must turn those into verified ``METADATA_ONLY`` sources."""
    from autoessay.clients.openlibrary import OpenLibraryBookMetadata

    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[
            ReferenceCandidate(
                author="郑振铎",
                year="2011",
                title="中国俗文学史",
                venue="中华书局",
                type="book",
                doi_or_isbn="9787101048420",
                why_relevant="late-imperial book history",
            ),
        ],
    )
    crossref = _FakeCrossrefClient({})  # no Crossref hits
    openlibrary = _FakeOpenLibraryClient(
        {
            "9787101048420": OpenLibraryBookMetadata(
                isbn="9787101048420",
                title="中国俗文学史",
                authors=["郑振铎"],
                publisher="中华书局",
                publish_year=2011,
                url=None,
            ),
        },
    )
    verified, warnings = await enrich_with_shadow_baseline(
        out,
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=None,
        openlibrary_client=openlibrary,  # type: ignore[arg-type]
    )
    assert len(verified) == 1
    assert verified[0].source_id == "openlibrary:isbn-9787101048420"
    assert verified[0].source_client == "openlibrary"
    assert verified[0].verified_by == "openlibrary"
    assert verified[0].title == "中国俗文学史"
    # The candidate didn't have a DOI so it was queried via fuzzy
    # against Crossref (and missed); then the OpenLibrary fallback
    # picked it up.
    assert openlibrary.calls == ["9787101048420"]


@pytest.mark.asyncio
async def test_enrich_openlibrary_skipped_when_no_isbn() -> None:
    """A reference candidate without an ISBN-13 doesn't trigger the
    OpenLibrary lookup — saves a needless network call."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[
            ReferenceCandidate(
                author="Joseph McDermott",
                year="2006",
                title="A Social History of the Chinese Book",
                doi_or_isbn=None,  # no ISBN, no DOI
            ),
        ],
    )
    crossref = _FakeCrossrefClient({})
    openlibrary = _FakeOpenLibraryClient({})
    verified, warnings = await enrich_with_shadow_baseline(
        out,
        crossref_client=crossref,  # type: ignore[arg-type]
        openalex_client=None,
        openlibrary_client=openlibrary,  # type: ignore[arg-type]
    )
    assert verified == []
    assert openlibrary.calls == []


@pytest.mark.asyncio
async def test_enrich_openlibrary_skipped_when_isbn10_with_x() -> None:
    """ISBN-10 with X check digit also doesn't trigger lookup —
    PR-263b only handles 978-prefix ISBN-13 in v1 (codex Q5
    suggested keep scope minimal)."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[
            ReferenceCandidate(
                author="X Author",
                year="1990",
                title="An Older Book",
                doi_or_isbn="0-306-40615-X",
            ),
        ],
    )
    openlibrary = _FakeOpenLibraryClient({})
    verified, warnings = await enrich_with_shadow_baseline(
        out,
        crossref_client=_FakeCrossrefClient({}),  # type: ignore[arg-type]
        openalex_client=None,
        openlibrary_client=openlibrary,  # type: ignore[arg-type]
    )
    assert openlibrary.calls == []


@pytest.mark.asyncio
async def test_enrich_openlibrary_miss_records_drop_warning() -> None:
    """OpenLibrary returning None (book not in their DB) → warning
    so the caller can surface the candidate as unverified."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[
            ReferenceCandidate(
                author="某作者",
                year="2020",
                title="某经典书",
                doi_or_isbn="9787999999999",
            ),
        ],
    )
    openlibrary = _FakeOpenLibraryClient({"9787999999999": None})
    verified, warnings = await enrich_with_shadow_baseline(
        out,
        crossref_client=_FakeCrossrefClient({}),  # type: ignore[arg-type]
        openalex_client=None,
        openlibrary_client=openlibrary,  # type: ignore[arg-type]
    )
    assert verified == []
    # Both verifiers tried + missed → both emit drop warnings.
    # Caller (e.g. PR-263c appendix) collapses them into a single
    # "unverified" entry per candidate.
    reasons = {w["reason"] for w in warnings}
    assert "openlibrary_no_match" in reasons


@pytest.mark.asyncio
async def test_enrich_no_openlibrary_client_keeps_legacy_behavior() -> None:
    """Legacy callers that don't pass ``openlibrary_client`` see
    the same (Crossref+OpenAlex only) result they always did —
    so PR-263b doesn't accidentally change behavior for code that
    hasn't been migrated yet."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        reference_candidates=[
            ReferenceCandidate(
                author="郑振铎",
                year="2011",
                title="中国俗文学史",
                doi_or_isbn="9787101048420",
            ),
        ],
    )
    verified, warnings = await enrich_with_shadow_baseline(
        out,
        crossref_client=_FakeCrossrefClient({}),  # type: ignore[arg-type]
        openalex_client=None,
        # openlibrary_client omitted = legacy behavior
    )
    assert verified == []
    assert len(warnings) == 1
