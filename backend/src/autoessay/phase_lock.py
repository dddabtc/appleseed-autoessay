"""Atomic phase-start claim (Stage 3.E follow-up P0).

Codex AGREE-with-amendments (system-wide audit P0): start_*
endpoints only check state then enqueue. Two clicks (multi-tab or
curl bypass) can both pass the state check and enqueue parallel
agent runs into the same run_dir. We add a run-level lock backed
by three columns on the ``runs`` table.

API surface:

- ``claim_phase_lock(session, run, phase, job_id)``: atomic
  ``UPDATE runs SET active_phase_lock = :phase, ...
   WHERE id = :run_id AND active_phase_lock IS NULL``. Returns
  True on success, False if another phase is already running.
- ``release_phase_lock(session, run, phase, job_id)``: atomic
  ``UPDATE ... SET active_phase_lock = NULL, ...
   WHERE id = :run_id AND active_phase_lock = :phase
     AND active_phase_lock_job_id = :job_id``. Owner-checked, so
  a crashed/late worker can't clear a newer lock that an
  admin-clear or rerun has already replaced.
- ``transfer_phase_lock(session, run, from_phase, to_phase, job_id)``:
  owner-checked phase handoff for wrapper flows that keep one job token
  while moving to the next phase.
- ``force_clear_phase_lock(session, run)``: ops escape hatch for
  zombie locks (no owner check). Use sparingly.
- ``get_active_phase_lock(run)``: read helper for UI display.

The lock is **per run**, not per (run, phase). The pipeline is
sequential — drafter → stylist → critic ... — so two phases on
the same run should never run concurrently. A run-level lock
catches double-click on the same phase AND cross-phase races
(e.g., user starts drafter then quickly clicks rerun on
synthesizer before drafter writes). codex AGREEd this granularity.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from autoessay.models import Run


def new_lock_token() -> str:
    """Generate a fresh owner token for a phase-start claim. The
    token is stored as ``active_phase_lock_job_id``; release must
    match it. Workers should pass this token through to whatever
    they pass back to ``release_phase_lock`` so a stale callback
    can't clear a newer claim.
    """
    return f"lock_{uuid.uuid4().hex}"


def claim_phase_lock(
    session: Session,
    run: Run,
    phase: str,
    job_id: str,
) -> bool:
    """Atomically claim the phase-start lock for ``run``. Returns
    True on success, False if another phase is already running.

    The transaction must be committed by the caller after this
    returns True (and after any state transition / event append
    bundled with the claim) so the lock + state move atomically.
    """
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(Run)
        .where(Run.id == run.id)
        .where(Run.active_phase_lock.is_(None))
        .values(
            active_phase_lock=phase,
            active_phase_lock_job_id=job_id,
            active_phase_lock_claimed_at=now,
        ),
    )
    # Result returned by ``session.execute(update(...))`` is in fact a
    # CursorResult that exposes ``rowcount``, but the static type
    # erases this. Cast for mypy.
    claimed = bool(getattr(result, "rowcount", 0))
    if claimed:
        # Refresh the in-memory ORM object so the caller sees the
        # new lock state without an extra round-trip.
        session.refresh(run)
    return claimed


def release_phase_lock(
    session: Session,
    run: Run,
    phase: str,
    job_id: str,
) -> bool:
    """Owner-checked release. Returns True if the row was actually
    cleared, False if a different owner held it (e.g., a previous
    worker came back late after the user manually cleared the
    lock and started a new attempt).

    Callers should NOT raise on False — it usually just means the
    worker raced with admin recovery. Log + ignore.
    """
    result = session.execute(
        update(Run)
        .where(Run.id == run.id)
        .where(Run.active_phase_lock == phase)
        .where(Run.active_phase_lock_job_id == job_id)
        .values(
            active_phase_lock=None,
            active_phase_lock_job_id=None,
            active_phase_lock_claimed_at=None,
        ),
    )
    released = bool(getattr(result, "rowcount", 0))
    if released:
        session.refresh(run)
    return released


def transfer_phase_lock(
    session: Session,
    run: Run,
    from_phase: str,
    to_phase: str,
    job_id: str,
) -> bool:
    """Owner-checked phase handoff without opening a lock-free gap.

    ``final_rewrite`` can immediately continue into ``critic`` under
    the same worker job. The run state moves to ``CRITIC_RUNNING`` once
    rewrite completes, so the visible lock must move with it; otherwise
    clients can observe ``state=CRITIC_RUNNING`` but
    ``active_phase_lock=final_rewrite`` and route to the wrong tab.
    """
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(Run)
        .where(Run.id == run.id)
        .where(Run.active_phase_lock == from_phase)
        .where(Run.active_phase_lock_job_id == job_id)
        .values(
            active_phase_lock=to_phase,
            active_phase_lock_job_id=job_id,
            active_phase_lock_claimed_at=now,
        ),
    )
    transferred = bool(getattr(result, "rowcount", 0))
    if transferred:
        session.refresh(run)
    return transferred


def force_clear_phase_lock(session: Session, run: Run) -> dict[str, str | None]:
    """Ops escape hatch — clear the lock regardless of owner.

    Use only for zombie-lock recovery (worker crash, ungraceful
    deploy, ops mistake). Returns the prior lock state for audit.
    """
    prior = {
        "phase": run.active_phase_lock,
        "job_id": run.active_phase_lock_job_id,
        "claimed_at": (
            run.active_phase_lock_claimed_at.isoformat()
            if run.active_phase_lock_claimed_at is not None
            else None
        ),
    }
    session.execute(
        update(Run)
        .where(Run.id == run.id)
        .values(
            active_phase_lock=None,
            active_phase_lock_job_id=None,
            active_phase_lock_claimed_at=None,
        ),
    )
    session.refresh(run)
    return prior


def get_active_phase_lock(run: Run) -> dict[str, str | None] | None:
    """Read helper — returns ``None`` when no lock is active."""
    if run.active_phase_lock is None:
        return None
    return {
        "phase": run.active_phase_lock,
        "job_id": run.active_phase_lock_job_id,
        "claimed_at": (
            run.active_phase_lock_claimed_at.isoformat()
            if run.active_phase_lock_claimed_at is not None
            else None
        ),
    }


@contextmanager
def phase_lock_release_on_exit(
    run_id: str,
    phase: str,
    lock_token: str | None,
    session: Session | None = None,
) -> Iterator[None]:
    """Context manager that releases the phase-start lock on exit.

    When ``session`` is provided (sync-worker / inline-from-API
    path), the release uses that same session so the test in-memory
    DB and the agent's session stay aligned. When ``session`` is
    None (true async via Redis worker), a fresh ``SessionLocal()``
    is opened.

    Yields control to the caller, then in ``finally`` calls
    ``release_phase_lock`` with the snapshot ``lock_token`` so a
    mid-flight admin force-clear + reclaim cannot fool us into
    clearing the newer lock.

    No-op when ``lock_token`` is None — used in tests and call
    sites that ran the agent without going through the API claim.
    """
    try:
        yield
    finally:
        # Crucially do NOT use ``return`` inside the finally block —
        # an early-return from a finally suppresses any pending
        # exception that's propagating out of the with-body. So we
        # gate on ``lock_token`` with an ``if`` instead.
        if lock_token is not None:
            if session is not None:
                run = session.scalar(select(Run).where(Run.id == run_id))
                if run is not None:
                    release_phase_lock(session, run, phase, lock_token)
                    session.commit()
                    _maybe_trigger_auto_advance(session, run)
            else:
                from autoessay.db import SessionLocal

                with SessionLocal() as cleanup:
                    run = cleanup.scalar(select(Run).where(Run.id == run_id))
                    if run is not None:
                        release_phase_lock(cleanup, run, phase, lock_token)
                        cleanup.commit()
                        _maybe_trigger_auto_advance(cleanup, run)


def _maybe_trigger_auto_advance(session: Session, run: Run) -> None:
    """PR-382 hook: phase has finished, lock released, state has
    been transitioned (typically into a ``USER_*_REVIEW``). If the
    run opted into auto-pilot, call the coordinator to fire the
    next phase automatically.

    Lazy import so this module stays importable even if
    ``auto_advance`` ever takes a hard dep on phase_lock helpers.
    Never raises — the coordinator catches its own exceptions and
    emits audit events instead.
    """
    if not getattr(run, "auto_advance", False):
        return
    try:
        from autoessay.auto_advance import maybe_advance

        maybe_advance(session, run, source="phase_done")
    except Exception:  # noqa: BLE001 — never let auto-pilot crash phase exit
        import logging

        logging.getLogger(__name__).exception(
            "auto_advance hook raised for run=%s; phase exit continues",
            run.id,
        )


__all__ = [
    "claim_phase_lock",
    "force_clear_phase_lock",
    "get_active_phase_lock",
    "new_lock_token",
    "phase_lock_release_on_exit",
    "release_phase_lock",
    "transfer_phase_lock",
]
