import httpx

from autoessay.clients.openalex import OPENALEX_WORKS_URL, OpenAlexClient, _topic_ids_for_domain

OPENALEX_WORK = {
    "id": "https://openalex.org/W1",
    "title": "Historical Studies in Financial Development",
    "authorships": [],
    "publication_year": 2020,
    "primary_location": {"source": {"display_name": "Economic History Review"}},
    "open_access": {"is_oa": False},
}


async def test_openalex_concepts_id_fallback_still_works(respx_mock) -> None:  # type: ignore[no-untyped-def]
    route = respx_mock.get(OPENALEX_WORKS_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": "bad filter"}),
            httpx.Response(
                200,
                json={"meta": {"next_cursor": None}, "results": [OPENALEX_WORK]},
            ),
        ],
    )
    client = OpenAlexClient(
        filters=None,
        topic_ids=("T14094",),
        legacy_concept_ids=("C162324750",),
    )

    results = await client.search("financial history", None, 1)
    await client.aclose()

    assert len(results) == 1
    assert route.calls[0].request.url.params["filter"] == "primary_topic.id:T14094"
    assert route.calls[1].request.url.params["filter"] == "concepts.id:C162324750"


async def test_openalex_primary_topic_id_preferred_when_mapping_exists(respx_mock) -> None:  # type: ignore[no-untyped-def]
    route = respx_mock.get(OPENALEX_WORKS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"meta": {"next_cursor": None}, "results": [OPENALEX_WORK]},
        ),
    )
    client = OpenAlexClient(
        filters="type:article",
        topic_ids=("https://openalex.org/T14094", "T13897"),
        legacy_concept_ids=("C162324750",),
    )

    await client.search("financial history", None, 1)
    await client.aclose()

    assert route.calls[0].request.url.params["filter"] == (
        "type:article,primary_topic.id:T14094|T13897"
    )


async def test_financial_history_domain_selects_humanities_topic_ids(respx_mock) -> None:  # type: ignore[no-untyped-def]
    route = respx_mock.get(OPENALEX_WORKS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"meta": {"next_cursor": None}, "results": [OPENALEX_WORK]},
        ),
    )
    client = OpenAlexClient(filters=None, domain_id="financial_history")

    await client.search("financial history", None, 1)
    await client.aclose()

    topic_filter = route.calls[0].request.url.params["filter"]
    expected_ids = "|".join(_topic_ids_for_domain("financial_history"))
    assert topic_filter == f"primary_topic.id:{expected_ids}"
