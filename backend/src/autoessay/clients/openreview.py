"""OpenReview API v2 client."""

from datetime import datetime, timezone
from typing import Any

import httpx

from autoessay.clients.common import (
    AccessStatus,
    AsyncLitClient,
    NormalizedSource,
    RateLimiter,
    clean_text,
    normalize_doi_value,
)

OPENREVIEW_SEARCH_URL = "https://api2.openreview.net/notes/search"


class OpenReviewClient(AsyncLitClient):
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(
            source_id="openreview",
            http_client=http_client,
            rate_limiter=rate_limiter,
            min_interval_seconds=1.0,
            max_concurrency=1,
            backoff_seconds=1.0,
        )

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        del year_window
        params: dict[str, str | int] = {
            "term": query,
            "limit": limit,
        }
        data = await self._get_json(OPENREVIEW_SEARCH_URL, params=params, query=query)
        notes = data.get("notes", [])
        if not isinstance(notes, list):
            return []
        return [source for note in notes[:limit] if (source := self._parse_note(note)) is not None]

    def _parse_note(self, note: object) -> NormalizedSource | None:
        if not isinstance(note, dict):
            return None
        note_id = clean_text(note.get("id"))
        if note_id is None:
            return None
        content = note.get("content", {})
        if not isinstance(content, dict):
            content = {}
        title = _content_value(content, "title")
        if title is None:
            return None
        authors = _content_list(content, "authors")
        forum = clean_text(note.get("forum")) or note_id
        pdf_path = _content_value(content, "pdf")
        pdf_url = _openreview_url(pdf_path) if pdf_path else None
        venue = _content_value(content, "venue") or _content_value(content, "venueid")
        doi = normalize_doi_value(_content_value(content, "doi"))
        return NormalizedSource(
            source_id=f"openreview:{note_id}",
            title=title,
            authors=authors,
            year=_year_from_timestamp(note.get("pdate")) or _year_from_timestamp(note.get("cdate")),
            venue=venue or "OpenReview",
            doi=doi,
            url=f"https://openreview.net/forum?id={forum}",
            pdf_url=pdf_url,
            abstract=_content_value(content, "abstract"),
            source_client=self.source_id,
            access_status=AccessStatus.OPEN if pdf_url else AccessStatus.METADATA_ONLY,
            license=clean_text(note.get("license")),
            rank_score=0.0,
            risk_flags=[],
        )


def _content_value(content: dict[str, Any], key: str) -> str | None:
    value = content.get(key)
    if isinstance(value, dict):
        return clean_text(value.get("value"))
    return clean_text(value)


def _content_list(content: dict[str, Any], key: str) -> list[str]:
    value = content.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    if not isinstance(value, list):
        return []
    return [item for raw in value if (item := clean_text(raw)) is not None]


def _year_from_timestamp(value: object) -> int | None:
    if not isinstance(value, int):
        return None
    # OpenReview stores millisecond timestamps.
    seconds = value / 1000 if value > 10_000_000_000 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc).year


def _openreview_url(path_or_url: str) -> str:
    if path_or_url.startswith("http"):
        return path_or_url
    if path_or_url.startswith("/"):
        return f"https://openreview.net{path_or_url}"
    return f"https://openreview.net/{path_or_url}"
