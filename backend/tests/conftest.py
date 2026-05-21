import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from autoessay.config import get_settings
from autoessay.db import get_engine, get_session
from autoessay.main import app
from autoessay.models import Base, Domain, Project, User


@pytest.fixture(autouse=True)
def _shadow_baseline_stub_for_tests(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Slice F flips the production default to real shadow-baseline
    # generation. Keep pytest explicitly stubbed unless a test opts
    # into the real branch with its own monkeypatch override.
    monkeypatch.setenv("AUTOESSAY_SHADOW_BASELINE_STUB", "1")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture()
def app_session(tmp_path, monkeypatch) -> Iterator[sessionmaker[Session]]:  # type: ignore[type-arg]
    monkeypatch.setenv("AUTOESSAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    monkeypatch.setenv("AUTOESSAY_SAFETY_GATE_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SAFETY_GATE_FAIL_OPEN", "1")
    # Most existing backend tests exercise the legacy/deep state
    # machine and omit the new ADR-0003 mode field. Keep that fixture
    # default explicit; dedicated generation-mode tests cover the
    # production default of MANUSCRIPT_DEFAULT_MODE=express.
    monkeypatch.setenv("MANUSCRIPT_DEFAULT_MODE", "deep")
    # Tests typically seed 1-2 fixture sources; the production threshold
    # of 3 would otherwise force every synthesizer-related test to seed a
    # full shortlist. Keep this opt-in for tests explicitly exercising
    # the threshold.
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_MIN_PROCESSED_SOURCES", "0")
    # Tests don't need the LLM-driven manuscript front matter; the
    # exporter falls back to project_title + skipped abstract/keywords.
    monkeypatch.setenv("AUTOESSAY_FRONT_MATTER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SELF_CHECK_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_MATERIAL_DIAGNOSTIC_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_DETAILED_OUTLINE_STUB", "1")
    # PR-J9: scout's LLM canonical/frontier mining defaults to ON in
    # prod; tests stub it off so vendor-only scout harness assertions
    # still pass with 1 provider call (vs 3: query expansion + canon
    # + frontier mining).
    monkeypatch.setenv("AUTOESSAY_CANONICAL_MINING_STUB", "1")
    # PR-J9b: curator's 4-axis LLM rerank defaults to ON in prod; tests
    # stub it off so existing curator fixtures (which assume legacy
    # single-axis ordering) still pass. This does NOT skip curator's
    # LLM call entirely (use AUTOESSAY_CURATOR_STUB for that) — it only
    # drops the 4-axis fields collected, so _rank_sources falls
    # through to the legacy formula. Tests that exercise the 4-axis
    # path toggle this off explicitly.
    monkeypatch.setenv("AUTOESSAY_CURATOR_RERANK_STUB", "1")
    # PR-C3.a: tension_extraction phase. Operational gate
    # (TENSION_TAXONOMY_ENABLED) defaults OFF until C3.b — only tests
    # exercising the new state explicitly flip it on. The stub flag
    # below mirrors framework_lens / canonical_mining / curator_rerank
    # — when the operational gate IS flipped on for a test, the LLM
    # branch is short-circuited to the deterministic stub artifact.
    monkeypatch.setenv("AUTOESSAY_TENSION_EXTRACTION_STUB", "1")
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)

    def override_get_session() -> Iterator[Session]:
        with testing_session() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_engine] = lambda: engine
    try:
        yield testing_session
    finally:
        app.dependency_overrides.clear()
        # SQLite doesn't support ALTER TABLE DROP CONSTRAINT, so the
        # FK cycle introduced by ``branches`` (codex-AGREEd #2 stage
        # 2.C) makes drop_all fail when foreign_keys is ON. Disable
        # the pragma for the drop; the engine is disposed right after,
        # so this is local-only.
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.commit()
        Base.metadata.drop_all(engine)
        engine.dispose()
        get_settings.cache_clear()


def seed_project(session: Session, project_id: str = "proj_test") -> Project:
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
        id=project_id,
        user_id="single-user",
        title="Test project",
        domain_id="financial_history",
        domain_version="0.1.0",
        status="CREATED",
    )
    session.add(project)
    session.commit()
    return project


def seed_styled_run(app_session, tmp_path, monkeypatch, run_id: str) -> tuple[str, object]:  # type: ignore[no-untyped-def]
    from autoessay.agents.curator import run_curator
    from autoessay.agents.drafter import run_drafter
    from autoessay.agents.ideator import run_ideator, select_thesis_for_run
    from autoessay.agents.scout import run_scout
    from autoessay.agents.stylist import run_stylist
    from autoessay.agents.synthesizer import run_synthesizer
    from autoessay.models import Run
    from autoessay.run_writer import create_run_directory

    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_STYLIST_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()
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
        run_scout(run_id, session)
        run_curator(run_id, session)
        run_synthesizer(run_id, session)
        run_ideator(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        select_thesis_for_run(run, "angle_001")
        session.commit()
        run_drafter(run_id, session)
        run_stylist(run_id, session)
    return run_id, run_dir


def seed_approved_scan(app_session, tmp_path, monkeypatch, run_id: str) -> tuple[str, object]:  # type: ignore[no-untyped-def]
    import json

    from autoessay.agents.critic import run_critic
    from autoessay.models import Checkpoint, utcnow

    run_id, run_dir = seed_styled_run(app_session, tmp_path, monkeypatch, run_id)
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "1")
    get_settings.cache_clear()
    with app_session() as session:
        run_critic(run_id, session)
        session.add(
            Checkpoint(
                id=f"checkpoint_{run_id}",
                run_id=run_id,
                checkpoint_type="USER_EXTERNAL_SCAN_APPROVAL",
                status="ACCEPTED",
                decision_payload=json.dumps(
                    {"approve": True, "scan_kinds": ["plagiarism", "ai_style"]},
                    sort_keys=True,
                ),
                decided_at=utcnow(),
            ),
        )
        session.commit()
    return run_id, run_dir


def seed_integrity_ready_run(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    run_id: str,
    scan_kinds: list[str] | None = None,
) -> tuple[str, Path]:
    from autoessay.models import Checkpoint, Run, utcnow
    from autoessay.run_writer import create_run_directory

    kinds = scan_kinds or ["plagiarism", "ai_style"]
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_EXTERNAL_SCAN_APPROVAL",
        domain_id="financial_history",
    )
    styled_dir = run_dir / "drafts" / "v001" / "style"
    styled_dir.mkdir(parents=True, exist_ok=True)
    (styled_dir / "paper_styled.md").write_text(
        "# Test Paper\n\n"
        "Deposit insurance changed bank behavior during interwar banking stress.\n\n"
        "Clearinghouse networks shaped liquidity responses in local credit markets.\n",
        encoding="utf-8",
    )
    with app_session() as session:
        project = session.get(Project, "proj_test")
        if project is None:
            project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(project)
        run = Run(
            id=run_id,
            project_id=project.id,
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="USER_EXTERNAL_SCAN_APPROVAL",
            baseline_hash="test",
        )
        session.add(run)
        session.flush()
        session.add(
            Checkpoint(
                id=f"checkpoint_{run_id}",
                run_id=run_id,
                checkpoint_type="USER_EXTERNAL_SCAN_APPROVAL",
                status="ACCEPTED",
                decision_payload=json.dumps(
                    {"approve": True, "scan_kinds": kinds},
                    sort_keys=True,
                ),
                decided_at=utcnow(),
            ),
        )
        session.commit()
    return run_id, run_dir
