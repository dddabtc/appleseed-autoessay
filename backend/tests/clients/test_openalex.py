from unittest.mock import AsyncMock

import httpx

from autoessay.clients import common
from autoessay.clients.openalex import OPENALEX_WORKS_URL, OpenAlexClient
from autoessay.config import get_settings

OPENALEX_WORK = {
    "id": "https://openalex.org/W2741809807",
    "doi": "https://doi.org/10.7717/peerj.4375",
    "title": "This Time Is Different: A Panoramic View of Eight Centuries of Financial Crises",
    "authorships": [
        {"author": {"display_name": "Carmen M. Reinhart"}},
        {"author": {"display_name": "Kenneth S. Rogoff"}},
    ],
    "publication_year": 2009,
    "primary_location": {
        "is_oa": True,
        "pdf_url": "https://example.test/openalex/this-time-is-different.pdf",
        "license": "cc-by",
        "source": {"display_name": "NBER Working Paper Series"},
    },
    "open_access": {"is_oa": True},
    "abstract_inverted_index": {
        "Financial": [0],
        "crises": [1],
        "share": [2],
        "common": [3],
        "historical": [4],
        "patterns.": [5],
    },
    "cited_by_count": 3351,
}

OPENALEX_SECOND_WORK = {
    "id": "https://openalex.org/W1234567890",
    "doi": "doi:10.5555/openalex.second",
    "display_name": "Banking Panics in Comparative Historical Perspective",
    "authorships": [{"author": {"display_name": "Jane Doe"}}],
    "publication_year": 2022,
    "primary_location": {
        "is_oa": False,
        "pdf_url": None,
        "license": None,
        "source": {"display_name": "Economic History Review"},
    },
    "open_access": {"is_oa": False},
    "abstract_inverted_index": {"Banking": [0], "panics": [1], "compared.": [2]},
}


async def test_openalex_env_api_key_and_normalization(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENALEX_API_KEY", "env-openalex-key")
    monkeypatch.setenv("OPENALEX_MAILTO", "lit@example.test")
    get_settings.cache_clear()
    route = respx_mock.get(OPENALEX_WORKS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"meta": {"next_cursor": "cursor-two"}, "results": [OPENALEX_WORK]},
            ),
            httpx.Response(
                200,
                json={"meta": {"next_cursor": None}, "results": [OPENALEX_SECOND_WORK]},
            ),
        ],
    )
    client = OpenAlexClient()

    results = await client.search("banking panic history", (2020, 2024), 30)
    await client.aclose()

    assert len(results) == 2
    assert results[0].source_id == "https://openalex.org/W2741809807"
    assert (
        results[0].title
        == "This Time Is Different: A Panoramic View of Eight Centuries of Financial Crises"
    )
    assert results[0].authors == ["Carmen M. Reinhart", "Kenneth S. Rogoff"]
    assert results[0].year == 2009
    assert results[0].venue == "NBER Working Paper Series"
    assert results[0].doi == "10.7717/peerj.4375"
    assert results[0].url == "https://openalex.org/W2741809807"
    assert results[0].pdf_url == "https://example.test/openalex/this-time-is-different.pdf"
    assert results[0].abstract == "Financial crises share common historical patterns."
    assert results[0].source_client == "openalex"
    assert results[0].access_status == "open"
    assert results[0].license == "cc-by"
    assert results[0].verified_by == "openalex"
    assert results[0].rank_score == 0.0
    assert results[0].risk_flags == []
    assert results[1].access_status == "metadata_only"
    assert route.call_count == 2
    first_params = route.calls[0].request.url.params
    second_params = route.calls[1].request.url.params
    assert first_params["search"] == "banking panic history"
    assert first_params["filter"] == "publication_year:>2018,is_oa:true,type:article"
    assert first_params["per_page"] == "25"
    assert first_params["cursor"] == "*"
    assert first_params["api_key"] == "env-openalex-key"
    assert "mailto" not in first_params
    assert second_params["cursor"] == "cursor-two"
    get_settings.cache_clear()


async def test_openalex_falls_back_to_mailto_when_api_key_missing(  # type: ignore[no-untyped-def]
    respx_mock,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.setenv("OPENALEX_MAILTO", "lit@example.test")
    get_settings.cache_clear()
    route = respx_mock.get(OPENALEX_WORKS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"meta": {"next_cursor": None}, "results": [OPENALEX_WORK]},
        ),
    )
    client = OpenAlexClient(filters=None)

    results = await client.search("banking panic history", (2020, 2024), 1)
    await client.aclose()

    assert len(results) == 1
    params = route.calls[0].request.url.params
    assert params["mailto"] == "lit@example.test"
    assert "api_key" not in params
    assert params["filter"] == "from_publication_date:2020-01-01,to_publication_date:2024-12-31"
    get_settings.cache_clear()


async def test_openalex_prefers_publisher_landing_url_for_resolver_candidates() -> None:  # type: ignore[no-untyped-def]
    item = dict(OPENALEX_WORK)
    item["primary_location"] = {
        "is_oa": True,
        "pdf_url": None,
        "landing_page_url": "https://publisher.test/article",
        "license": "cc-by",
        "source": {"display_name": "Publisher Journal"},
    }
    item["open_access"] = {"is_oa": True, "oa_url": "https://repository.test/oa"}
    client = OpenAlexClient(filters=None)

    source = client._parse_item(item)
    await client.aclose()

    assert source is not None
    assert source.url == "https://publisher.test/article"
    assert source.pdf_url is None
    assert source.access_status == "open"


async def test_openalex_retries_429_with_backoff(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sleep = AsyncMock()
    monkeypatch.setattr(common.asyncio, "sleep", sleep)
    route = respx_mock.get(OPENALEX_WORKS_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(
                200,
                json={"meta": {"next_cursor": None}, "results": [OPENALEX_WORK]},
            ),
        ],
    )
    client = OpenAlexClient(api_key="explicit-key")

    results = await client.search("banking panic history", None, 1)
    await client.aclose()

    assert len(results) == 1
    assert route.call_count == 2
    sleep.assert_awaited()


async def test_openalex_stub_mode_returns_three_fixture_sources(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_OPENALEX_STUB", "1")
    get_settings.cache_clear()
    client = OpenAlexClient(api_key="unused")

    results = await client.search("banking panic history", None, 10)
    await client.aclose()

    assert len(results) == 3
    assert all(source.source_client == "openalex" for source in results)
    assert any(source.pdf_url for source in results)
    get_settings.cache_clear()
