import json

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import Project, Run, RunEvent, utcnow


async def _save_source_review_checkpoint(
    client: AsyncClient,
    run_id: str,
    *,
    checkpoint_type: str,
    scope: str,
    source_ids: list[str],
) -> None:
    response = await client.post(
        f"/api/runs/{run_id}/checkpoints/{checkpoint_type}",
        json={
            "status": "ACCEPTED",
            "decision_payload": {
                "source_ids": source_ids,
                "approved_source_ids": source_ids,
                "rejected_source_ids": [],
                "pinned_source_ids": [],
                "review_scope": scope,
            },
        },
    )
    assert response.status_code == 201, response.text


async def _approve_all_skim_candidates(client: AsyncClient, run_id: str) -> list[str]:
    response = await client.get(f"/api/runs/{run_id}/sources")
    assert response.status_code == 200, response.text
    body = response.json()
    source_ids = [
        str(row["source_id"])
        for row in body["skim_candidates"]
        if isinstance(row, dict) and row.get("source_id")
    ]
    assert source_ids
    await _save_source_review_checkpoint(
        client,
        run_id,
        checkpoint_type="USER_SEARCH_REVIEW",
        scope="search_review",
        source_ids=source_ids,
    )
    return source_ids


async def _approve_all_shortlist_sources(client: AsyncClient, run_id: str) -> list[str]:
    response = await client.get(f"/api/runs/{run_id}/sources")
    assert response.status_code == 200, response.text
    body = response.json()
    source_ids = [
        str(row["source_id"])
        for row in body["shortlist"]
        if isinstance(row, dict) and row.get("source_id")
    ]
    assert source_ids
    await _save_source_review_checkpoint(
        client,
        run_id,
        checkpoint_type="USER_DEEP_DIVE_REVIEW",
        scope="deep_dive_review",
        source_ids=source_ids,
    )
    return source_ids


async def test_create_get_transition_run_appends_event(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={
                "title": "API test",
                "domain_id": "financial_history",
                "target_journal": None,
            },
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]

        create_response = await client.post(f"/api/projects/{project_id}/runs")
        assert create_response.status_code == 201
        created_run = create_response.json()
        run_id = created_run["id"]
        # create_run auto-advances TOPIC_ENTERED -> DOMAIN_LOADED so the
        # workspace lands on a state with a real next-step button.
        assert created_run["state"] == "DOMAIN_LOADED"
        assert created_run["last_event"]["event_type"] == "state_transition"

        get_response = await client.get(f"/api/runs/{run_id}")
        assert get_response.status_code == 200
        assert get_response.json()["state"] == "DOMAIN_LOADED"

    with app_session() as session:
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )
    # run_created + auto-transition to DOMAIN_LOADED.
    assert [event.event_type for event in events] == [
        "run_created",
        "state_transition",
    ]


async def test_delete_run_soft_deletes_only_that_run(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={
                "title": "Run delete scope",
                "domain_id": "financial_history",
                "target_journal": None,
            },
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]
        first_response = await client.post(f"/api/projects/{project_id}/runs")
        second_response = await client.post(f"/api/projects/{project_id}/runs")
        assert first_response.status_code == 201
        assert second_response.status_code == 201
        first_id = first_response.json()["id"]
        second_id = second_response.json()["id"]

        delete_response = await client.delete(f"/api/runs/{first_id}")
        assert delete_response.status_code == 204

        default_runs = await client.get("/api/runs")
        assert default_runs.status_code == 200
        default_ids = {row["id"] for row in default_runs.json()}
        assert first_id not in default_ids
        assert second_id in default_ids

        all_runs = await client.get("/api/runs?include_deleted=1")
        assert all_runs.status_code == 200
        rows = {row["id"]: row for row in all_runs.json()}
        assert rows[first_id]["deleted_at"] is not None
        assert rows[second_id]["deleted_at"] is None

    with app_session() as session:
        project = session.get(Project, project_id)
        first = session.get(Run, first_id)
        second = session.get(Run, second_id)
        assert project is not None
        assert project.deleted_at is None
        assert first is not None
        assert first.deleted_at is not None
        assert first.cancel_requested_at is not None
        assert second is not None
        assert second.deleted_at is None
        assert second.cancel_requested_at is None
        event = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == first_id, RunEvent.event_type == "run_deleted")
            .limit(1),
        )
        assert event is not None


