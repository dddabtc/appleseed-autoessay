"""Tests for migration 017 (backfill v001 phase_versions rows for
vanilla first runs).

Codex AGREE-with-amendments (2026-05-02 PR-A4 design review)
amendment 6: backfill must be safe — only insert v001 when the
run has zero phase_versions rows AND on-disk artifacts are
present.

Strategy: build three fixture run_dirs in a temp data root,
exercise the migration's logic by calling its helpers
directly (the migration uses raw SQL against the alembic
context which is awkward to drive from pytest), and assert
the resulting db state.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from autoessay.models import Branch, PhaseVersion, PhaseVersionInput, Run, RunHead


def _load_migration_module():
    """Load migration 017 by absolute path so we can call its
    helper functions in a test process."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "017_backfill_pv_first_runs.py"
    )
    spec = importlib.util.spec_from_file_location("migration_017", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_test_run(
    app_session,  # type: ignore[no-untyped-def]
    run_dir: Path,
    *,
    phases_with_artifacts: list[str],
    create_pv_for: list[str] | None = None,
) -> str:
    """Build a Run row pointing at ``run_dir`` and lay down each
    phase's sentinel file. Optionally pre-create pv rows for
    phases listed in ``create_pv_for`` to test the "skip if any
    pv exists" guard.
    """
    sentinels = {
        "scout": "discovery/scout_report.md",
        "curator": "sources/shortlist.json",
        "synthesizer": "synthesis/claims.jsonl",
        "ideator": "novelty/angle_cards.json",
        "drafter": "drafts/v001/manuscript.md",
        "stylist": "drafts/v001/style/paper_styled.md",
        "critic": "reviews/critic_report.json",
        "integrity": "integrity/integrity_summary.json",
        "exports": "exports/manifest.json",
    }
    for phase in phases_with_artifacts:
        path = run_dir / sentinels[phase]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("seed\n", encoding="utf-8")

    run_id = f"run_{uuid4().hex}"
    with app_session() as session:
        # Project + user must exist for FK.
        from autoessay.models import Domain, Project, User

        user_id = "single-user"
        if session.get(User, user_id) is None:
            session.add(User(id=user_id, display_name="Single User"))
            session.flush()
        if session.get(Domain, "financial_history") is None:
            session.add(
                Domain(
                    id="financial_history",
                    display_name="Financial history",
                    version="0.0",
                ),
            )
            session.flush()
        project_id = f"proj_{uuid4().hex}"
        session.add(
            Project(
                id=project_id,
                user_id=user_id,
                title="Migration 017 fixture",
                domain_id="financial_history",
                domain_version="0.0",
                language="en",
                status="ACTIVE",
            ),
        )
        session.flush()
        session.add(
            Run(
                id=run_id,
                project_id=project_id,
                domain_version="0.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="0" * 64,
            ),
        )
        session.flush()
        if create_pv_for:
            for phase in create_pv_for:
                session.add(
                    PhaseVersion(
                        id=f"pv_seed_{phase}_{uuid4().hex[:8]}",
                        run_id=run_id,
                        phase=phase,
                        version_no=1,
                        status="done",
                        artifacts_dir=f"phases/seed_{phase}",
                        source="agent",
                    ),
                )
        session.commit()
    return run_id


def _run_migration_upgrade(app_session) -> None:  # type: ignore[no-untyped-def]
    """Drive the migration's upgrade() with the test session bound
    to ``op.get_bind()``."""
    module = _load_migration_module()

    # ``op.get_bind()`` returns the current alembic connection.
    # In tests we bypass alembic entirely and inline the upgrade
    # body against the test session's connection.
    with app_session() as session:
        connection = session.connection()
        # Monkey-patch op.get_bind to return our connection.
        from alembic import op as _op

        original_bind = _op.get_bind
        _op.get_bind = lambda: connection
        try:
            module.upgrade()
        finally:
            _op.get_bind = original_bind
        session.commit()


def test_017_backfills_vanilla_first_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """A run with on-disk artifacts but no pv rows gets v001 rows
    + run_heads + main branch."""
    run_dir = tmp_path / "vanilla-run"
    run_id = _create_test_run(
        app_session,
        run_dir,
        phases_with_artifacts=["scout", "curator", "synthesizer"],
    )

    _run_migration_upgrade(app_session)

    with app_session() as session:
        pvs = session.scalars(
            select(PhaseVersion).where(PhaseVersion.run_id == run_id),
        ).all()
        assert {pv.phase for pv in pvs} == {"scout", "curator", "synthesizer"}
        assert all(pv.version_no == 1 for pv in pvs)
        assert all(pv.source == "agent" for pv in pvs)
        assert all(pv.status == "done" for pv in pvs)
        assert all(pv.input_snapshot_hash is None for pv in pvs)
        assert all(pv.prompt_hash is None for pv in pvs)

        # Lineage rows: synthesizer references scout + curator;
        # curator references scout; scout has no lineage.
        synth_pv = next(pv for pv in pvs if pv.phase == "synthesizer")
        synth_inputs = session.scalars(
            select(PhaseVersionInput).where(
                PhaseVersionInput.phase_version_id == synth_pv.id,
            ),
        ).all()
        upstream_phases = {row.upstream_phase for row in synth_inputs}
        assert upstream_phases == {"scout", "curator"}

        # main branch + run_heads.
        branch = session.scalars(
            select(Branch).where(Branch.run_id == run_id),
        ).one()
        assert branch.name == "main"
        run = session.get(Run, run_id)
        assert run is not None
        assert run.active_branch_id == branch.id

        heads = session.scalars(
            select(RunHead).where(RunHead.run_id == run_id),
        ).all()
        assert {h.phase for h in heads} == {"scout", "curator", "synthesizer"}


def test_017_skips_when_any_pv_already_exists(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """A run that already has a pv row for ANY phase is left alone
    for that phase (codex amendment 6: don't renumber existing
    history)."""
    run_dir = tmp_path / "pre-versioned-run"
    run_id = _create_test_run(
        app_session,
        run_dir,
        phases_with_artifacts=["scout", "curator"],
        create_pv_for=["scout"],  # scout already has a pv row
    )

    _run_migration_upgrade(app_session)

    with app_session() as session:
        scout_pvs = session.scalars(
            select(PhaseVersion)
            .where(PhaseVersion.run_id == run_id)
            .where(PhaseVersion.phase == "scout"),
        ).all()
        # Still exactly one scout pv (the seeded one).
        assert len(scout_pvs) == 1
        # Curator should still have been backfilled (it's a
        # different phase with no pre-existing rows).
        curator_pvs = session.scalars(
            select(PhaseVersion)
            .where(PhaseVersion.run_id == run_id)
            .where(PhaseVersion.phase == "curator"),
        ).all()
        assert len(curator_pvs) == 1
        assert curator_pvs[0].version_no == 1


def test_017_skips_phases_without_on_disk_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """A phase without its sentinel file is NOT backfilled (we
    cannot claim it completed)."""
    run_dir = tmp_path / "partial-run"
    run_id = _create_test_run(
        app_session,
        run_dir,
        phases_with_artifacts=["scout"],  # only scout produced output
    )

    _run_migration_upgrade(app_session)

    with app_session() as session:
        pvs = session.scalars(
            select(PhaseVersion).where(PhaseVersion.run_id == run_id),
        ).all()
        assert {pv.phase for pv in pvs} == {"scout"}


def test_017_idempotent_on_rerun(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """Running upgrade() twice is a no-op the second time."""
    run_dir = tmp_path / "idempotent-run"
    run_id = _create_test_run(
        app_session,
        run_dir,
        phases_with_artifacts=["scout"],
    )

    _run_migration_upgrade(app_session)
    _run_migration_upgrade(app_session)

    with app_session() as session:
        pvs = session.scalars(
            select(PhaseVersion).where(PhaseVersion.run_id == run_id),
        ).all()
        assert len(pvs) == 1
        assert pvs[0].phase == "scout"
        assert pvs[0].version_no == 1


def test_017_skips_runs_without_run_dir_on_disk(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    """If the run row points at a run_dir that no longer exists
    on disk (cleaned up), the migration leaves it alone."""
    missing_dir = tmp_path / "does-not-exist"
    run_id = _create_test_run(
        app_session,
        missing_dir,
        phases_with_artifacts=[],  # no artifacts created (also no dir)
    )
    # The fixture's mkdir on sentinel files normally creates the
    # parent — by passing empty phases_with_artifacts we keep the
    # dir absent.
    assert not missing_dir.exists()

    _run_migration_upgrade(app_session)

    with app_session() as session:
        pvs = session.scalars(
            select(PhaseVersion).where(PhaseVersion.run_id == run_id),
        ).all()
        assert pvs == []
