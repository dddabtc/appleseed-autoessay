from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents._source_verification import classify_source
from autoessay.agents.curator import (
    _apply_literature_policy,
    _apply_verification_gate,
    run_curator,
)
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.config import Settings, get_settings
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory


def test_default_gate_keeps_only_verified_sources() -> None:
    kept, rejected = _apply_verification_gate(
        [
            _source("verified", status=VerificationStatus.VERIFIED),
            _source("unverified", status=VerificationStatus.UNVERIFIED),
            _source("disputed", status=VerificationStatus.DISPUTED),
        ],
        _settings(include_unverified=False),
    )

    assert [source.source_id for source in kept] == ["verified"]
    assert [record["verification_status"] for record in rejected] == [
        "unverified",
        "disputed",
    ]
    assert all(record["reason"] == "verification_gate_default_verified_only" for record in rejected)


def test_experimental_flag_keeps_all_sources() -> None:
    sources = [
        _source("verified", status=VerificationStatus.VERIFIED),
        _source("unverified", status=VerificationStatus.UNVERIFIED),
        _source("disputed", status=VerificationStatus.DISPUTED),
    ]

    kept, rejected = _apply_verification_gate(sources, _settings(include_unverified=True))

    assert [source.source_id for source in kept] == ["verified", "unverified", "disputed"]
    assert rejected == []


def test_cnki_stub_disputed_source_is_gated_by_default() -> None:
    source = _classified(_source("cnki:stub-modern-banking-history", risk_flags=["cnki_stub"]))

    kept, rejected = _apply_verification_gate([source], _settings(include_unverified=False))

    assert kept == []
    assert rejected[0]["verification_status"] == "disputed"
    assert rejected[0]["risk_flags"] == ["cnki_stub"]


def test_weak_entity_anchor_unverified_source_is_gated_by_default() -> None:
    source = _classified(_source("weak", risk_flags=["weak_entity_anchor"]))

    kept, rejected = _apply_verification_gate([source], _settings(include_unverified=False))

    assert kept == []
    assert rejected[0]["verification_status"] == "unverified"
    assert rejected[0]["confidence"] == 0.25


def test_pending_source_is_gated_by_default() -> None:
    source = _source("pending", status=VerificationStatus.PENDING, confidence=0.1)

    kept, rejected = _apply_verification_gate([source], _settings(include_unverified=False))

    assert kept == []
    assert rejected[0]["verification_status"] == "pending"


def test_literature_policy_rejects_wikipedia_before_verification_gate() -> None:
    wikipedia = _source(
        "wikipedia_zh:456",
        provenance="wiki_canonical_seed",
        risk_flags=["wikipedia_zh"],
        status=VerificationStatus.UNVERIFIED,
    )
    verified = _source("crossref:eichengreen", status=VerificationStatus.VERIFIED)

    policy_sources, policy_rejections = _apply_literature_policy(
        [wikipedia, verified],
        {"include_working_papers": True, "include_books": True, "include_preprints": True},
    )
    gate_kept, gate_rejected = _apply_verification_gate(
        policy_sources,
        _settings(include_unverified=False),
    )

    assert [record["reason"] for record in policy_rejections] == ["canonical_seed_not_citable"]
    assert [source.source_id for source in policy_sources] == ["crossref:eichengreen"]
    assert [source.source_id for source in gate_kept] == ["crossref:eichengreen"]
    assert gate_rejected == []


