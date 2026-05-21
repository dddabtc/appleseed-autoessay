from urllib.parse import quote

import httpx

from autoessay.agents._source_verification import classify_source
from autoessay.agents.curator import _apply_literature_policy
from autoessay.clients.common import VerificationStatus
from autoessay.clients.wikipedia_zh import (
    WIKIPEDIA_ZH_SEARCH_URL,
    WIKIPEDIA_ZH_SUMMARY_URL,
    WikipediaZhClient,
)


async def test_wikipedia_zh_summary_maps_to_canonical_seed(respx_mock) -> None:  # type: ignore[no-untyped-def]
    title = "明清财政史"
    respx_mock.get(WIKIPEDIA_ZH_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"query": {"search": [{"title": title}]}},
        ),
    )
    respx_mock.get(f"{WIKIPEDIA_ZH_SUMMARY_URL}/{quote(title, safe='')}").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": title,
                "pageid": 123,
                "extract": "明清财政史相关页面。",
                "content_urls": {"desktop": {"page": "https://zh.wikipedia.org/wiki/明清财政史"}},
            },
        ),
    )
    client = WikipediaZhClient()

    results = await client.search("明清财政史", None, 2)
    await client.aclose()

    assert len(results) == 1
    source = results[0]
    assert source.source_id == "wikipedia_zh:123"
    assert source.provenance == "wiki_canonical_seed"
    assert source.verified_by is None
    assert "wikipedia_zh" in source.risk_flags


async def test_wikipedia_zh_classified_unverified_and_excluded_from_shortlist(
    respx_mock,
) -> None:  # type: ignore[no-untyped-def]
    title = "中国社会科学"
    respx_mock.get(WIKIPEDIA_ZH_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"query": {"search": [{"title": title}]}}),
    )
    respx_mock.get(f"{WIKIPEDIA_ZH_SUMMARY_URL}/{quote(title, safe='')}").mock(
        return_value=httpx.Response(
            200,
            json={"title": title, "pageid": 456, "extract": "期刊页面。"},
        ),
    )
    client = WikipediaZhClient()
    source = (await client.search("中国社会科学", None, 1))[0]
    await client.aclose()

    status, confidence = classify_source(source)
    kept, rejected = _apply_literature_policy(
        [source],
        {"include_working_papers": True, "include_books": True, "include_preprints": True},
    )

    assert status == VerificationStatus.UNVERIFIED
    assert confidence == 0.4
    assert kept == []
    assert rejected[0]["reason"] == "canonical_seed_not_citable"


async def test_wikipedia_zh_default_client_carries_explicit_user_agent() -> None:
    # Wikimedia rejects python-httpx default UA with HTTP 403:
    # "Please set a user-agent and respect our robot policy."
    # Regression for slice C R11.0 failure.
    client = WikipediaZhClient()
    try:
        ua = client._client.headers.get("user-agent", "")
        assert "appleseed-autoessay" in ua
        assert "github.com/dddabtc/appleseed-autoessay" in ua
        assert "python-httpx" not in ua.lower()
    finally:
        await client.aclose()
