"""CNKI local scraper API client."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

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


class CNKIClient(AsyncLitClient):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(
            source_id="cnki",
            http_client=http_client,
            rate_limiter=rate_limiter,
            min_interval_seconds=0.2,
            max_concurrency=2,
            backoff_seconds=1.0,
        )
        self._base_url = base_url if base_url is not None else get_settings().cnki_api_base_url

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        if get_settings().cnki_stub:
            return _stub_sources(query)[:limit]

        params: dict[str, str | int] = {
            "q": query,
            "query": query,
            "limit": limit,
        }
        year_range = resolve_year_range(year_window)
        if year_range is not None:
            params["start_year"] = year_range[0]
            params["end_year"] = year_range[1]
        data = await self._get_json(self._base_url, params=params, query=query)
        records = _records_from_response(data)
        return [
            source for item in records[:limit] if (source := self._parse_item(item)) is not None
        ]

    def _parse_item(self, item: object) -> NormalizedSource | None:
        if not isinstance(item, dict):
            return None
        title = _first_text_from_keys(item, ("title", "篇名", "name"))
        if title is None:
            return None
        raw_id = _first_text_from_keys(item, ("source_id", "id", "cnki_id", "filename", "url"))
        source_id = _source_id(raw_id, title)
        pdf_url = _first_text_from_keys(item, ("pdf_url", "download_url", "pdf", "fulltext_url"))
        score = _number_value(item, ("score", "rank_score", "relevance", "similarity"))
        return NormalizedSource(
            source_id=source_id,
            title=title,
            authors=_authors(item.get("authors") or item.get("author") or item.get("作者")),
            year=_year(item.get("year") or item.get("publish_year") or item.get("年份")),
            venue=_first_text_from_keys(item, ("venue", "journal", "source", "刊名")),
            doi=normalize_doi_value(item.get("doi") or item.get("DOI")),
            url=_first_text_from_keys(item, ("url", "link", "detail_url")),
            pdf_url=pdf_url,
            abstract=_first_text_from_keys(item, ("abstract", "summary", "摘要")),
            source_client=self.source_id,
            access_status=AccessStatus.OPEN if pdf_url else AccessStatus.METADATA_ONLY,
            license=_first_text_from_keys(item, ("license", "licence")),
            rank_score=score or 0.0,
            risk_flags=[],
        )


def _records_from_response(data: dict[str, Any]) -> list[object]:
    for key in ("data", "results", "items", "records"):
        value = data.get(key)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            nested = value.get("items") or value.get("records") or value.get("results")
            if isinstance(nested, list):
                return list(nested)
    return []


def _first_text_from_keys(item: dict[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        text = clean_text(item.get(key))
        if text is not None:
            return text
    return None


def _authors(value: object) -> list[str]:
    if isinstance(value, list):
        authors = [clean_text(item) for item in value]
        return [author for author in authors if author is not None]
    text = clean_text(value)
    if text is None:
        return []
    return [
        part.strip()
        for part in text.replace("；", ";").replace("，", ";").split(";")
        if part.strip()
    ]


def _year(value: object) -> int | None:
    if isinstance(value, int):
        return value
    text = clean_text(value)
    if text is None or len(text) < 4:
        return None
    prefix = text[:4]
    return int(prefix) if prefix.isdigit() else None


def _number_value(item: dict[str, object], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, int | float):
            return float(value)
        text = clean_text(value)
        if text is None:
            continue
        try:
            return float(text)
        except ValueError:
            continue
    return None


def _source_id(raw_id: str | None, title: str) -> str:
    value = raw_id or title
    if value.startswith("cnki:"):
        return value
    return f"cnki:{value}"


def _stub_sources(query: str) -> list[NormalizedSource]:
    return [
        NormalizedSource(
            source_id="cnki:stub-modern-banking-history",
            title="近代银行业危机与信用市场调整研究",
            authors=["李明", "王晓"],
            year=2021,
            venue="中国经济史研究",
            doi=None,
            url="https://kns.cnki.net/kcms/detail/stub-modern-banking-history.html",
            pdf_url=None,
            abstract=f"围绕“{query}”的中文文献存根，讨论银行危机、信用收缩与制度回应。",
            source_client="cnki",
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=0.88,
            risk_flags=["cnki_stub"],
        ),
        NormalizedSource(
            source_id="cnki:stub-financial-institutions",
            title="金融制度变迁中的区域信贷网络",
            authors=["陈静"],
            year=2019,
            venue="财经研究",
            doi=None,
            url="https://kns.cnki.net/kcms/detail/stub-financial-institutions.html",
            pdf_url=None,
            abstract="中文文献存根，覆盖区域信贷网络、制度变迁与金融史研究线索。",
            source_client="cnki",
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=0.75,
            risk_flags=["cnki_stub"],
        ),
    ]
