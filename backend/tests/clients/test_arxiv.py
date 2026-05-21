from unittest.mock import AsyncMock

import httpx

from autoessay.clients import common
from autoessay.clients.arxiv import ArxivClient

ATOM_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title> Banking crises in credit markets </title>
    <summary> A paper abstract. </summary>
    <published>2024-01-01T00:00:00Z</published>
    <author><name>A. Scholar</name></author>
    <category term="q-fin.EC" />
    <arxiv:doi>10.1234/arxiv</arxiv:doi>
    <link rel="alternate" href="https://arxiv.org/abs/2401.00001" />
    <link title="pdf" type="application/pdf" href="https://arxiv.org/pdf/2401.00001" />
  </entry>
</feed>
"""


async def test_arxiv_query_parse_and_rate_limit_sleep(respx_mock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sleep = AsyncMock()
    monkeypatch.setattr(common.asyncio, "sleep", sleep)
    route = respx_mock.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, text=ATOM_RESPONSE),
    )
    client = ArxivClient(categories=["q-fin.EC"])

    first = await client.search("banking crises", (2020, 2024), 2)
    second = await client.search("banking crises", (2020, 2024), 2)
    await client.aclose()

    assert len(first) == 1
    assert second[0].source_id == "arxiv:2401.00001v1"
    assert first[0].title == "Banking crises in credit markets"
    assert first[0].authors == ["A. Scholar"]
    assert first[0].year == 2024
    assert first[0].doi == "10.1234/arxiv"
    assert first[0].access_status == "open"
    params = route.calls[0].request.url.params
    assert params["max_results"] == "2"
    assert 'all:"banking crises"' in params["search_query"]
    assert "cat:q-fin.EC" in params["search_query"]
    assert "submittedDate:[202001010000 TO 202412312359]" in params["search_query"]
    sleep.assert_awaited()
