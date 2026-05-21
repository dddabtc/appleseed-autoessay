"""Cross-source duplicate detection for literature records."""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from autoessay.clients.common import AccessStatus, NormalizedSource, normalize_doi_value

TITLE_MATCH_THRESHOLD = 92


@dataclass(frozen=True)
class DedupStats:
    total: int
    kept: int
    doi_duplicates: int
    fuzzy_duplicates: int


def deduplicate_sources(
    sources: list[NormalizedSource],
) -> tuple[list[NormalizedSource], DedupStats]:
    kept: list[NormalizedSource] = []
    doi_index: dict[str, int] = {}
    doi_duplicates = 0
    fuzzy_duplicates = 0

    for source in sources:
        doi = normalize_doi_value(source.doi)
        if doi is not None and doi in doi_index:
            index = doi_index[doi]
            kept[index] = merge_sources(kept[index], source)
            doi_duplicates += 1
            continue

        fuzzy_index = _find_title_match(kept, source.title)
        if fuzzy_index is not None:
            kept[fuzzy_index] = merge_sources(kept[fuzzy_index], source)
            fuzzy_duplicates += 1
            if doi is not None:
                doi_index[doi] = fuzzy_index
            continue

        if doi is not None:
            doi_index[doi] = len(kept)
        kept.append(source)

    return kept, DedupStats(
        total=len(sources),
        kept=len(kept),
        doi_duplicates=doi_duplicates,
        fuzzy_duplicates=fuzzy_duplicates,
    )


def merge_sources(first: NormalizedSource, incoming: NormalizedSource) -> NormalizedSource:
    updates = {
        "authors": _richer_list(first.authors, incoming.authors),
        "year": first.year if first.year is not None else incoming.year,
        "venue": _richer_text(first.venue, incoming.venue),
        "doi": _richer_text(first.doi, incoming.doi),
        "url": _richer_text(first.url, incoming.url),
        "pdf_url": _richer_text(first.pdf_url, incoming.pdf_url),
        "abstract": _longer_text(first.abstract, incoming.abstract),
        "access_status": _better_access_status(first.access_status, incoming.access_status),
        "license": _richer_text(first.license, incoming.license),
        "rank_score": max(first.rank_score, incoming.rank_score),
        "risk_flags": sorted(set(first.risk_flags + incoming.risk_flags)),
    }
    return first.copy(update=updates)


def normalized_title(value: str) -> str:
    lowered = value.casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", lowered)).strip()


def _find_title_match(kept: list[NormalizedSource], title: str) -> int | None:
    normalized = normalized_title(title)
    if not normalized:
        return None
    for index, source in enumerate(kept):
        existing = normalized_title(source.title)
        if existing and fuzz.token_sort_ratio(existing, normalized) >= TITLE_MATCH_THRESHOLD:
            return index
    return None


def _richer_list(first: list[str], incoming: list[str]) -> list[str]:
    return first if len(first) >= len(incoming) else incoming


def _richer_text(first: str | None, incoming: str | None) -> str | None:
    return first or incoming


def _longer_text(first: str | None, incoming: str | None) -> str | None:
    if not first:
        return incoming
    if not incoming:
        return first
    return first if len(first) >= len(incoming) else incoming


def _better_access_status(first: AccessStatus | str, incoming: AccessStatus | str) -> str:
    rank = {
        AccessStatus.OPEN.value: 4,
        AccessStatus.METADATA_ONLY.value: 3,
        AccessStatus.UNAVAILABLE.value: 2,
        AccessStatus.BLOCKED.value: 1,
    }
    first_value = first.value if isinstance(first, AccessStatus) else first
    incoming_value = incoming.value if isinstance(incoming, AccessStatus) else incoming
    if rank.get(first_value, 0) >= rank.get(incoming_value, 0):
        return first_value
    return incoming_value
