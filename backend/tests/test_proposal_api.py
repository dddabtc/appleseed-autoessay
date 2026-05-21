from httpx import ASGITransport, AsyncClient

from autoessay.config import get_settings
from autoessay.main import app


async def test_proposal_put_and_get_return_user_edited_version(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Banking proposal API"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )

        start_response = await client.post(
            f"/api/runs/{run_id}/proposal",
            json={"user_draft": "Look at clearinghouses."},
        )
        get_response = await client.get(f"/api/runs/{run_id}/proposal")
        edited = {
            "research_question": "How did clearinghouses shape banking panic responses?",
            "significance": "This edit narrows the opening motivation.",
            "preliminary_approach": "Compare literature on clearinghouses and bank runs.",
            "expected_contribution": "A sharper starting map for later novelty work.",
            "scope": "Initial literature search only.",
            "preliminary_keywords": ["clearinghouses", "bank runs"],
        }
        put_response = await client.put(
            f"/api/runs/{run_id}/proposal",
            json={"proposal_json": edited},
        )
        final_get_response = await client.get(f"/api/runs/{run_id}/proposal")

    assert start_response.status_code == 202
    assert start_response.json()["job_id"] == "sync"
    assert get_response.status_code == 200
    assert get_response.json()["version"] == 1
    assert put_response.status_code == 200
    assert put_response.json()["version"] == 2
    assert put_response.json()["proposal_json"] == edited
    assert final_get_response.status_code == 200
    assert final_get_response.json()["proposal_json"] == edited


async def test_accept_proposal_checkpoint_starts_scout(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Proposal checkpoint scout"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/proposal", json={})

        checkpoint_response = await client.post(
            f"/api/runs/{run_id}/checkpoints/USER_PROPOSAL_REVIEW",
            json={"accept": True},
        )
        run_after_response = await client.get(f"/api/runs/{run_id}")
        discovery_response = await client.get(f"/api/runs/{run_id}/discovery")

    assert checkpoint_response.status_code == 201
    assert checkpoint_response.json()["status"] == "ACCEPTED"
    assert run_after_response.json()["state"] == "USER_SEARCH_REVIEW"
    assert discovery_response.status_code == 200
    assert discovery_response.json()["skim_candidates"]


async def test_proposal_put_rejects_invalid_schema(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Invalid proposal edit"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/proposal", json={})

        put_response = await client.put(
            f"/api/runs/{run_id}/proposal",
            json={"proposal_json": {"research_question": "too little"}},
        )
        get_response = await client.get(f"/api/runs/{run_id}/proposal")

    assert put_response.status_code == 400
    assert get_response.status_code == 200
    assert get_response.json()["version"] == 1


async def test_proposal_put_accepted_in_post_accept_state_marks_scout_stale(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """The user reported on 2026-05-01 that after accepting the
    proposal, the proposal subview becomes permanently read-only.
    The fix relaxes save_proposal's state guard to any quiescent
    state with a proposal AND marks the earliest already-completed
    downstream phase as stale on the active branch (codex
    amendment 1+3).

    This test simulates that flow: drop into USER_SEARCH_REVIEW
    (i.e. scout has completed), simulate scout output on disk so
    has_completed_output sees it, edit the proposal, expect 200
    + branch.stale_from_phase == "scout".
    """
    from sqlalchemy import select

    from autoessay.branches import ensure_main_branch, get_branch
    from autoessay.models import Run

    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Post-accept proposal edit"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/proposal", json={})
        # Move the run forward and lay down a fake scout artifact so
        # has_completed_output("scout") returns True. Without this
        # the post-accept branch leaves stale_from_phase=None — also
        # a correct outcome, but not the one this test exercises.
        # USER_PROPOSAL_REVIEW → USER_SEARCH_REVIEW must hop through
        # SCOUT_RUNNING per ALLOWED_TRANSITIONS.
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "SCOUT_RUNNING", "reason": "fake-scout-start"},
        )
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "USER_SEARCH_REVIEW", "reason": "fake-scout-done"},
        )
        from pathlib import Path as _Path

        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            scout_path = _Path(run.run_dir) / "discovery" / "scout_report.md"
            scout_path.parent.mkdir(parents=True, exist_ok=True)
            scout_path.write_text("# fake scout output\n", encoding="utf-8")
            ensure_main_branch(session, run)
            session.commit()

        edited = {
            "research_question": "Edited after accepting.",
            "significance": "Stale should propagate.",
            "preliminary_approach": "Whatever.",
            "expected_contribution": "A test.",
            "scope": "narrow",
            "preliminary_keywords": ["edit"],
        }
        put_response = await client.put(
            f"/api/runs/{run_id}/proposal",
            json={"proposal_json": edited},
        )

    assert put_response.status_code == 200
    assert put_response.json()["version"] == 2

    # Refetch the run and its branch — the request committed the
    # stale flag on the branch row.
    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        branch = get_branch(session, run)
        assert branch.stale_from_phase == "scout"


