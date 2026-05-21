from pathlib import Path

from conftest import seed_project

from autoessay.agents.curator import run_curator
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


def test_run_curator_empty_input_enters_failed_fixable(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_curator_empty"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    (run_dir / "discovery").mkdir(parents=True)
    (run_dir / "discovery" / "skim_candidates.jsonl").write_text("", encoding="utf-8")

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

    report = (run_dir / "sources" / "curation_report.md").read_text(encoding="utf-8")
    assert run is not None
    assert run.state == "FAILED_FIXABLE"
    assert summary["state"] == "FAILED_FIXABLE"
    assert "No skim candidates or manual uploads" in report
