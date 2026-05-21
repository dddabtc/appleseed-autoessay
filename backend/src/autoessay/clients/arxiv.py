"""arXiv Atom API client."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable

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

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


class ArxivClient(AsyncLitClient):
    def __init__(
        self,
        *,
        categories: Iterable[str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(
            source_id="arxiv",
            http_client=http_client,
            rate_limiter=rate_limiter,
            min_interval_seconds=3.0,
            max_concurrency=1,
            backoff_seconds=3.0,
        )
        self._categories = tuple(categories or ())

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        params: dict[str, str | int] = {
            "search_query": self._build_query(query, year_window),
            "start": 0,
            "max_results": limit,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        text = await self._get_text(ARXIV_API_URL, params=params, query=query)
        return self._parse_entries(text, limit)

    def _build_query(self, query: str, year_window: int | tuple[int, int] | None) -> str:
        parts = [f'all:"{query}"']
        if self._categories:
            category_query = " OR ".join(f"cat:{category}" for category in self._categories)
            parts.append(f"({category_query})")
        year_range = resolve_year_range(year_window)
        if year_range is not None:
            start_year, end_year = year_range
            parts.append(f"submittedDate:[{start_year}01010000 TO {end_year}12312359]")
        return " AND ".join(parts)

    def _parse_entries(self, text: str, limit: int) -> list[NormalizedSource]:
        root = ET.fromstring(text)
        results: list[NormalizedSource] = []
        for entry in root.findall(f"{ATOM_NS}entry"):
            categories = [
                category.attrib.get("term", "") for category in entry.findall(f"{ATOM_NS}category")
            ]
            category_matches = any(category in self._categories for category in categories)
            if self._categories and not category_matches:
                continue
            source = self._parse_entry(entry)
            if source is not None:
                results.append(source)
            if len(results) >= limit:
                break
        return results

    def _parse_entry(self, entry: ET.Element) -> NormalizedSource | None:
        title = clean_text(_child_text(entry, "title"))
        source_url = clean_text(_child_text(entry, "id"))
        if title is None or source_url is None:
            return None
        authors = [
            author_name
            for author in entry.findall(f"{ATOM_NS}author")
            if (author_name := clean_text(_child_text(author, "name"))) is not None
        ]
        pdf_url: str | None = None
        landing_url = source_url
        for link in entry.findall(f"{ATOM_NS}link"):
            href = link.attrib.get("href")
            if not href:
                continue
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = href
            if link.attrib.get("rel") == "alternate":
                landing_url = href
        published = clean_text(_child_text(entry, "published"))
        year = _year_from_date(published)
        arxiv_id = source_url.rstrip("/").rsplit("/", 1)[-1]
        return NormalizedSource(
            source_id=f"arxiv:{arxiv_id}",
            title=title,
            authors=authors,
            year=year,
            venue="arXiv",
            doi=normalize_doi_value(_child_text(entry, f"{ARXIV_NS}doi")),
            url=landing_url,
            pdf_url=pdf_url,
            abstract=clean_text(_child_text(entry, "summary")),
            source_client=self.source_id,
            access_status=AccessStatus.OPEN if pdf_url else AccessStatus.METADATA_ONLY,
            license=clean_text(_child_text(entry, f"{ARXIV_NS}license")),
            rank_score=0.0,
            risk_flags=[],
        )


def _child_text(entry: ET.Element, child: str) -> str | None:
    found = entry.find(child) if child.startswith("{") else entry.find(f"{ATOM_NS}{child}")
    return found.text if found is not None else None


def _year_from_date(value: str | None) -> int | None:
    if value is None or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None
