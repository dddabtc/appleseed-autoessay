"""Factory registry for configured literature source clients."""

from collections.abc import Mapping
from typing import Any

from autoessay.clients._stubs import StubLitClient
from autoessay.clients.arxiv import ArxivClient
from autoessay.clients.cnki import CNKIClient
from autoessay.clients.common import AsyncLitClient, NormalizedSource
from autoessay.clients.crossref import CrossrefClient
from autoessay.clients.openalex import DEFAULT_OPENALEX_FILTER, OpenAlexClient
from autoessay.clients.openreview import OpenReviewClient
from autoessay.clients.semantic_scholar import SemanticScholarClient
from autoessay.clients.wikipedia_zh import WikipediaZhClient
from autoessay.config import get_settings

AUTOMATED_SOURCE_IDS = frozenset(
    {
        "arxiv",
        "semantic_scholar",
        "openalex",
        "crossref",
        "openreview",
        "cnki",
        "wikipedia_zh",
    },
)
MANUAL_SOURCE_IDS = frozenset({"ssrn_manual"})
VALID_SOURCE_IDS = AUTOMATED_SOURCE_IDS | MANUAL_SOURCE_IDS


class ManualLaneClient(AsyncLitClient):
    automated = False

    def __init__(self, source_id: str) -> None:
        super().__init__(source_id=source_id, min_interval_seconds=0.0, max_concurrency=1)

    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        del query, year_window, limit
        return []


def scout_stub_enabled() -> bool:
    return get_settings().scout_stub


def get_lit_client(
    source_id: str,
    source_config: Mapping[str, Any] | None = None,
    domain_config: Mapping[str, Any] | None = None,
) -> AsyncLitClient:
    if source_id not in VALID_SOURCE_IDS:
        raise KeyError(f"unknown literature source_id: {source_id}")
    if scout_stub_enabled():
        return StubLitClient(source_id)
    if source_id == "arxiv":
        return ArxivClient(categories=_arxiv_categories(source_config, domain_config))
    if source_id == "semantic_scholar":
        return SemanticScholarClient()
    if source_id == "openalex":
        return OpenAlexClient(
            filters=_openalex_filters(source_config),
            domain_id=_openalex_domain_id(source_config, domain_config),
        )
    if source_id == "crossref":
        return CrossrefClient()
    if source_id == "openreview":
        return OpenReviewClient()
    if source_id == "cnki":
        return CNKIClient()
    if source_id == "wikipedia_zh":
        return WikipediaZhClient()
    return ManualLaneClient(source_id)


def _arxiv_categories(
    source_config: Mapping[str, Any] | None,
    domain_config: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    raw = None
    if source_config is not None:
        raw = source_config.get("arxiv_categories")
    if raw is None and domain_config is not None:
        search = domain_config.get("search", {})
        if isinstance(search, Mapping):
            raw = search.get("arxiv_categories")
        if raw is None:
            raw = domain_config.get("arxiv_categories")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


def _openalex_filters(source_config: Mapping[str, Any] | None) -> str | tuple[str, ...] | None:
    if source_config is None:
        return DEFAULT_OPENALEX_FILTER
    has_filter_key = "filter" in source_config
    has_filters_key = "filters" in source_config
    raw = source_config.get("filter") if has_filter_key else None
    if raw is None and has_filters_key:
        raw = source_config.get("filters")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return tuple(item for item in raw if isinstance(item, str) and item)
    return DEFAULT_OPENALEX_FILTER


def _openalex_domain_id(
    source_config: Mapping[str, Any] | None,
    domain_config: Mapping[str, Any] | None,
) -> str | None:
    if source_config is not None and source_config.get("topic_filter") is False:
        return None
    return _domain_id(domain_config)


def _domain_id(domain_config: Mapping[str, Any] | None) -> str | None:
    if domain_config is None:
        return None
    raw = domain_config.get("id")
    return raw if isinstance(raw, str) and raw else None
