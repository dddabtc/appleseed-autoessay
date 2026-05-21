from __future__ import annotations

from autoessay.agents._source_verification import classify_source
from autoessay.clients.common import NormalizedSource, VerificationStatus


def _source(
    *,
    provenance: str = "search",
    verified_by: str | None = None,
    source_client: str = "semantic_scholar",
    risk_flags: list[str] | None = None,
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED,
) -> NormalizedSource:
    return NormalizedSource(
        source_id="test:source",
        title="Test source",
        authors=[],
        year=None,
        venue=None,
        doi=None,
        url=None,
        pdf_url=None,
        abstract=None,
        source_client=source_client,
        access_status="metadata_only",
        license=None,
        risk_flags=risk_flags or [],
        provenance=provenance,
        verified_by=verified_by,
        verification_status=verification_status,
    )


def test_homophone_ban_is_disputed_even_when_verified() -> None:
    status, confidence = classify_source(
        _source(verified_by="crossref", risk_flags=["homophone_ban"])
    )

    assert status == VerificationStatus.DISPUTED
    assert confidence == 0.0


def test_cnki_stub_is_disputed() -> None:
    status, confidence = classify_source(_source(risk_flags=["cnki_stub"]))

    assert status == VerificationStatus.DISPUTED
    assert confidence == 0.05


def test_weak_entity_anchor_is_low_confidence_unverified() -> None:
    status, confidence = classify_source(_source(risk_flags=["weak_entity_anchor"]))

    assert status == VerificationStatus.UNVERIFIED
    assert confidence == 0.25


def test_llm_canon_crossref_verified_is_high_confidence_verified() -> None:
    status, confidence = classify_source(_source(provenance="llm_canon", verified_by="crossref"))

    assert status == VerificationStatus.VERIFIED
    assert confidence == 0.85


def test_search_openlibrary_verified_uses_openlibrary_confidence_band() -> None:
    status, confidence = classify_source(_source(provenance="search", verified_by="openlibrary"))

    assert status == VerificationStatus.VERIFIED
    assert confidence == 0.7


def test_llm_canon_openlibrary_verified_uses_openlibrary_confidence_band() -> None:
    status, confidence = classify_source(
        _source(
            provenance="llm_canon",
            verified_by="openlibrary",
            risk_flags=["metadata_only_no_full_text"],
        )
    )

    assert status == VerificationStatus.VERIFIED
    assert confidence == 0.7


def test_search_crossref_verified_is_verified() -> None:
    status, confidence = classify_source(_source(provenance="search", verified_by="crossref"))

    assert status == VerificationStatus.VERIFIED
    assert confidence == 0.7


def test_manual_upload_verified_by_is_verified() -> None:
    status, confidence = classify_source(
        _source(provenance="search", verified_by="manual_upload", source_client="user_upload")
    )

    assert status == VerificationStatus.VERIFIED
    assert confidence == 0.8


def test_official_archive_verified_by_is_verified() -> None:
    status, confidence = classify_source(
        _source(
            provenance="search",
            verified_by="official_archive",
            source_client="official_archive",
        )
    )

    assert status == VerificationStatus.VERIFIED
    assert confidence == 0.85


def test_llm_canon_without_verifier_is_unverified_lower_confidence() -> None:
    status, confidence = classify_source(_source(provenance="llm_canon"))

    assert status == VerificationStatus.UNVERIFIED
    assert confidence == 0.4


def test_search_without_verifier_or_critical_risk_uses_default_unverified() -> None:
    status, confidence = classify_source(_source(provenance="search"))

    assert status == VerificationStatus.UNVERIFIED
    assert confidence == 0.5


def test_cnki_stub_takes_priority_over_weak_entity_anchor() -> None:
    status, confidence = classify_source(_source(risk_flags=["cnki_stub", "weak_entity_anchor"]))

    assert status == VerificationStatus.DISPUTED
    assert confidence == 0.05


def test_non_critical_access_risk_does_not_dispute_verified_source() -> None:
    status, confidence = classify_source(
        _source(
            provenance="search",
            verified_by="crossref",
            risk_flags=["metadata_only_no_full_text"],
        )
    )

    assert status == VerificationStatus.VERIFIED
    assert confidence == 0.7


def test_verification_status_serializes_as_string() -> None:
    payload = _source(verification_status=VerificationStatus.VERIFIED).dict()

    assert payload["verification_status"] == "verified"
