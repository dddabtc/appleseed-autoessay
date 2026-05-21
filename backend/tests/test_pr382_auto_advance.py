"""PR-382 coverage for the one-click auto-pilot coordinator.

Codex AGREE-WITH-AMENDMENTS 2026-05-13:
- B1 idempotent service (in-transaction).
- Approve all qualified-and-deduped sources (amendment 2).
- Read ``recommended_angle_id`` from ideator artifact (amendment 3).
- ``FAILED_*`` always pause; ``FAILED_VENDOR`` included (amendment 4).
- Table-driven coordinator (amendment X).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.auto_advance import (
    _DISPATCH,
    _PAUSE_STATES,
    _all_source_ids_from_shortlist,
    _all_source_ids_from_skim,
    _pick_recommended_angle,
    maybe_advance,
)
from autoessay.main import app
from autoessay.models import Run, RunEvent

# ---- Pure-function helpers ---------------------------------------------------


def test_dispatch_table_covers_every_user_review_state_and_kickoff() -> None:
    """Coordinator is the single source of truth. Every ``USER_*_REVIEW``
    state the state-machine can land in must have a handler entry; the
    fresh-run ``DOMAIN_LOADED`` kickoff also belongs (PR-386 — without
    it auto-pilot leaves runs stranded at "Generate Initial Proposal")."""
    expected = {
        # PR-386 fresh-run kickoff
        "DOMAIN_LOADED",
        # PR-393 drafter→stylist bridge (drafter writes phase_done
        # without transitioning state)
        "DRAFTER_RUNNING",
        "USER_PROPOSAL_REVIEW",
        "USER_SEARCH_REVIEW",
        "USER_DEEP_DIVE_REVIEW",
        "USER_FIELD_REVIEW",
        "USER_LENS_REVIEW",
        "USER_NOVELTY_REVIEW",
        "USER_REVISION_REVIEW",
        "USER_EXTERNAL_SCAN_APPROVAL",
        "USER_INTEGRITY_REVIEW",
        "USER_FINAL_ACCEPTANCE",
    }
    assert set(_DISPATCH.keys()) == expected


def test_pause_states_include_failed_vendor() -> None:
    """Codex amendment 4: FAILED_VENDOR must pause auto-pilot too."""
    assert "FAILED_VENDOR" in _PAUSE_STATES
    assert "FAILED_FIXABLE" in _PAUSE_STATES
    assert "FAILED_POLICY" in _PAUSE_STATES
    assert "FAILED_NEEDS_USER" in _PAUSE_STATES


def test_all_source_ids_from_skim_dedupes() -> None:
    payload = {
        "skim_candidates": [
            {"source_id": "src1"},
            {"source_id": "src2"},
            {"source_id": "src1"},  # duplicate
            {"source_id": "src3"},
        ],
    }
    assert _all_source_ids_from_skim(payload) == ["src1", "src2", "src3"]


def test_all_source_ids_from_skim_handles_missing_payload() -> None:
    assert _all_source_ids_from_skim(None) == []
    assert _all_source_ids_from_skim({}) == []
    assert _all_source_ids_from_skim({"skim_candidates": []}) == []


def test_all_source_ids_from_shortlist_dedupes() -> None:
    payload = {
        "shortlist": [
            {"source_id": "a"},
            {"source_id": "b"},
            {"source_id": "a"},
        ],
    }
    assert _all_source_ids_from_shortlist(payload) == ["a", "b"]


def test_pick_recommended_angle_prefers_explicit_field() -> None:
    """Codex amendment 3: when ideator artifact carries
    ``recommended_angle_id``, prefer it over the first card."""
    payload = {
        "recommended_angle_id": "angle_003",
        "angle_cards": [{"angle_id": "angle_001"}],
    }
    assert _pick_recommended_angle(payload) == "angle_003"


def test_pick_recommended_angle_falls_back_to_first_card() -> None:
    payload = {"angle_cards": [{"angle_id": "angle_042"}, {"angle_id": "angle_002"}]}
    assert _pick_recommended_angle(payload) == "angle_042"


def test_pick_recommended_angle_returns_none_for_empty_payload() -> None:
    assert _pick_recommended_angle({}) is None
    assert _pick_recommended_angle({"angle_cards": []}) is None
    assert _pick_recommended_angle(None) is None


# ---- maybe_advance behavior --------------------------------------------------


def test_maybe_advance_is_noop_when_disabled(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``run.auto_advance=False`` → coordinator returns False without
    side effects."""
    from conftest import seed_project

    run_dir = tmp_path / "run_disabled"
    run_dir.mkdir()
    with app_session() as session:
        project = seed_project(session)
        run = Run(
            id="run_disabled",
            project_id=project.id,
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="USER_PROPOSAL_REVIEW",
            baseline_hash="test",
            auto_advance=False,
        )
        session.add(run)
        session.commit()
        result = maybe_advance(session, run, source="test")
        assert result is False
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run.id),
        ).all()
        assert not any(e.event_type.startswith("auto_advance") for e in events)


