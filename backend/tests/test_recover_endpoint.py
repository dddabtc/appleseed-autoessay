"""PR-I3 — POST /api/runs/{run_id}/phases/{phase}/recover.

User-triggered escape hatch for stuck ``*_RUNNING`` runs. Same
compound gate as the reaper background sweep; the only difference
is who pulls the lever. These tests exercise both paths (gate
fires / gate does not fire) and the input-validation guards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.main import app
from autoessay.models import Domain, Project, Run, RunEvent, User
from autoessay.state_machine import append_event, transition


def _ensure_single_user_project(session) -> Project:
    """auth_bypass synthesizes a User(id='single-user') in memory but
    never persists it; the project FK requires the row to exist, so
    we seed both the user and the project here.
    """
    user = session.scalar(select(User).where(User.id == "single-user"))
    if user is None:
        session.add(User(id="single-user", display_name="Single User"))
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
    project = session.scalar(select(Project).where(Project.id == "proj_pri3"))
    if project is None:
        project = Project(
            id="proj_pri3",
            user_id="single-user",
            title="PR-I3 test",
            domain_id="general_academic",
            domain_version="0.0",
            language="en",
            status="ACTIVE",
        )
        session.add(project)
        session.flush()
    return project


def _make_zombie_synthesizer_run(session, run_id: str, tmp_path: Path) -> Run:
    """Insert a synthesizer-running run with a stale phase lock so it
    looks like a SIGKILL victim from 2h ago. Mirrors the helper in
    ``test_zombie_reaper.py`` so the recover endpoint sees an
    identical zombie shape.
    """
    _ensure_single_user_project(session)
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run = Run(
        id=run_id,
        project_id="proj_pri3",
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
    run.active_phase_lock_job_id = "lock_test_pri3"
    run.active_phase_lock_claimed_at = stale
    run.updated_at = stale_naive
    session.commit()
    return run


async def test_recover_gate_fires_on_stale_synthesizer(
    app_session,
    tmp_path: Path,
) -> None:
    """Stale SYNTHESIZER_RUNNING + 2h-old lock → gate fires →
    state moves to FAILED_FIXABLE → endpoint returns 200 with the
    new state."""
    with app_session() as session:
        run = _make_zombie_synthesizer_run(session, "run_pri3_stale", tmp_path)
        run_id = run.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/phases/synthesizer/recover",
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "FAILED_FIXABLE"

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"
        assert run.active_phase_lock is None


async def test_recover_gate_does_not_fire_when_worker_alive(
    app_session,
    tmp_path: Path,
) -> None:
    """Recent phase event + fresh lock → gate refuses → 409 with
    discriminator body. Run state must stay RUNNING (no mutation)."""
    with app_session() as session:
        run = _make_zombie_synthesizer_run(session, "run_pri3_alive", tmp_path)
        run_id = run.id
        # Overwrite the staleness: bring lock + last event to "now"
        # so the gate's idle check refuses.
        now = datetime.now(timezone.utc)
        now_naive = now.replace(tzinfo=None)
        for ev in session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)).all():
            ev.created_at = now_naive
        run.active_phase_lock_claimed_at = now
        run.updated_at = now_naive
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/phases/synthesizer/recover",
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "recovery_gate_not_triggered"
    assert detail["phase"] == "synthesizer"
    assert detail["expected_state"] == "SYNTHESIZER_RUNNING"
    assert detail["current_state"] == "SYNTHESIZER_RUNNING"

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "SYNTHESIZER_RUNNING"


async def test_recover_unknown_phase_returns_404(
    app_session,
    tmp_path: Path,
) -> None:
    """Phase name that isn't in _PHASE_RUNNING_STATE → 404.
    material_diagnostic is the canonical example: it's a synthesizer
    sub-step, not a phase, so the user can't recover it directly —
    they have to recover the parent synthesizer phase instead.
    """
    with app_session() as session:
        run = _make_zombie_synthesizer_run(session, "run_pri3_unknown", tmp_path)
        run_id = run.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/phases/material_diagnostic/recover",
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_phase"
    assert detail["phase"] == "material_diagnostic"


async def test_recover_unknown_run_returns_404(
    app_session,
) -> None:
    """Run id not owned by user → 404 (per _get_user_run_or_404).

    Uses ``app_session`` even though no DB seeding is needed — the
    fixture wires up ``AUTOESSAY_AUTH_BYPASS=1`` so the request makes
    it past the auth gate to the actual route handler.
    """
    del app_session  # only needed for its env-var side effects
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/runs/run_does_not_exist/phases/synthesizer/recover",
        )
    assert response.status_code == 404


async def test_recover_state_mismatch_returns_409(
    app_session,
    tmp_path: Path,
) -> None:
    """Run state is DRAFTER_RUNNING but caller asks for synthesizer.
    Helper bails (state mismatch); endpoint surfaces 409 with
    current_state so UI can show "wrong phase, refresh"."""
    with app_session() as session:
        run = _make_zombie_synthesizer_run(session, "run_pri3_mismatch", tmp_path)
        # Directly mutate state to a non-matching RUNNING state.
        # We don't care about transition validity here — we only
        # need the recover helper to see state != expected so it
        # bails with the state-mismatch branch.
        run.state = "DRAFTER_RUNNING"
        session.commit()
        run_id = run.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/phases/synthesizer/recover",
        )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "recovery_gate_not_triggered"
    assert detail["expected_state"] == "SYNTHESIZER_RUNNING"
    assert detail["current_state"] == "DRAFTER_RUNNING"
