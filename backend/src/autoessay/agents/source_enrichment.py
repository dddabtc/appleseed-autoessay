"""PR-263a — source enrichment from the shadow baseline's
``reference_candidates`` list.

Converts each ``ReferenceCandidate`` produced by the shadow
baseline (PR-262) into the existing ``CanonicalWork`` shape and
runs it through the dual-source Crossref / OpenAlex verifier
(``verify_canonical_dual_source``). Verified results become
``NormalizedSource`` rows that downstream pipeline phases (PR-263b
wiring) merge into the curator's shortlist.

Codex round-1 verdict (Q6 redesign): NEVER merge baseline refs
directly into pipeline ``cited_sources`` — ``合规性`` would be
contaminated by un-verified author / year / title triples. Every
candidate must pass the same Crossref / OpenAlex roundtrip the
J9b curator already uses for its LLM-canon mining.

PR-263a v1 scope is INTENTIONALLY NARROW:
- adapter that turns the shadow output into the existing
  verifier's input shape
- no wiring into curator / synthesizer / new phase yet (PR-263b)
- no schema relaxation for non-OA-PDF sources yet (PR-263c — codex
  Q6 amendment about supporting verified metadata + abstracts +
  archival web pages + verified non-OA entries with confidence
  flags)

This keeps the diff small + lets us verify the verifier accepts
shadow-baseline-shaped input before committing to the wiring.
"""

from __future__ import annotations

from autoessay.agents._canonical_mining import verify_canonical_dual_source
from autoessay.agents._canonical_sources_schema import (
    CanonicalBucket,
    CanonicalWork,
)
from autoessay.agents.shadow_baseline import (
    ReferenceCandidate,
    ShadowBaselineOutput,
)
from autoessay.clients.common import NormalizedSource
from autoessay.clients.crossref import CrossrefClient
from autoessay.clients.openalex import OpenAlexClient
from autoessay.clients.openlibrary import (
    OpenLibraryClient,
    metadata_to_normalized_source,
)

# Shadow-baseline candidates always go into the ``frontier`` bucket
# because the shadow baseline is itself a "what would a frontier
# scholar cite" exercise — codex Q1 verdict on PR-262 explicitly
# called the artifact a benchmark anchor. The curator's J9b mining
# uses ``consensus`` / ``disagreement`` / ``frontier`` for its own
# 3-bucket structure; reusing ``frontier`` here keeps provenance
# traceable in audit logs without adding a fourth literal value.
_DEFAULT_BUCKET: CanonicalBucket = "frontier"
_DEFAULT_RATIONALE = "shadow baseline reference_candidate; verified before merge"


def _candidate_to_canonical_work(
    candidate: ReferenceCandidate,
) -> CanonicalWork | None:
    """Convert a ``ReferenceCandidate`` to the verifier's input
    shape. Returns ``None`` when the candidate is missing data the
    verifier can't recover from (year unparseable, title too short,
    author too short).

    The mapping is:
    - ``candidate.author`` → ``CanonicalWork.first_author``
    - ``candidate.year`` (str) → ``CanonicalWork.year`` (int) via
      ``int()`` parse; null on failure
    - ``candidate.title`` → ``CanonicalWork.title`` (must be ≥4
      chars per CanonicalWork schema)
    - ``candidate.doi_or_isbn`` → ``CanonicalWork.doi`` only when
      it looks like a Crossref-style DOI (starts with ``10.``); ISBN
      strings stay null and the verifier falls back to
      title+author+year fuzzy.
    - ``candidate.venue`` → ``CanonicalWork.journal_or_publisher``
    - ``candidate.why_relevant`` → ``CanonicalWork.rationale``
    """
    title = (candidate.title or "").strip()
    author = (candidate.author or "").strip()
    if len(title) < 4 or len(author) < 2:
        return None
    year_int: int | None
    try:
        year_int = int((candidate.year or "").strip()) if candidate.year else None
    except ValueError:
        year_int = None
    doi: str | None = None
    if candidate.doi_or_isbn and candidate.doi_or_isbn.strip().startswith("10."):
        doi = candidate.doi_or_isbn.strip()
    venue = (candidate.venue or "").strip() or None
    rationale = (candidate.why_relevant or "").strip() or _DEFAULT_RATIONALE
    try:
        return CanonicalWork(
            title=title[:400],  # CanonicalWork caps at 400
            first_author=author[:200],
            year=year_int,
            doi=doi,
            journal_or_publisher=venue[:300] if venue else None,
            rationale=rationale[:400],
        )
    except Exception:
        # Pydantic validation can fail (e.g. year out of bounds);
        # treat as un-convertible candidate.
        return None


