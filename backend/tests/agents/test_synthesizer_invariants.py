import json
import os
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.synthesizer import SynthesizerSourceNote, run_synthesizer
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.models import Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory

pytestmark = pytest.mark.skipif(
    os.getenv("AUTOESSAY_LIVE_SYNTHESIZER") != "1",
    reason="live Synthesizer invariant test is opt-in via AUTOESSAY_LIVE_SYNTHESIZER=1",
)


@pytest.mark.live
def test_live_synthesizer_legacy_and_harness_paths_satisfy_invariants(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "0")
    legacy_run_dir = _seed_synthesizer_run(
        app_session,
        tmp_path,
        run_id="run_live_synthesizer_legacy",
    )
    harness_run_dir = _seed_synthesizer_run(
        app_session,
        tmp_path,
        run_id="run_live_synthesizer_harness",
    )

    with app_session() as session:
        get_settings.cache_clear()
        legacy_summary = run_synthesizer("run_live_synthesizer_legacy", session)
        get_settings.cache_clear()
        harness_summary = run_synthesizer("run_live_synthesizer_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(
                    ProviderCall.run_id == "run_live_synthesizer_harness",
                ),
            ),
        )

    legacy_claims = _read_jsonl(legacy_run_dir / "synthesis" / "claims.jsonl")
    harness_claims = _read_jsonl(harness_run_dir / "synthesis" / "claims.jsonl")
    source_note = _read_json(
        harness_run_dir / "synthesis" / "source_notes" / "source_001.json",
    )

    assert legacy_summary["state"] == harness_summary["state"] == "USER_FIELD_REVIEW"
    assert legacy_summary["sources_processed"] >= 1
    assert harness_summary["sources_processed"] >= 1
    assert legacy_claims
    assert harness_claims
    parsed = SynthesizerSourceNote.parse_obj(source_note)
    assert parsed.source_id == "source_001"
    assert parsed.claims
    assert any(call.status == "accepted" for call in provider_calls)
    assert 1 <= len(provider_calls) <= 2


def _seed_synthesizer_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_DEEP_DIVE_REVIEW",
        domain_id="financial_history",
    )
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    source = _source("source_001")
    (sources_dir / "shortlist.json").write_text(
        json.dumps([source.dict()], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with app_session() as session:
        project = session.get(Project, "proj_test")
        if project is None:
            project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(project)
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
    return run_dir


def _source(source_id: str) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024,
        venue=f"Journal {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=None,
        abstract=(
            "This source discusses banking crises, institutional response, lender-of-last-resort "
            "practice, evidence limits, and research methods in financial history."
        ),
        source_client="semantic_scholar",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )


def _read_json(path: Path) -> dict[str, object]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
