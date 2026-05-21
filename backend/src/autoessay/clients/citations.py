"""BibTeX formatting helpers for normalized literature sources."""

from __future__ import annotations

import re
from collections.abc import Sequence

from autoessay.clients.common import NormalizedSource


def normalized_source_to_bibtex(source: NormalizedSource) -> str:
    """Return a stable BibTeX entry for a normalized source record."""

    entry_type = _entry_type(source)
    fields: list[tuple[str, str]] = []
    if source.authors:
        fields.append(("author", " and ".join(source.authors)))
    fields.append(("title", source.title))
    if source.year is not None:
        fields.append(("year", str(source.year)))
    if entry_type == "article" and source.venue:
        fields.append(("journal", source.venue))
    elif entry_type == "book" and source.venue:
        fields.append(("publisher", source.venue))
    elif source.venue:
        fields.append(("note", source.venue))
    if source.doi:
        fields.append(("doi", source.doi))
    elif source.url:
        fields.append(("url", source.url))

    lines = [f"@{entry_type}{{{_entry_key(source.source_id)},"]
    for index, (field, value) in enumerate(fields):
        suffix = "," if index < len(fields) - 1 else ""
        lines.append(f"  {field} = {{{_escape_bibtex(value)}}}{suffix}")
    lines.append("}")
    return "\n".join(lines)


def generate_bib(sources: Sequence[NormalizedSource]) -> str:
    """Return BibTeX entries sorted by source_id."""

    ordered = sorted(sources, key=lambda item: item.source_id)
    if not ordered:
        return ""
    return "\n\n".join(normalized_source_to_bibtex(source) for source in ordered) + "\n"


def _entry_type(source: NormalizedSource) -> str:
    client = source.source_client.casefold()
    venue = (source.venue or "").casefold()
    if client in {"book", "books", "google_books", "worldcat", "library"}:
        return "book"
    if "press" in venue and not source.doi:
        return "book"
    if source.doi or source.venue:
        return "article"
    return "misc"


def _entry_key(source_id: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_:-]+", "_", source_id).strip("_")
    return key or "source"


def _escape_bibtex(value: str) -> str:
    return value.replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}")
