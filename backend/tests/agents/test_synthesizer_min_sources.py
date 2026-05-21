"""Synthesizer hard-fails when fewer than N real sources got processed.

This is the upstream half of the sentinel mechanism: stop the run before
Drafter ever sees too-thin material, rather than relying on sentinels
to catch every form of empty-content boilerplate the LLM might emit
when starved for evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import seed_project

from autoessay.agents.synthesizer import _source_text, run_synthesizer
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


def test_synthesizer_fails_fixable_below_min_processed_sources(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_MIN_PROCESSED_SOURCES", "3")
    get_settings.cache_clear()

    run_id = "run_synth_threshold"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_DEEP_DIVE_REVIEW",
        domain_id="financial_history",
    )
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    # Only 1 fixture source — well below the threshold of 3.
    (sources_dir / "shortlist.json").write_text(
        json.dumps(
            [
                {
                    "source_id": "src_1",
                    "title": "Single source",
                    "authors": ["Author"],
                    "year": 2024,
                    "venue": "Journal",
                    "doi": None,
                    "url": None,
                    "pdf_url": None,
                    "abstract": "Abstract content for the only seeded source.",
                    "source_client": "crossref",
                    "access_status": "metadata_only",
                    "license": None,
                    "rank_score": 1.0,
                    "risk_flags": [],
                },
            ],
        ),
        encoding="utf-8",
    )

    with app_session() as session:
        seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_DEEP_DIVE_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

    with app_session() as session:
        # Approve the single source so synthesizer picks it up.
        from autoessay.models import Checkpoint

        session.add(
            Checkpoint(
                id="ck_threshold",
                run_id=run_id,
                checkpoint_type="USER_DEEP_DIVE_REVIEW",
                status="ACCEPTED",
                decision_payload=json.dumps({"approved_source_ids": ["src_1"]}),
                decided_at=None,
            ),
        )
        session.commit()

    with app_session() as session:
        result = run_synthesizer(run_id, session)
        assert result["state"] == "FAILED_FIXABLE"
        assert "minimum required: 3" in result.get("guidance", "")


def test_source_text_uses_verified_metadata_fallback_for_canonical_source(tmp_path: Path) -> None:
    source = NormalizedSource(
        source_id="crossref:10.1234/book",
        title="A Verified Canonical Book",
        authors=["Author One"],
        year=1993,
        venue="University Press",
        doi="10.1234/book",
        url="https://doi.org/10.1234/book",
        pdf_url=None,
        abstract=None,
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
        provenance="llm_canon",
        canonical_bucket="frontier",
        canonical_rationale="Verified source used for literature positioning.",
        verified_by="crossref",
        verification_status=VerificationStatus.VERIFIED,
    )

    text, warning = _source_text(tmp_path, source, {})

    assert warning is None
    assert text is not None
    assert "VERIFIED BIBLIOGRAPHIC METADATA ONLY" in text
    assert "A Verified Canonical Book" in text
    assert "Do not infer substantive arguments" in text


def test_source_text_does_not_use_metadata_fallback_for_unverified_source(tmp_path: Path) -> None:
    source = NormalizedSource(
        source_id="crossref:10.1234/unverified",
        title="An Unverified Book",
        authors=["Author One"],
        year=1993,
        venue="University Press",
        doi="10.1234/unverified",
        url="https://doi.org/10.1234/unverified",
        pdf_url=None,
        abstract=None,
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
        provenance="search",
        verified_by=None,
        verification_status=VerificationStatus.UNVERIFIED,
    )

    text, warning = _source_text(tmp_path, source, {})

    assert text is None
    assert warning is None