async def test_restore_run_undeletes_only_that_run(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={
                "title": "Run restore scope",
                "domain_id": "financial_history",
                "target_journal": None,
            },
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]
        first_response = await client.post(f"/api/projects/{project_id}/runs")
        second_response = await client.post(f"/api/projects/{project_id}/runs")
        assert first_response.status_code == 201
        assert second_response.status_code == 201
        first_id = first_response.json()["id"]
        second_id = second_response.json()["id"]

        delete_response = await client.delete(f"/api/runs/{first_id}")
        assert delete_response.status_code == 204

        restore_response = await client.post(f"/api/runs/{first_id}/restore")
        assert restore_response.status_code == 200
        restored = restore_response.json()
        assert restored["deleted_at"] is None

        default_runs = await client.get("/api/runs")
        assert default_runs.status_code == 200
        default_ids = {row["id"] for row in default_runs.json()}
        assert first_id in default_ids
        assert second_id in default_ids

        all_runs = await client.get("/api/runs?include_deleted=1")
        rows = {row["id"]: row for row in all_runs.json()}
        assert rows[first_id]["deleted_at"] is None
        assert rows[second_id]["deleted_at"] is None

    with app_session() as session:
        project = session.get(Project, project_id)
        first = session.get(Run, first_id)
        second = session.get(Run, second_id)
        assert project is not None
        assert project.deleted_at is None
        assert first is not None
        assert first.deleted_at is None
        assert first.cancel_requested_at is None
        assert second is not None
        assert second.deleted_at is None
        event = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == first_id, RunEvent.event_type == "run_restored")
            .limit(1),
        )
        assert event is not None


async def test_restore_run_rejects_active_phase_lock(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={
                "title": "Run restore active lock",
                "domain_id": "financial_history",
                "target_journal": None,
            },
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        assert run_response.status_code == 201
        run_id = run_response.json()["id"]

        delete_response = await client.delete(f"/api/runs/{run_id}")
        assert delete_response.status_code == 204

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.active_phase_lock = "curator"
            run.active_phase_lock_job_id = "lock_restore_run"
            run.active_phase_lock_claimed_at = utcnow()
            session.commit()

        restore_response = await client.post(f"/api/runs/{run_id}/restore")

    assert restore_response.status_code == 409
    assert "active_phase_lock" in restore_response.text
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.deleted_at is not None


async def test_restore_run_records_recovery_warning_for_late_phase_done(
    app_session,
) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={
                "title": "Run restore late phase done",
                "domain_id": "financial_history",
                "target_journal": None,
            },
        )
        assert project_response.status_code == 201
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        assert run_response.status_code == 201
        run_id = run_response.json()["id"]

        delete_response = await client.delete(f"/api/runs/{run_id}")
        assert delete_response.status_code == 204

        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            assert run.cancel_requested_at is not None
            session.add(
                RunEvent(
                    id="event_late_phase_done",
                    run_id=run_id,
                    event_type="phase_done",
                    payload=json.dumps({"phase": "curator"}),
                ),
            )
            session.commit()

        restore_response = await client.post(f"/api/runs/{run_id}/restore")

    assert restore_response.status_code == 200, restore_response.text
    body = restore_response.json()
    assert body["deleted_at"] is None
    assert body["last_event"]["event_type"] == "run_restore_recovery_warning"
    assert body["last_event"]["payload"]["reason"] == "phase_done_after_cancel_intent"
    assert body["last_event"]["payload"]["phase"] == "curator"

    with app_session() as session:
        warning = session.scalar(
            select(RunEvent)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run_restore_recovery_warning",
            )
            .limit(1),
        )
        assert warning is not None


async def test_restore_run_rejects_not_deleted_run(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "Run restore no-op"})
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]

        restore_response = await client.post(f"/api/runs/{run_id}/restore")

    assert restore_response.status_code == 409
    assert "run is not deleted" in restore_response.text


async def test_deleted_run_rejects_mutation(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "Deleted run guard"})
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]

        delete_response = await client.delete(f"/api/runs/{run_id}")
        assert delete_response.status_code == 204
        scout_response = await client.post(f"/api/runs/{run_id}/scout")

    assert scout_response.status_code == 409
    assert "run is deleted" in scout_response.text


async def test_transition_run_rejects_disallowed_move(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "Invalid move"})
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]

        response = await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "EXPORTS_DONE", "reason": "too far"},
        )

    assert response.status_code == 409