def test_maybe_advance_pauses_on_failed_state(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Codex amendment 4: ``FAILED_*`` always pause + emit
    ``auto_advance_paused`` event."""
    from conftest import seed_project

    run_dir = tmp_path / "run_failed_pr382"
    run_dir.mkdir()
    with app_session() as session:
        project = seed_project(session)
        run = Run(
            id="run_failed",
            project_id=project.id,
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="FAILED_FIXABLE",
            baseline_hash="test",
            auto_advance=True,
        )
        session.add(run)
        session.commit()
        result = maybe_advance(session, run, source="test")
        assert result is False
        events = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run.id, RunEvent.event_type == "auto_advance_paused"
            ),
        ).all()
        assert len(events) == 1
        payload = json.loads(events[0].payload)
        assert payload["state"] == "FAILED_FIXABLE"
        assert payload["source"] == "test"


def test_maybe_advance_pauses_on_failed_vendor(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from conftest import seed_project

    run_dir = tmp_path / "run_vendor"
    run_dir.mkdir()
    with app_session() as session:
        project = seed_project(session)
        run = Run(
            id="run_vendor",
            project_id=project.id,
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="FAILED_VENDOR",
            baseline_hash="test",
            auto_advance=True,
        )
        session.add(run)
        session.commit()
        assert maybe_advance(session, run, source="test") is False


def test_maybe_advance_returns_false_for_running_state(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A worker is mid-flight (state is ``*_RUNNING``) — coordinator
    has nothing to do; never raises."""
    from conftest import seed_project

    run_dir = tmp_path / "run_running"
    run_dir.mkdir()
    with app_session() as session:
        project = seed_project(session)
        run = Run(
            id="run_running",
            project_id=project.id,
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="SCOUT_RUNNING",
            baseline_hash="test",
            auto_advance=True,
        )
        session.add(run)
        session.commit()
        assert maybe_advance(session, run, source="test") is False


# ---- HTTP integration --------------------------------------------------------


async def _make_project(client: AsyncClient, *, title: str) -> str:
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


async def test_create_run_accepts_auto_advance_true(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="AA create")
        response = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"auto_advance": True},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["auto_advance"] is True

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == body["id"]))
        assert run is not None
        assert run.auto_advance is True


async def test_create_run_inherits_prior_auto_advance(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="AA inherit")
        first = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"auto_advance": True},
        )
        assert first.json()["auto_advance"] is True
        # Omit auto_advance on the re-run → inherits True from first.
        second = await client.post(f"/api/projects/{project_id}/runs")
        assert second.json()["auto_advance"] is True


async def test_patch_settings_flips_auto_advance(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="AA patch")
        run_id = (await client.post(f"/api/projects/{project_id}/runs")).json()["id"]
        # Stub the coordinator so the PATCH doesn't try to actually
        # advance the run (state machine + LLM dependencies are out
        # of scope for the toggle test).
        with patch("autoessay.auto_advance.maybe_advance", return_value=False):
            response = await client.patch(
                f"/api/runs/{run_id}/settings",
                json={"auto_advance": True},
            )
        assert response.status_code == 200
        assert response.json()["auto_advance"] is True

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.auto_advance is True


async def test_patch_settings_flipping_on_triggers_coordinator(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    """Codex amendment 5 / phase_done parity: flipping ``auto_advance``
    ON should immediately call the coordinator so the run advances
    from whatever ``USER_*_REVIEW`` state it's in, not just at the
    NEXT phase-done."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="AA trigger")
        run_id = (await client.post(f"/api/projects/{project_id}/runs")).json()["id"]
        with patch("autoessay.auto_advance.maybe_advance", return_value=False) as mock_advance:
            response = await client.patch(
                f"/api/runs/{run_id}/settings",
                json={"auto_advance": True},
            )
            assert response.status_code == 200
            mock_advance.assert_called_once()
            assert mock_advance.call_args.kwargs["source"] == "settings_toggle"


async def test_patch_settings_extra_forbids_unknown_keys(app_session) -> None:  # type: ignore[no-untyped-def]
    """Regression: ``extra=forbid`` keeps working with the new field."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="AA strict")
        run_id = (await client.post(f"/api/projects/{project_id}/runs")).json()["id"]
        response = await client.patch(
            f"/api/runs/{run_id}/settings",
            json={"auto_advnce": True},  # typo
        )
        assert response.status_code == 422


async def test_run_created_event_records_auto_advance(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="AA audit")
        created = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"auto_advance": True},
        )
        run_id = created.json()["id"]
    with app_session() as session:
        event = session.scalar(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run_created"),
        )
        assert event is not None
        payload = json.loads(event.payload)
        assert payload["auto_advance"] is True
