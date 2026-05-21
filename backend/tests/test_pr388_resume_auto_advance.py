"""PR-388: ``resume_auto_advance_idle_runs`` picks up runs left
stranded at a review gate after a container restart, enqueue failure,
or any other interruption that broke the phase-lock-release hook.

Scope (codex amendments):
- Only runs with ``auto_advance=True``.
- Only states in ``RESUMABLE_STATES`` (= dispatch table keys,
  i.e. all 10 USER_*_REVIEW states + DOMAIN_LOADED).
- Skip runs with an ``active_phase_lock`` (worker is mid-flight; that
  case is handled by the zombie reaper's primary path).
- Skip soft-deleted runs (``deleted_at IS NOT NULL``) and runs whose
  parent project is soft-deleted.
- Skip cancel-requested runs.
"""

from __future__ import annotations

from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.auto_advance import (
    RESUMABLE_STATES,
    resume_auto_advance_idle_runs,
)
from autoessay.main import app
from autoessay.models import Project, Run


def test_resumable_states_match_dispatch_keys() -> None:
    """``RESUMABLE_STATES`` is the public name we want callers (the
    reaper) to import. Pin it to the dispatch table so adding a new
    handler automatically widens resume scope."""
    from autoessay.auto_advance import _DISPATCH

    assert frozenset(_DISPATCH.keys()) == RESUMABLE_STATES
    # The two non-USER_*_REVIEW states must be in the set — these
    # are easy to miss with a naive ``USER_*_REVIEW`` scan.
    assert "DOMAIN_LOADED" in RESUMABLE_STATES
    assert "USER_EXTERNAL_SCAN_APPROVAL" in RESUMABLE_STATES
    assert "USER_FINAL_ACCEPTANCE" in RESUMABLE_STATES


async def _make_project_and_run(
    client: AsyncClient,
    *,
    title: str,
    auto_advance: bool = True,
) -> tuple[str, str]:
    proj = await client.post(
        "/api/projects",
        json={
            "title": title,
            "domain_id": "financial_history",
            "target_journal": None,
        },
    )
    assert proj.status_code == 201, proj.text
    project_id = proj.json()["id"]
    with patch("autoessay.auto_advance.maybe_advance", return_value=False):
        run_resp = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"auto_advance": auto_advance},
        )
    assert run_resp.status_code == 201, run_resp.text
    return project_id, run_resp.json()["id"]


async def test_resume_picks_up_auto_advance_run_at_domain_loaded(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, run_id = await _make_project_and_run(
            client,
            title="PR-388 resume DOMAIN_LOADED",
        )

    with patch(
        "autoessay.auto_advance.maybe_advance",
        return_value=True,
    ) as coordinator:
        counters = resume_auto_advance_idle_runs(session_factory=app_session)

    sources = [c.kwargs.get("source") for c in coordinator.call_args_list]
    assert "reaper_resume" in sources
    assert counters["candidates"] >= 1
    assert counters["resumed"] >= 1
    assert counters["errors"] == 0


async def test_resume_skips_auto_advance_off_runs(app_session) -> None:  # type: ignore[no-untyped-def]
    """Toggle off → never auto-resume even if the run is in a
    review state."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, run_id = await _make_project_and_run(
            client,
            title="PR-388 toggle off",
            auto_advance=False,
        )

    with patch("autoessay.auto_advance.maybe_advance") as coordinator:
        resume_auto_advance_idle_runs(session_factory=app_session)

    # The created run shouldn't be a candidate.
    target_ids = [c.args[1].id for c in coordinator.call_args_list if c.args] + [
        c.kwargs.get("run").id for c in coordinator.call_args_list if c.kwargs.get("run")
    ]
    assert run_id not in target_ids


async def test_resume_skips_runs_with_active_phase_lock(app_session) -> None:  # type: ignore[no-untyped-def]
    """If a worker holds the phase lock, the run is in-flight — leave
    it alone. The zombie reaper's primary path handles stale locks."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, run_id = await _make_project_and_run(
            client,
            title="PR-388 locked run",
        )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        run.active_phase_lock = "proposal"
        run.active_phase_lock_job_id = "test_lock_123"
        session.commit()

    with patch("autoessay.auto_advance.maybe_advance") as coordinator:
        resume_auto_advance_idle_runs(session_factory=app_session)

    locked_ids = [c.args[1].id for c in coordinator.call_args_list if len(c.args) > 1] + [
        c.kwargs.get("run").id
        for c in coordinator.call_args_list
        if c.kwargs.get("run") is not None
    ]
    assert run_id not in locked_ids


