"""OpenAlex Works API client."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from autoessay.clients.common import (
    AccessStatus,
    AsyncLitClient,
    ClientSearchError,
    NormalizedSource,
    RateLimiter,
    clean_text,
    normalize_doi_value,
    resolve_year_range,
)
from autoessay.config import get_settings

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
DEFAULT_OPENALEX_FILTER = "publication_year:>2018,is_oa:true,type:article"
DEFAULT_PER_PAGE = 25
_HUMANITIES_TOPIC_IDS: dict[str, tuple[str, ...]] = {
    # Topic IDs sampled from OpenAlex /topics on 2026-05-08. Keep these broad and
    # domain-level; topic taxonomy can drift, so query text still carries precision.
    "history": ("T13938", "T12454", "T10893"),
    "literature": ("T12678", "T11893", "T12130"),
    "philosophy": ("T11463", "T10778", "T10718"),
    "economic_history": ("T14094", "T13897", "T13158"),
    "financial_history": ("T14094", "T13897", "T13158"),
}
_LEGACY_CONCEPT_IDS: dict[str, tuple[str, ...]] = {
    "history": ("C95457728",),
    "literature": ("C124952713",),
    "philosophy": ("C138885662",),
    "economic_history": ("C162324750", "C95457728"),
    "financial_history": ("C162324750", "C95457728"),
}


class OpenAlexClient(AsyncLitClient):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        mailto: str | None = None,
        filters: str | Iterable[str] | None = DEFAULT_OPENALEX_FILTER,
        domain_id: str | None = None,
        topic_ids: Iterable[str] | None = None,
        legacy_concept_ids: Iterable[str] | None = None,
        per_page: int = DEFAULT_PER_PAGE,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(
            source_id="openalex",
            http_client=http_client,
            rate_limiter=rate_limiter,
            min_interval_seconds=0.1,
            max_concurrency=5,
            backoff_seconds=1.0,
        )
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.openalex_api_key
        self._mailto = mailto if mailto is not None else settings.openalex_mailto
        self._filters = _normalize_filters(filters)
        self._domain_id = domain_id
        self._topic_ids = (
            _normalize_openalex_ids(topic_ids)
            if topic_ids is not None
            else _topic_ids_for_domain(domain_id)
        )
        self._legacy_concept_ids = (
            _normalize_openalex_ids(legacy_concept_ids)
            if legacy_concept_ids is not None
            else _legacy_concept_ids_for_domain(domain_id)
        )
        self._per_page = max(1, min(per_page, 100))

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        if limit <= 0:
            return []
        if get_settings().openalex_stub:
            return _stub_sources(query)[:limit]

        results: list[NormalizedSource] = []
        cursor: str | None = "*"
        while cursor is not None and len(results) < limit:
            try:
                params = self._search_params(query, year_window, limit - len(results), cursor)
                data = await self._get_json(
                    OPENALEX_WORKS_URL,
                    params=params,
                    query=query,
                    retries=2,
                )
            except ClientSearchError:
                if not self._legacy_concept_ids:
                    raise
                params = self._search_params(
                    query,
                    year_window,
                    limit - len(results),
                    cursor,
                    use_legacy_concepts=True,
                )
                data = await self._get_json(
                    OPENALEX_WORKS_URL,
                    params=params,
                    query=query,
                    retries=2,
                )
            records = data.get("results", [])
            if not isinstance(records, list) or not records:
                break
            for item in records:
                source = self._parse_item(item)
                if source is not None:
                    results.append(source)
                if len(results) >= limit:
                    break
            cursor = _next_cursor(data)
        return results

    def _search_params(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        remaining: int,
        cursor: str,
        *,
        use_legacy_concepts: bool = False,
    ) -> dict[str, str | int]:
        params: dict[str, str | int] = {
            "search": query,
            "per_page": min(self._per_page, max(1, remaining)),
            "cursor": cursor,
        }
        filters = _filters_with_humanities_topics(
            self._filters,
            self._topic_ids,
            self._legacy_concept_ids,
            use_legacy_concepts=use_legacy_concepts,
        )
        filters = _filters_with_year_window(filters, year_window)
        if filters:
            params["filter"] = filters
        if self._api_key:
            params["api_key"] = self._api_key
        elif self._mailto:
            params["mailto"] = self._mailto
        return params

    def _parse_item(self, item: object) -> NormalizedSource | None:
        if not isinstance(item, dict):
            return None
        openalex_id = clean_text(item.get("id"))
        title = clean_text(item.get("title") or item.get("display_name"))
        if openalex_id is None or title is None:
            return None
        primary_location = item.get("primary_location")
        primary_location_dict = primary_location if isinstance(primary_location, dict) else {}
        open_access = item.get("open_access")
        open_access_dict = open_access if isinstance(open_access, dict) else {}
        pdf_url = clean_text(primary_location_dict.get("pdf_url"))
        is_oa = _is_open_access(item, primary_location_dict)
        year = (
            item.get("publication_year") if isinstance(item.get("publication_year"), int) else None
        )
        return NormalizedSource(
            source_id=openalex_id,
            title=title,
            authors=_authors(item.get("authorships")),
            year=year,
            venue=_venue(primary_location_dict),
            doi=normalize_doi_value(item.get("doi")),
            url=_best_landing_url(openalex_id, primary_location_dict, open_access_dict),
            pdf_url=pdf_url,
            abstract=_abstract_from_inverted_index(item.get("abstract_inverted_index")),
            source_client=self.source_id,
            access_status=AccessStatus.OPEN if is_oa else AccessStatus.METADATA_ONLY,
            license=clean_text(primary_location_dict.get("license")),
            rank_score=0.0,
            risk_flags=[],
            verified_by="openalex",
        )


def _normalize_filters(filters: str | Iterable[str] | None) -> str | None:
    if filters is None:
        return None
    if isinstance(filters, str):
        return clean_text(filters)
    parts = [clean_text(item) for item in filters]
    cleaned = [part for part in parts if part is not None]
    return ",".join(cleaned) if cleaned else None


def _best_landing_url(
    openalex_id: str,
    primary_location: dict[str, object],
    open_access: dict[str, object],
) -> str:
    return (
        clean_text(primary_location.get("landing_page_url"))
        or clean_text(open_access.get("oa_url"))
        or openalex_id
    )


def _filters_with_humanities_topics(
    filters: str | None,
    topic_ids: tuple[str, ...],
    legacy_concept_ids: tuple[str, ...],
    *,
    use_legacy_concepts: bool,
) -> str | None:
    parts: list[str] = []
    if filters:
        parts.append(filters)
    if use_legacy_concepts:
        if legacy_concept_ids:
            parts.append(_openalex_or_filter("concepts.id", legacy_concept_ids))
    elif topic_ids:
        parts.append(_openalex_or_filter("primary_topic.id", topic_ids))
    return ",".join(parts) if parts else None


def _openalex_or_filter(attribute: str, ids: Iterable[str]) -> str:
    return f"{attribute}:{'|'.join(_normalize_openalex_ids(ids))}"


def _topic_ids_for_domain(domain_id: str | None) -> tuple[str, ...]:
    if not domain_id:
        return ()
    return _HUMANITIES_TOPIC_IDS.get(domain_id, ())


def _legacy_concept_ids_for_domain(domain_id: str | None) -> tuple[str, ...]:
    if not domain_id:
        return ()
    return _LEGACY_CONCEPT_IDS.get(domain_id, ())


def _normalize_openalex_ids(ids: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        if not isinstance(raw, str):
            continue
        key = raw.rsplit("/", 1)[-1].strip()
        if not key:
            continue
        key = key.upper()
        if key not in seen:
            seen.add(key)
            normalized.append(key)
    return tuple(normalized)


def _filters_with_year_window(
    filters: str | None,
    year_window: int | tuple[int, int] | None,
) -> str | None:
    parts: list[str] = []
    if filters:
        parts.append(filters)
    year_range = resolve_year_range(year_window)
    if year_range is not None and not _has_publication_filter(filters):
        start_year, end_year = year_range
        parts.append(f"from_publication_date:{start_year}-01-01")
        parts.append(f"to_publication_date:{end_year}-12-31")
    return ",".join(parts) if parts else None


def _has_publication_filter(filters: str | None) -> bool:
    if filters is None:
        return False
    names = [part.split(":", 1)[0].strip() for part in filters.split(",")]
    return any(
        name in {"publication_year", "from_publication_date", "to_publication_date"}
        for name in names
    )


def _next_cursor(data: dict[str, Any]) -> str | None:
    meta = data.get("meta", {})
    if not isinstance(meta, dict):
        return None
    cursor = meta.get("next_cursor")
    return cursor if isinstance(cursor, str) and cursor else None


def _authors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    authors: list[str] = []
    for authorship in value:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        if not isinstance(author, dict):
            continue
        name = clean_text(author.get("display_name"))
        if name is not None:
            authors.append(name)
    return authors


def _venue(primary_location: dict[str, object]) -> str | None:
    source = primary_location.get("source")
    if not isinstance(source, dict):
        return None
    return clean_text(source.get("display_name"))


def _is_open_access(item: dict[str, object], primary_location: dict[str, object]) -> bool:
    if item.get("is_oa") is True or primary_location.get("is_oa") is True:
        return True
    open_access = item.get("open_access")
    return isinstance(open_access, dict) and open_access.get("is_oa") is True


def _abstract_from_inverted_index(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    positioned_words: dict[int, str] = {}
    for raw_word, raw_positions in value.items():
        if not isinstance(raw_word, str) or not isinstance(raw_positions, list):
            continue
        word = clean_text(raw_word)
        if word is None:
            continue
        for position in raw_positions:
            if isinstance(position, int) and position >= 0:
                positioned_words[position] = word
    if not positioned_words:
        return None
    return clean_text(" ".join(positioned_words[index] for index in sorted(positioned_words)))


def _stub_sources(query: str) -> list[NormalizedSource]:
    return [
        NormalizedSource(
            source_id="https://openalex.org/Wstub-financial-crises",
            title="Banking Panics and Credit Market Adjustment in Historical Perspective",
            authors=["Jane Historical", "A. Credit"],
            year=2023,
            venue="Financial History Review",
            doi="10.5555/openalex.stub.1",
            url="https://openalex.org/Wstub-financial-crises",
            pdf_url="https://example.test/openalex/financial-crises.pdf",
            abstract=(
                f"OpenAlex stub result for {query}, centered on banking panics and credit markets."
            ),
            source_client="openalex",
            access_status=AccessStatus.OPEN,
            license="cc-by",
            rank_score=0.0,
            risk_flags=[],
        ),
        NormalizedSource(
            source_id="https://openalex.org/Wstub-monetary-history",
            title="Monetary Institutions and Crisis Response across Economic History",
            authors=["R. Monetary"],
            year=2021,
            venue="Journal of Economic History",
            doi="10.5555/openalex.stub.2",
            url="https://openalex.org/Wstub-monetary-history",
            pdf_url=None,
            abstract=(
                "OpenAlex stub result covering monetary institutions, crises, "
                "and historical evidence."
            ),
            source_client="openalex",
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=0.0,
            risk_flags=[],
        ),
        NormalizedSource(
            source_id="https://openalex.org/Wstub-long-run-credit",
            title="Long-Run Credit Networks and Financial Instability",
            authors=["L. Network"],
            year=2020,
            venue="Economic History Review",
            doi="10.5555/openalex.stub.3",
            url="https://openalex.org/Wstub-long-run-credit",
            pdf_url="https://example.test/openalex/long-run-credit.pdf",
            abstract="OpenAlex stub result for long-run credit networks and financial instability.",
            source_client="openalex",
            access_status=AccessStatus.OPEN,
            license="cc-by-nc",
            rank_score=0.0,
            risk_flags=[],
        ),
    ]
