import json
from pathlib import Path

from conftest import seed_project

from autoessay.agents.synthesizer import run_synthesizer
from autoessay.clients import pdf_text
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


def test_run_synthesizer_enters_failed_fixable_when_most_extractions_are_poor(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_synthesizer_poor_extraction"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_DEEP_DIVE_REVIEW",
        domain_id="financial_history",
    )
    _write_source_pack(run_dir)

    def fake_extract_text(pdf_bytes: bytes, source_id: str | None = None) -> str:
        del pdf_bytes
        if source_id in {"source_1", "source_2"}:
            raise pdf_text.PoorExtraction("synthetic poor extraction")
        return "usable extracted text for source_3 " * 10

    monkeypatch.setattr(pdf_text, "extract_text", fake_extract_text)

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_DEEP_DIVE_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

        summary = run_synthesizer(run_id, session)
        run = session.get(Run, run_id)

    report = (run_dir / "synthesis" / "synthesizer_report.md").read_text(encoding="utf-8")

    assert run is not None
    assert run.state == "FAILED_FIXABLE"
    assert summary["state"] == "FAILED_FIXABLE"
    assert "More than half" in str(summary["guidance"])
    assert "source_1" in report
    assert "source_2" in report


def _write_source_pack(run_dir: Path) -> None:
    sources_dir = run_dir / "sources"
    fulltext_dir = sources_dir / "fulltext"
    fulltext_dir.mkdir(parents=True)
    sources = [_source(f"source_{index}", rank_score=1.0 - (index * 0.1)) for index in range(1, 4)]
    (sources_dir / "shortlist.json").write_text(
        json.dumps([source.dict() for source in sources], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest: dict[str, dict[str, object]] = {}
    for source in sources:
        pdf_path = fulltext_dir / f"{source.source_id}.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 synthetic")
        manifest[source.source_id] = {
            "pdf_path": str(pdf_path.relative_to(run_dir)),
            "sha256": "test",
            "size_bytes": 18,
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "license": "CC-BY",
        }
    (sources_dir / "fulltext_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _source(source_id: str, rank_score: float) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024,
        venue=f"Venue {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=f"https://example.test/{source_id}.pdf",
        abstract=f"Fallback abstract for {source_id}.",
        source_client="semantic_scholar",
        access_status=AccessStatus.OPEN,
        license="CC-BY",
        rank_score=rank_score,
        risk_flags=[],
    )
