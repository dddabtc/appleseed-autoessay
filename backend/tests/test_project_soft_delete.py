"""Tests for soft-delete + restore on essays.

Covers the codex-aligned design:
- DELETE is idempotent (re-delete = 204 no-op)
- Mutating endpoints reject deleted projects with 409
- list_projects hides deleted by default; ``?include_deleted=1`` reveals them
- Single GET still works for the owner so the workspace can render history
- DELETE stamps cancel_requested_at on every unfinished run
- Restore returns 409 when the project isn't deleted; doesn't clear
  cancel intent on already-cancelled runs
- Worker entry points honor cancel intent and transition to CANCELLED
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import Project, Run, RunEvent, utcnow


async def _create_project(client: AsyncClient, title: str = "essay 1") -> str:
    resp = await client.post(
        "/api/projects",
        json={"title": title, "domain_id": "financial_history", "language": "en"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_delete_then_list_default_hides(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project(client, "keeper")
        b = await _create_project(client, "trash")
        d = await client.delete(f"/api/projects/{b}")
        assert d.status_code == 204
        listed = await client.get("/api/projects")
        assert listed.status_code == 200
        ids = [p["id"] for p in listed.json()]
        assert a in ids
        assert b not in ids
        listed_all = await client.get("/api/projects?include_deleted=1")
        ids_all = [p["id"] for p in listed_all.json()]
        assert a in ids_all
        assert b in ids_all
        deleted_entry = next(p for p in listed_all.json() if p["id"] == b)
        assert deleted_entry["deleted_at"] is not None


async def test_delete_is_idempotent(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client)
        first = await client.delete(f"/api/projects/{pid}")
        second = await client.delete(f"/api/projects/{pid}")
    assert first.status_code == 204
    assert second.status_code == 204


async def test_get_single_deleted_returns_project(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "history")
        await client.delete(f"/api/projects/{pid}")
        get = await client.get(f"/api/projects/{pid}")
    assert get.status_code == 200
    body = get.json()
    assert body["id"] == pid
    assert body["deleted_at"] is not None


async def test_mutation_rejected_on_deleted_project(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "to-trash")
        await client.delete(f"/api/projects/{pid}")
        # Try to create a new run on the deleted project.
        new_run = await client.post(f"/api/projects/{pid}/runs")
        assert new_run.status_code == 409
        # Try to PATCH it.
        patch = await client.patch(f"/api/projects/{pid}", json={"language": "ja"})
        assert patch.status_code == 409


async def test_mutation_rejected_on_runs_of_deleted_project(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    transport = ASGITransport(app=app)
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "with-run")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        await client.delete(f"/api/projects/{pid}")
        # Try to start scout on the run of a deleted project.
        scout = await client.post(f"/api/runs/{run_id}/scout")
        assert scout.status_code == 409


async def test_delete_stamps_cancel_intent_on_unfinished_runs(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "with-running")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        await client.delete(f"/api/projects/{pid}")
    with app_session() as session:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.cancel_requested_at is not None


async def test_restore_undeletes_and_409_when_not_deleted(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "back-from-dead")
        # 409 before deletion
        early = await client.post(f"/api/projects/{pid}/restore")
        assert early.status_code == 409
        await client.delete(f"/api/projects/{pid}")
        restore = await client.post(f"/api/projects/{pid}/restore")
        assert restore.status_code == 200
        body = restore.json()
        assert body["deleted_at"] is None
        # Now mutating endpoints work again.
        patch = await client.patch(f"/api/projects/{pid}", json={"language": "ja"})
        assert patch.status_code == 200


async def test_restore_project_rejects_active_phase_lock(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "restore-project-active-lock")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        await client.delete(f"/api/projects/{pid}")
        with app_session() as session:
            row = session.get(Run, run_id)
            assert row is not None
            row.active_phase_lock = "scout"
            row.active_phase_lock_job_id = "lock_restore_project"
            row.active_phase_lock_claimed_at = utcnow()
            session.commit()

        restore = await client.post(f"/api/projects/{pid}/restore")

    assert restore.status_code == 409
    assert "active_phase_lock" in restore.text
    with app_session() as session:
        project = session.get(Project, pid)
        assert project is not None
        assert project.deleted_at is not None


async def test_restore_project_records_recovery_warning_for_late_phase_done(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "restore-project-late-phase-done")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        await client.delete(f"/api/projects/{pid}")

        with app_session() as session:
            row = session.get(Run, run_id)
            assert row is not None
            assert row.cancel_requested_at is not None
            session.add(
                RunEvent(
                    id="event_project_restore_late_phase_done",
                    run_id=run_id,
                    event_type="phase_done",
                    payload=json.dumps({"phase": "scout"}),
                    created_at=utcnow(),
                ),
            )
            session.commit()

        restore = await client.post(f"/api/projects/{pid}/restore")

    assert restore.status_code == 200, restore.text
    with app_session() as session:
        warning = (
            session.query(RunEvent)
            .filter_by(
                run_id=run_id,
                event_type="run_restore_recovery_warning",
            )
            .one_or_none()
        )
        assert warning is not None
        payload = json.loads(warning.payload)
        assert payload["reason"] == "phase_done_after_cancel_intent"
        assert payload["phase"] == "scout"


async def test_restore_clears_cancel_intent_on_waiting_state_runs(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """2026-05-07 incident: a Claude playwright walk's cleanup
    soft-deleted a user's project, which stamped
    ``cancel_requested_at`` on the run. The project was later
    restored, but ``restore_project`` originally did NOT clear the
    cancel intent — so the next phase trigger silently transitioned
    the run to ``CANCELLED``, losing all the user's work.

    Codex AGREE-WITH-AMENDMENTS direction B: when the run is at a
    non-running USER_*_REVIEW waiting state at restore time, clear
    the residual cancel intent so resuming Just Works."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "restore-clears-waiting-cancel")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        # Brand-new runs sit in DOMAIN_LOADED — a non-running waiting
        # state — so the delete-stamped cancel intent should be
        # cleared by restore.
        await client.delete(f"/api/projects/{pid}")
        await client.post(f"/api/projects/{pid}/restore")
    with app_session() as session:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.cancel_requested_at is None
        assert row.state != "CANCELLED"


