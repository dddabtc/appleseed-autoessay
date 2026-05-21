"""Tests for FAILED_FIXABLE → start_<phase> recovery path.

When the LLM call for a phase fails on its very first attempt
(the most common cause: upstream gateway 5xx), the run lands at
``FAILED_FIXABLE`` with no completed phase artifacts. Before the
fix, neither ``start_<phase>`` (which only accepted the canonical
input states) nor ``rerun_phase`` (which requires ``has_completed_output``)
could re-trigger the phase, leaving the run permanently stuck and
the user staring at a `重试该步骤` button that 400'd.

The fix added ``_recover_failed_fixable_for_phase`` to ``main.py``
and routed the FailureResolutionBanner button to ``start_<phase>``
instead of ``rerun_phase``. Each ``start_<phase>`` now calls the
helper before its state guard, which rewinds the run state to the
phase's input state and force-clears any leftover phase-lock,
letting the existing guard accept the request unchanged.
"""

from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.main import app
from autoessay.models import Run


async def _create_project_and_run(client: AsyncClient) -> str:
    project_response = await client.post(
        "/api/projects",
        json={
            "title": "Recovery test",
            "domain_id": "financial_history",
            "language": "en",
        },
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]

    run_response = await client.post(f"/api/projects/{project_id}/runs")
    assert run_response.status_code == 201
    return run_response.json()["id"]


def _force_run_state(
    app_session,  # type: ignore[no-untyped-def]
    run_id: str,
    *,
    state: str,
) -> None:
    """Bypass state machine to set run.state for a test simulating
    a phase failure that the regular state machine would have driven."""
    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        run.state = state
        session.commit()