def _shadow_to_verifier_input(
    output: ShadowBaselineOutput,
) -> list[tuple[CanonicalBucket, str, CanonicalWork]]:
    """Build the (bucket, rationale, CanonicalWork) triples the
    existing dual-source verifier expects. Drops candidates that
    can't be converted (logged via the caller's audit later)."""
    triples: list[tuple[CanonicalBucket, str, CanonicalWork]] = []
    for candidate in output.reference_candidates:
        work = _candidate_to_canonical_work(candidate)
        if work is None:
            continue
        triples.append((_DEFAULT_BUCKET, _DEFAULT_RATIONALE, work))
    return triples


def _isbn_from_candidate(candidate: ReferenceCandidate) -> str | None:
    """Pluck an ISBN-shaped string out of the candidate's
    ``doi_or_isbn`` field. Returns the cleaned ISBN-13 OR None when
    the field is missing / DOI-shaped / unparseable. Used by the
    OpenLibrary fallback in ``enrich_with_shadow_baseline``.

    Note: PR-263b only handles ISBN-13 starting with ``978`` (the
    only assignment block currently in active use; ISBN-10 maps to
    ISBN-13 via 978-prefix and gets re-emitted with that form by
    the LLM in 100% of observed cases). ISBN-10 with X check digits
    falls through to None — caller will skip the OpenLibrary lookup
    and the candidate ends up in drop_warnings as before.
    """
    raw = candidate.doi_or_isbn
    if not raw:
        return None
    cleaned = raw.strip().replace("-", "").replace(" ", "")
    # Reject anything that looks like a Crossref DOI.
    if cleaned.startswith("10."):
        return None
    # Accept only 13-digit numeric ISBNs.
    if len(cleaned) == 13 and cleaned.isdigit() and cleaned.startswith("978"):
        return cleaned
    return None


async def _enrich_via_openlibrary(
    output: ShadowBaselineOutput,
    openlibrary_client: OpenLibraryClient,
    already_verified_ids: set[str],
) -> tuple[list[NormalizedSource], list[dict[str, object]]]:
    """Walk every reference_candidate that carries an ISBN-13 the
    Crossref+OpenAlex verifier didn't already accept, and try
    OpenLibrary as a third verifier. Verified hits become
    ``NormalizedSource`` records with ``source_client="openlibrary"``
    + ``access_status=METADATA_ONLY`` + ``verified_by="openlibrary"``;
    misses produce drop_warnings shaped like the canonical-mining
    verifier's so downstream consumers don't have to special-case.

    Codex round-2 verdict (PR-263b, D + A-lite): keep
    ``cited_sources`` represents "system has done external
    verification"; OpenLibrary counts as external verification for
    metadata-only book sources. Don't trust ISBN structural check
    alone (codex Q2: "不要把 ISBN 结构合法当成 per-book existence
    verification").
    """
    verified: list[NormalizedSource] = []
    warnings: list[dict[str, object]] = []
    for candidate in output.reference_candidates:
        isbn = _isbn_from_candidate(candidate)
        if isbn is None:
            continue
        synthetic_id = f"openlibrary:isbn-{isbn}"
        if synthetic_id in already_verified_ids:
            continue
        metadata = await openlibrary_client.lookup_isbn(isbn)
        if metadata is None:
            warnings.append(
                {
                    "candidate": {
                        "title": candidate.title,
                        "author": candidate.author,
                        "year": candidate.year,
                        "isbn": isbn,
                    },
                    "reason": "openlibrary_no_match",
                    "verified_by": None,
                },
            )
            continue
        rationale = (candidate.why_relevant or "").strip() or _DEFAULT_RATIONALE
        verified.append(
            metadata_to_normalized_source(
                metadata,
                canonical_bucket=_DEFAULT_BUCKET,
                canonical_rationale=rationale[:200],
            ),
        )
    return verified, warnings


