"""PR-393 regression: auto-pilot left runs stranded at
``DRAFTER_RUNNING`` because the drafter agent emits ``phase_done``
without transitioning state (per-design, the UI flow expects a user
click on ``phase-action-stylist``). PR-382's ``_DISPATCH`` only
handled ``USER_*_REVIEW`` states, so ``maybe_advance(source=
"phase_done")`` returned False and the run sat there forever.

Real reproduction: auto-pilot live test 2026-05-13 (Test #1) ran
drafter cleanly in 4 min, phase_done emitted, ``active_phase_lock``
released — and the run sat at DRAFTER_RUNNING for 30+ minutes
until manual ``POST /api/runs/{id}/stylist``.

Codex AGREE B' 2026-05-13: only bridge ``DRAFTER_RUNNING ->
start_stylist``. Stylist self-transitions to USER_REVISION_REVIEW
on success, where ``_advance_revision_review`` picks up the chain
(critic wrapper handles rewrite slice). Don't add similar bridges
for STYLIST/REWRITE/CRITIC.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.auto_advance import (
    _DISPATCH,
    _phase_finished_without_transition,
    maybe_advance,
)
from autoessay.main import app
from autoessay.models import Run, RunEvent


def test_drafter_running_is_in_dispatch() -> None:
    assert "DRAFTER_RUNNING" in _DISPATCH


async def _make_run_at_drafter(  # type: ignore[no-untyped-def]
    client: AsyncClient,
    *,
    title: str,
    auto_advance: bool = True,
) -> str:
    proj = await client.post(
        "/api/projects",
        json={
            "title": title,
            "domain_id": "financial_history",
            "target_journal": None,
        },
    )
    project_id = proj.json()["id"]
    with patch("autoessay.auto_advance.maybe_advance", return_value=False):
        run_resp = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"auto_advance": auto_advance},
        )
    return run_resp.json()["id"]


def _seed_phase_started(
    session,
    run_id: str,
    phase: str,  # type: ignore[no-untyped-def]
) -> None:
    from uuid import uuid4

    session.add(
        RunEvent(
            id=f"evt_{uuid4().hex[:24]}",
            run_id=run_id,
            event_type="phase_started",
            payload=json.dumps({"phase": phase}),
        ),
    )
    session.commit()


def _seed_phase_done(
    session,
    run_id: str,
    phase: str,  # type: ignore[no-untyped-def]
) -> None:
    from uuid import uuid4

    session.add(
        RunEvent(
            id=f"evt_{uuid4().hex[:24]}",
            run_id=run_id,
            event_type="phase_done",
            payload=json.dumps({"phase": phase, "next_stage": "stylist_pending"}),
        ),
    )
    session.commit()


async def test_phase_finished_helper_done_after_started(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _make_run_at_drafter(client, title="PR-393 helper")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        _seed_phase_started(session, run_id, "drafter")
        _seed_phase_done(session, run_id, "drafter")
        assert _phase_finished_without_transition(session, run, "drafter") is True


async def test_phase_finished_helper_started_without_done(app_session) -> None:  # type: ignore[no-untyped-def]
    """Worker still mid-drafter (no phase_done yet) → must return
    False so we don't double-enqueue stylist."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _make_run_at_drafter(client, title="PR-393 in-flight")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        _seed_phase_started(session, run_id, "drafter")
        assert _phase_finished_without_transition(session, run, "drafter") is False


async def test_phase_finished_helper_phase_failed(app_session) -> None:  # type: ignore[no-untyped-def]
    """Most recent drafter event is phase_failed → don't chain to
    stylist (the user has to deal with FAILED_FIXABLE)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _make_run_at_drafter(client, title="PR-393 failed")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        _seed_phase_started(session, run_id, "drafter")
        from uuid import uuid4

        session.add(
            RunEvent(
                id=f"evt_{uuid4().hex[:24]}",
                run_id=run_id,
                event_type="phase_failed",
                payload=json.dumps({"phase": "drafter"}),
            ),
        )
        session.commit()
        assert _phase_finished_without_transition(session, run, "drafter") is False


async def test_advance_drafter_running_fires_stylist(app_session) -> None:  # type: ignore[no-untyped-def]
    """End-to-end on the dispatch path: ``maybe_advance`` on a
    DRAFTER_RUNNING run with phase_done and lock=None must call
    ``enqueue_stylist_job``."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _make_run_at_drafter(client, title="PR-393 dispatch async")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        run.state = "DRAFTER_RUNNING"
        run.active_phase_lock = None
        _seed_phase_started(session, run_id, "drafter")
        _seed_phase_done(session, run_id, "drafter")
        session.commit()

        with (
            patch("autoessay.main._claim_or_409", return_value="tok-stylist"),
            patch(
                "autoessay.config.get_settings",
                lambda: type("S", (), {"sync_worker": False})(),
            ),
            patch(
                "autoessay.worker.enqueue_stylist_job",
                return_value="job-id-stylist",
            ) as enqueue,
        ):
            advanced = maybe_advance(session, run, source="phase_done")

        assert advanced is True
        enqueue.assert_called_once()


async def test_advance_drafter_running_no_op_with_active_lock(app_session) -> None:  # type: ignore[no-untyped-def]
    """While the worker still holds the drafter lock, the chain
    handler must return False — drafter is still in flight, double-
    enqueueing stylist would be a bug."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _make_run_at_drafter(client, title="PR-393 locked")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        run.state = "DRAFTER_RUNNING"
        run.active_phase_lock = "drafter"
        _seed_phase_started(session, run_id, "drafter")
        # Note: no phase_done yet — drafter is still in flight.
        session.commit()

        with (
            patch("autoessay.main._claim_or_409") as claim,
            patch("autoessay.worker.enqueue_stylist_job") as enqueue,
        ):
            advanced = maybe_advance(session, run, source="phase_done")

        assert advanced is False
        claim.assert_not_called()
        enqueue.assert_not_called()


async def test_advance_drafter_running_no_op_without_phase_done(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """Lock released but no phase_done — likely a crash or zombie.
    Don't try to chain; let the zombie reaper handle it."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _make_run_at_drafter(client, title="PR-393 lock_released_no_done")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        run.state = "DRAFTER_RUNNING"
        run.active_phase_lock = None
        _seed_phase_started(session, run_id, "drafter")
        session.commit()

        with (
            patch("autoessay.main._claim_or_409") as claim,
            patch("autoessay.worker.enqueue_stylist_job") as enqueue,
        ):
            advanced = maybe_advance(session, run, source="phase_done")

        assert advanced is False
        claim.assert_not_called()
        enqueue.assert_not_called()
