import json
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.scout import run_scout
from autoessay.config import get_settings
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory


def test_run_scout_stub_end_to_end_transitions_and_writes_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_scout_success"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()

        summary = run_scout(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        assert run.state == "USER_SEARCH_REVIEW"

        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    discovery_dir = run_dir / "discovery"
    queries = json.loads((discovery_dir / "queries.json").read_text(encoding="utf-8"))
    candidates = _read_jsonl(discovery_dir / "skim_candidates.jsonl")
    report = (discovery_dir / "scout_report.md").read_text(encoding="utf-8")

    assert summary["state"] == "USER_SEARCH_REVIEW"
    assert len(queries) >= 3
    assert candidates
    assert [item["doi"] for item in candidates].count("10.5555/shared-scout") == 1
    assert "DOI duplicates removed:" in report
    assert "Top 10" in report
    assert "phase_started" in [event.event_type for event in events]
    assert "source_progress" in [event.event_type for event in events]
    assert events[-1].event_type == "phase_done"


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
