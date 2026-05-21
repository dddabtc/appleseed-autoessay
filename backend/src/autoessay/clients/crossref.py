"""Crossref Works API client."""

import re

import httpx

from autoessay.clients.common import (
    AccessStatus,
    AsyncLitClient,
    NormalizedSource,
    RateLimiter,
    clean_text,
    first_text,
    normalize_doi_value,
    resolve_year_range,
)
from autoessay.config import get_settings

CROSSREF_WORKS_URL = "https://api.crossref.org/works"
CHINESE_HUMANITIES_VENUES: tuple[str, ...] = (
    "历史研究",
    "中国社会科学",
    "文学评论",
    "哲学研究",
    "经济研究",
)
CHINESE_VENUE_RANK_BOOST = 0.3


class CrossrefClient(AsyncLitClient):
    def __init__(
        self,
        *,
        mailto: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        venue_boosts: tuple[str, ...] = CHINESE_HUMANITIES_VENUES,
    ) -> None:
        super().__init__(
            source_id="crossref",
            http_client=http_client,
            rate_limiter=rate_limiter,
            min_interval_seconds=0.1,
            max_concurrency=5,
            backoff_seconds=1.0,
        )
        self._mailto = mailto if mailto is not None else get_settings().crossref_mailto
        self._venue_boosts = venue_boosts

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        params: dict[str, str | int] = {
            "query.bibliographic": query,
            "rows": limit,
        }
        venue_query = _matched_venue(query, self._venue_boosts)
        if venue_query is not None:
            params["query.container-title"] = venue_query
        if self._mailto:
            params["mailto"] = self._mailto
        year_range = resolve_year_range(year_window)
        if year_range is not None:
            params["filter"] = f"from-pub-date:{year_range[0]},until-pub-date:{year_range[1]}"
        data = await self._get_json(CROSSREF_WORKS_URL, params=params, query=query)
        message = data.get("message", {})
        items = message.get("items", []) if isinstance(message, dict) else []
        if not isinstance(items, list):
            return []
        return [source for item in items[:limit] if (source := self._parse_item(item)) is not None]

    def _parse_item(self, item: object) -> NormalizedSource | None:
        if not isinstance(item, dict):
            return None
        title = first_text(item.get("title"))
        doi = normalize_doi_value(item.get("DOI"))
        if title is None:
            return None
        authors = []
        for author in item.get("author", []):
            if not isinstance(author, dict):
                continue
            given = clean_text(author.get("given")) or ""
            family = clean_text(author.get("family")) or ""
            name = " ".join(part for part in (given, family) if part).strip()
            if name:
                authors.append(name)
        pdf_url = _pdf_link(item.get("link"))
        license_url = _license_url(item.get("license"))
        score = item.get("score")
        venue = first_text(item.get("container-title"))
        rank_score = float(score) if isinstance(score, int | float) else 0.0
        if _venue_matches(venue, self._venue_boosts):
            rank_score += CHINESE_VENUE_RANK_BOOST
        return NormalizedSource(
            source_id=f"crossref:{doi or clean_text(item.get('URL')) or title}",
            title=title,
            authors=authors,
            year=_issued_year(item.get("issued")),
            venue=venue,
            doi=doi,
            url=clean_text(item.get("URL")),
            pdf_url=pdf_url,
            abstract=_clean_abstract(item.get("abstract")),
            source_client=self.source_id,
            access_status=AccessStatus.OPEN if pdf_url else AccessStatus.METADATA_ONLY,
            license=license_url,
            rank_score=rank_score,
            risk_flags=[],
            verified_by="crossref",
        )


def _issued_year(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    date_parts = value.get("date-parts")
    if (
        isinstance(date_parts, list)
        and date_parts
        and isinstance(date_parts[0], list)
        and date_parts[0]
        and isinstance(date_parts[0][0], int)
    ):
        return date_parts[0][0]
    return None


def _pdf_link(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for link in value:
        if not isinstance(link, dict):
            continue
        content_type = clean_text(link.get("content-type")) or ""
        url = clean_text(link.get("URL"))
        if url and "pdf" in content_type.lower():
            return url
    return None


def _license_url(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    if isinstance(first, dict):
        return clean_text(first.get("URL"))
    return None


def _clean_abstract(value: object) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    return clean_text(re.sub(r"<[^>]+>", " ", text))


def _matched_venue(query: str, venues: tuple[str, ...]) -> str | None:
    normalized_query = _normalize_venue_text(query)
    for venue in venues:
        if _normalize_venue_text(venue) in normalized_query:
            return venue
    return None


def _venue_matches(venue: str | None, venues: tuple[str, ...]) -> bool:
    if venue is None:
        return False
    normalized = _normalize_venue_text(venue)
    return any(_normalize_venue_text(candidate) in normalized for candidate in venues)


def _normalize_venue_text(value: str) -> str:
    return re.sub(r"[《》\s]+", "", value).casefold()
