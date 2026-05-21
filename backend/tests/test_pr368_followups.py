"""PR-368 follow-up coverage (review consensus 2026-05-13).

Targets the 3 P1 issues found in the Claude+codex deep review of
PR-355~366:

* P1-1: PATCH /settings must also refuse while ``active_phase_lock``
  is held for ``final_rewrite`` / ``critic``. ``start_critic`` claims
  the lock BEFORE the worker transitions state, so without this guard
  there is a race window where ``state=USER_REVISION_REVIEW`` but the
  rewriter is about to read ``run.mathematical_mode``.
* P1-2: V3 paired critic prompt must carry the empirical_completeness
  checklist that V2 has, otherwise round-0 stage B's LaTeX / 待填
  scaffolding can sail through the V3 critic without hard caps.
* P2-1 (audit upgrade): ``run_created`` records the initial
  ``mathematical_mode``; ``run_settings_updated`` records the
  state + lock at flip time.
* P2-2: ``UpdateRunSettingsRequest`` rejects unknown keys (422)
  instead of silently no-opping on client typos.
* P2-3: When the caller omits ``mathematical_mode`` (or sends null),
  ``create_run`` inherits the latest non-deleted run's value on the
  same project. Explicit ``true`` / ``false`` always wins.
* P2-5: PATCH /settings refuses a soft-deleted run with 409.

The P1-3 deterministic-compliance fallback is covered in
``test_round0_polish_loop.py`` (round-0-only fallback) and exercised
indirectly via the critic-loop replacement path; those tests already
pass on this branch.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.main import app
from autoessay.models import Run, RunEvent


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


async def test_run_created_event_records_initial_mathematical_mode(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-368 audit")
        created = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"mathematical_mode": True},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
    with app_session() as session:
        event = session.scalar(
            select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "run_created")
        )
        assert event is not None
        payload = json.loads(event.payload)
        assert payload["mathematical_mode"] is True


async def test_run_settings_updated_event_records_state_and_lock(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-368 patch audit")
        created = await client.post(f"/api/projects/{project_id}/runs")
        run_id = created.json()["id"]
        patched = await client.patch(
            f"/api/runs/{run_id}/settings",
            json={"mathematical_mode": True},
        )
        assert patched.status_code == 200
    with app_session() as session:
        event = session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run_settings_updated",
            )
        )
        assert event is not None
        payload = json.loads(event.payload)
        assert payload["state"] == "DOMAIN_LOADED"
        # No phase lock at this state.
        assert payload["active_phase_lock"] is None


async def test_patch_settings_refuses_unknown_keys(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-368 extra=forbid")
        created = await client.post(f"/api/projects/{project_id}/runs")
        run_id = created.json()["id"]
        response = await client.patch(
            f"/api/runs/{run_id}/settings",
            json={"mathmode": True},
        )
        assert response.status_code == 422, response.text


async def test_patch_settings_refuses_when_phase_lock_held(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-368 phase-lock guard")
        created = await client.post(f"/api/projects/{project_id}/runs")
        run_id = created.json()["id"]

        # Simulate the race window: start_critic has claimed
        # final_rewrite lock but the worker hasn't transitioned state
        # yet, so state is still USER_REVISION_REVIEW. Without P1-1
        # the PATCH would 200 here. With P1-1 it must 409.
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.state = "USER_REVISION_REVIEW"
            run.active_phase_lock = "final_rewrite"
            run.active_phase_lock_job_id = "fake-token"
            run.active_phase_lock_claimed_at = datetime.now(timezone.utc)
            session.commit()

        response = await client.patch(
            f"/api/runs/{run_id}/settings",
            json={"mathematical_mode": True},
        )
        assert response.status_code == 409, response.text
        assert "final_rewrite" in response.json()["detail"]

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.mathematical_mode is False


async def test_patch_settings_refuses_when_critic_lock_held(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-368 critic-lock guard")
        created = await client.post(f"/api/projects/{project_id}/runs")
        run_id = created.json()["id"]
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            # State is still pre-critic, but the critic phase lock
            # has already been claimed.
            run.state = "USER_REVISION_REVIEW"
            run.active_phase_lock = "critic"
            run.active_phase_lock_job_id = "fake-token"
            run.active_phase_lock_claimed_at = datetime.now(timezone.utc)
            session.commit()
        response = await client.patch(
            f"/api/runs/{run_id}/settings",
            json={"mathematical_mode": True},
        )
        assert response.status_code == 409


async def test_patch_settings_refuses_soft_deleted_run(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-368 soft-delete guard")
        created = await client.post(f"/api/projects/{project_id}/runs")
        run_id = created.json()["id"]
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            run.deleted_at = datetime.now(timezone.utc)
            session.commit()
        response = await client.patch(
            f"/api/runs/{run_id}/settings",
            json={"mathematical_mode": True},
        )
        assert response.status_code == 409
        # Backend message must explicitly mention deletion so users
        # know the right next step.
        assert "delete" in response.json()["detail"].lower()


async def test_create_run_inherits_prior_mathematical_mode_when_omitted(  # type: ignore[no-untyped-def]
    app_session,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_id = await _make_project(client, title="PR-368 inherit")
        first = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"mathematical_mode": True},
        )
        assert first.status_code == 201
        first_id = first.json()["id"]
        assert first.json()["mathematical_mode"] is True

        # Caller omits the field on the re-run → inherits True.
        second = await client.post(f"/api/projects/{project_id}/runs")
        assert second.status_code == 201
        assert second.json()["mathematical_mode"] is True
        assert second.json()["id"] != first_id

        # Explicit False on the third re-run wins over inheritance.
        third = await client.post(
            f"/api/projects/{project_id}/runs",
            json={"mathematical_mode": False},
        )
        assert third.status_code == 201
        assert third.json()["mathematical_mode"] is False

        # Fourth re-run with omitted field inherits from the latest
        # (third = False), not the original (first = True).
        fourth = await client.post(f"/api/projects/{project_id}/runs")
        assert fourth.status_code == 201
        assert fourth.json()["mathematical_mode"] is False


async def test_v3_critic_prompt_carries_empirical_completeness_checklist() -> None:
    # P1-2: the V3 paired critic prompt must include the empirical
    # checklist + hard caps that were originally only in V2.
    from autoessay.agents._critic_polish_loop import (
        POLISH_BLIND_EVAL_SYSTEM_PROMPT,
        POLISH_BLIND_EVAL_V3_SYSTEM_PROMPT,
    )

    assert POLISH_BLIND_EVAL_SYSTEM_PROMPT is POLISH_BLIND_EVAL_V3_SYSTEM_PROMPT
    assert "empirical_completeness" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    # Hard caps must reference the V3 dim names (compliance,
    # completeness, evidence_strength) — V3 has no
    # methodological_rigor / reproducibility, so the V2 caps had to
    # be re-mapped (codex AGREE PR-368 P1-2 amendment 2).
    assert "completeness 不得高于" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert "evidence_strength 不得高于" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    # Paired-prompt clarification (codex amendment 1).
    assert "对 paired prompt 中每个 candidate 独立执行" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    # suspicious_numeric_results refined to empirical-finding
    # numbers only (codex amendment 2).
    assert "作为实证发现呈现" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
