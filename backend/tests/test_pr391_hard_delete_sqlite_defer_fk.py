"""PR-391 regression coverage for SQLite hard-delete FK cycles.

PR-390 used ``PRAGMA foreign_keys=OFF`` inside an already-open
SQLAlchemy transaction, so SQLite ignored it. This file uses a
file-backed SQLite engine with the production pragma listener and seeds
the real branches <-> phase_versions cycle plus phase-version child
tables that do not carry ``run_id``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from autoessay.config import get_settings
from autoessay.db import make_engine
from autoessay.main import hard_delete_project
from autoessay.models import (
    Base,
    Branch,
    Corpus,
    CorpusDocument,
    Domain,
    MemoryRef,
    PhaseArtifact,
    PhasePromptDraft,
    PhaseVersion,
    PhaseVersionInput,
    PhaseVersionPrompt,
    Project,
    Run,
    RunHead,
    User,
    utcnow,
)


@pytest.fixture()
def prod_like_sqlite_session(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[sessionmaker[Session]]:  # type: ignore[type-arg]
    monkeypatch.setenv("AUTOESSAY_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    engine = make_engine(f"sqlite:///{tmp_path / 'prod-like.db'}")
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    try:
        yield testing_session
    finally:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.commit()
        Base.metadata.drop_all(engine)
        engine.dispose()
        get_settings.cache_clear()


def test_hard_delete_project_defers_sqlite_fk_cycles_and_deletes_pv_children(
    prod_like_sqlite_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    project_id = "proj_pr391"
    run_id = "run_pr391"

    with prod_like_sqlite_session() as session:
        user = User(id="single-user", display_name="Single User")
        session.add(user)
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
            id=project_id,
            user_id=user.id,
            title="PR-391 prod-like hard delete",
            domain_id="financial_history",
            domain_version="0.1.0",
            status="CREATED",
            deleted_at=utcnow(),
        )
        run = Run(
            id=run_id,
            project_id=project_id,
            domain_version="0.1.0",
            run_dir=str(tmp_path / "runs" / run_id),
            state="CREATED",
            baseline_hash="baseline",
        )
        session.add_all([project, run])
        session.flush()

        main_branch = Branch(
            id="branch_pr391_main",
            run_id=run_id,
            name="main",
        )
        session.add(main_branch)
        session.flush()
        run.active_branch_id = main_branch.id

        upstream = PhaseVersion(
            id="pv_pr391_upstream",
            run_id=run_id,
            phase="proposal",
            version_no=1,
            status="done",
            artifacts_dir="phases/pv_pr391_upstream",
            created_on_branch_id=main_branch.id,
        )
        downstream = PhaseVersion(
            id="pv_pr391_downstream",
            run_id=run_id,
            phase="drafter",
            version_no=1,
            parent_pv_id=upstream.id,
            status="done",
            artifacts_dir="phases/pv_pr391_downstream",
            created_on_branch_id=main_branch.id,
        )
        session.add_all([upstream, downstream])
        session.flush()

        child_branch = Branch(
            id="branch_pr391_child",
            run_id=run_id,
            name="experiment",
            parent_branch_id=main_branch.id,
            forked_from_pv_id=upstream.id,
        )
        session.add(child_branch)
        session.flush()
        downstream.created_on_branch_id = child_branch.id

        session.add_all(
            [
                Corpus(
                    id="corpus_pr391",
                    owner_user_id=user.id,
                    user_id=user.id,
                    project_id=project_id,
                    name="project corpus",
                    enabled=True,
                ),
                RunHead(
                    run_id=run_id,
                    branch_id=main_branch.id,
                    phase="proposal",
                    version_id=upstream.id,
                ),
                PhasePromptDraft(
                    run_id=run_id,
                    branch_id=main_branch.id,
                    phase="proposal",
                    prompt_key="main",
                    content="prompt",
                    content_hash="hash",
                ),
                PhaseArtifact(
                    id="artifact_pr391",
                    phase_version_id=downstream.id,
                    kind="draft",
                    logical_path="drafts/main.md",
                    blob_path="phases/pv_pr391_downstream/drafts/main.md",
                    sha256="0" * 64,
                    size_bytes=1,
                ),
                PhaseVersionPrompt(
                    phase_version_id=downstream.id,
                    prompt_key="main",
                    phase="drafter",
                    source="default",
                    content="prompt",
                    content_hash="hash",
                ),
                PhaseVersionInput(
                    phase_version_id=downstream.id,
                    upstream_phase="proposal",
                    upstream_pv_id=upstream.id,
                ),
            ],
        )
        session.flush()
        session.add(
            CorpusDocument(
                id="doc_pr391",
                corpus_id="corpus_pr391",
                title="Project corpus document",
                source_path="corpus/doc.txt",
                document_type="txt",
                privacy_level="private",
                ingest_status="done",
                sensitivity="normal",
            ),
        )
        session.flush()
        session.add(
            MemoryRef(
                id="memory_pr391",
                corpus_document_id="doc_pr391",
                memory_id="memory-1",
            ),
        )
        session.commit()

    with prod_like_sqlite_session() as session:
        user = session.get(User, "single-user")
        assert user is not None
        response = hard_delete_project(project_id, session, user)
        assert response.status_code == 204

    with prod_like_sqlite_session() as session:
        assert session.scalar(select(Project).where(Project.id == project_id)) is None
        assert session.scalar(select(Run).where(Run.id == run_id)) is None
        assert session.scalars(select(Branch).where(Branch.run_id == run_id)).all() == []
        remaining_pvs = session.scalars(
            select(PhaseVersion).where(PhaseVersion.run_id == run_id),
        ).all()
        assert remaining_pvs == []
        assert session.get(PhaseArtifact, "artifact_pr391") is None
        assert session.get(PhaseVersionPrompt, ("pv_pr391_downstream", "main")) is None
        assert session.get(PhaseVersionInput, ("pv_pr391_downstream", "proposal")) is None
        assert session.get(Corpus, "corpus_pr391") is None
        assert session.get(CorpusDocument, "doc_pr391") is None
        assert session.get(MemoryRef, "memory_pr391") is None