async def test_worker_entry_aborts_on_cancel_intent(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """If cancel_requested_at is set on the run, the agent's entry
    point transitions the run to CANCELLED instead of running."""
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")

    from autoessay.agents.scout import run_scout
    from autoessay.models import Domain, Project, User, utcnow
    from autoessay.run_writer import create_run_directory
    from autoessay.state_machine import RunCancelled

    run_id = "run_cancel_test"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_cancel_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )
    with app_session() as session:
        # Inline the project seed so this test does not depend on
        # the ``tests`` package being importable (CI uses a flat
        # tests directory without an ``__init__.py``).
        session.add(User(id="single-user", display_name="Single User"))
        session.add(
            Domain(
                id="financial_history",
                display_name="Financial History",
                version="0.1.0",
                enabled=True,
            ),
        )
        session.flush()
        session.add(
            Project(
                id="proj_cancel_test",
                user_id="single-user",
                title="cancel test",
                domain_id="financial_history",
                domain_version="0.1.0",
                status="CREATED",
            ),
        )
        run = Run(
            id=run_id,
            project_id="proj_cancel_test",
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="DOMAIN_LOADED",
            baseline_hash="test",
            cancel_requested_at=utcnow(),
        )
        session.add(run)
        session.commit()
        with pytest.raises(RunCancelled):
            run_scout(run_id, session)
        session.refresh(run)
        assert run.state == "CANCELLED"


