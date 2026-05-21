import json
from pathlib import Path

from conftest import seed_project

from autoessay.agents.proposal import run_proposal_draft
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


def test_user_draft_is_carried_into_stub_proposal(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_proposal_user_draft"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )
    with app_session() as session:
        project = seed_project(session)
        project.title = "central bank lender of last resort"
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

        run_proposal_draft(
            run_id,
            session,
            "Focus on regional clearinghouse networks during interwar banking stress.",
        )

    proposal = json.loads(
        (run_dir / "proposal" / "proposal_v001.json").read_text(encoding="utf-8"),
    )
    combined = " ".join(
        [
            proposal["research_question"],
            proposal["scope"],
            " ".join(proposal["preliminary_keywords"]),
        ],
    )
    assert "regional clearinghouse networks" in combined
