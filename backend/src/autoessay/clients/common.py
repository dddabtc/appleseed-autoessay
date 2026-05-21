"""Shared models and rate-limited HTTP helpers for literature clients."""

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field


class AccessStatus(str, Enum):
    OPEN = "open"
    METADATA_ONLY = "metadata_only"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    PENDING = "pending"
    DISPUTED = "disputed"


class NormalizedSource(BaseModel):
    source_id: str
    title: str
    authors: list[str]
    year: int | None
    venue: str | None
    doi: str | None
    url: str | None
    pdf_url: str | None
    abstract: str | None
    source_client: str
    access_status: AccessStatus
    license: str | None
    rank_score: float = Field(default=0.0)
    risk_flags: list[str]
    # PR-C1.a research_role: per-run-context tier. Mirrors the
    # SourceRecord column so downstream code (synthesizer dual-track,
    # frontend Sources tab badge in C1.b) can read it from the
    # shortlist payload directly without joining DB.
    research_role: str = Field(default="secondary_argument")
    # PR-J9: scout-side provenance tag. ``"search"`` = vendor lit
    # client (OpenAlex / Crossref / etc); ``"llm_canon"`` = LLM
    # mining surface (verified via Crossref / OpenAlex roundtrip).
    # ``source_client`` keeps the ACTUAL verifier (codex round-1
    # amendment 3.3 — don't pollute source_client with synthetic
    # values; downstream weighting / diversity reranks read it).
    provenance: str = Field(default="search")
    # PR-J9: when provenance="llm_canon", which bucket the LLM put it
    # in. ``"consensus"`` = top-cited canon; ``"disagreement"`` = one
    # side of a major scholarly debate; ``"frontier"`` = recent
    # active direction. None for vendor-search sources.
    canonical_bucket: str | None = Field(default=None)
    # PR-J9: short LLM rationale (≤200 chars) explaining why the
    # canonical surface picked this work; surfaced to UI + ideator
    # via shortlist.json for the user to evaluate canon claims.
    canonical_rationale: str | None = Field(default=None)
    # PR-J9b: which verifier confirmed the canonical claim, if any.
    # ``"crossref"`` (default for J9 v1 path) / ``"openalex"`` (J9b
    # OpenAlex monograph fallback) / None (search-provenance sources
    # or unverified). codex round-1 A6: keep alongside source_client
    # — source_client is the actual metadata vendor, verified_by is
    # the canonical-mining audit trail.
    verified_by: str | None = Field(default=None)
    # PR-J9b: 4-axis LLM rerank breakdown from curator. Keys:
    # ``scope_fit`` (35%) / ``relevance`` (25%) / ``impact`` (25%) /
    # ``frontier_currency`` (15%). None when the run fell back to the
    # legacy single-axis path (rerank stub on or 4-axis LLM error).
    # codex round-1 A4: persist on NormalizedSource so shortlist.json
    # carries per-source audit; A2: never fed back into the rerank
    # prompt (would create confirmation bias).
    rerank_axes: dict[str, float] | None = Field(default=None)
    # PR-J9b: ≤200-char rationale from the rerank LLM, parallel to
    # canonical_rationale. UI/ideator can surface but must not feed
    # back into rerank prompt (codex round-1 A2).
    rerank_rationale: str | None = Field(default=None)
    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    class Config:
        use_enum_values = True


class ClientSearchError(RuntimeError):
    def __init__(self, source_id: str, query: str, message: str) -> None:
        super().__init__(f"{source_id} search failed for {query!r}: {message}")
        self.source_id = source_id
        self.query = query


class RateLimiter:
    def __init__(self, *, min_interval_seconds: float, max_concurrency: int) -> None:
        self._min_interval_seconds = min_interval_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._last_request_at: float | None = None

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    async def wait(self) -> None:
        if self._min_interval_seconds <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._last_request_at is not None:
                elapsed = now - self._last_request_at
                remaining = self._min_interval_seconds - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            self._last_request_at = time.monotonic()


class AsyncLitClient(ABC):
    source_id: str
    automated: bool = True

    def __init__(
        self,
        *,
        source_id: str,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        min_interval_seconds: float = 0.0,
        max_concurrency: int = 1,
        backoff_seconds: float = 0.5,
    ) -> None:
        self.source_id = source_id
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._rate_limiter = rate_limiter or RateLimiter(
            min_interval_seconds=min_interval_seconds,
            max_concurrency=max_concurrency,
        )
        self._backoff_seconds = backoff_seconds

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @abstractmethod
    async def search(
        self,
        query: str,
        year_window: int | tuple[int, int] | None,
        limit: int,
    ) -> list[NormalizedSource]:
        raise NotImplementedError

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
        query: str,
        retries: int = 1,
    ) -> dict[str, Any]:
        response = await self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            query=query,
            retries=retries,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise ClientSearchError(self.source_id, query, "response JSON was not an object")
        return data

    async def _get_text(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | float] | None = None,
        headers: Mapping[str, str] | None = None,
        query: str,
        retries: int = 1,
    ) -> str:
        response = await self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            query=query,
            retries=retries,
        )
        return response.text

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int | float] | None,
        headers: Mapping[str, str] | None,
        query: str,
        retries: int,
    ) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(retries + 1):
            try:
                async with self._rate_limiter.semaphore:
                    await self._rate_limiter.wait()
                    response = await self._client.request(
                        method,
                        url,
                        params=params,
                        headers=headers,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < retries:
                        await asyncio.sleep(self._backoff_seconds * (2**attempt))
                        continue
                    raise ClientSearchError(
                        self.source_id,
                        query,
                        f"HTTP {response.status_code}",
                    )
                response.raise_for_status()
                return response
            except httpx.TransportError as exc:
                if attempt < retries:
                    await asyncio.sleep(self._backoff_seconds * (2**attempt))
                    continue
                raise ClientSearchError(self.source_id, query, str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                raise ClientSearchError(self.source_id, query, str(exc)) from exc
        if response is not None:
            raise ClientSearchError(self.source_id, query, f"HTTP {response.status_code}")
        raise ClientSearchError(self.source_id, query, "request failed without response")


def resolve_year_range(year_window: int | tuple[int, int] | None) -> tuple[int, int] | None:
    if year_window is None:
        return None
    if isinstance(year_window, tuple):
        if len(year_window) != 2:
            return None
        return year_window
    if year_window <= 0:
        return None
    current_year = time.gmtime().tm_year
    return current_year - year_window, current_year


def clean_text(value: object) -> str | None:
    if isinstance(value, str):
        cleaned = " ".join(value.split())
        return cleaned or None
    return None


def first_text(value: object) -> str | None:
    if isinstance(value, list) and value:
        return clean_text(value[0])
    return clean_text(value)


def normalize_doi_value(value: object) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    lowered = text.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip().lower()
    return text.lower()