async def test_restore_keeps_cancel_intent_when_run_was_running(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """If the run was in a RUNNING_STATES state at delete time
    (truly mid-flight), restore must NOT clear cancel_requested_at
    — recovery from a real interrupted phase is the user's explicit
    intent and goes through ``clear_run_cancel_intent`` if at all."""
    from autoessay.models import Run

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "restore-keeps-running-cancel")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        # Force the run into a running state before delete.
        with app_session() as session:
            row = session.get(Run, run_id)
            assert row is not None
            row.state = "DRAFTER_RUNNING"
            session.commit()
        await client.delete(f"/api/projects/{pid}")
        await client.post(f"/api/projects/{pid}/restore")
    with app_session() as session:
        row = session.get(Run, run_id)
        assert row is not None
        # cancel intent stays on a true mid-flight cancel.
        assert row.cancel_requested_at is not None


async def test_clear_cancel_intent_recovers_cancelled_run(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """End-to-end: delete + restore lingering cancel + worker
    transitions to CANCELLED, then ``clear_run_cancel_intent`` reverts
    to the pre-cancel state and clears the intent."""
    from autoessay.models import Run, RunEvent, utcnow

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "clear-cancel-recovery")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        # Simulate the chain: run was at USER_EXTERNAL_SCAN_APPROVAL,
        # got delete-stamped, then assert_run_active transitioned to
        # CANCELLED. We compose this manually via DB writes since the
        # full chain requires running every phase.
        import json as _json

        with app_session() as session:
            row = session.get(Run, run_id)
            assert row is not None
            row.state = "USER_EXTERNAL_SCAN_APPROVAL"
            session.commit()
            # Manual cancel transition mirroring assert_run_active.
            row.state = "CANCELLED"
            row.cancel_requested_at = utcnow()
            session.add(
                RunEvent(
                    id="event_cancel_test_xyz",
                    run_id=run_id,
                    event_type="state_transition",
                    payload=_json.dumps(
                        {
                            "from_state": "USER_EXTERNAL_SCAN_APPROVAL",
                            "to_state": "CANCELLED",
                            "reason": "test cancel",
                        }
                    ),
                    created_at=utcnow(),
                ),
            )
            session.commit()
        # Now invoke the recovery endpoint.
        resp = await client.post(f"/api/runs/{run_id}/clear-cancel-intent")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "USER_EXTERNAL_SCAN_APPROVAL"
    with app_session() as session:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.state == "USER_EXTERNAL_SCAN_APPROVAL"
        assert row.cancel_requested_at is None


async def test_clear_cancel_intent_refuses_running_phase_cancel(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Refuse to recover a cancel that hit a real mid-flight phase
    — artifacts may be half-written."""
    from autoessay.models import Run, RunEvent, utcnow

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "no-recover-running")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        import json as _json

        with app_session() as session:
            row = session.get(Run, run_id)
            assert row is not None
            row.state = "CANCELLED"
            row.cancel_requested_at = utcnow()
            session.add(
                RunEvent(
                    id="event_running_cancel_test_xyz",
                    run_id=run_id,
                    event_type="state_transition",
                    payload=_json.dumps(
                        {
                            "from_state": "DRAFTER_RUNNING",
                            "to_state": "CANCELLED",
                        }
                    ),
                    created_at=utcnow(),
                ),
            )
            session.commit()
        resp = await client.post(f"/api/runs/{run_id}/clear-cancel-intent")
        assert resp.status_code == 409
        assert "running phase" in resp.json()["detail"].lower()


async def test_clear_cancel_intent_refuses_non_cancelled(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """A run not in CANCELLED state has nothing to recover from."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pid = await _create_project(client, "no-recover-not-cancelled")
        run = await client.post(f"/api/projects/{pid}/runs")
        run_id = run.json()["id"]
        resp = await client.post(f"/api/runs/{run_id}/clear-cancel-intent")
        assert resp.status_code == 409
        assert "not cancelled" in resp.json()["detail"].lower()
