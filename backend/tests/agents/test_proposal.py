import json
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.proposal import run_proposal_draft
from autoessay.config import get_settings
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory


def test_run_proposal_stub_transitions_and_writes_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_proposal_success"
    run_dir = _seed_loaded_run(app_session, tmp_path, run_id, "banking crises")

    with app_session() as session:
        summary = run_proposal_draft(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        assert run.state == "USER_PROPOSAL_REVIEW"
        assert run.proposal_version == 1
        assert run.proposal_content_path == "proposal/proposal_v001.json"
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    proposal_path = run_dir / "proposal" / "proposal_v001.json"
    markdown_path = run_dir / "proposal" / "proposal_v001.md"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))

    assert summary["state"] == "USER_PROPOSAL_REVIEW"
    assert set(proposal) == {
        "research_question",
        "significance",
        "preliminary_approach",
        "expected_contribution",
        "scope",
        "preliminary_keywords",
    }
    assert proposal["research_question"]
    assert proposal["preliminary_keywords"]
    assert markdown_path.is_file()
    assert "Research Question" in markdown_path.read_text(encoding="utf-8")
    assert "PROPOSAL_DRAFTING" in [json.loads(event.payload).get("to_state") for event in events]
    assert events[-1].event_type == "phase_done"


def _seed_loaded_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    run_id: str,
    title: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )
    with app_session() as session:
        project = seed_project(session)
        project.title = title
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
    return run_dir
