import httpx

from autoessay.clients.cnki import CNKIClient
from autoessay.config import get_settings

CNKI_RESPONSE = {
    "data": {
        "items": [
            {
                "id": "CNKI:SUN:JJYJ.0.2021-01-001",
                "title": "近代银行业危机与信用市场调整研究",
                "authors": "李明; 王晓",
                "year": "2021",
                "journal": "中国经济史研究",
                "doi": "10.1234/cnki.test",
                "url": "https://kns.cnki.net/kcms/detail/test.html",
                "abstract": "讨论银行危机、信用收缩与制度回应。",
                "score": "9.5",
            },
        ],
    },
}


async def test_cnki_search_uses_configured_endpoint_and_normalizes_sources(
    respx_mock,
) -> None:  # type: ignore[no-untyped-def]
    route = respx_mock.get("https://cnki.example.test/api/search").mock(
        return_value=httpx.Response(200, json=CNKI_RESPONSE),
    )
    client = CNKIClient(base_url="https://cnki.example.test/api/search")

    results = await client.search("银行危机 金融史", (2018, 2024), 5)
    await client.aclose()

    assert len(results) == 1
    assert results[0].source_id == "cnki:CNKI:SUN:JJYJ.0.2021-01-001"
    assert results[0].title == "近代银行业危机与信用市场调整研究"
    assert results[0].authors == ["李明", "王晓"]
    assert results[0].year == 2021
    assert results[0].venue == "中国经济史研究"
    assert results[0].doi == "10.1234/cnki.test"
    assert results[0].source_client == "cnki"
    assert results[0].rank_score == 9.5
    params = route.calls[0].request.url.params
    assert params["q"] == "银行危机 金融史"
    assert params["query"] == "银行危机 金融史"
    assert params["limit"] == "5"
    assert params["start_year"] == "2018"
    assert params["end_year"] == "2024"


async def test_cnki_stub_mode_returns_chinese_sources(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_CNKI_STUB", "1")
    get_settings.cache_clear()
    client = CNKIClient(base_url="https://cnki.example.test/api/search")

    results = await client.search("银行史", None, 10)
    await client.aclose()

    assert len(results) == 2
    assert all(source.source_client == "cnki" for source in results)
    assert any("银行" in source.title for source in results)
    get_settings.cache_clear()
