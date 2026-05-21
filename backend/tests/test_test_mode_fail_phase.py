"""PR-248 — POST /api/test/runs/{run_id}/fail-phase test-only injector.

Gates:
1. test_mode flag off → 404 (looks like missing endpoint).
2. test_mode flag on + unknown phase → 404 with discriminator.
3. test_mode flag on + known phase → 202 with FAILED_FIXABLE +
   phase_failed event (failure_class="test_injected").
4. Non-existent run → 404.
5. Production env + test_mode → Settings root_validator rejects at boot.

These tests are the proof that the retry-leg specs (FR-01.30 ~ .40)
have a deterministic injector to use.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.config import Settings, get_settings
from autoessay.main import app
from autoessay.models import Domain, Project, Run, RunEvent, User


def _ensure_seed(session) -> Project:
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
    project = session.scalar(select(Project).where(Project.id == "proj_pr248"))
    if project is None:
        project = Project(
            id="proj_pr248",
            user_id="single-user",
            title="PR-248 test",
            domain_id="general_academic",
            domain_version="0.0",
            language="en",
            status="ACTIVE",
        )
        session.add(project)
        session.flush()
    return project


def _make_run(session, run_id: str, tmp_path: Path, state: str = "USER_FIELD_REVIEW") -> Run:
    _ensure_seed(session)
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run = Run(
        id=run_id,
        project_id="proj_pr248",
        run_dir=str(run_dir),
        state=state,
        baseline_hash="0" * 64,
        domain_version="0.0",
    )
    session.add(run)
    session.flush()
    return run


@pytest.fixture(autouse=True)
def _enable_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests in this module exercise the test-mode-on path; the
    one off-path test below explicitly clears it."""
    monkeypatch.setenv("AUTOESSAY_TEST_MODE", "1")
    get_settings.cache_clear()


def _disable_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOESSAY_TEST_MODE", raising=False)
    get_settings.cache_clear()


async def test_fail_phase_injects_failed_fixable(
    app_session,
    tmp_path: Path,
) -> None:
    """test_mode on + known phase → FAILED_FIXABLE + test_injected event."""
    with app_session() as session:
        run = _make_run(session, "run_pr248_inject", tmp_path)
        session.commit()
        run_id = run.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/test/runs/{run_id}/fail-phase",
            json={"phase": "synthesizer"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "FAILED_FIXABLE"

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"
        # The phase_failed event with failure_class=test_injected
        # is what the FailureResolutionBanner reads to drive the
        # retry button.
        events = session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .where(RunEvent.event_type == "phase_failed")
        ).all()
        assert events, "expected at least one phase_failed event"
        last = events[-1]
        # payload is a JSON-encoded string in the DB column; deserialize.
        import json as _json

        payload = _json.loads(last.payload) if isinstance(last.payload, str) else last.payload
        assert payload.get("phase") == "synthesizer"
        assert payload.get("failure_class") == "test_injected"
        assert payload.get("failure_state") == "FAILED_FIXABLE"


async def test_fail_phase_can_inject_failed_policy(
    app_session,
    tmp_path: Path,
) -> None:
    """test_mode supports FAILED_POLICY for blocked landing-tab coverage."""
    with app_session() as session:
        run = _make_run(session, "run_pr248_policy", tmp_path)
        session.commit()
        run_id = run.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/test/runs/{run_id}/fail-phase",
            json={"phase": "exports", "failure_state": "FAILED_POLICY"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "FAILED_POLICY"

    with app_session() as session:
        events = session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .where(RunEvent.event_type == "phase_failed")
        ).all()
        assert events
        import json as _json

        payload = (
            _json.loads(events[-1].payload)
            if isinstance(events[-1].payload, str)
            else events[-1].payload
        )
        assert payload.get("phase") == "exports"
        assert payload.get("failure_state") == "FAILED_POLICY"


async def test_state_lock_injects_running_state_with_active_lock(
    app_session,
    tmp_path: Path,
) -> None:
    """test_mode can reproduce state/lock handoff mismatch for UI routing."""
    with app_session() as session:
        run = _make_run(session, "run_pr248_state_lock", tmp_path)
        session.commit()
        run_id = run.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/test/runs/{run_id}/state-lock",
            json={"state": "CRITIC_RUNNING", "active_phase_lock": "final_rewrite"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "CRITIC_RUNNING"
    assert body["active_phase_lock"]["phase"] == "final_rewrite"

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "CRITIC_RUNNING"
        assert run.active_phase_lock == "final_rewrite"
        assert run.active_phase_lock_job_id


async def test_fail_phase_unknown_phase_404(
    app_session,
    tmp_path: Path,
) -> None:
    """Unknown phase string → 404 with discriminator. Material_diagnostic
    is a sub-step, not a phase — must reject."""
    with app_session() as session:
        run = _make_run(session, "run_pr248_unknown", tmp_path)
        run_id = run.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/test/runs/{run_id}/fail-phase",
            json={"phase": "material_diagnostic"},
        )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "unknown_phase"
    assert detail["phase"] == "material_diagnostic"


async def test_fail_phase_unknown_run_404(
    app_session,
) -> None:
    """Non-existent run → 404 (from _get_user_run_for_mutation_or_404)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/test/runs/run_does_not_exist/fail-phase",
            json={"phase": "synthesizer"},
        )
    assert response.status_code == 404


async def test_fail_phase_test_mode_off_404(
    app_session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """test_mode off → 404 even with a valid run + valid phase. Looks
    identical to the route not existing — protects production-like
    envs that probe URLs."""
    with app_session() as session:
        run = _make_run(session, "run_pr248_off", tmp_path)
        run_id = run.id

    _disable_test_mode(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/test/runs/{run_id}/fail-phase",
            json={"phase": "synthesizer"},
        )
    assert response.status_code == 404
    # Must NOT include the unknown_phase discriminator — it's a
    # bare 404 (route disabled). FastAPI's default 404 detail is
    # the string "Not Found"; the unknown_phase variant is a dict.
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    detail = body.get("detail")
    if isinstance(detail, dict):
        assert detail.get("code") != "unknown_phase"


def test_settings_rejects_production_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings root_validator must hard-reject test_mode in production
    so the endpoint is structurally impossible to reach in prod.
    Same protection pattern as auth_bypass.
    """
    monkeypatch.setenv("AUTOESSAY_ENV", "production")
    monkeypatch.setenv("AUTOESSAY_TEST_MODE", "1")
    with pytest.raises(ValueError, match="AUTOESSAY_TEST_MODE=1 is not allowed"):
        Settings()  # type: ignore[call-arg]
