"""PR-386 regression: PR-382 auto-pilot left runs stranded at
``DOMAIN_LOADED`` because the dispatch table only handled
``USER_*_REVIEW`` gates AND ``create_run`` never fired the coordinator
after the initial state transition. Real user reproduction
2026-05-13: clicked 一键全自动 + 创建项目, watched the run sit at
"(1/9) Loading domain…" indefinitely.

Two fixes both must hold:
- ``DOMAIN_LOADED`` is in ``_DISPATCH``
- ``create_run`` calls ``maybe_advance`` after the final commit when
  ``auto_advance=True``
"""

from __future__ import annotations

from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.auto_advance import _DISPATCH, maybe_advance
from autoessay.main import app
from autoessay.models import Run


def test_dispatch_includes_domain_loaded() -> None:
    assert "DOMAIN_LOADED" in _DISPATCH


async def _make_project_and_run(
    client: AsyncClient,
    *,
    auto_advance: bool,
    title: str,
) -> tuple[str, str]:
    """Project + run via the public HTTP route so we hit the real
    ``create_run`` code path; returns (project_id, run_id)."""
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
    run_resp = await client.post(
        f"/api/projects/{project_id}/runs",
        json={"auto_advance": auto_advance},
    )
    assert run_resp.status_code == 201, run_resp.text
    return project_id, run_resp.json()["id"]


async def test_create_run_with_auto_advance_fires_coordinator(app_session) -> None:  # type: ignore[no-untyped-def]
    """Codex amendment: this is the actual root-cause coverage. The
    dispatch fix alone is useless if ``create_run`` never calls
    ``maybe_advance`` in the first place."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch(
            "autoessay.auto_advance.maybe_advance",
            return_value=True,
        ) as coordinator:
            await _make_project_and_run(
                client,
                auto_advance=True,
                title="PR-386 kickoff fires",
            )
        coordinator.assert_called()
        sources = [c.kwargs.get("source") for c in coordinator.call_args_list]
        assert "run_created" in sources


async def test_create_run_without_auto_advance_does_not_fire_coordinator(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """Coordinator only fires when the toggle is on at create time."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("autoessay.auto_advance.maybe_advance") as coordinator:
            await _make_project_and_run(
                client,
                auto_advance=False,
                title="PR-386 no kickoff",
            )
        sources = [c.kwargs.get("source") for c in coordinator.call_args_list]
        assert "run_created" not in sources


async def test_handler_kicks_off_proposal_async(app_session) -> None:  # type: ignore[no-untyped-def]
    """End-to-end on the dispatch path: ``maybe_advance`` invoked on a
    DOMAIN_LOADED run with auto_advance=True must call
    ``enqueue_proposal_job`` (async worker) with ``user_draft=None``."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("autoessay.auto_advance.maybe_advance", return_value=False):
            _, run_id = await _make_project_and_run(
                client,
                auto_advance=True,
                title="PR-386 dispatch async",
            )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.auto_advance is True
        assert run.state == "DOMAIN_LOADED"

        with (
            patch("autoessay.main._claim_or_409", return_value="tok-X"),
            patch(
                "autoessay.config.get_settings",
                lambda: type("S", (), {"sync_worker": False})(),
            ),
            patch(
                "autoessay.worker.enqueue_proposal_job",
                return_value="job-id-1",
            ) as enqueue,
        ):
            advanced = maybe_advance(session, run, source="run_created")

        assert advanced is True
        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        assert kwargs.get("user_draft") is None
        assert kwargs.get("lock_token") == "tok-X"


async def test_handler_no_op_when_auto_advance_off(app_session) -> None:  # type: ignore[no-untyped-def]
    """Without the toggle the coordinator must not start phases on
    DOMAIN_LOADED."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        _, run_id = await _make_project_and_run(
            client,
            auto_advance=False,
            title="PR-386 off",
        )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None

        with (
            patch("autoessay.main._claim_or_409") as claim,
            patch("autoessay.worker.enqueue_proposal_job") as enqueue,
        ):
            advanced = maybe_advance(session, run, source="run_created")

        assert advanced is False
        claim.assert_not_called()
        enqueue.assert_not_called()
