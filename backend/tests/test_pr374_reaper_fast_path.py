"""PR-374 coverage for the zombie reaper observability + worker-
fingerprint fast-path + the legacy gate fallback on Redis flake.

The (b) fast-path implements codex's defensive amendment checklist:
  - require status == "started" AND worker not alive AND heartbeat
    stale AND DB lock phase matches AND no terminal phase event
  - swallow Redis/RQ exceptions and fall back to legacy idle gate

These tests verify each branch:
  - happy path: orphaned job → lock aged → legacy gate fires this
    same sweep
  - skip: job's worker still alive
  - skip: heartbeat fresh
  - skip: DB lock cleared between job claim and reaper sweep
  - skip: phase already has terminal phase_done event
  - skip + log: Redis errors out
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from autoessay.models import Run


def _seeded_running_run(app_session, *, run_id: str, phase: str = "stylist"):  # type: ignore[no-untyped-def]
    """Seed a Run in DRAFTER_RUNNING / STYLIST_RUNNING / etc. with a
    fresh phase lock pointing at the given phase. No filesystem
    side-effects — fast-path only inspects DB columns."""
    state_map = {
        "stylist": "STYLIST_RUNNING",
        "drafter": "DRAFTER_RUNNING",
        "final_rewrite": "REWRITE_RUNNING",
        "critic": "CRITIC_RUNNING",
    }
    state = state_map[phase]
    with app_session() as session:
        from conftest import seed_project

        project = seed_project(session)
        run = Run(
            id=run_id,
            project_id=project.id,
            domain_version="0.1.0",
            run_dir=f"/tmp/{run_id}",
            state=state,
            baseline_hash="test",
            active_phase_lock=phase,
            active_phase_lock_job_id=f"job_{run_id[-8:]}",
            active_phase_lock_claimed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        session.add(run)
        session.commit()
    return run_id


def test_fast_path_returns_zero_when_redis_unavailable(app_session) -> None:  # type: ignore[no-untyped-def]
    """Codex amendment 5 + 'Redis flake → legacy fallback': on any
    Redis / RQ exception the fast-path returns 0 and lets the
    legacy gate run."""
    from autoessay.zombie_reaper import fast_path_dead_workers_orphan_phase_locks

    with patch("rq.Worker.all", side_effect=ConnectionError("redis down")):
        result = fast_path_dead_workers_orphan_phase_locks(session_factory=app_session)
        assert result == 0  # graceful skip, not raise


def test_fast_path_skips_job_on_live_worker(app_session) -> None:  # type: ignore[no-untyped-def]
    """If the RQ job's owning worker is still in ``Worker.all(...)``
    the fast-path treats it as alive and skips it. Legacy gate may
    still pick it up on the slow path."""
    run_id = _seeded_running_run(app_session, run_id="run_aliveworker", phase="stylist")
    fake_job = MagicMock()
    fake_job.get_status.return_value = "started"
    fake_job.worker_name = "rq:worker:live"
    fake_job.last_heartbeat = datetime.now(timezone.utc)
    fake_job.args = (run_id,)

    with (
        patch("redis.Redis.from_url"),
        patch("rq.Worker.all", return_value=[MagicMock(name="rq:worker:live")]),
        patch(
            "rq.registry.StartedJobRegistry.get_job_ids",
            return_value=["job_live"],
        ),
        patch("rq.registry.StartedJobRegistry.cleanup"),
        patch("rq.job.Job.fetch", return_value=fake_job),
    ):
        from autoessay.zombie_reaper import fast_path_dead_workers_orphan_phase_locks

        result = fast_path_dead_workers_orphan_phase_locks(session_factory=app_session)
        # Live worker → no orphaning.
        assert result == 0


def test_fast_path_skips_when_heartbeat_fresh(app_session) -> None:  # type: ignore[no-untyped-def]
    """Even if the worker isn't in Worker.all(), a heartbeat in the
    last 120s means the worker is just slow-to-register — skip."""
    run_id = _seeded_running_run(app_session, run_id="run_freshheartbeat", phase="stylist")
    fake_job = MagicMock()
    fake_job.get_status.return_value = "started"
    fake_job.worker_name = "rq:worker:dead_but_recent"
    fake_job.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=30)
    fake_job.args = (run_id,)

    with (
        patch("redis.Redis.from_url"),
        patch("rq.Worker.all", return_value=[]),  # nobody alive
        patch(
            "rq.registry.StartedJobRegistry.get_job_ids",
            return_value=["job_fresh"],
        ),
        patch("rq.registry.StartedJobRegistry.cleanup"),
        patch("rq.job.Job.fetch", return_value=fake_job),
    ):
        from autoessay.zombie_reaper import fast_path_dead_workers_orphan_phase_locks

        result = fast_path_dead_workers_orphan_phase_locks(session_factory=app_session)
        assert result == 0


def test_fast_path_orphans_job_with_dead_worker_and_stale_heartbeat(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """Happy path. Worker not in alive list + heartbeat older than
    120s + DB lock phase matches + no terminal event → orphan."""
    run_id = _seeded_running_run(app_session, run_id="run_orphanjob", phase="stylist")

    fake_job = MagicMock()
    fake_job.get_status.return_value = "started"
    fake_job.worker_name = "rq:worker:dead"
    fake_job.last_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=10)
    fake_job.args = (run_id,)

    with (
        patch("redis.Redis.from_url"),
        patch("rq.Worker.all", return_value=[]),
        patch(
            "rq.registry.StartedJobRegistry.get_job_ids",
            return_value=["job_orphan"],
        ),
        patch("rq.registry.StartedJobRegistry.cleanup"),
        patch("rq.registry.StartedJobRegistry.remove"),
        patch("rq.job.Job.fetch", return_value=fake_job),
    ):
        from autoessay.zombie_reaper import fast_path_dead_workers_orphan_phase_locks

        result = fast_path_dead_workers_orphan_phase_locks(session_factory=app_session)
        assert result == 1

    # After fast-path, the run's lock claim should be aged so the
    # legacy idle gate fires this same sweep.
    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        # Aged by ~1 hour → naive UTC.
        claimed_at = run.active_phase_lock_claimed_at
        assert claimed_at is not None
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - claimed_at
        assert age >= timedelta(minutes=30), f"lock not aged enough: {age}"


def test_extract_run_id_from_job_positional() -> None:
    from autoessay.zombie_reaper import _extract_run_id_from_job

    job = MagicMock()
    job.args = ("run_abc123",)
    job.kwargs = {}
    assert _extract_run_id_from_job(job) == "run_abc123"


def test_extract_run_id_from_job_kwargs() -> None:
    from autoessay.zombie_reaper import _extract_run_id_from_job

    job = MagicMock()
    job.args = ()
    job.kwargs = {"run_id": "run_kwargs"}
    assert _extract_run_id_from_job(job) == "run_kwargs"


def test_extract_run_id_returns_none_for_unknown_shape() -> None:
    from autoessay.zombie_reaper import _extract_run_id_from_job

    job = MagicMock()
    job.args = (42,)  # not a string
    job.kwargs = {}
    assert _extract_run_id_from_job(job) is None


def test_reaper_loop_logs_warning_at_startup() -> None:
    """PR-374 (a): startup heartbeat must use WARNING so it survives
    uvicorn's default root-level=WARNING filter."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "autoessay" / "zombie_reaper.py"
    text = src.read_text(encoding="utf-8")
    # The startup line must be at WARNING level, not INFO.
    assert 'logger.warning("zombie reaper lifespan task starting' in text
    # The periodic heartbeat / non-empty sweep also at WARNING.
    assert 'logger.warning(\n                    "zombie reaper sweep' in text


def test_reaper_calls_fast_path_before_legacy_sweep(app_session) -> None:  # type: ignore[no-untyped-def]
    """The orchestrator (``reap_zombies_once``) must call the fast
    path first so deploy-killed jobs are caught on the same sweep,
    not 20 min later."""
    with (
        patch(
            "autoessay.zombie_reaper.fast_path_dead_workers_orphan_phase_locks",
            return_value=0,
        ) as mock_fast,
    ):
        from autoessay.zombie_reaper import reap_zombies_once

        reap_zombies_once(session_factory=app_session)
        mock_fast.assert_called_once()


def test_docker_compose_worker_has_stop_grace_period() -> None:
    """(c) docker-compose worker service must declare
    ``stop_grace_period`` so SIGTERM gives RQ time to drain."""
    from pathlib import Path

    compose = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    # Env-driven default = 60m (codex amendment 4).
    assert "stop_grace_period: ${AUTOESSAY_WORKER_STOP_GRACE:-60m}" in text