async def test_proposal_put_rejected_during_running_state(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Codex amendment 2 (2026-05-01): edits during RUNNING_STATES
    must 409 because an agent is consuming the current proposal."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Reject during scout running"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/proposal", json={})
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "SCOUT_RUNNING", "reason": "test"},
        )

        edited = {
            "research_question": "While scout runs.",
            "significance": "Should be blocked.",
            "preliminary_approach": "Whatever.",
            "expected_contribution": "A test.",
            "scope": "narrow",
            "preliminary_keywords": ["block"],
        }
        put_response = await client.put(
            f"/api/runs/{run_id}/proposal",
            json={"proposal_json": edited},
        )

    assert put_response.status_code == 409
    assert "currently running" in put_response.json()["detail"].lower()


async def test_reject_proposal_checkpoint_keeps_review_state(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Rejected proposal checkpoint"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/proposal", json={})

        checkpoint_response = await client.post(
            f"/api/runs/{run_id}/checkpoints/USER_PROPOSAL_REVIEW",
            json={"accept": False},
        )
        run_after_response = await client.get(f"/api/runs/{run_id}")

    assert checkpoint_response.status_code == 201
    assert checkpoint_response.json()["status"] == "REJECTED"
    assert run_after_response.json()["state"] == "USER_PROPOSAL_REVIEW"


async def test_proposal_replace_at_user_proposal_review_does_not_bump_version(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """``mode=replace`` overwrites proposal_v001 in place when no
    pipeline phase has completed; ``proposal_version`` stays at 1.
    Codex AGREE 2026-05-01."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Replace at USER_PROPOSAL_REVIEW"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/proposal", json={})
        edited = {
            "research_question": "Replaced not bumped.",
            "significance": "Should overwrite v001.",
            "preliminary_approach": "Overwrite mode.",
            "expected_contribution": "Lower clutter on the version timeline.",
            "scope": "narrow",
            "preliminary_keywords": ["replace"],
        }

        put_response = await client.put(
            f"/api/runs/{run_id}/proposal",
            json={"proposal_json": edited, "mode": "replace", "base_version": 1},
        )
        get_response = await client.get(f"/api/runs/{run_id}/proposal")

    assert put_response.status_code == 200, put_response.text
    assert put_response.json()["version"] == 1
    assert get_response.status_code == 200
    assert get_response.json()["version"] == 1
    assert get_response.json()["proposal_json"] == edited


async def test_proposal_replace_rejected_after_scout_completed(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Same setup as the post-accept stale test, but with
    ``mode=replace``. Codex AGREE amendment 5: 409 because scout
    has produced output."""
    from sqlalchemy import select

    from autoessay.branches import ensure_main_branch
    from autoessay.models import Run

    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Replace blocked after scout"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/proposal", json={})
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "SCOUT_RUNNING", "reason": "fake-scout"},
        )
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "USER_SEARCH_REVIEW", "reason": "fake-scout-done"},
        )
        from pathlib import Path as _Path

        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            scout_path = _Path(run.run_dir) / "discovery" / "scout_report.md"
            scout_path.parent.mkdir(parents=True, exist_ok=True)
            scout_path.write_text("# scout\n", encoding="utf-8")
            ensure_main_branch(session, run)
            session.commit()

        edited = {
            "research_question": "Should be rejected.",
            "significance": "Replace not allowed post-scout.",
            "preliminary_approach": "Doesn't matter.",
            "expected_contribution": "N/A.",
            "scope": "narrow",
            "preliminary_keywords": ["replace"],
        }
        put_response = await client.put(
            f"/api/runs/{run_id}/proposal",
            json={"proposal_json": edited, "mode": "replace"},
        )

    assert put_response.status_code == 409
    detail = put_response.json()["detail"].lower()
    assert "replace" in detail and "scout" in detail


async def test_proposal_base_version_concurrency_token(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Codex amendment 6: client echoes back ``base_version``; if the
    head moved underneath, return 409."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Base version concurrency"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/proposal", json={})
        # Now version=1. A stale client sending base_version=0 should 409.
        edited = {
            "research_question": "Stale save.",
            "significance": "Should reject.",
            "preliminary_approach": "Outdated.",
            "expected_contribution": "Concurrency.",
            "scope": "narrow",
            "preliminary_keywords": ["stale"],
        }
        put_response = await client.put(
            f"/api/runs/{run_id}/proposal",
            json={"proposal_json": edited, "base_version": 0},
        )

    assert put_response.status_code == 409
    detail = put_response.json()["detail"].lower()
    assert "another save" in detail or "base_version" in detail
