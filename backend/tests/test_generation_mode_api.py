from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.config import Settings, get_settings
from autoessay.main import app
from autoessay.models import Run, RunEvent


async def _make_project(client: AsyncClient, *, title: str = "Generation mode") -> str:
    response = await client.post(
        "/api/projects",
        json={
            "title": title,
            "domain_id": "financial_history",
            "target_journal": None,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _create_run(
    client: AsyncClient, project_id: str, payload: dict[str, object]
) -> dict[str, object]:
    response = await client.post(f"/api/projects/{project_id}/runs", json=payload)
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_settings_default_mode_is_express() -> None:
    assert Settings().manuscript_default_mode == "express"


async def test_create_run_defaults_from_manuscript_default_mode_express(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("MANUSCRIPT_DEFAULT_MODE", "express")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client)
        body = await _create_run(client, project_id, {})
        assert body["mode"] == "express"
        assert body["auto_advance"] is False

    with app_session() as session:
        run = session.get(Run, body["id"])
        assert run is not None
        assert run.generation_mode == "express"


async def test_create_run_accepts_deep_and_express_modes(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Explicit modes")
        deep = await _create_run(client, project_id, {"mode": "deep"})
        express = await _create_run(client, project_id, {"mode": "express"})

    assert deep["mode"] == "deep"
    assert express["mode"] == "express"
    with app_session() as session:
        modes = session.scalars(
            select(Run.generation_mode).where(Run.id.in_([deep["id"], express["id"]])),
        ).all()
        assert set(modes) == {"deep", "express"}


async def test_generation_modes_endpoint_exposes_default(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("MANUSCRIPT_DEFAULT_MODE", "express")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/generation_modes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["default_mode"] == "express"
    assert {mode["id"] for mode in body["modes"]} == {"express", "deep"}


async def test_create_run_rejects_unknown_mode(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Unknown mode")
        response = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"mode": "turbo"},
        )
    assert response.status_code == 422
    assert "mode" in response.text


async def test_manuscript_default_mode_deep_fallback(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("MANUSCRIPT_DEFAULT_MODE", "deep")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Default deep")
        body = await _create_run(client, project_id, {})
    assert body["mode"] == "deep"


async def test_express_rejects_auto_advance_true(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Express auto reject")
        response = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"mode": "express", "auto_advance": True},
        )
    assert response.status_code == 422
    assert "auto_advance" in response.json()["detail"]


async def test_mode_can_change_before_generation_begins_and_writes_event(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Mode patch")
        created = await _create_run(client, project_id, {"mode": "express"})
        response = await client.patch(
            f"/api/runs/{created['id']}/settings",
            json={"mode": "deep"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["mode"] == "deep"

    with app_session() as session:
        run = session.get(Run, created["id"])
        assert run is not None
        assert run.generation_mode == "deep"
        event = session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == created["id"])
            .where(RunEvent.event_type == "run_settings_updated")
            .order_by(RunEvent.created_at.desc()),
        ).first()
        assert event is not None
        payload = json.loads(event.payload)
        assert payload["field"] == "mode"
        assert payload["from"] == "express"
        assert payload["to"] == "deep"


async def test_mode_immutable_after_generation_begins(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Mode immutable")
        created = await _create_run(client, project_id, {"mode": "deep"})
        with app_session() as session:
            run = session.get(Run, created["id"])
            assert run is not None
            run.state = "PROPOSAL_DRAFTING"
            session.commit()

        response = await client.patch(
            f"/api/runs/{created['id']}/settings",
            json={"mode": "express"},
        )
        assert response.status_code == 409, response.text
        assert "mode cannot be changed" in response.json()["detail"]

    with app_session() as session:
        run = session.get(Run, created["id"])
        assert run is not None
        assert run.generation_mode == "deep"


async def test_deep_start_proposal_uses_deep_enqueue(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Deep route")
        created = await _create_run(client, project_id, {"mode": "deep"})
        with (
            patch("autoessay.main.enqueue_proposal_job", return_value="deep-job") as deep_enqueue,
            patch(
                "autoessay.main.enqueue_express_job", return_value="express-job"
            ) as express_enqueue,
        ):
            response = await client.post(
                f"/api/runs/{created['id']}/proposal",
                json={"user_draft": None},
            )
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "deep-job"
    assert response.json()["expected_state"] == "PROPOSAL_DRAFTING"
    deep_enqueue.assert_called_once()
    express_enqueue.assert_not_called()


async def test_express_start_proposal_routes_to_express_stub(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Express route")
        created = await _create_run(client, project_id, {"mode": "express"})
        with (
            patch("autoessay.main.enqueue_proposal_job", return_value="deep-job") as deep_enqueue,
            patch(
                "autoessay.main.enqueue_express_job", return_value="express-job"
            ) as express_enqueue,
        ):
            response = await client.post(
                f"/api/runs/{created['id']}/proposal",
                json={"user_draft": None},
            )
    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "express-job"
    assert response.json()["expected_state"] == "EXPRESS_RUNNING"
    deep_enqueue.assert_not_called()
    express_enqueue.assert_called_once()

    with app_session() as session:
        run = session.get(Run, created["id"])
        assert run is not None
        assert run.state == "EXPRESS_RUNNING"
        assert run.active_phase_lock == "express"
        event = session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == created["id"])
            .where(RunEvent.event_type == "express_generation_enqueued")
        ).first()
        assert event is not None


async def test_express_start_proposal_always_enqueues_even_in_sync_worker(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Express route sync")
        created = await _create_run(client, project_id, {"mode": "express"})
        with patch("autoessay.main.enqueue_express_job", return_value="express-job") as enqueue:
            response = await client.post(
                f"/api/runs/{created['id']}/proposal",
                json={"user_draft": None},
            )

    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "express-job"
    enqueue.assert_called_once()

    with app_session() as session:
        run = session.get(Run, created["id"])
        assert run is not None
        assert run.state == "EXPRESS_RUNNING"


async def test_express_run_cannot_enter_deep_phase_even_if_state_matches(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="No fallback")
        created = await _create_run(client, project_id, {"mode": "express"})
        with app_session() as session:
            run = session.get(Run, created["id"])
            assert run is not None
            run.state = "USER_NOVELTY_REVIEW"
            session.commit()
        response = await client.post(f"/api/runs/{created['id']}/drafter")
    assert response.status_code == 409
    assert "mode=deep" in response.json()["detail"]


async def test_express_transparency_endpoint_reads_runner_artifacts(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="Express transparency")
        created = await _create_run(client, project_id, {"mode": "express"})

    with app_session() as session:
        run = session.get(Run, created["id"])
        assert run is not None
        run.state = "EXPRESS_DONE"
        express_dir = Path(run.run_dir) / "express"
        draft_dir = Path(run.run_dir) / "drafts" / "v001"
        express_dir.mkdir(parents=True, exist_ok=True)
        draft_dir.mkdir(parents=True, exist_ok=True)
        (express_dir / "ars_prompt.redacted.md").write_text("prompt", encoding="utf-8")
        (draft_dir / "manuscript.md").write_text("# Title\n\n## Body\nText", encoding="utf-8")
        (express_dir / "audit_critic.json").write_text(
            json.dumps({"status": "pass", "summary": "ok", "issues": []}),
            encoding="utf-8",
        )
        (express_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "provider": "test",
                    "provider_model": "gpt-5.4",
                    "token_cap": 100000,
                    "token_usage": {"total_tokens": 30000},
                    "prompt_sha256": "abc",
                },
            ),
            encoding="utf-8",
        )
        session.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/runs/{created['id']}/express_transparency")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider_model"] == "gpt-5.4"
    assert body["token_usage"]["total_tokens"] == 30000
    assert body["audit_summary"]["status"] == "pass"
    assert body["outline"][0]["title"] == "Title"
