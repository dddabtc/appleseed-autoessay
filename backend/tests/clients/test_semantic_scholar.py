from unittest.mock import AsyncMock

import httpx

from autoessay.clients import common
from autoessay.clients.semantic_scholar import SemanticScholarClient

SEMANTIC_RESPONSE = {
    "data": [
        {
            "paperId": "paper-1",
            "title": "Credit markets and bank runs",
            "authors": [{"name": "Jane Doe"}],
            "year": 2023,
            "venue": "Economic History Review",
            "externalIds": {"DOI": "https://doi.org/10.5678/sem"},
            "url": "https://semanticscholar.org/paper-1",
            "openAccessPdf": {"url": "https://example.test/paper.pdf"},
            "abstract": "A Semantic Scholar abstract.",
            "isOpenAccess": True,
            "citationCount": 12,
        },
    ],
}


async def test_semantic_scholar_query_parse_and_rate_limit_sleep(  # type: ignore[no-untyped-def]
    respx_mock,
    monkeypatch,
) -> None:
    sleep = AsyncMock()
    monkeypatch.setattr(common.asyncio, "sleep", sleep)
    route = respx_mock.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
        return_value=httpx.Response(200, json=SEMANTIC_RESPONSE),
    )
    client = SemanticScholarClient(api_key="secret-key")

    first = await client.search("banking crises", (2020, 2024), 3)
    await client.search("banking crises", (2020, 2024), 3)
    await client.aclose()

    assert len(first) == 1
    assert first[0].source_id == "semantic_scholar:paper-1"
    assert first[0].doi == "10.5678/sem"
    assert first[0].pdf_url == "https://example.test/paper.pdf"
    assert first[0].rank_score == 12.0
    request = route.calls[0].request
    assert request.headers["x-api-key"] == "secret-key"
    assert request.url.params["query"] == "banking crises"
    assert request.url.params["year"] == "2020-2024"
    assert "externalIds" in request.url.params["fields"]
    sleep.assert_awaited()