async def test_record_checkpoint_endpoint_appends_event(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post("/api/projects", json={"title": "Checkpoint"})
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]

        response = await client.post(
            f"/api/runs/{run_id}/checkpoints/baseline-lock",
            json={
                "status": "ACCEPTED",
                "decision_payload": {"accepted_by": "test"},
            },
        )

    assert response.status_code == 201
    checkpoint = response.json()
    assert checkpoint["status"] == "ACCEPTED"
    assert checkpoint["decision_payload"] == {"accepted_by": "test"}

    with app_session() as session:
        last_event = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
            .limit(1),
        )
    assert last_event is not None
    assert last_event.event_type == "checkpoint_recorded"


async def test_start_scout_sync_and_get_discovery(app_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Banking panic discovery"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )

        scout_response = await client.post(f"/api/runs/{run_id}/scout")
        discovery_response = await client.get(f"/api/runs/{run_id}/discovery")

    assert scout_response.status_code == 202
    assert scout_response.json()["job_id"] == "sync"
    assert scout_response.json()["expected_state"] == "SCOUT_RUNNING"
    assert discovery_response.status_code == 200
    discovery = discovery_response.json()
    assert discovery["skim_candidates"]
    assert "Scout Report" in discovery["scout_report"]


async def test_start_curator_sync_and_get_sources(app_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Banking panic curation"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/scout")
        await _approve_all_skim_candidates(client, run_id)

        curator_response = await client.post(f"/api/runs/{run_id}/curator")
        sources_response = await client.get(f"/api/runs/{run_id}/sources")

    assert curator_response.status_code == 202
    assert curator_response.json()["job_id"] == "sync"
    assert curator_response.json()["expected_state"] == "CURATOR_RUNNING"
    assert sources_response.status_code == 200
    sources = sources_response.json()
    assert sources["shortlist"]
    assert "Curation Report" in sources["curation_report"]


async def test_start_curator_requires_current_search_review_checkpoint(
    app_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "curator requires search review"},
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/scout")

        missing_response = await client.post(f"/api/runs/{run_id}/curator")
        await _approve_all_skim_candidates(client, run_id)

        with app_session() as session:
            session.add(
                RunEvent(
                    id="evt_new_scout_done_after_checkpoint",
                    run_id=run_id,
                    event_type="phase_done",
                    payload=json.dumps({"phase": "scout", "candidate_count": 1}),
                )
            )
            session.commit()

        stale_response = await client.post(f"/api/runs/{run_id}/curator")
        get_response = await client.get(f"/api/runs/{run_id}")

    assert missing_response.status_code == 409
    assert "USER_SEARCH_REVIEW source review checkpoint" in missing_response.json()["detail"]
    assert stale_response.status_code == 409
    assert "current upstream output" in stale_response.json()["detail"]
    assert get_response.json()["state"] == "USER_SEARCH_REVIEW"


async def test_source_review_checkpoint_requires_matching_state_and_payload(
    app_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "source checkpoint contract"},
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        wrong_state_response = await client.post(
            f"/api/runs/{run_id}/checkpoints/USER_SEARCH_REVIEW",
            json={
                "status": "ACCEPTED",
                "decision_payload": {"source_ids": ["source_001"]},
            },
        )
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/scout")
        malformed_response = await client.post(
            f"/api/runs/{run_id}/checkpoints/USER_SEARCH_REVIEW",
            json={
                "status": "ACCEPTED",
                "decision_payload": {"review_scope": "search_review"},
            },
        )

    assert wrong_state_response.status_code == 409
    assert malformed_response.status_code == 400
    assert "source_ids" in malformed_response.json()["detail"]


async def test_start_synthesizer_sync_and_get_synthesis(app_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Banking panic synthesis"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/scout")
        await _approve_all_skim_candidates(client, run_id)
        await client.post(f"/api/runs/{run_id}/curator")
        await _approve_all_shortlist_sources(client, run_id)

        synthesizer_response = await client.post(f"/api/runs/{run_id}/synthesizer")
        synthesis_response = await client.get(f"/api/runs/{run_id}/synthesis")

    assert synthesizer_response.status_code == 202
    assert synthesizer_response.json()["job_id"] == "sync"
    assert synthesizer_response.json()["expected_state"] == "SYNTHESIZER_RUNNING"
    assert synthesis_response.status_code == 200
    synthesis = synthesis_response.json()
    assert synthesis["claims"]
    assert "Synthesizer Report" in synthesis["synthesizer_report"]


async def test_start_synthesizer_requires_deep_dive_review_checkpoint(
    app_session, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "synthesizer requires deep dive review"},
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/scout")
        await _approve_all_skim_candidates(client, run_id)
        await client.post(f"/api/runs/{run_id}/curator")

        missing_response = await client.post(f"/api/runs/{run_id}/synthesizer")
        await _approve_all_shortlist_sources(client, run_id)
        accepted_response = await client.post(f"/api/runs/{run_id}/synthesizer")

    assert missing_response.status_code == 409
    assert "USER_DEEP_DIVE_REVIEW source review checkpoint" in missing_response.json()["detail"]
    assert accepted_response.status_code == 202


async def test_novelty_checkpoint_sync_triggers_drafter(app_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
            "/api/projects",
            json={"title": "Banking panic novelty"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/scout")
        await _approve_all_skim_candidates(client, run_id)
        await client.post(f"/api/runs/{run_id}/curator")
        await _approve_all_shortlist_sources(client, run_id)
        await client.post(f"/api/runs/{run_id}/synthesizer")

        ideator_response = await client.post(f"/api/runs/{run_id}/ideator")
        novelty_response = await client.get(f"/api/runs/{run_id}/novelty")
        checkpoint_response = await client.post(
            f"/api/runs/{run_id}/checkpoints/USER_NOVELTY_REVIEW",
            json={"selected_angle_id": "angle_001"},
        )
        draft_list_response = await client.get(f"/api/runs/{run_id}/drafts")
        draft_response = await client.get(f"/api/runs/{run_id}/drafts/v001")

    assert ideator_response.status_code == 202
    assert ideator_response.json()["job_id"] == "sync"
    assert novelty_response.status_code == 200
    assert novelty_response.json()["angle_cards"]
    assert checkpoint_response.status_code == 201
    assert checkpoint_response.json()["decision_payload"]["selected_angle_id"] == "angle_001"
    assert draft_list_response.status_code == 200
    assert draft_list_response.json()["drafts"][0]["version"] == "v001"
    assert draft_response.status_code == 200
    assert "## Introduction" in draft_response.json()["manuscript"]


async def test_start_stylist_sync_and_get_style(app_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_STYLIST_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Banking panic style"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/scout")
        await _approve_all_skim_candidates(client, run_id)
        await client.post(f"/api/runs/{run_id}/curator")
        await _approve_all_shortlist_sources(client, run_id)
        await client.post(f"/api/runs/{run_id}/synthesizer")
        await client.post(f"/api/runs/{run_id}/ideator")
        await client.post(
            f"/api/runs/{run_id}/checkpoints/USER_NOVELTY_REVIEW",
            json={"selected_angle_id": "angle_001"},
        )

        stylist_response = await client.post(f"/api/runs/{run_id}/stylist")
        style_response = await client.get(f"/api/runs/{run_id}/style")
        score_response = await client.get(f"/api/runs/{run_id}/style/score")

    assert stylist_response.status_code == 202
    assert stylist_response.json()["job_id"] == "sync"
    assert style_response.status_code == 200
    assert "paper_styled" in style_response.json()
    assert score_response.status_code == 200
    assert score_response.json()["final"]["total"] >= 0


async def test_start_drafter_rejects_when_no_angle_selected(app_session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Stage 3.E follow-up: prod incident run_18b9e31c... reached
    USER_NOVELTY_REVIEW and a drafter click without picking an angle
    transitioned the run to DRAFTER_RUNNING, then FAILED_FIXABLE 11ms
    later. The endpoint now rejects up-front so state stays clean.
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
            "/api/projects", json={"title": "no-angle drafter rejection"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        await client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": "DOMAIN_LOADED", "reason": "test"},
        )
        await client.post(f"/api/runs/{run_id}/scout")
        await _approve_all_skim_candidates(client, run_id)
        await client.post(f"/api/runs/{run_id}/curator")
        await _approve_all_shortlist_sources(client, run_id)
        await client.post(f"/api/runs/{run_id}/synthesizer")
        await client.post(f"/api/runs/{run_id}/ideator")
        # Skip the USER_NOVELTY_REVIEW checkpoint (the bug condition).
        # Run is still in USER_NOVELTY_REVIEW with no angle picked.
        drafter_response = await client.post(f"/api/runs/{run_id}/drafter")
        get_response = await client.get(f"/api/runs/{run_id}")

    assert drafter_response.status_code == 409
    assert "selected novelty angle" in drafter_response.json()["detail"]
    # Critical: the rejected request must not transition the state.
    assert get_response.json()["state"] == "USER_NOVELTY_REVIEW"


async def test_start_stylist_rejects_when_no_draft_artifacts(
    app_session, monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Stage 3.E follow-up: stylist button appears the moment state hits
    DRAFTER_RUNNING, but drafter takes 5-10min to actually write
    manuscript.md. A click during that window used to enqueue stylist
    and FAIL_FIXABLE on missing artifacts. Endpoint now rejects.
    """
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    from autoessay.models import Run

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "no-artifact stylist rejection"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]

        # Force state directly to DRAFTER_RUNNING by writing the runs
        # row. State-machine validation would refuse this jump, but
        # the prod race produces it via the legitimate path
        # (checkpoint→transition→drafter, mid-flight) — same observable
        # state from the API's perspective.
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "DRAFTER_RUNNING"
            session.commit()

        stylist_response = await client.post(f"/api/runs/{run_id}/stylist")
        get_response = await client.get(f"/api/runs/{run_id}")

    assert stylist_response.status_code == 409
    detail = stylist_response.json()["detail"]
    assert "Drafter has not" in detail
    # Run state must be unchanged.
    assert get_response.json()["state"] == "DRAFTER_RUNNING"


async def test_start_curator_rejects_when_no_source_candidates(  # type: ignore[no-untyped-def]
    app_session, monkeypatch, tmp_path
) -> None:
    """Stage 3.E follow-up (codex AGREE: system-wide audit): curator
    needs scout output OR a manual upload. Without either the agent
    fails fixable; the API now rejects up-front."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    from autoessay.models import Run

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "no-source curator rejection"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        # Force state to USER_SEARCH_REVIEW without scout output.
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "USER_SEARCH_REVIEW"
            session.commit()

        curator_response = await client.post(f"/api/runs/{run_id}/curator")
        get_response = await client.get(f"/api/runs/{run_id}")

    assert curator_response.status_code == 409
    assert "at least one source" in curator_response.json()["detail"]
    assert get_response.json()["state"] == "USER_SEARCH_REVIEW"


async def test_start_synthesizer_rejects_when_shortlist_empty(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """Synthesizer needs Curator's shortlist non-empty."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    from autoessay.models import Run

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "empty-shortlist synth rejection"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "USER_DEEP_DIVE_REVIEW"
            session.commit()
        # No shortlist.json → synthesizer_ready returns False.
        synth_response = await client.post(f"/api/runs/{run_id}/synthesizer")
        get_response = await client.get(f"/api/runs/{run_id}")

    assert synth_response.status_code == 409
    assert "shortlist" in synth_response.json()["detail"].lower()
    assert get_response.json()["state"] == "USER_DEEP_DIVE_REVIEW"


async def test_start_critic_rejects_when_no_styled_draft(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """Critic needs a styled draft."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    from autoessay.models import Run

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "no-styled critic rejection"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "USER_REVISION_REVIEW"
            session.commit()

        critic_response = await client.post(f"/api/runs/{run_id}/critic")
        get_response = await client.get(f"/api/runs/{run_id}")

    assert critic_response.status_code == 409
    assert "styled draft" in critic_response.json()["detail"].lower()
    assert get_response.json()["state"] == "USER_REVISION_REVIEW"


async def test_start_exports_rejects_when_no_styled_manuscript(  # type: ignore[no-untyped-def]
    app_session, monkeypatch
) -> None:
    """Exports needs a final styled manuscript."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    from autoessay.models import Run

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects", json={"title": "no-manuscript exports rejection"}
        )
        run_response = await client.post(f"/api/projects/{project_response.json()['id']}/runs")
        run_id = run_response.json()["id"]
        with app_session() as session:
            run = session.get(Run, run_id)
            assert run is not None
            run.state = "USER_FINAL_ACCEPTANCE"
            session.commit()

        exports_response = await client.post(f"/api/runs/{run_id}/export")
        get_response = await client.get(f"/api/runs/{run_id}")

    assert exports_response.status_code == 409
    detail = exports_response.json()["detail"].lower()
    assert "draft" in detail or "manuscript" in detail
    assert get_response.json()["state"] == "USER_FINAL_ACCEPTANCE"
