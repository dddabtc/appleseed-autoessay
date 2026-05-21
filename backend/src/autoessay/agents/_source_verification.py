"""Scout-side source verification metadata classification."""

from __future__ import annotations

from typing import Final

from autoessay.clients.common import NormalizedSource, VerificationStatus

# Identity-impacting risk flags that can change verification status.
# Access risks such as metadata_only_no_full_text or paywalled stay out of
# this allowlist and should not automatically make a source disputed.
_CRITICAL_RISK_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "cnki_stub",
        "homophone_ban",
        "weak_entity_anchor",
    }
)
_OPENLIBRARY_VERIFIER: Final[str] = "openlibrary"
_CROSSREF_VERIFIER: Final[str] = "crossref"
_OPENALEX_VERIFIER: Final[str] = "openalex"
_MANUAL_UPLOAD_VERIFIER: Final[str] = "manual_upload"
_OFFICIAL_ARCHIVE_VERIFIER: Final[str] = "official_archive"


def classify_source(source: NormalizedSource) -> tuple[VerificationStatus, float]:
    """Initial verification classification from existing scout signals.

    Priority, highest first:
    1. risk_flags contains homophone_ban -> DISPUTED, 0.0
    2. risk_flags contains cnki_stub -> DISPUTED, 0.05
    3. risk_flags contains weak_entity_anchor -> UNVERIFIED, 0.25
    4. wikipedia_zh canonical seed -> UNVERIFIED, 0.4
    5. verified_by=manual_upload -> VERIFIED, 0.8
    6. verified_by=openlibrary -> VERIFIED, 0.7
    7. provenance=llm_canon and verified_by is present -> VERIFIED, 0.85
    8. provenance=search and verified_by in {crossref, openalex} -> VERIFIED, 0.7
    9. provenance=llm_canon and verified_by is absent -> UNVERIFIED, 0.4
    10. other sources -> UNVERIFIED, 0.5

    PENDING is reserved for real in-flight verification and is not emitted here.
    """
    risk_flags = set(source.risk_flags)
    critical_risk_flags = risk_flags & _CRITICAL_RISK_FLAGS
    if "homophone_ban" in critical_risk_flags:
        return VerificationStatus.DISPUTED, 0.0
    if "cnki_stub" in critical_risk_flags:
        return VerificationStatus.DISPUTED, 0.05
    if "weak_entity_anchor" in critical_risk_flags:
        return VerificationStatus.UNVERIFIED, 0.25
    if source.source_id.startswith("wikipedia_zh:") or source.provenance == "wiki_canonical_seed":
        return VerificationStatus.UNVERIFIED, 0.4

    verified_by = source.verified_by.lower() if source.verified_by else None
    if verified_by == _MANUAL_UPLOAD_VERIFIER:
        return VerificationStatus.VERIFIED, 0.8

    if verified_by == _OFFICIAL_ARCHIVE_VERIFIER:
        return VerificationStatus.VERIFIED, 0.85

    if verified_by == _OPENLIBRARY_VERIFIER:
        return VerificationStatus.VERIFIED, 0.7

    if source.provenance == "llm_canon" and verified_by:
        return VerificationStatus.VERIFIED, 0.85

    if source.provenance == "search" and verified_by in {
        _CROSSREF_VERIFIER,
        _OPENALEX_VERIFIER,
    }:
        return VerificationStatus.VERIFIED, 0.7

    if source.provenance == "llm_canon":
        return VerificationStatus.UNVERIFIED, 0.4

    return VerificationStatus.UNVERIFIED, 0.5
