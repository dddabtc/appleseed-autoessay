"""Chinese Wikipedia canonical-seed client."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from autoessay.clients.common import (
    AccessStatus,
    AsyncLitClient,
    ClientSearchError,
    NormalizedSource,
    RateLimiter,
    clean_text,
)

WIKIPEDIA_ZH_SEARCH_URL = "https://zh.wikipedia.org/w/api.php"
WIKIPEDIA_ZH_SUMMARY_URL = "https://zh.wikipedia.org/api/rest_v1/page/summary"
MAX_WIKIPEDIA_ZH_TITLES = 2

# Wikimedia user-agent policy requires explicit identification:
# https://meta.wikimedia.org/wiki/User-Agent_policy
# Default ``python-httpx/...`` UA is blocked with HTTP 403 ("Please set
# a user-agent and respect our robot policy").
_USER_AGENT = "appleseed-autoessay/0.1 (+https://github.com/dddabtc/appleseed-autoessay) httpx"


class WikipediaZhClient(AsyncLitClient):
    """Chinese Wikipedia client.

    Returned sources are canonical entity seeds only. They help mining and
    filtering surface entities, but curator excludes ``wikipedia_zh:*`` from
    shortlist/citation pools.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if http_client is None:
            http_client = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": _USER_AGENT},
            )
        super().__init__(
            source_id="wikipedia_zh",
            http_client=http_client,
            rate_limiter=rate_limiter,
            min_interval_seconds=0.1,
            max_concurrency=2,
            backoff_seconds=1.0,
        )

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        del year_window
        if limit <= 0:
            return []
        titles = await self._search_titles(query, min(limit, MAX_WIKIPEDIA_ZH_TITLES))
        sources: list[NormalizedSource] = []
        for title in titles:
            try:
                summary = await self._get_json(
                    f"{WIKIPEDIA_ZH_SUMMARY_URL}/{quote(title, safe='')}",
                    query=query,
                    retries=1,
                )
            except ClientSearchError:
                continue
            source = _summary_to_source(summary)
            if source is not None:
                sources.append(source)
            if len(sources) >= limit:
                break
        return sources

    async def _search_titles(self, query: str, limit: int) -> list[str]:
        data = await self._get_json(
            WIKIPEDIA_ZH_SEARCH_URL,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": limit,
                "format": "json",
                "utf8": 1,
            },
            query=query,
            retries=1,
        )
        raw_query = data.get("query")
        if not isinstance(raw_query, dict):
            return []
        search_results = raw_query.get("search")
        if not isinstance(search_results, list):
            return []
        titles: list[str] = []
        for item in search_results:
            if not isinstance(item, dict):
                continue
            title = clean_text(item.get("title"))
            if title is not None:
                titles.append(title)
        return titles[:limit]


def _summary_to_source(summary: object) -> NormalizedSource | None:
    if not isinstance(summary, dict):
        return None
    title = clean_text(summary.get("title"))
    if title is None:
        return None
    page_id = summary.get("pageid")
    source_key = str(page_id) if isinstance(page_id, int) else title.replace(" ", "_")
    content_urls = summary.get("content_urls")
    desktop_urls = content_urls.get("desktop") if isinstance(content_urls, dict) else None
    page_url = clean_text(desktop_urls.get("page")) if isinstance(desktop_urls, dict) else None
    return NormalizedSource(
        source_id=f"wikipedia_zh:{source_key}",
        title=title,
        authors=[],
        year=None,
        venue="Chinese Wikipedia",
        doi=None,
        url=page_url,
        pdf_url=None,
        abstract=clean_text(summary.get("extract")),
        source_client="wikipedia_zh",
        access_status=AccessStatus.METADATA_ONLY,
        license="CC BY-SA 4.0",
        rank_score=0.0,
        risk_flags=["wikipedia_zh"],
        provenance="wiki_canonical_seed",
        verified_by=None,
    )
