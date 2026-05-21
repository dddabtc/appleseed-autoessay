"""PR-I2.a — zombie reaper tests.

Covers:
  * ``reap_zombies_once`` calls ``_recover_zombie_running_phase`` for
    each run in a ``*_RUNNING`` state, derives phase via the reverse
    map, and skips runs in non-running states.
  * Single-run reaping correctly transitions a stale
    ``SYNTHESIZER_RUNNING`` to ``FAILED_FIXABLE`` with a
    ``zombie_recovered`` ``phase_failed`` event.
  * ``zombie_reaper_lifespan`` is no-op when the env flag is False.
  * ``zombie_reaper_lifespan`` spawns + cancels the loop cleanly when
    the env flag is True.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from autoessay.config import get_settings
from autoessay.models import Project, Run, RunEvent, User
from autoessay.zombie_reaper import (
    reap_zombies_once,
    zombie_reaper_lifespan,
)


def _ensure_user_project(session) -> tuple[User, Project]:
    from autoessay.models import Domain

    user = session.scalar(select(User).where(User.id == "user_zombie"))
    if user is None:
        user = User(
            id="user_zombie",
            oidc_subject="subject-zombie",
            oidc_issuer="https://auth.example.test/casdoor",
            email="zombie@example.test",
            display_name="Zombie",
        )
        session.add(user)
        session.flush()
    domain = session.scalar(select(Domain).where(Domain.id == "general_academic"))
    if domain is None:
        domain = Domain(
            id="general_academic",
            display_name="General Academic",
            version="0.0",
        )
        session.add(domain)
        session.flush()
    project = session.scalar(select(Project).where(Project.id == "proj_zombie"))
    if project is None:
        project = Project(
            id="proj_zombie",
            user_id=user.id,
            title="Zombie test",
            domain_id="general_academic",
            domain_version="0.0",
            language="en",
            status="ACTIVE",
        )
        session.add(project)
        session.flush()
    return user, project


def _make_zombie_run(session, run_id: str, tmp_path: Path) -> Run:
    """Insert a synthesizer-running run with a stale phase lock so it
    looks like a SIGKILL victim from 2h ago."""
    from autoessay.state_machine import append_event, transition

    _ensure_user_project(session)
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run = Run(
        id=run_id,
        project_id="proj_zombie",
        run_dir=str(run_dir),
        state="TOPIC_ENTERED",
        baseline_hash="0" * 64,
        domain_version="0.0",
    )
    session.add(run)
    session.flush()
    for state in (
        "DOMAIN_LOADED",
        "PROPOSAL_DRAFTING",
        "USER_PROPOSAL_REVIEW",
        "SCOUT_RUNNING",
        "USER_SEARCH_REVIEW",
        "CURATOR_RUNNING",
        "USER_DEEP_DIVE_REVIEW",
        "SYNTHESIZER_RUNNING",
    ):
        transition(run, state, session, reason="test fixture")
    append_event(session, run, "phase_started", {"phase": "synthesizer", "run_id": run.id})
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    stale_naive = stale.replace(tzinfo=None)
    for ev in session.scalars(select(RunEvent).where(RunEvent.run_id == run.id)).all():
        ev.created_at = stale_naive
    run.active_phase_lock = "synthesizer"
    run.active_phase_lock_job_id = "lock_test"
    # PR-I1's helper compares lock_claimed_at against an aware cutoff;
    # SQLite returns naive on read so we must store aware here for
    # _recover_zombie_running_phase to detect the lock as dead.
    run.active_phase_lock_claimed_at = stale
    run.updated_at = stale_naive
    session.commit()
    return run


def test_reap_zombies_once_recovers_stuck_synthesizer(app_session, tmp_path: Path) -> None:
    with app_session() as session:
        run = _make_zombie_run(session, "run_zombie_synth", tmp_path)
        run_id = run.id

    visited = reap_zombies_once(session_factory=app_session)
    assert visited >= 1

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"
        assert run.active_phase_lock is None
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at.desc()),
        ).all()
        latest = events[0]
        assert latest.event_type == "phase_failed"


def test_reap_zombies_once_skips_runs_in_non_running_state(app_session, tmp_path: Path) -> None:
    with app_session() as session:
        _ensure_user_project(session)
        run_dir = tmp_path / "data" / "runs" / "run_idle"
        run_dir.mkdir(parents=True, exist_ok=True)
        run = Run(
            id="run_idle",
            project_id="proj_zombie",
            run_dir=str(run_dir),
            state="USER_FIELD_REVIEW",
            baseline_hash="0" * 64,
            domain_version="0.0",
        )
        session.add(run)
        session.commit()

    visited = reap_zombies_once(session_factory=app_session)

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_idle"))
        assert run is not None
        assert run.state == "USER_FIELD_REVIEW"
        # The reaper visits 0 here because USER_FIELD_REVIEW isn't in
        # _PHASE_RUNNING_STATE → not selected by the query.
    assert visited == 0


def test_reap_zombies_once_idempotent_on_already_recovered_run(app_session, tmp_path: Path) -> None:
    """Second pass over the same zombie should be a no-op (run is no
    longer in *_RUNNING after the first pass)."""
    with app_session() as session:
        _make_zombie_run(session, "run_zombie_idem", tmp_path)
    reap_zombies_once(session_factory=app_session)
    visited_second = reap_zombies_once(session_factory=app_session)
    assert visited_second == 0


def test_zombie_reaper_default_is_enabled(monkeypatch) -> None:
    """PR-I3: ``zombie_reaper_enabled`` default flipped to True so new
    deployments get self-healing without ops needing to remember an
    env flag. Regression guard against accidental flip back to False.
    """
    monkeypatch.delenv("AUTOESSAY_ZOMBIE_REAPER_ENABLED", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.zombie_reaper_enabled is True
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_zombie_reaper_lifespan_noop_when_disabled(monkeypatch) -> None:
    """When AUTOESSAY_ZOMBIE_REAPER_ENABLED is unset/False, the lifespan
    helper must not spawn the loop — the cheap guard for default-OFF."""
    monkeypatch.setenv("AUTOESSAY_ZOMBIE_REAPER_ENABLED", "0")
    get_settings.cache_clear()

    spawn_count = 0

    async def fake_loop():
        nonlocal spawn_count
        spawn_count += 1

    with patch("autoessay.zombie_reaper._reaper_loop", fake_loop):
        async with zombie_reaper_lifespan():
            pass
    assert spawn_count == 0
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_zombie_reaper_lifespan_cancels_loop_on_exit(monkeypatch) -> None:
    """When enabled, the lifespan helper must spawn + cancel the loop
    cleanly so uvicorn shutdown doesn't hang."""
    monkeypatch.setenv("AUTOESSAY_ZOMBIE_REAPER_ENABLED", "1")
    monkeypatch.setenv("AUTOESSAY_ZOMBIE_REAPER_INTERVAL_SECONDS", "30")
    get_settings.cache_clear()

    iterations = 0

    async def fake_loop():
        nonlocal iterations
        try:
            while True:
                iterations += 1
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise

    with patch("autoessay.zombie_reaper._reaper_loop", fake_loop):
        async with zombie_reaper_lifespan():
            await asyncio.sleep(0.15)
    assert iterations >= 2

    monkeypatch.setenv("AUTOESSAY_ZOMBIE_REAPER_ENABLED", "0")
    get_settings.cache_clear()


def test_worker_startup_sweep_swallows_exceptions(monkeypatch) -> None:
    """PR-I3: worker startup zombie sweep is best-effort. If
    ``reap_zombies_once`` raises, ``_startup_zombie_sweep`` must log
    and return — never propagate, never block worker startup.
    """
    from autoessay import worker as worker_mod

    def boom() -> int:
        raise RuntimeError("simulated DB outage at boot")

    monkeypatch.setattr(worker_mod, "reap_zombies_once", boom)
    # Should NOT raise.
    worker_mod._startup_zombie_sweep()


def test_worker_startup_sweep_calls_reap_zombies_once(monkeypatch) -> None:
    """Happy-path: ``_startup_zombie_sweep`` invokes the reaper exactly
    once and returns the visited count to the logs."""
    from autoessay import worker as worker_mod

    calls: list[int] = []

    def fake_reap() -> int:
        calls.append(1)
        return 3

    monkeypatch.setattr(worker_mod, "reap_zombies_once", fake_reap)
    worker_mod._startup_zombie_sweep()
    assert len(calls) == 1
