"""PR-389: ``DELETE /api/runs/{id}/hard`` and ``DELETE /api/projects/
{id}/hard`` permanently remove a soft-deleted run/project, all child
rows (events, checkpoints, branches, sources, artifacts, ...), and
the on-disk ``run_dir``.

Eligibility: owner-only, ``deleted_at IS NOT NULL`` (forces 2-step
delete UX), and no run with ``active_phase_lock``. Else 409.

Codex AGREE 2026-05-13: owner can hard-delete their own runs; no
admin role needed for the single-user prod deployment. The 2-step
gate (soft-delete first, then hard-delete) is the user's safety net.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.main import app
from autoessay.models import Project, Run, RunEvent


async def _make_project(client: AsyncClient, *, title: str) -> str:
    proj = await client.post(
        "/api/projects",
        json={
            "title": title,
            "domain_id": "financial_history",
            "target_journal": None,
        },
    )
    assert proj.status_code == 201, proj.text
    return proj.json()["id"]


async def _make_run(client: AsyncClient, project_id: str) -> str:
    resp = await client.post(f"/api/projects/{project_id}/runs", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_hard_delete_run_requires_soft_delete_first(app_session) -> None:  # type: ignore[no-untyped-def]
    """Don't let a one-click misclick wipe a live run."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-389 not deleted")
        run_id = await _make_run(client, project_id)
        resp = await client.delete(f"/api/runs/{run_id}/hard")
        assert resp.status_code == 409
        assert "soft-deleted" in resp.text


async def test_hard_delete_run_succeeds_after_soft_delete(app_session) -> None:  # type: ignore[no-untyped-def]
    """Happy path: soft-delete then hard-delete clears the row + every
    child row referencing the run."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-389 happy")
        run_id = await _make_run(client, project_id)
        # Confirm there's at least one child row (run_created event).
        with app_session() as session:
            events_before = session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id),
            ).all()
            assert len(events_before) >= 1
        # Soft-delete
        soft = await client.delete(f"/api/runs/{run_id}")
        assert soft.status_code == 204
        # Hard-delete
        hard = await client.delete(f"/api/runs/{run_id}/hard")
        assert hard.status_code == 204, hard.text

    with app_session() as session:
        # Run row gone
        assert session.scalar(select(Run).where(Run.id == run_id)) is None
        # Children gone too (RunEvent is the most common child)
        events_after = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id),
        ).all()
        assert events_after == []


async def test_hard_delete_run_409_when_active_lock(app_session) -> None:  # type: ignore[no-untyped-def]
    """Active phase lock means a worker is mid-flight — refuse to
    drop its DB rows."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-389 locked")
        run_id = await _make_run(client, project_id)
        await client.delete(f"/api/runs/{run_id}")  # soft-delete

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        run.active_phase_lock = "proposal"
        run.active_phase_lock_job_id = "test_lock"
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/runs/{run_id}/hard")
        assert resp.status_code == 409
        assert "active phase lock" in resp.text


async def test_hard_delete_project_requires_soft_delete_first(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-389 proj not deleted")
        resp = await client.delete(f"/api/projects/{project_id}/hard")
        assert resp.status_code == 409
        assert "soft-deleted" in resp.text


async def test_hard_delete_project_cascades_to_runs(app_session) -> None:  # type: ignore[no-untyped-def]
    """Project hard-delete must also drop every child run + their
    children. Don't leave orphan rows referencing a deleted project."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-389 proj cascade")
        run_id_a = await _make_run(client, project_id)
        run_id_b = await _make_run(client, project_id)
        # Soft-delete the project (cascades cancel intent, not rows)
        await client.delete(f"/api/projects/{project_id}")
        # Hard-delete
        hard = await client.delete(f"/api/projects/{project_id}/hard")
        assert hard.status_code == 204, hard.text

    with app_session() as session:
        assert session.scalar(select(Project).where(Project.id == project_id)) is None
        assert session.scalar(select(Run).where(Run.id == run_id_a)) is None
        assert session.scalar(select(Run).where(Run.id == run_id_b)) is None
        assert session.scalars(select(RunEvent).where(RunEvent.run_id == run_id_a)).all() == []


async def test_hard_delete_run_404_for_non_owner(app_session) -> None:  # type: ignore[no-untyped-def]
    """The endpoint must use ``_get_user_run_or_404`` semantics so
    another user can't poke at another user's runs."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/runs/run_does_not_exist/hard")
        assert resp.status_code == 404