def test_curator_integration_writes_gate_event_audit_and_verified_only_shortlist(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_verification_gate_mixed"
    run_dir = _seed_curator_run(
        app_session,
        tmp_path,
        run_id=run_id,
        sources=[
            _source(
                "crossref:eichengreen",
                title="Globalizing Capital: A History of the International Monetary System",
                authors=["Barry Eichengreen"],
                source_client="crossref",
                verified_by="crossref",
                status=VerificationStatus.VERIFIED,
                confidence=0.7,
            ),
            _source(
                "crossref:steil",
                title="The Battle of Bretton Woods",
                authors=["Benn Steil"],
                source_client="crossref",
                verified_by="crossref",
                status=VerificationStatus.VERIFIED,
                confidence=0.85,
                provenance="llm_canon",
            ),
            _source("openalex:unverified", status=VerificationStatus.UNVERIFIED),
            _source("cnki:stub", status=VerificationStatus.DISPUTED, risk_flags=["cnki_stub"]),
            _source("pending:verification", status=VerificationStatus.PENDING),
        ],
    )

    with app_session() as session:
        summary = run_curator(run_id, session)
        payloads = _event_payloads(session, run_id, "verification_gate_applied")

    shortlist = _read_json(run_dir / "sources" / "shortlist.json")
    rejected = _read_jsonl(run_dir / "sources" / "verification_gate_rejected.jsonl")

    assert summary["shortlisted"] == 2
    assert {item["source_id"] for item in shortlist} == {
        "crossref:steil",
        "crossref:eichengreen",
    }
    assert all(item["verification_status"] == "verified" for item in shortlist)
    assert len(rejected) == 3
    assert payloads == [
        {
            "experimental_flag": False,
            "kept_count": 2,
            "phase": "curator",
            "rejected_breakdown": {"disputed": 1, "pending": 1, "unverified": 1},
            "rejected_count": 3,
            "warning": None,
        }
    ]


def test_experimental_flag_emits_warning_event_and_keeps_unverified_sources(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="autoessay.agents.curator")
    run_id = "run_verification_gate_experimental"
    run_dir = _seed_curator_run(
        app_session,
        tmp_path,
        run_id=run_id,
        sources=[
            _source("verified", status=VerificationStatus.VERIFIED),
            _source("unverified", status=VerificationStatus.UNVERIFIED),
        ],
    )

    with app_session() as session:
        summary = run_curator(run_id, session)
        applied_payload = _event_payloads(session, run_id, "verification_gate_applied")[0]
        warning_payload = _event_payloads(session, run_id, "verification_gate_warning")[0]

    shortlist = _read_json(run_dir / "sources" / "shortlist.json")
    rejected = _read_jsonl(run_dir / "sources" / "verification_gate_rejected.jsonl")

    assert summary["shortlisted"] == 2
    assert {item["source_id"] for item in shortlist} == {"verified", "unverified"}
    assert rejected == []
    assert applied_payload["experimental_flag"] is True
    assert applied_payload["warning"] == (
        "experimental flag ON, citation pool includes non-verified sources"
    )
    assert warning_payload["severity"] == "warning"
    assert warning_payload["included_count"] == 2
    assert any("Experimental citation-pool flag is ON" in item.message for item in caplog.records)


@pytest.mark.real_r10_fixture
def test_r10_fixture_replay_keeps_only_verified_sources_after_gate() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[3]
        / "frontend/tmp/qa-artifacts/run_d9295f9ad25146008fe5870cadcbe2d6"
        / "phase-outputs/02-curator.json"
    )
    if not fixture_path.exists():
        pytest.skip("real R10 fixture is not present in this checkout")

    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    candidates = [
        _classified(NormalizedSource.parse_obj(item))
        for item in payload["artifact"]["skim_candidates"]
    ]
    policy_sources, policy_rejections = _apply_literature_policy(
        candidates,
        {"include_working_papers": True, "include_books": True, "include_preprints": True},
    )
    kept, rejected = _apply_verification_gate(policy_sources, _settings(include_unverified=False))

    assert len(candidates) == 113
    assert len(kept) <= 5
    assert all(_status_value(source) == VerificationStatus.VERIFIED.value for source in kept)
    assert any("Battle of Bretton Woods" in source.title for source in kept)
    assert any(
        record["verification_status"] == "disputed" and "cnki_stub" in record["risk_flags"]
        for record in rejected
    )
    assert all(record["reason"] == "canonical_seed_not_citable" for record in policy_rejections)


def _settings(*, include_unverified: bool) -> Settings:
    return Settings(include_unverified_in_citation_pool=include_unverified)


def _classified(source: NormalizedSource) -> NormalizedSource:
    status, confidence = classify_source(source)
    return source.copy(update={"verification_status": status, "confidence": confidence})


def _status_value(source: NormalizedSource) -> str:
    status = source.verification_status
    return status.value if hasattr(status, "value") else str(status)


def _source(
    source_id: str,
    *,
    title: str | None = None,
    authors: list[str] | None = None,
    source_client: str = "semantic_scholar",
    provenance: str = "search",
    verified_by: str | None = None,
    status: VerificationStatus = VerificationStatus.UNVERIFIED,
    confidence: float = 0.5,
    risk_flags: list[str] | None = None,
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title or f"Paper {source_id}",
        authors=authors or [f"Author {source_id}"],
        year=2024,
        venue=f"Venue {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=None,
        abstract="A source about Bretton Woods, banking crises, and monetary commitments.",
        source_client=source_client,
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=0.0,
        risk_flags=risk_flags or [],
        provenance=provenance,
        verified_by=verified_by,
        verification_status=status,
        confidence=confidence,
    )


def _seed_curator_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
    sources: list[NormalizedSource],
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_sources_jsonl(discovery_dir / "skim_candidates.jsonl", sources)
    with app_session() as session:
        project = seed_project(session)
        project.title = "Bretton Woods monetary commitments"
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _event_payloads(
    session,  # type: ignore[no-untyped-def]
    run_id: str,
    event_type: str,
) -> list[dict[str, object]]:
    events = session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == event_type)
        .order_by(RunEvent.created_at.asc())
    )
    return [json.loads(event.payload) for event in events]


def _write_sources_jsonl(path: Path, sources: list[NormalizedSource]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for source in sources:
            handle.write(source.json(sort_keys=True) + "\n")


def _read_json(path: Path) -> list[dict[str, object]]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, list)
    return [item for item in decoded if isinstance(item, dict)]


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
