"""Tests for the 3-active-essay-per-user limit.

Codex-AGREEd semantics:
- "Active" = ``Project.deleted_at IS NULL`` AND
  (no Run exists OR latest Run.state not in
  {EXPORTS_DONE, CANCELLED, FAILED_VENDOR, FAILED_POLICY}).
- ``FAILED_FIXABLE`` and ``FAILED_NEEDS_USER`` keep consuming a slot —
  they're absorbing states the user must come back and resolve.
- Three activation paths gate against the limit: ``create_project``,
  ``create_run`` (only when the project would *newly* become active),
  and ``restore_project``.
- The 409 body has ``code: "essay_limit"`` so the frontend keys off it.
"""

from __future__ import annotations

import json

from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import Project, Run, utcnow


async def _create_project(client: AsyncClient, title: str) -> int:
    """Returns the HTTP status; on success the project_id can be read
    from the JSON body."""
    resp = await client.post(
        "/api/projects",
        json={"title": title, "domain_id": "financial_history", "language": "en"},
    )
    return resp.status_code


async def _create_project_returning_id(client: AsyncClient, title: str) -> str:
    resp = await client.post(
        "/api/projects",
        json={"title": title, "domain_id": "financial_history", "language": "en"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_fourth_project_blocked_with_essay_limit_code(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(3):
            assert await _create_project(client, f"essay {i}") == 201
        resp = await client.post(
            "/api/projects",
            json={"title": "fourth", "domain_id": "financial_history", "language": "en"},
        )
    assert resp.status_code == 409
    body = json.loads(resp.text)
    assert body["code"] == "essay_limit"
    assert body["limit"] == 3
    assert body["active_count"] == 3


async def test_fourth_create_succeeds_after_one_done_or_deleted(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project_returning_id(client, "essay a")
        await _create_project_returning_id(client, "essay b")
        await _create_project_returning_id(client, "essay c")
        # 4th is blocked.
        assert await _create_project(client, "essay d") == 409
        # Mark 'a' as EXPORTS_DONE — it no longer consumes a slot.
        with app_session() as session:
            project_a = session.get(Project, a)
            assert project_a is not None
            run = Run(
                id="run_a_done",
                project_id=a,
                domain_version="0.1.0",
                run_dir="/tmp/run_a_done",
                state="EXPORTS_DONE",
                baseline_hash="x",
            )
            session.add(run)
            session.commit()
        assert await _create_project(client, "essay d again") == 201


async def test_fourth_create_succeeds_when_one_is_soft_deleted(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project_returning_id(client, "essay a")
        await _create_project_returning_id(client, "essay b")
        await _create_project_returning_id(client, "essay c")
        await client.delete(f"/api/projects/{a}")
        assert await _create_project(client, "essay d") == 201


async def test_failed_fixable_keeps_consuming_a_slot(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project_returning_id(client, "essay a")
        await _create_project_returning_id(client, "essay b")
        await _create_project_returning_id(client, "essay c")
        # Stamp project a's latest run as FAILED_FIXABLE — by codex
        # design this still occupies a slot.
        with app_session() as session:
            session.add(
                Run(
                    id="run_a_fixable",
                    project_id=a,
                    domain_version="0.1.0",
                    run_dir="/tmp/run_a_fixable",
                    state="FAILED_FIXABLE",
                    baseline_hash="x",
                ),
            )
            session.commit()
        assert await _create_project(client, "essay d") == 409


async def test_no_run_project_counts_as_active(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """A freshly-created project with no Run yet is still active."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(3):
            await _create_project_returning_id(client, f"essay {i}")
        # All three have no Run yet; 4th must still be blocked.
        assert await _create_project(client, "fourth") == 409


async def test_create_run_on_no_run_active_project_is_not_blocked(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """``create_run`` on an already-active no-run project does not
    count as a new activation — it shouldn't trigger the gate."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project_returning_id(client, "essay a")
        await _create_project_returning_id(client, "essay b")
        await _create_project_returning_id(client, "essay c")
        # Three active essays — at the limit. Creating a run on 'a'
        # (which is already active with no run) must succeed because
        # it doesn't change the activation count.
        resp = await client.post(f"/api/projects/{a}/runs")
    assert resp.status_code == 201, resp.text


async def test_create_run_on_done_project_is_blocked_at_limit(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Re-running an EXPORTS_DONE project re-activates a slot — must
    be blocked when the user is already at the limit."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project_returning_id(client, "essay a")
        # Mark 'a' as done — it stops consuming a slot.
        with app_session() as session:
            session.add(
                Run(
                    id="run_a_done",
                    project_id=a,
                    domain_version="0.1.0",
                    run_dir="/tmp/run_a_done",
                    state="EXPORTS_DONE",
                    baseline_hash="x",
                ),
            )
            session.commit()
        # Add three other actives — user now has 3 active + 1 done.
        await _create_project_returning_id(client, "essay b")
        await _create_project_returning_id(client, "essay c")
        await _create_project_returning_id(client, "essay d")
        # Re-running 'a' would re-activate it → 4th active → blocked.
        resp = await client.post(f"/api/projects/{a}/runs")
    assert resp.status_code == 409
    body = json.loads(resp.text)
    assert body["code"] == "essay_limit"


async def test_restore_blocked_when_at_limit_and_was_non_terminal(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Restoring a soft-deleted project whose latest run was
    non-terminal (so it would re-consume a slot) is blocked when the
    user is already at the limit."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project_returning_id(client, "essay a")
        # Soft-delete 'a' — it was active (no run), so restore would
        # re-activate it.
        await client.delete(f"/api/projects/{a}")
        # Add three new actives.
        await _create_project_returning_id(client, "essay b")
        await _create_project_returning_id(client, "essay c")
        await _create_project_returning_id(client, "essay d")
        # Restore 'a' would push to 4 active → blocked.
        resp = await client.post(f"/api/projects/{a}/restore")
    assert resp.status_code == 409
    body = json.loads(resp.text)
    assert body["code"] == "essay_limit"


async def test_restore_succeeds_when_was_done(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """If the deleted project's latest run was in LIMIT_TERMINAL, the
    restore brings it back as inactive — no slot consumed, so the
    limit doesn't apply."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project_returning_id(client, "essay a")
        # Mark 'a' as DONE then delete it.
        with app_session() as session:
            session.add(
                Run(
                    id="run_a_done",
                    project_id=a,
                    domain_version="0.1.0",
                    run_dir="/tmp/run_a_done",
                    state="EXPORTS_DONE",
                    baseline_hash="x",
                    created_at=utcnow(),
                ),
            )
            session.commit()
        await client.delete(f"/api/projects/{a}")
        # Add three other actives.
        await _create_project_returning_id(client, "essay b")
        await _create_project_returning_id(client, "essay c")
        await _create_project_returning_id(client, "essay d")
        # Restore should succeed — 'a' stays inactive after restore.
        resp = await client.post(f"/api/projects/{a}/restore")
    assert resp.status_code == 200


async def test_latest_run_uses_id_tiebreaker_when_created_at_equal(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """If two runs share ``created_at``, ``Run.id`` desc decides who
    is newer. This locks the deterministic latest-run subquery."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        a = await _create_project_returning_id(client, "essay a")
        await _create_project_returning_id(client, "essay b")
        await _create_project_returning_id(client, "essay c")
        # Add two runs with identical created_at — older id is FAILED,
        # newer id is EXPORTS_DONE. The latest-run subquery should
        # see EXPORTS_DONE so 'a' becomes inactive.
        from datetime import datetime, timezone

        same_ts = datetime(2026, 4, 1, tzinfo=timezone.utc)
        with app_session() as session:
            session.add(
                Run(
                    id="run_a_aaa",
                    project_id=a,
                    domain_version="0.1.0",
                    run_dir="/tmp/aaa",
                    state="FAILED_FIXABLE",
                    baseline_hash="x",
                    created_at=same_ts,
                ),
            )
            session.add(
                Run(
                    id="run_a_zzz",
                    project_id=a,
                    domain_version="0.1.0",
                    run_dir="/tmp/zzz",
                    state="EXPORTS_DONE",
                    baseline_hash="x",
                    created_at=same_ts,
                ),
            )
            session.commit()
        # 'a' should now count as inactive (latest by id desc =
        # run_a_zzz with EXPORTS_DONE), so a 4th create succeeds.
        assert await _create_project(client, "essay d") == 201
