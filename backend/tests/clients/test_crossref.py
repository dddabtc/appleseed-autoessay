from unittest.mock import AsyncMock

import httpx

from autoessay.clients import common
from autoessay.clients.crossref import CrossrefClient

CROSSREF_RESPONSE = {
    "message": {
        "items": [
            {
                "DOI": "10.1111/cross",
                "title": ["Banking history and credit supply"],
                "author": [{"given": "Alex", "family": "Taylor"}],
                "issued": {"date-parts": [[2022, 5, 1]]},
                "container-title": ["Journal of Economic History"],
                "URL": "https://doi.org/10.1111/cross",
                "link": [
                    {
                        "URL": "https://example.test/cross.pdf",
                        "content-type": "application/pdf",
                    },
                ],
                "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
                "score": 18.5,
                "abstract": "<jats:p>Crossref abstract.</jats:p>",
            },
        ],
    },
}


async def test_crossref_query_parse_and_rate_limit_sleep(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sleep = AsyncMock()
    monkeypatch.setattr(common.asyncio, "sleep", sleep)
    route = respx_mock.get("https://api.crossref.org/works").mock(
        return_value=httpx.Response(200, json=CROSSREF_RESPONSE),
    )
    client = CrossrefClient(mailto="lit@example.test")

    first = await client.search("banking crises", (2020, 2024), 4)
    await client.search("banking crises", (2020, 2024), 4)
    await client.aclose()

    assert len(first) == 1
    assert first[0].source_id == "crossref:10.1111/cross"
    assert first[0].authors == ["Alex Taylor"]
    assert first[0].year == 2022
    assert first[0].venue == "Journal of Economic History"
    assert first[0].abstract == "Crossref abstract."
    assert first[0].verified_by == "crossref"
    params = route.calls[0].request.url.params
    assert params["query.bibliographic"] == "banking crises"
    assert params["rows"] == "4"
    assert params["mailto"] == "lit@example.test"
    assert params["filter"] == "from-pub-date:2020,until-pub-date:2024"
    sleep.assert_awaited()


async def test_crossref_retries_5xx_once(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sleep = AsyncMock()
    monkeypatch.setattr(common.asyncio, "sleep", sleep)
    route = respx_mock.get("https://api.crossref.org/works").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json=CROSSREF_RESPONSE),
        ],
    )
    client = CrossrefClient(mailto="lit@example.test")

    results = await client.search("banking crises", (2020, 2024), 4)
    await client.aclose()

    assert len(results) == 1
    assert route.call_count == 2
    sleep.assert_awaited()
