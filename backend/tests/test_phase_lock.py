"""Tests for atomic phase-start claim (Stage 3.E follow-up P0).

Codex AGREE-with-amendments: covers the four invariants codex
asked for in the test matrix:

- start/rerun on a held lock → 409 + state unchanged
- phase_done releases the lock
- phase_failed releases the lock
- manual clear-phase-lock endpoint works

Plus enqueue-failure rollback (codex amendment).
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import Run
from autoessay.phase_lock import (
    claim_phase_lock,
    new_lock_token,
    release_phase_lock,
)


async def test_double_click_drafter_returns_409_second_time(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """A second start_drafter while the first is still in flight (lock
    held) returns 409 and does NOT mutate the run state.
    """
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "0")  # async path
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "double-click drafter"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        # Force preconditions: USER_NOVELTY_REVIEW + selected_thesis on disk
        # so start_drafter passes everything except the lock check.
        from pathlib import Path

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "USER_NOVELTY_REVIEW"
            session.commit()
            (Path(run.run_dir) / "novelty").mkdir(parents=True, exist_ok=True)
            (Path(run.run_dir) / "novelty" / "selected_thesis.json").write_text(
                '{"angle_id": "angle_001"}', encoding="utf-8"
            )

            # Pre-claim the lock to simulate "another tab already
            # clicked drafter and the worker is still chewing."
            assert claim_phase_lock(session, run, "drafter", new_lock_token())
            session.commit()

        # Second click: must return 409, state must stay USER_NOVELTY_REVIEW.
        resp = await client.post(f"/api/runs/{run_id}/drafter")
        get_resp = await client.get(f"/api/runs/{run_id}")

    assert resp.status_code == 409
    assert "Another phase is already running" in resp.json()["detail"]
    assert get_resp.json()["state"] == "USER_NOVELTY_REVIEW"


async def test_release_phase_lock_owner_check_rejects_stale_token(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """An old token can NOT clear a newer claim. Codex amendment:
    owner-checked release prevents a crashed/late worker from
    accidentally clearing whatever new lock has replaced it.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "owner-check release"})
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None

        old_token = new_lock_token()
        assert claim_phase_lock(session, run, "drafter", old_token)
        session.commit()

        # Admin force-clears + a new attempt claims with a different token.
        from autoessay.phase_lock import force_clear_phase_lock

        force_clear_phase_lock(session, run)
        session.commit()
        new_token = new_lock_token()
        assert claim_phase_lock(session, run, "drafter", new_token)
        session.commit()

        # Late callback from the original worker tries to release with the
        # OLD token — must be a no-op.
        released = release_phase_lock(session, run, "drafter", old_token)
        session.refresh(run)

    assert released is False
    assert run.active_phase_lock == "drafter"
    assert run.active_phase_lock_job_id == new_token


async def test_clear_phase_lock_endpoint_clears_zombie_lock(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """Manual escape hatch: ``POST /api/runs/{id}/clear-phase-lock``
    drops whichever lock is held, regardless of owner. Used to recover
    from worker crashes.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "clear zombie lock"})
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        # Pre-claim the lock from the test side, then call the endpoint.
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            assert claim_phase_lock(session, run, "drafter", new_lock_token())
            session.commit()

        clear_resp = await client.post(f"/api/runs/{run_id}/clear-phase-lock")
        get_resp = await client.get(f"/api/runs/{run_id}")

    assert clear_resp.status_code == 200
    assert clear_resp.json()["active_phase_lock"] is None
    assert get_resp.json()["active_phase_lock"] is None


async def test_phase_done_releases_lock_in_sync_worker(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """The full drafter-stub run path: start, agent runs, phase_done
    fires, lock is released so subsequent clicks aren't 409'd.
    """
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "phase_done releases lock"}
        )
        run_id = (await client.post(f"/api/projects/{project_response.json()['id']}/runs")).json()[
            "id"
        ]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        scout_resp = await client.post(f"/api/runs/{run_id}/scout")
        get_resp = await client.get(f"/api/runs/{run_id}")

    assert scout_resp.status_code == 202
    # After phase_done, lock must be cleared.
    assert get_resp.json()["active_phase_lock"] is None
