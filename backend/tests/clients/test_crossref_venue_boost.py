import httpx

from autoessay.clients.crossref import CROSSREF_WORKS_URL, CrossrefClient


def _response(venue: str, score: float = 2.0) -> dict[str, object]:
    return {
        "message": {
            "items": [
                {
                    "DOI": "10.5555/venue",
                    "title": ["明清财政史研究"],
                    "container-title": [venue],
                    "issued": {"date-parts": [[2021]]},
                    "score": score,
                },
            ],
        },
    }


async def test_crossref_venue_boost_raises_rank_score(respx_mock) -> None:  # type: ignore[no-untyped-def]
    route = respx_mock.get(CROSSREF_WORKS_URL).mock(
        return_value=httpx.Response(200, json=_response("历史研究", 2.0)),
    )
    client = CrossrefClient(mailto=None)

    results = await client.search("明清财政史 《历史研究》", None, 1)
    await client.aclose()

    assert results[0].rank_score == 2.3
    assert route.calls[0].request.url.params["query.container-title"] == "历史研究"


async def test_crossref_chinese_venue_detection_handles_book_marks(respx_mock) -> None:  # type: ignore[no-untyped-def]
    respx_mock.get(CROSSREF_WORKS_URL).mock(
        return_value=httpx.Response(200, json=_response("《中国社会科学》", 1.0)),
    )
    client = CrossrefClient(mailto=None)

    results = await client.search("经济思想史 中国社会科学", None, 1)
    await client.aclose()

    assert results[0].venue == "《中国社会科学》"
    assert results[0].rank_score == 1.3
