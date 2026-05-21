import json
from pathlib import Path

from conftest import seed_project

from autoessay.agents import curator
from autoessay.agents.curator import run_curator
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.clients.pdf_fetcher import OpenAccessUnavailable
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


def test_run_curator_pdf_failure_requests_manual_upload_without_failed_vendor(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_fetch_partial"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True)
    _write_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [
            _source("source_success", "https://example.test/success.pdf", 2024),
            _source("source_fail", "https://example.test/fail.pdf", 2023),
        ],
    )

    async def fake_fetch(url: str, timeout: float, max_size_mb: int) -> bytes:
        del timeout, max_size_mb
        if url.endswith("fail.pdf"):
            raise OpenAccessUnavailable("synthetic fetch failure")
        return b"%PDF-1.4"

    monkeypatch.setattr(curator, "fetch_pdf", fake_fetch)

    with app_session() as session:
        project = seed_project(session)
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

        summary = run_curator(run_id, session)
        run = session.get(Run, run_id)

    manual_requests = _read_jsonl(run_dir / "sources" / "manual_upload_requests.jsonl")
    manifest = json.loads(
        (run_dir / "sources" / "fulltext_manifest.json").read_text(encoding="utf-8"),
    )

    assert run is not None
    assert run.state == "USER_DEEP_DIVE_REVIEW"
    assert summary["manual_required"] == 1
    assert "source_success" in manifest
    assert [item["source_id"] for item in manual_requests] == ["source_fail"]


def _source(source_id: str, pdf_url: str, year: int) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=year,
        venue=f"Venue {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=pdf_url,
        abstract="A source about banking crises.",
        source_client="semantic_scholar",
        access_status=AccessStatus.OPEN,
        license="CC-BY",
        rank_score=0.0,
        risk_flags=[],
        verified_by="crossref",
        verification_status=VerificationStatus.VERIFIED,
        confidence=0.7,
    )


def _write_jsonl(path: Path, sources: list[NormalizedSource]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for source in sources:
            handle.write(source.json(sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