async def test_resume_skips_soft_deleted_runs(app_session) -> None:  # type: ignore[no-untyped-def]
    """If the user trashed the run, don't auto-resume it on the
    next reaper sweep."""
    from autoessay.models import utcnow

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, run_id = await _make_project_and_run(
            client,
            title="PR-388 soft-deleted",
        )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        run.deleted_at = utcnow()
        session.commit()

    with patch("autoessay.auto_advance.maybe_advance") as coordinator:
        resume_auto_advance_idle_runs(session_factory=app_session)

    deleted_ids = [c.args[1].id for c in coordinator.call_args_list if len(c.args) > 1] + [
        c.kwargs.get("run").id
        for c in coordinator.call_args_list
        if c.kwargs.get("run") is not None
    ]
    assert run_id not in deleted_ids


async def test_resume_skips_runs_whose_project_was_deleted(app_session) -> None:  # type: ignore[no-untyped-def]
    """Same as run-level soft-delete but at the project level —
    delete the parent project and the run mustn't auto-resume."""
    from autoessay.models import utcnow

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id, run_id = await _make_project_and_run(
            client,
            title="PR-388 project-deleted",
        )

    with app_session() as session:
        project = session.scalar(select(Project).where(Project.id == project_id))
        project.deleted_at = utcnow()
        session.commit()

    with patch("autoessay.auto_advance.maybe_advance") as coordinator:
        resume_auto_advance_idle_runs(session_factory=app_session)

    deleted_ids = [c.args[1].id for c in coordinator.call_args_list if len(c.args) > 1] + [
        c.kwargs.get("run").id
        for c in coordinator.call_args_list
        if c.kwargs.get("run") is not None
    ]
    assert run_id not in deleted_ids


async def test_resume_skips_cancel_requested_runs(app_session) -> None:  # type: ignore[no-untyped-def]
    """``cancel_requested_at`` means user explicitly hit cancel; don't
    auto-resume."""
    from autoessay.models import utcnow

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, run_id = await _make_project_and_run(
            client,
            title="PR-388 cancelled",
        )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        run.cancel_requested_at = utcnow()
        session.commit()

    with patch("autoessay.auto_advance.maybe_advance") as coordinator:
        resume_auto_advance_idle_runs(session_factory=app_session)

    cancelled_ids = [c.args[1].id for c in coordinator.call_args_list if len(c.args) > 1] + [
        c.kwargs.get("run").id
        for c in coordinator.call_args_list
        if c.kwargs.get("run") is not None
    ]
    assert run_id not in cancelled_ids


async def test_per_run_exception_does_not_stop_sweep(app_session) -> None:  # type: ignore[no-untyped-def]
    """A handler crash on one run must not prevent the sweep from
    visiting subsequent runs (codex amendment for resilience)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _make_project_and_run(client, title="PR-388 sweep A")
        await _make_project_and_run(client, title="PR-388 sweep B")

    call_count = {"n": 0}

    def fake_advance(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("synthetic handler crash")
        return True

    with patch("autoessay.auto_advance.maybe_advance", side_effect=fake_advance):
        counters = resume_auto_advance_idle_runs(session_factory=app_session)

    assert counters["candidates"] >= 2
    # First run raised → error counter increments, second run continues.
    assert counters["errors"] >= 1
    assert counters["resumed"] >= 1