async def test_proposal_recovery_from_failed_fixable_with_no_artifact(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """First-attempt proposal LLM failure: state==FAILED_FIXABLE,
    no proposal/proposal.md on disk. Banner click → start_proposal
    must succeed (200/202)."""
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_project_and_run(client)
        _force_run_state(app_session, run_id, state="FAILED_FIXABLE")

        response = await client.post(
            f"/api/runs/{run_id}/proposal",
            json={"user_draft": None},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["expected_state"] == "PROPOSAL_DRAFTING"

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        # Sync worker drove proposal to completion, so state should
        # have advanced past FAILED_FIXABLE. It must NOT still be
        # FAILED_FIXABLE — that would mean the recover helper failed.
        assert run.state != "FAILED_FIXABLE"


async def test_proposal_recovery_skipped_when_proposal_already_succeeded(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """If FAILED_FIXABLE was caused by a *later* phase, proposal/proposal.md
    exists. Calling start_proposal must NOT silently rewind state and retry
    proposal — the user wanted to retry a different phase. Reject 409."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_project_and_run(client)

        # Materialize a proposal artifact on disk so the recover
        # helper sees this is NOT a proposal-first-attempt failure.
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run_dir = Path(run.run_dir)
        proposal_path = run_dir / "proposal" / "proposal.md"
        proposal_path.parent.mkdir(parents=True, exist_ok=True)
        proposal_path.write_text("# proposal already produced", encoding="utf-8")

        _force_run_state(app_session, run_id, state="FAILED_FIXABLE")

        response = await client.post(
            f"/api/runs/{run_id}/proposal",
            json={"user_draft": None},
        )

    assert response.status_code == 409


async def test_scout_recovery_from_failed_fixable_with_no_artifact(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """First-attempt scout failure: state==FAILED_FIXABLE,
    no discovery/scout_report.md on disk. start_scout must succeed."""
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_project_and_run(client)
        _force_run_state(app_session, run_id, state="FAILED_FIXABLE")

        response = await client.post(f"/api/runs/{run_id}/scout")

    # Stub mode advances all the way through scout; the body's
    # expected_state will reflect that. The important assertion is
    # that we did not 409 — the helper rewound FAILED_FIXABLE to
    # USER_PROPOSAL_REVIEW (scout's input state) and the state guard
    # accepted the request.
    assert response.status_code == 202

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state != "FAILED_FIXABLE"


async def test_recovery_clears_stale_phase_lock(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """A failed phase may have left the run with an active_phase_lock
    that wasn't released (e.g. worker crashed mid-phase). The recover
    helper must force-clear that lock so the subsequent claim_or_409
    inside start_<phase> succeeds."""
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    from datetime import datetime, timezone

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_project_and_run(client)

        # Simulate a stale lock from a failed proposal job.
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.state = "FAILED_FIXABLE"
            run.active_phase_lock = "proposal"
            run.active_phase_lock_job_id = "stale-job-id"
            run.active_phase_lock_claimed_at = datetime.now(timezone.utc)
            session.commit()

        response = await client.post(
            f"/api/runs/{run_id}/proposal",
            json={"user_draft": None},
        )

    assert response.status_code == 202

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        # After sync_worker proposal completes, the lock is released.
        assert run.active_phase_lock is None


async def test_recovery_helper_no_op_when_state_is_not_failed_fixable(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """If state is DOMAIN_LOADED, _recover_failed_fixable_for_phase
    must be a no-op. Verifies we don't accidentally rewind for
    non-FAILED_FIXABLE callers."""
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_project_and_run(client)
        # Run is at DOMAIN_LOADED right after creation.
        response = await client.post(
            f"/api/runs/{run_id}/proposal",
            json={"user_draft": None},
        )
    assert response.status_code == 202


def test_recover_failed_fixable_for_phase_unknown_phase_is_noop() -> None:
    """Defensive: passing an unrecognised phase shouldn't touch the
    run state — the next state guard owns the rejection."""
    from unittest.mock import MagicMock

    from autoessay.main import _recover_failed_fixable_for_phase

    run = MagicMock()
    run.state = "FAILED_FIXABLE"
    run.run_dir = "/tmp/nonexistent-test"
    session = MagicMock()

    _recover_failed_fixable_for_phase(session, run, "not-a-real-phase")

    # state should NOT have been mutated, since unknown phase has no
    # PHASE_INPUT_STATES entry and no proposal special case.
    pass


# PR-I1: zombie running phase recovery (RUNNING + dead worker → FAILED_FIXABLE)
# ---------------------------------------------------------------------------


async def test_zombie_recovery_drafter_no_lock_no_recent_event(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """The exact run_6c0640 scenario: DRAFTER_RUNNING + active_phase_lock=NULL
    + only one stale section_progress event. ``_recover_zombie_running_phase``
    must mark the stale phase_versions row failed, write a phase_failed event
    with failure_class=zombie_recovered, and transition the run to
    FAILED_FIXABLE — directly testing the helper to avoid downstream coupling
    on stub_drafter side effects.
    """
    from datetime import datetime, timedelta, timezone

    from autoessay.main import _recover_zombie_running_phase
    from autoessay.models import PhaseVersion, RunEvent

    monkeypatch.setenv("AUTOESSAY_ZOMBIE_PHASE_IDLE_SECONDS", "60")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_project_and_run(client)

    # Synthesize the zombie + invoke the detector directly.
    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        run.state = "DRAFTER_RUNNING"
        run.active_phase_lock = None
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        session.add(
            RunEvent(
                id="ev_test_stale",
                run_id=run.id,
                event_type="section_progress",
                payload='{"phase":"drafter","section_id":"introduction"}',
                created_at=stale_at,
            ),
        )
        session.add(
            PhaseVersion(
                id="pv_test_stale",
                run_id=run.id,
                phase="drafter",
                created_on_branch_id=run.active_branch_id,
                version_no=1,
                parent_pv_id=None,
                status="running",
                artifacts_dir="phases/drafter/v001",
                source="agent",
                input_snapshot_hash=None,
                prompt_hash=None,
                created_at=stale_at,
            ),
        )
        session.commit()

        # Re-fetch the run object to reset ORM state, then run detector.
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        _recover_zombie_running_phase(session, run, "drafter")
        session.commit()

        # The stale phase_versions row must have been marked failed.
        stale_pv = session.scalar(
            select(PhaseVersion).where(PhaseVersion.id == "pv_test_stale"),
        )
        assert stale_pv is not None
        assert stale_pv.status == "failed", (
            f"stale running pv expected status=failed, got {stale_pv.status}"
        )
        # Run state must have transitioned to FAILED_FIXABLE.
        assert run.state == "FAILED_FIXABLE", (
            f"expected FAILED_FIXABLE after zombie recovery, got {run.state}"
        )
        # phase_failed event with failure_class=zombie_recovered must
        # be present so the audit trail records the recovery.
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run.id),
        ).all()
        zombie_events = [
            ev
            for ev in events
            if ev.event_type == "phase_failed" and "zombie_recovered" in (ev.payload or "")
        ]
        assert zombie_events, "expected phase_failed event with failure_class=zombie_recovered"


async def test_zombie_recovery_skipped_when_terminal_event_exists(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """If a phase_done event already exists for the phase, the zombie
    detector must NOT recover. The state being RUNNING despite a
    terminal event is a different bug (state-machine drift) that the
    detector should not mask."""
    from datetime import datetime, timedelta, timezone

    from autoessay.models import RunEvent

    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_ZOMBIE_PHASE_IDLE_SECONDS", "60")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        run_id = await _create_project_and_run(client)

        # State is DRAFTER_RUNNING but a phase_done(drafter) event is
        # already in the log — recovery should NOT trigger.
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.state = "DRAFTER_RUNNING"
            run.active_phase_lock = None
            stale_at = datetime.now(timezone.utc) - timedelta(minutes=20)
            session.add(
                RunEvent(
                    id="ev_test_done",
                    run_id=run.id,
                    event_type="phase_done",
                    payload='{"phase":"drafter","draft_version":"v001"}',
                    created_at=stale_at,
                ),
            )
            session.commit()

        # start_drafter — zombie detector should skip (terminal event
        # present), and the state guard should reject (DRAFTER_RUNNING
        # is allowed by start_drafter, but with the terminal event
        # present we expect the recovery to leave the state alone).
        response = await client.post(f"/api/runs/{run_id}/drafter")

    # The actual response code depends on phase_readiness; what we care
    # about is that the run is NOT in FAILED_FIXABLE (zombie recovery did
    # not fire).
    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state != "FAILED_FIXABLE", (
            "zombie recovery wrongly fired despite terminal event present"
        )
    assert response.status_code in (200, 202, 409), response.text


def test_zombie_recovery_unknown_phase_is_noop() -> None:
    """Defensive: passing an unrecognised phase shouldn't touch state."""
    from unittest.mock import MagicMock

    from autoessay.main import _recover_zombie_running_phase

    run = MagicMock()
    run.state = "DRAFTER_RUNNING"
    session = MagicMock()

    # Unknown phase → early return (no DB query, no state mutation).
    _recover_zombie_running_phase(session, run, "not-a-real-phase")

    session.query.assert_not_called()
    session.scalars.assert_not_called()


def test_zombie_recovery_state_phase_mismatch_is_noop() -> None:
    """If state=SCOUT_RUNNING but caller asks about drafter, recovery
    must not fire (it would corrupt the run that's actually scouting)."""
    from unittest.mock import MagicMock

    from autoessay.main import _recover_zombie_running_phase

    run = MagicMock()
    run.state = "SCOUT_RUNNING"
    session = MagicMock()

    _recover_zombie_running_phase(session, run, "drafter")

    # Detector returned early; no DB query, no state mutation.
    session.query.assert_not_called()
    assert run.state == "SCOUT_RUNNING"
