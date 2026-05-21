from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.dedup import deduplicate_sources


def test_dedup_prefers_exact_doi_and_merges_richer_metadata() -> None:
    first = _source(
        source_id="semantic_scholar:a",
        title="Banking Panics",
        doi="https://doi.org/10.1000/example",
        pdf_url=None,
        authors=["A"],
        rank_score=0.5,
    )
    duplicate = _source(
        source_id="crossref:b",
        title="Banking Panics",
        doi="10.1000/example",
        pdf_url="https://example.test/paper.pdf",
        authors=["A", "B"],
        rank_score=0.8,
    )

    deduped, stats = deduplicate_sources([first, duplicate])

    assert stats.doi_duplicates == 1
    assert stats.kept == 1
    assert deduped[0].source_id == "semantic_scholar:a"
    assert deduped[0].authors == ["A", "B"]
    assert deduped[0].pdf_url == "https://example.test/paper.pdf"
    assert deduped[0].access_status == "open"
    assert deduped[0].rank_score == 0.8


def test_dedup_fuzzy_title_match_without_doi() -> None:
    first = _source(
        source_id="semantic_scholar:a",
        title="Credit Markets and Bank Runs in Historical Perspective",
        doi=None,
    )
    duplicate = _source(
        source_id="openreview:b",
        title="Bank Runs and Credit Markets: A Historical Perspective",
        doi=None,
    )

    deduped, stats = deduplicate_sources([first, duplicate])

    assert stats.fuzzy_duplicates == 1
    assert len(deduped) == 1


def _source(
    *,
    source_id: str,
    title: str,
    doi: str | None,
    pdf_url: str | None = None,
    authors: list[str] | None = None,
    rank_score: float = 0.1,
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=authors or ["Author"],
        year=2021,
        venue="Venue",
        doi=doi,
        url="https://example.test",
        pdf_url=pdf_url,
        abstract="Abstract",
        source_client=source_id.split(":", 1)[0],
        access_status=AccessStatus.OPEN if pdf_url else AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=rank_score,
        risk_flags=[],
    )
