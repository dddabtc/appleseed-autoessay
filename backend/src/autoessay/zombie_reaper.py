"""PR-I2.a — proactive zombie-phase-lock reaper.

Closes the gap between PR-I1 (lazy zombie recovery on ``start_*``
clicks) and the prod failure mode where users sit on a stuck
``*_RUNNING`` page for hours without retrying. The reaper runs on a
uvicorn lifespan task: every ``AUTOESSAY_ZOMBIE_REAPER_INTERVAL_SECONDS``
(default 300) it scans every run in a ``*_RUNNING`` state and calls
PR-I1's ``_recover_zombie_running_phase`` per (run, phase). Recovery
semantics are identical (codex Q2 amendment — must not just look at
``lock_claimed_at`` age; reuses the (lock age + last-phase-event idle
+ no terminal event) compound check).

Default ON since PR-I3 (`Settings.zombie_reaper_enabled` defaults
True in `config.py`). Override with `AUTOESSAY_ZOMBIE_REAPER_ENABLED=0`
to disable. Single-uvicorn deployments are safe; multi-replica
needs a DB-level reaper-lease row first (see Multi-replica below).

Multi-replica safety: each pass runs in its own DB transaction, and
``_recover_zombie_running_phase`` itself is idempotent — once a run is
moved to ``FAILED_FIXABLE`` it's no longer in ``RUNNING_STATES`` and
subsequent passes skip it. For multi-replica deployments we'd add a
DB-level reaper-lease row; the single-uvicorn deployment can skip
that.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.models import Run

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


# Reverse of main.py::_PHASE_RUNNING_STATE — derive phase from a run's
# *_RUNNING state. Imported lazily inside the reaper to avoid a
# circular import on module load.
def _state_to_phase_map() -> dict[str, str]:
    from autoessay.main import _PHASE_RUNNING_STATE

    return {state: phase for phase, state in _PHASE_RUNNING_STATE.items()}


def reap_zombies_once(
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> int:
    """Single sweep: scan runs in ``*_RUNNING`` states; call
    ``_recover_zombie_running_phase`` per candidate. Returns the
    number of (run, phase) pairs the recovery helper visited (NOT the
    number actually transitioned — the helper itself is idempotent /
    no-op when the gate doesn't trigger).

    Synchronous on purpose — the reaper coroutine schedules this in a
    threadpool via ``asyncio.to_thread`` so the long-running DB scan
    doesn't block the event loop.

    PR-374 (b): run the RQ worker-fingerprint fast-path first. The
    legacy idle-threshold gate stays as the safety net.

    ``session_factory`` is optional for testability — callers can pass
    a context-manager-yielding factory bound to a non-default engine.
    Defaults to ``autoessay.db.SessionLocal``.
    """
    from autoessay.main import _recover_zombie_running_phase

    # PR-374 (b): orphan-RQ-job fast-path. On Redis / RQ failure we
    # log + fall back to the legacy idle-threshold sweep so reaper
    # liveness never depends on Redis being healthy.
    try:
        fast_path_dead_workers_orphan_phase_locks(session_factory)
    except Exception:  # noqa: BLE001 — never let RQ issues block legacy sweep
        logger.exception(
            "zombie reaper RQ fast-path failed; falling back to idle-threshold sweep",
        )

    state_to_phase = _state_to_phase_map()
    running_states = set(state_to_phase.keys())
    factory = session_factory or SessionLocal
    visited = 0
    with factory() as session:
        runs = session.scalars(
            select(Run).where(Run.state.in_(running_states)),
        ).all()
        for run in runs:
            phase = state_to_phase.get(run.state)
            if phase is None:
                continue
            try:
                _recover_zombie_running_phase(session, run, phase)
                session.commit()
                visited += 1
            except Exception:  # noqa: BLE001 — one bad run shouldn't kill the sweep
                logger.exception(
                    "zombie reaper failed for run=%s phase=%s; continuing",
                    run.id,
                    phase,
                )
                session.rollback()
    # PR-388: piggyback on the reaper schedule to resume auto_advance
    # runs left stranded at a review gate by a container restart or
    # an enqueue failure. Best-effort — never let resume errors abort
    # the zombie-recovery primary path.
    try:
        from autoessay.auto_advance import resume_auto_advance_idle_runs

        resume_auto_advance_idle_runs(session_factory)
    except Exception:  # noqa: BLE001
        logger.exception(
            "auto_advance resume sweep failed; zombie-recovery still committed",
        )
    return visited


def fast_path_dead_workers_orphan_phase_locks(
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> int:
    """PR-374 (b): orphan-RQ-job fast-path for the zombie reaper.

    Detects RQ jobs in the ``started_job_registry`` whose owning worker
    is no longer alive (worker container restart, OOM, SIGKILL) and
    immediately ages the matching run's phase lock so
    ``_recover_zombie_running_phase`` fires on the same sweep instead
    of waiting the full 20-min idle threshold. The compound gate stays
    in charge: this helper only short-circuits the "wait for the lock
    to age out" step.

    Codex AGREE-WITH-AMENDMENTS PR-374 (2026-05-13). Amendments
    applied:
      - Reuse RQ's own ``StartedJobRegistry.cleanup()`` first so the
        registry's TTL-driven sweep moves obvious stragglers to
        ``FailedJobRegistry`` before we look.
      - Require ALL of: status == "started", worker_name not in
        alive list, last_heartbeat stale (older than the registry's
        own threshold), job's run_id / phase / lock_token matches the
        DB's currently-held lock, AND no terminal phase_done /
        phase_failed event for the run+phase. Anything else and we
        skip — let the legacy idle sweep handle it on a later pass.
      - Redis / RQ exceptions are swallowed and logged at WARNING; the
        legacy sweep still runs. Reaper liveness never depends on
        Redis being healthy.

    Returns the number of (run, phase) pairs marked as orphaned. The
    caller's legacy ``_recover_zombie_running_phase`` will then
    transition these to ``FAILED_FIXABLE``.
    """
    from datetime import datetime, timedelta, timezone

    from autoessay.config import get_settings

    settings = get_settings()
    try:
        # Lazy import so the module loads even when redis isn't
        # configured (unit test default).
        from redis import Redis
        from rq import Queue, Worker
        from rq.job import Job, JobStatus
        from rq.registry import StartedJobRegistry
    except ImportError:
        logger.warning("zombie reaper fast-path: RQ / redis not importable; skipping")
        return 0

    try:
        redis_conn = Redis.from_url(settings.redis_url)
        queue_name = settings.rq_queue_name
        queue = Queue(queue_name, connection=redis_conn)
        registry = StartedJobRegistry(queue=queue)
        # Best-effort: ask RQ to sweep its own expired entries first.
        # Cleanup signature differs across versions; catch + continue.
        import contextlib

        try:
            registry.cleanup()
        except TypeError:
            # Older RQ wants an explicit timestamp arg.
            with contextlib.suppress(Exception):
                registry.cleanup(datetime.now(timezone.utc).timestamp())
        except Exception:  # noqa: BLE001
            pass
        alive_workers = {w.name for w in Worker.all(queue=queue)}
        started_ids = registry.get_job_ids()
    except Exception:  # noqa: BLE001 — Redis flake → legacy gate
        logger.warning(
            "zombie reaper fast-path: redis/RQ probe failed; falling back to legacy gate",
            exc_info=True,
        )
        return 0

    if not started_ids:
        return 0

    # Stale heartbeat threshold: RQ's own ``job_timeout + grace``; we
    # use 2x heartbeat interval (default 30s) = 60s past the
    # heartbeat. Don't mark a 1-min-stale worker as dead since RQ
    # might just be on a slow heartbeat tick.
    stale_after = timedelta(seconds=120)
    now = datetime.now(timezone.utc)
    factory = session_factory or SessionLocal
    orphaned = 0
    with factory() as session:
        for job_id in started_ids:
            try:
                job = Job.fetch(job_id, connection=redis_conn)
            except Exception:  # noqa: BLE001 — job vanished between get_job_ids and fetch
                continue
            try:
                # Defensive checks per codex amendment.
                if job.get_status() != JobStatus.STARTED:
                    continue
                worker_name = getattr(job, "worker_name", None)
                if worker_name and worker_name in alive_workers:
                    continue  # job is on a live worker
                last_heartbeat = getattr(job, "last_heartbeat", None)
                if last_heartbeat is not None:
                    if last_heartbeat.tzinfo is None:
                        last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
                    if now - last_heartbeat < stale_after:
                        continue  # recent heartbeat, give it another sweep
                # Extract run_id from job args.
                run_id = _extract_run_id_from_job(job)
                if run_id is None:
                    continue
                run = session.get(Run, run_id)
                if run is None:
                    continue
                if run.state not in _state_to_phase_map():
                    continue  # not in a *_RUNNING state, leave it
                expected_phase = _state_to_phase_map()[run.state]
                # Lock-token / phase match: the DB lock should be the
                # same phase the orphaned job claimed. If they don't
                # match, the DB has already been cleaned up or another
                # phase took over; skip to avoid double-recovery.
                current_lock = run.active_phase_lock
                if current_lock is None:
                    # Lock already cleared somehow; let the legacy
                    # event-idle gate handle the state transition.
                    continue
                if current_lock != expected_phase:
                    continue
                # No terminal phase event for this phase since
                # phase_started.
                if _has_terminal_phase_event(session, run.id, expected_phase):
                    continue
                # All defensive checks passed: orphaned job.
                logger.warning(
                    "zombie reaper fast-path: orphaned RQ job "
                    "run=%s phase=%s job_id=%s worker_name=%s last_heartbeat=%s",
                    run.id,
                    expected_phase,
                    job_id,
                    worker_name,
                    last_heartbeat.isoformat() if last_heartbeat else "<none>",
                )
                # Age the lock so the legacy gate fires this sweep
                # instead of waiting 20 min.
                run.active_phase_lock_claimed_at = (
                    (now - timedelta(hours=1)).astimezone(timezone.utc).replace(tzinfo=None)
                )
                session.commit()
                # Mark the RQ job as failed so the registry is clean.
                try:
                    job.set_status(JobStatus.FAILED)
                    job.save_meta()
                    registry.remove(job_id)
                except Exception:  # noqa: BLE001
                    pass
                orphaned += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "zombie reaper fast-path: failed processing job_id=%s; continuing",
                    job_id,
                )
                session.rollback()
    if orphaned > 0:
        logger.warning(
            "zombie reaper fast-path: marked %d orphaned RQ job(s) for immediate recovery",
            orphaned,
        )
    return orphaned


def _extract_run_id_from_job(job: object) -> str | None:
    """Best-effort extraction of ``run_id`` from an RQ ``Job``. Every
    autoessay phase job has ``run_id`` as the first positional arg
    (see ``worker._enqueue_phase`` call sites)."""
    args = getattr(job, "args", None) or ()
    if args and isinstance(args[0], str) and args[0].startswith("run_"):
        return args[0]
    kwargs = getattr(job, "kwargs", None) or {}
    rid = kwargs.get("run_id") if isinstance(kwargs, dict) else None
    if isinstance(rid, str) and rid.startswith("run_"):
        return rid
    return None


def _has_terminal_phase_event(session: Session, run_id: str, phase: str) -> bool:
    """Return True if the run already has a ``phase_done`` or
    ``phase_failed`` event for the given phase since the last
    ``phase_started`` — same predicate the legacy gate uses."""
    from autoessay.models import RunEvent

    rows = session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id)
        .order_by(RunEvent.created_at.desc())
        .limit(50),
    ).all()
    for row in rows:
        if row.event_type == "phase_started":
            # Walked back to the most recent phase_started. If we
            # haven't seen a terminal event yet, the phase didn't
            # finish.
            return False
        if row.event_type in {"phase_done", "phase_failed"}:
            # Need to check this is for the same phase.
            import json as _json

            try:
                payload = _json.loads(row.payload or "{}")
            except (ValueError, TypeError):
                continue
            if payload.get("phase") == phase:
                return True
    return False


async def _reaper_loop() -> None:
    """Background coroutine — sleeps ``zombie_reaper_interval_seconds``
    between sweeps. Survives individual sweep exceptions (logged and
    counted, not propagated)."""
    settings = get_settings()
    interval = settings.zombie_reaper_interval_seconds
    # PR-374 (a): upgrade startup heartbeat to WARNING so it survives
    # uvicorn's default root-level=WARNING filter. Without this we
    # had no way to tell from prod docker logs whether the lifespan
    # task actually started (live-discovered 2026-05-13 when 2 zombies
    # piled up despite the reaper being "enabled"). Per-sweep INFO
    # logs stay below the filter so we don't spam logs with empty
    # sweeps; sweeps that actually reaped something log a WARNING.
    logger.warning("zombie reaper lifespan task starting: interval=%ds", interval)
    sweep_count = 0
    while True:
        sweep_count += 1
        try:
            visited = await asyncio.to_thread(reap_zombies_once)
            # Heartbeat WARNING every 12 sweeps (~1h at default 300s
            # interval) so we can confirm liveness from prod logs
            # without trusting INFO visibility. Reaped runs always
            # log a WARNING below regardless of cadence.
            if visited > 0 or sweep_count % 12 == 0:
                logger.warning(
                    "zombie reaper sweep #%d: visited=%d candidates",
                    sweep_count,
                    visited,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — log + continue, don't crash the loop
            logger.exception("zombie reaper sweep failed; will retry next interval")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


@asynccontextmanager
async def zombie_reaper_lifespan() -> AsyncIterator[None]:
    """Lifespan helper: spawn the reaper on enter; cancel on exit.
    No-op when ``zombie_reaper_enabled`` is False."""
    settings = get_settings()
    if not settings.zombie_reaper_enabled:
        yield
        return
    task = asyncio.create_task(_reaper_loop(), name="zombie_reaper")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — don't break shutdown over reaper teardown
            logger.exception("zombie reaper task raised during shutdown")


__all__ = ["reap_zombies_once", "zombie_reaper_lifespan"]
