"""Semantic Scholar Graph API client."""

import httpx

from autoessay.clients.common import (
    AccessStatus,
    AsyncLitClient,
    NormalizedSource,
    RateLimiter,
    clean_text,
    normalize_doi_value,
    resolve_year_range,
)
from autoessay.config import get_settings

SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
SEMANTIC_FIELDS = (
    "paperId,title,authors,year,venue,externalIds,url,openAccessPdf,abstract,"
    "publicationTypes,isOpenAccess,citationCount,influentialCitationCount"
)


class SemanticScholarClient(AsyncLitClient):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(
            source_id="semantic_scholar",
            http_client=http_client,
            rate_limiter=rate_limiter,
            min_interval_seconds=1.0,
            max_concurrency=1,
            backoff_seconds=1.0,
        )
        self._api_key = api_key if api_key is not None else get_settings().semantic_scholar_api_key

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        params: dict[str, str | int] = {
            "query": query,
            "limit": limit,
            "fields": SEMANTIC_FIELDS,
        }
        year_range = resolve_year_range(year_window)
        if year_range is not None:
            params["year"] = f"{year_range[0]}-{year_range[1]}"
        headers = {"x-api-key": self._api_key} if self._api_key else None
        data = await self._get_json(
            SEMANTIC_SCHOLAR_SEARCH_URL,
            params=params,
            headers=headers,
            query=query,
        )
        records = data.get("data", [])
        if not isinstance(records, list):
            return []
        return [
            source for item in records[:limit] if (source := self._parse_item(item)) is not None
        ]

    def _parse_item(self, item: object) -> NormalizedSource | None:
        if not isinstance(item, dict):
            return None
        title = clean_text(item.get("title"))
        paper_id = clean_text(item.get("paperId"))
        if title is None or paper_id is None:
            return None
        authors = []
        for author in item.get("authors", []):
            if isinstance(author, dict):
                name = clean_text(author.get("name"))
                if name is not None:
                    authors.append(name)
        external_ids = item.get("externalIds", {})
        doi = None
        if isinstance(external_ids, dict):
            doi = normalize_doi_value(external_ids.get("DOI"))
        pdf_url = None
        open_access_pdf = item.get("openAccessPdf")
        if isinstance(open_access_pdf, dict):
            pdf_url = clean_text(open_access_pdf.get("url"))
        year = item.get("year") if isinstance(item.get("year"), int) else None
        citation_count = item.get("citationCount")
        rank_score = float(citation_count) if isinstance(citation_count, (int, float)) else 0.0
        return NormalizedSource(
            source_id=f"semantic_scholar:{paper_id}",
            title=title,
            authors=authors,
            year=year,
            venue=clean_text(item.get("venue")),
            doi=doi,
            url=clean_text(item.get("url")),
            pdf_url=pdf_url,
            abstract=clean_text(item.get("abstract")),
            source_client=self.source_id,
            access_status=AccessStatus.OPEN
            if pdf_url or item.get("isOpenAccess") is True
            else AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=rank_score,
            risk_flags=[],
        )