async def enrich_with_shadow_baseline(
    output: ShadowBaselineOutput,
    *,
    crossref_client: CrossrefClient | None = None,
    openalex_client: OpenAlexClient | None = None,
    openlibrary_client: OpenLibraryClient | None = None,
    fuzzy_threshold: float = 0.90,
) -> tuple[list[NormalizedSource], list[dict[str, object]]]:
    """Verify every reference_candidate against Crossref → OpenAlex
    → OpenLibrary (in that order) and return the verified
    ``NormalizedSource`` list plus drop-warnings for the un-
    verifiable.

    Crossref + OpenAlex catch DOI-bearing scholarly articles + en
    monographs OpenAlex indexes (J9b semantics: DOI exact short-
    circuit, then composite ≥0.90 fuzzy of 0.7 title + 0.2 author
    family + 0.1 year ±2). OpenLibrary catches the long tail of
    Chinese-humanities monographs that real-paper run #N validator
    showed Crossref+OpenAlex miss in 100% of cases. Codex round-2
    verdict (PR-263b, D + A-lite): make OpenLibrary a fallback, not
    a parallel — it only fires for candidates the first two
    verifiers didn't accept.

    Empty reference_candidates → empty result, no network calls.
    """
    triples = _shadow_to_verifier_input(output)
    if not triples:
        return [], []
    primary_verified, primary_warnings = await verify_canonical_dual_source(
        triples,
        crossref_client=crossref_client,
        openalex_client=openalex_client,
        fuzzy_threshold=fuzzy_threshold,
    )

    if openlibrary_client is None:
        # Caller didn't opt into the OpenLibrary fallback (e.g.
        # legacy callers / tests that only want Crossref+OpenAlex).
        return primary_verified, primary_warnings

    primary_ids = {src.source_id for src in primary_verified}
    fallback_verified, fallback_warnings = await _enrich_via_openlibrary(
        output,
        openlibrary_client,
        primary_ids,
    )
    # Drop primary warnings whose corresponding candidate just got
    # verified by OpenLibrary, so the caller doesn't see a "drop"
    # warning for a source that's actually in the verified list.
    remaining_primary_warnings = [
        warning
        for warning in primary_warnings
        if not _warning_was_recovered(warning, fallback_verified)
    ]
    return (
        primary_verified + fallback_verified,
        remaining_primary_warnings + fallback_warnings,
    )


def _warning_was_recovered(
    warning: dict[str, object],
    fallback_verified: list[NormalizedSource],
) -> bool:
    """Crude title-match between a Crossref/OpenAlex drop warning
    and the OpenLibrary-verified set. The primary verifier doesn't
    expose a stable "candidate id" so we compare titles
    case-insensitively. False matches are unlikely because the
    LLM-emitted candidates have distinctive long Chinese titles.
    """
    raw_work = warning.get("work")
    if not isinstance(raw_work, dict):
        return False
    warn_title = (raw_work.get("title") or "").strip().lower()
    if not warn_title:
        return False
    return any(source.title.strip().lower() == warn_title for source in fallback_verified)


__all__ = [
    "enrich_with_shadow_baseline",
]
