from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from autoessay.models import Domain, Project, Run, User
from autoessay.run_writer import create_run_directory


@contextmanager
def harness_run_context(
    app_session: sessionmaker[Session],
    tmp_path: Path,
) -> Iterator[tuple[Session, Path, str]]:
    run_id = "run_harness"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="SCOUT_RUNNING",
        domain_id="financial_history",
    )
    with app_session() as session:
        session.add(User(id="single-user", display_name="Single User"))
        session.add(
            Domain(
                id="financial_history",
                display_name="Financial History",
                version="0.1.0",
                enabled=True,
            ),
        )
        session.flush()
        project = Project(
            id="proj_test",
            user_id="single-user",
            title="Test project",
            domain_id="financial_history",
            domain_version="0.1.0",
            status="CREATED",
        )
        session.add(project)
        session.flush()
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="SCOUT_RUNNING",
                baseline_hash="test",
            ),
        )
        session.commit()
        yield session, run_dir, run_id
