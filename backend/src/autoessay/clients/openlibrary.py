"""OpenLibrary book metadata client (PR-263b).

Used by ``source_enrichment`` to verify shadow-baseline candidates
that carry an ISBN-13 instead of a Crossref-style DOI. Real-paper
shadow-baseline runs on Chinese-humanities kernels show ~100% of
LLM-emitted reference candidates are 9787-prefix mainland books
that Crossref+OpenAlex don't index — codex round-2 verdict on
PR-263b (D + A-lite): add OpenLibrary as the third verifier so
those classic monographs can enter ``cited_sources`` with
``access_status=METADATA_ONLY`` after a real third-party lookup.

OpenLibrary is free, no API key, JSON-only, ~1 req/s polite limit.
We only use the ``api/books?bibkeys=ISBN:{isbn}`` endpoint —
single-shot lookup per ISBN, no search / index queries.

Coverage realism: OpenLibrary's Chinese-book coverage is partial
(estimated 30-60% hit rate per the codex review). The miss path
falls through to source_enrichment's existing ``drop_warnings``;
PR-263c will add a fallback "candidate appendix" so even unverified
candidates surface to the user as suggested-but-unverified
references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from autoessay.clients.common import (
    AccessStatus,
    NormalizedSource,
    RateLimiter,
)

OPENLIBRARY_BOOKS_URL = "https://openlibrary.org/api/books"
DEFAULT_TIMEOUT_SECONDS = 8.0
# OpenLibrary asks for ≤100 req/min sustained; we run far below that
# so 1 req / 0.6s is safe.
DEFAULT_RATE_LIMITER = RateLimiter(
    min_interval_seconds=0.6,
    max_concurrency=2,
)


@dataclass(frozen=True)
class OpenLibraryBookMetadata:
    """Subset of the OpenLibrary book record that maps cleanly into
    ``NormalizedSource``. Fields beyond these (cover URLs, table of
    contents, classifications) are dropped — they're not used by the
    drafter / citation-format pipeline."""

    isbn: str
    title: str
    authors: list[str]
    publisher: str | None
    publish_year: int | None
    url: str | None  # canonical OpenLibrary work URL


def _normalize_isbn(raw: str) -> str | None:
    """Strip non-digit chars + Xx suffix; reject anything that
    doesn't look like a 10- or 13-digit ISBN. Returns the
    cleaned form OR None when the input is unusable."""
    if not raw:
        return None
    cleaned = re.sub(r"[^0-9Xx]", "", raw).upper()
    if len(cleaned) not in (10, 13):
        return None
    if len(cleaned) == 13 and not cleaned.isdigit():
        return None
    return cleaned


def _extract_publish_year(record: dict[str, Any]) -> int | None:
    """OpenLibrary returns ``publish_date`` as a free-form string
    (``"2011"`` / ``"May 2011"`` / ``"2011-05-01"`` / ``"二〇一一"``).
    Pull a 4-digit year if one is present."""
    raw = record.get("publish_date")
    if not isinstance(raw, str):
        return None
    match = re.search(r"\b(1[5-9]\d\d|20\d\d|21\d\d)\b", raw)
    if not match:
        return None
    return int(match.group(1))


def _extract_authors(record: dict[str, Any]) -> list[str]:
    """OpenLibrary returns authors under ``authors`` (list of dicts
    with ``name`` key) OR under ``by_statement`` as free text. Prefer
    the structured ``authors`` field."""
    authors_raw = record.get("authors")
    if isinstance(authors_raw, list):
        names: list[str] = []
        for entry in authors_raw:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
        if names:
            return names
    by_statement = record.get("by_statement")
    if isinstance(by_statement, str) and by_statement.strip():
        return [by_statement.strip()]
    return []


def _extract_publisher(record: dict[str, Any]) -> str | None:
    """``publishers`` is a list of dicts with ``name``; take the first."""
    raw = record.get("publishers")
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def parse_openlibrary_response(
    isbn: str,
    payload: dict[str, Any],
) -> OpenLibraryBookMetadata | None:
    """Pluck a single ISBN's record out of the
    ``{"ISBN:9787...": {...record...}}`` envelope. Returns None when
    the ISBN isn't in the payload OR when title is missing
    (title-less hits are useless for citation rendering)."""
    key = f"ISBN:{isbn}"
    record = payload.get(key)
    if not isinstance(record, dict):
        return None
    title_raw = record.get("title")
    if not isinstance(title_raw, str) or not title_raw.strip():
        return None
    return OpenLibraryBookMetadata(
        isbn=isbn,
        title=title_raw.strip(),
        authors=_extract_authors(record),
        publisher=_extract_publisher(record),
        publish_year=_extract_publish_year(record),
        url=record.get("url") if isinstance(record.get("url"), str) else None,
    )


class OpenLibraryClient:
    """Thin async wrapper around OpenLibrary's books-by-ISBN endpoint."""

    source_id = "openlibrary"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._http = http_client
        self._owns_http = http_client is None
        self._rate_limiter = rate_limiter or DEFAULT_RATE_LIMITER
        self._timeout = timeout_seconds

    async def lookup_isbn(self, isbn: str) -> OpenLibraryBookMetadata | None:
        """Single-ISBN lookup. Returns ``None`` on:
        - malformed input ISBN
        - HTTP error (network / 4xx / 5xx)
        - OpenLibrary returned empty payload
        - record present but title missing

        Rate-limited via the shared limiter (default 0.6s between
        requests, max 2 concurrent)."""
        normalized = _normalize_isbn(isbn)
        if normalized is None:
            return None

        await self._rate_limiter.wait()
        async with self._rate_limiter.semaphore:
            client = self._http or httpx.AsyncClient(timeout=self._timeout)
            close_client = self._http is None
            try:
                response = await client.get(
                    OPENLIBRARY_BOOKS_URL,
                    params={
                        "bibkeys": f"ISBN:{normalized}",
                        "format": "json",
                        "jscmd": "data",
                    },
                )
            except httpx.HTTPError:
                return None
            finally:
                if close_client:
                    await client.aclose()

            if response.status_code != 200:
                return None
            try:
                payload = response.json()
            except ValueError:
                return None
            if not isinstance(payload, dict):
                return None
            return parse_openlibrary_response(normalized, payload)

    async def aclose(self) -> None:
        """Close the inner http client when we own it. Safe to call
        even when an injected client is in use (no-op)."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()


def metadata_to_normalized_source(
    metadata: OpenLibraryBookMetadata,
    *,
    canonical_bucket: str | None = None,
    canonical_rationale: str | None = None,
) -> NormalizedSource:
    """Lift an ``OpenLibraryBookMetadata`` into the pipeline's
    ``NormalizedSource`` shape so it can feed downstream phases.

    Defaults applied:
    - ``source_id`` = ``"openlibrary:isbn-{isbn}"``
    - ``source_client`` = ``"openlibrary"`` (the verifier)
    - ``access_status`` = ``METADATA_ONLY`` (we have the record but
      no PDF / abstract)
    - ``provenance`` = ``"llm_canon"`` (came in via shadow baseline)
    - ``verified_by`` = ``"openlibrary"`` (third-party verification
      audit trail per PR-J9b ``verified_by`` semantics)
    - ``risk_flags`` includes ``"metadata_only_no_full_text"`` so
      drafter / synthesizer downstream know to weight cautiously
    """
    return NormalizedSource(
        source_id=f"openlibrary:isbn-{metadata.isbn}",
        title=metadata.title,
        authors=metadata.authors,
        year=metadata.publish_year,
        venue=metadata.publisher,
        doi=None,
        url=metadata.url,
        pdf_url=None,
        abstract=None,
        source_client="openlibrary",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        risk_flags=["metadata_only_no_full_text"],
        provenance="llm_canon",
        canonical_bucket=canonical_bucket,
        canonical_rationale=canonical_rationale,
        verified_by="openlibrary",
    )


__all__ = [
    "OPENLIBRARY_BOOKS_URL",
    "OpenLibraryBookMetadata",
    "OpenLibraryClient",
    "metadata_to_normalized_source",
    "parse_openlibrary_response",
]
