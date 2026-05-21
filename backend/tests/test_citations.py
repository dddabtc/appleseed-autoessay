from autoessay.clients.citations import generate_bib, normalized_source_to_bibtex
from autoessay.clients.common import NormalizedSource


def test_normalized_source_to_bibtex_article_includes_doi() -> None:
    source = _source(
        source_id="b_article",
        source_client="crossref",
        venue="Financial History Review",
        doi="10.1000/test",
        url=None,
    )

    bibtex = normalized_source_to_bibtex(source)

    assert bibtex.startswith("@article{b_article,")
    assert "journal = {Financial History Review}" in bibtex
    assert "doi = {10.1000/test}" in bibtex


def test_normalized_source_to_bibtex_book_uses_publisher() -> None:
    source = _source(
        source_id="a_book",
        source_client="book",
        venue="Cambridge University Press",
        doi=None,
        url="https://example.test/book",
    )

    bibtex = normalized_source_to_bibtex(source)

    assert bibtex.startswith("@book{a_book,")
    assert "publisher = {Cambridge University Press}" in bibtex
    assert "url = {https://example.test/book}" in bibtex


def test_generate_bib_sorts_misc_entries_by_source_id() -> None:
    later = _source(
        source_id="z_misc", source_client="manual", venue=None, doi=None, url="https://z"
    )
    first = _source(
        source_id="a_misc", source_client="manual", venue=None, doi=None, url="https://a"
    )

    bib = generate_bib([later, first])

    assert bib.index("@misc{a_misc,") < bib.index("@misc{z_misc,")
    assert "url = {https://a}" in bib


def test_generate_bib_empty_sources_returns_empty_string() -> None:
    assert generate_bib([]) == ""


def _source(
    *,
    source_id: str,
    source_client: str,
    venue: str | None,
    doi: str | None,
    url: str | None,
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Title for {source_id}",
        authors=["Ada Lovelace", "Grace Hopper"],
        year=2024,
        venue=venue,
        doi=doi,
        url=url,
        pdf_url=None,
        abstract="abstract",
        source_client=source_client,
        access_status="open",
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )
