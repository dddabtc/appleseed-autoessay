from pathlib import Path

from conftest import seed_project

from autoessay.agents.proposal import run_proposal_draft
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


def test_proposal_regeneration_bumps_versions(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_proposal_versions"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )
    with app_session() as session:
        project = seed_project(session)
        project.title = "credit markets after banking panics"
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

        run_proposal_draft(run_id, session)
        run_proposal_draft(run_id, session, "Add comparative regional scope.")
        run_proposal_draft(run_id, session, "Narrow the time period.")

        run = session.get(Run, run_id)
        assert run is not None
        assert run.state == "USER_PROPOSAL_REVIEW"
        assert run.proposal_version == 3
        assert run.proposal_content_path == "proposal/proposal_v003.json"

    proposal_dir = run_dir / "proposal"
    assert [path.name for path in sorted(proposal_dir.glob("proposal_v*.json"))] == [
        "proposal_v001.json",
        "proposal_v002.json",
        "proposal_v003.json",
    ]
    assert [path.name for path in sorted(proposal_dir.glob("proposal_v*.md"))] == [
        "proposal_v001.md",
        "proposal_v002.md",
        "proposal_v003.md",
    ]
