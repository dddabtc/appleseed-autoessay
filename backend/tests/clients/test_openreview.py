from unittest.mock import AsyncMock

import httpx

from autoessay.clients import common
from autoessay.clients.openreview import OpenReviewClient

OPENREVIEW_RESPONSE = {
    "notes": [
        {
            "id": "note-1",
            "forum": "forum-1",
            "pdate": 1704067200000,
            "license": "CC BY 4.0",
            "content": {
                "title": {"value": "Open review evidence on credit markets"},
                "authors": {"value": ["Pat Reviewer"]},
                "abstract": {"value": "An OpenReview abstract."},
                "venue": {"value": "OpenReview"},
                "pdf": {"value": "/pdf?id=note-1"},
                "doi": {"value": "10.2222/open"},
            },
        },
    ],
}


async def test_openreview_query_parse_and_rate_limit_sleep(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sleep = AsyncMock()
    monkeypatch.setattr(common.asyncio, "sleep", sleep)
    route = respx_mock.get("https://api2.openreview.net/notes/search").mock(
        return_value=httpx.Response(200, json=OPENREVIEW_RESPONSE),
    )
    client = OpenReviewClient()

    first = await client.search("banking crises", (2020, 2024), 5)
    await client.search("banking crises", (2020, 2024), 5)
    await client.aclose()

    assert len(first) == 1
    assert first[0].source_id == "openreview:note-1"
    assert first[0].authors == ["Pat Reviewer"]
    assert first[0].year == 2024
    assert first[0].url == "https://openreview.net/forum?id=forum-1"
    assert first[0].pdf_url == "https://openreview.net/pdf?id=note-1"
    assert first[0].access_status == "open"
    params = route.calls[0].request.url.params
    assert params["term"] == "banking crises"
    assert params["limit"] == "5"
    sleep.assert_awaited()
