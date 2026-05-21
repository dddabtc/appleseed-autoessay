"""Tests for the per-phase rerun endpoint (codex-AGREEd #2 stage 1).

Codex enforced the following invariants:
- 409 on deleted project / cancelled run / a run currently in any
  *_RUNNING state
- 409 on a phase with no completed output (use artifact existence,
  not Run.state)
- After a successful rerun upstream of an existing stale phase, the
  marker resets to the first completed downstream of the rerun phase
- After successfully rerunning the current ``stale_from_phase``, the
  marker advances to the next still-stale downstream phase or clears
- 409 when rerunning a phase strictly downstream of stale_from_phase
- A failed rerun does NOT update stale_from_phase
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import Domain, Project, Run, RunEvent, User, utcnow
from autoessay.run_writer import create_run_directory


def _seed_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    run_id: str,
    state: str = "USER_DEEP_DIVE_REVIEW",
    proposal_version: int = 0,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state=state,
        domain_id="financial_history",
    )
    _seed_upstream_phase_artifacts(run_dir)
    with app_session() as session:
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
                id="proj_test",
                user_id="single-user",
                title="t",
                domain_id="financial_history",
                domain_version="0.1.0",
                status="CREATED",
            ),
        )
        session.add(
            Run(
                id=run_id,
                project_id="proj_test",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state=state,
                baseline_hash="x",
                proposal_version=proposal_version,
            ),
        )
        session.commit()
    return run_dir


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_upstream_phase_artifacts(run_dir: Path) -> None:
    """Write only the *upstream* prereqs that ``phase_readiness``
    checks for phases up to and including ideator. We deliberately
    do NOT seed drafter/stylist/critic outputs — tests that exercise
    those phases write their own artifacts, and seeding them here
    would falsely advertise downstream completion (e.g., make
    ``has_completed_output(drafter)`` true and break tests that
    assert "ideator was the last completed phase").

    Stage 3.E follow-up: ``assert_can_rerun`` now also calls
    ``assert_phase_ready`` (codex AGREE: start_* and rerun must enforce
    identical preconditions).
    """
    _touch(run_dir / "discovery" / "skim_candidates.jsonl", '{"id":"x"}\n')
    _touch(run_dir / "sources" / "shortlist.json", '[{"source_id":"x"}]\n')
    _touch(
        run_dir / "synthesis" / "claims.jsonl",
        '{"claim_id":"c1","source_id":"x"}\n',
    )
    _touch(
        run_dir / "novelty" / "selected_thesis.json",
        '{"angle_id":"angle_001"}',
    )


def test_rewind_for_rerun_records_state_transition_event(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed_run(app_session, tmp_path, "run_rewind_helper", state="USER_FIELD_REVIEW")
    from autoessay.phase_rerun import rewind_for_rerun

    with app_session() as session:
        run = session.get(Run, "run_rewind_helper")
        assert run is not None
        event = rewind_for_rerun(
            run,
            "synthesizer",
            "USER_DEEP_DIVE_REVIEW",
            session,
            source="unit_test",
        )
        payload = json.loads(event.payload)
        session.commit()

    assert payload == {
        "from_state": "USER_FIELD_REVIEW",
        "to_state": "USER_DEEP_DIVE_REVIEW",
        "phase": "synthesizer",
        "reason": "rerun_rewind",
        "source": "unit_test",
    }


def test_proposal_less_scout_rerun_resolves_to_domain_loaded(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed_run(
        app_session,
        tmp_path,
        "run_rewind_scout_without_proposal",
        state="USER_SEARCH_REVIEW",
        proposal_version=0,
    )
    from autoessay.phase_rerun import resolve_rewind_state, rewind_for_rerun

    with app_session() as session:
        run = session.get(Run, "run_rewind_scout_without_proposal")
        assert run is not None
        target_state = resolve_rewind_state("scout", run)
        assert target_state == "DOMAIN_LOADED"
        event = rewind_for_rerun(run, "scout", target_state, session, source="unit_test")
        payload = json.loads(event.payload)
        session.commit()

    assert payload["from_state"] == "USER_SEARCH_REVIEW"
    assert payload["to_state"] == "DOMAIN_LOADED"
    assert payload["reason"] == "rerun_rewind"


async def test_rerun_phase_with_no_output_returns_409(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed_run(app_session, tmp_path, "run_no_output")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs/run_no_output/phases/scout/rerun")
    assert resp.status_code == 409
    assert "not produced" in resp.json()["detail"].lower()


async def test_rerun_unknown_phase_returns_400(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    _seed_run(app_session, tmp_path, "run_unknown_phase")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_unknown_phase/phases/proposal/rerun",
        )
    # ``proposal`` is not in the rerun PHASES set.
    assert resp.status_code == 400


async def test_rerun_blocked_when_run_currently_running(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed_run(app_session, tmp_path, "run_busy", state="DRAFTER_RUNNING")
    _touch(run_dir / "sources" / "shortlist.json", "[]")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs/run_busy/phases/curator/rerun")
    assert resp.status_code == 409
    assert "currently running" in resp.json()["detail"].lower()


async def test_rerun_blocked_on_cancelled_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed_run(app_session, tmp_path, "run_cancelled")
    _touch(run_dir / "sources" / "shortlist.json", "[]")
    with app_session() as session:
        run = session.get(Run, "run_cancelled")
        run.cancel_requested_at = utcnow()
        session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs/run_cancelled/phases/curator/rerun")
    assert resp.status_code == 409
    assert "cancelled" in resp.json()["detail"].lower()


async def test_rerun_blocked_on_failed_policy(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed_run(app_session, tmp_path, "run_failed_policy", state="FAILED_POLICY")
    _touch(run_dir / "discovery" / "scout_report.md", "# scout done")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs/run_failed_policy/phases/scout/rerun")
    assert resp.status_code == 409
    detail = resp.json()["detail"].lower()
    assert "failed_policy" in detail
    assert "force-approve" in detail


async def test_rerun_blocked_when_project_deleted(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    run_dir = _seed_run(app_session, tmp_path, "run_deleted")
    _touch(run_dir / "sources" / "shortlist.json", "[]")
    with app_session() as session:
        project = session.get(Project, "proj_test")
        project.deleted_at = utcnow()
        session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs/run_deleted/phases/curator/rerun")
    assert resp.status_code == 409


async def test_rerun_blocked_when_phase_is_downstream_of_stale_marker(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Codex's monotonic refresh order: while ``stale_from_phase=ideator``,
    rerunning ``stylist`` (downstream) must 409. The user has to refresh
    ideator first."""
    run_dir = _seed_run(app_session, tmp_path, "run_stale_order")
    _touch(run_dir / "sources" / "shortlist.json", "[]")
    _touch(run_dir / "synthesis" / "claims.jsonl", "{}")
    _touch(run_dir / "novelty" / "angle_cards.json", "{}")
    _touch(run_dir / "drafts" / "v001" / "manuscript.md", "x")
    # Stylist-owned sentinel — without this, has_completed_output for
    # stylist returns False and the rerun is rejected as "no output"
    # before the stale-marker guard ever fires.
    _touch(run_dir / "drafts" / "v001" / "style" / "stop_slop_score.json", "{}")
    with app_session() as session:
        run = session.get(Run, "run_stale_order")
        from autoessay.branches import ensure_main_branch, set_branch_stale

        ensure_main_branch(session, run)
        set_branch_stale(session, run, "ideator")
        session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_stale_order/phases/stylist/rerun",
        )
    assert resp.status_code == 409
    assert "stale" in resp.json()["detail"].lower()


async def test_rerun_upstream_of_stale_marker_is_allowed(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Rerunning a phase upstream of stale_from_phase is allowed (it
    just refreshes earlier in the chain)."""
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    run_dir = _seed_run(app_session, tmp_path, "run_upstream_ok")
    _touch(run_dir / "sources" / "shortlist.json", "[]")
    # Prior synthesizer + ideator outputs make ideator stale.
    _touch(run_dir / "synthesis" / "claims.jsonl", "{}")
    _touch(run_dir / "novelty" / "angle_cards.json", "{}")
    with app_session() as session:
        run = session.get(Run, "run_upstream_ok")
        from autoessay.branches import ensure_main_branch, set_branch_stale

        ensure_main_branch(session, run)
        set_branch_stale(session, run, "ideator")
        run.state = "USER_DEEP_DIVE_REVIEW"
        session.commit()
    transport = ASGITransport(app=app)
    # Rerunning curator (upstream of ideator) is allowed; we don't
    # actually invoke the agent here — assert_can_rerun returns 2xx
    # before we hit the runner. We stub by patching the runner.
    from autoessay import main as main_mod

    called: list[str] = []

    def fake_curator(run_id, session=None):  # type: ignore[no-untyped-def]
        called.append(run_id)

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "curator", fake_curator)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_upstream_ok/phases/curator/rerun",
        )
    assert resp.status_code == 202, resp.text
    assert called == ["run_upstream_ok"]


async def test_successful_rerun_upstream_clears_stale_after_cascade(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """PR-A4.3 (codex AGREE 2026-05-02): a successful rerun now
    cascades through downstream phases via lineage match. The
    cascade DELETES downstream RunHeads whose lineage no longer
    matches the new upstream vector. After the cascade,
    ``first_completed_downstream`` finds no completed downstream
    on this branch (their heads are gone), so
    ``stale_from_phase`` resets to None.

    The single-pointer ``branch.stale_from_phase`` column is now
    a redundant cache; the per-pv ``head_missing`` /
    ``lineage_dirty`` flags from PR-A4.2's phase-history
    endpoint carry the precise downstream state.

    This test was named ``..._resets_stale_to_first_downstream``
    pre-A4.3 and asserted ``synthesizer``; renamed and re-asserted
    to match the new cascade semantics.
    """
    run_dir = _seed_run(app_session, tmp_path, "run_reset_stale")
    _touch(run_dir / "sources" / "shortlist.json", "[]")
    _touch(run_dir / "synthesis" / "claims.jsonl", "{}")
    _touch(run_dir / "novelty" / "angle_cards.json", "{}")
    with app_session() as session:
        run = session.get(Run, "run_reset_stale")
        from autoessay.branches import ensure_main_branch, set_branch_stale

        ensure_main_branch(session, run)
        set_branch_stale(session, run, "ideator")
        session.commit()
    from autoessay import main as main_mod

    def fake_curator(run_id, session=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "curator", fake_curator)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_reset_stale/phases/curator/rerun",
        )
    assert resp.status_code == 202, resp.text
    # Cascade deleted downstream heads → no completed downstream
    # → stale_from_phase resets to None.
    assert resp.json()["stale_from_phase"] is None


async def test_rerun_of_stale_phase_clears_marker_after_cascade(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """PR-A4.3 (codex AGREE 2026-05-02): rerun cascade now deletes
    downstream RunHeads whose lineage no longer matches.
    ``first_completed_downstream`` then sees no completed
    downstream → ``stale_from_phase`` clears to None.

    Pre-A4.3 this test asserted the marker advanced to
    ``drafter``; the new cascade replaces the single-pointer
    semantics with per-pv flags.
    """
    run_dir = _seed_run(app_session, tmp_path, "run_advance")
    _touch(run_dir / "synthesis" / "claims.jsonl", "{}")
    _touch(run_dir / "novelty" / "angle_cards.json", "{}")
    _touch(run_dir / "drafts" / "v001" / "manuscript.md", "x")
    with app_session() as session:
        run = session.get(Run, "run_advance")
        from autoessay.branches import ensure_main_branch, set_branch_stale

        ensure_main_branch(session, run)
        set_branch_stale(session, run, "ideator")
        session.commit()
    from autoessay import main as main_mod

    def fake_ideator(run_id, session=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "ideator", fake_ideator)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_advance/phases/ideator/rerun",
        )
    assert resp.status_code == 202, resp.text
    # Cascade deleted downstream heads (drafter etc.) → marker
    # clears.
    assert resp.json()["stale_from_phase"] is None


async def test_rerun_clears_stale_marker_when_no_downstream_remaining(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """If the rerun phase has no completed downstream (e.g. exports is
    last; only synthesizer + ideator exist as completed downstream of
    curator), and we rerun the last completed phase, marker clears."""
    run_dir = _seed_run(app_session, tmp_path, "run_clear")
    _touch(run_dir / "novelty" / "angle_cards.json", "{}")
    with app_session() as session:
        run = session.get(Run, "run_clear")
        from autoessay.branches import ensure_main_branch, set_branch_stale

        ensure_main_branch(session, run)
        set_branch_stale(session, run, "ideator")
        session.commit()
    from autoessay import main as main_mod

    def fake_ideator(run_id, session=None):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "ideator", fake_ideator)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_clear/phases/ideator/rerun",
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["stale_from_phase"] is None


async def test_failed_rerun_does_not_update_stale_marker(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """If the agent raises, the stale marker must NOT be updated.
    Otherwise the UI would falsely advertise that downstream phases
    are now consistent."""
    run_dir = _seed_run(app_session, tmp_path, "run_fail")
    _touch(run_dir / "synthesis" / "claims.jsonl", "{}")
    _touch(run_dir / "novelty" / "angle_cards.json", "{}")
    with app_session() as session:
        run = session.get(Run, "run_fail")
        from autoessay.branches import ensure_main_branch, set_branch_stale

        ensure_main_branch(session, run)
        set_branch_stale(session, run, "ideator")
        session.commit()
    from autoessay import main as main_mod

    def boom_ideator(run_id, session=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("LLM hiccup")

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "ideator", boom_ideator)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError):
            await client.post("/api/runs/run_fail/phases/ideator/rerun")
    with app_session() as session:
        run = session.get(Run, "run_fail")
        from autoessay.branches import get_branch_stale

        assert get_branch_stale(session, run) == "ideator"  # unchanged


async def test_rerun_rewinds_state_for_real_agent_input(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Each phase agent rejects calls when run.state isn't its expected
    input state. After a successful first run the run has progressed
    past that state, so rerun must rewind it before invoking the agent.
    Without the rewind, a real synthesizer rerun would raise
    InvalidTransition when state is USER_FIELD_REVIEW (post-success)."""
    run_dir = _seed_run(app_session, tmp_path, "run_rewind", state="USER_FIELD_REVIEW")
    _touch(run_dir / "synthesis" / "claims.jsonl", "{}")
    observed_states: list[str] = []
    from autoessay import main as main_mod

    def fake_synthesizer(run_id, session=None):  # type: ignore[no-untyped-def]
        # Capture what state the agent saw — must be the rewound input.
        run = session.get(Run, run_id)
        observed_states.append(run.state)

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "synthesizer", fake_synthesizer)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs/run_rewind/phases/synthesizer/rerun")
    assert resp.status_code == 202, resp.text
    assert observed_states == ["USER_DEEP_DIVE_REVIEW"]
    with app_session() as session:
        events = (
            session.query(RunEvent)
            .filter(
                RunEvent.run_id == "run_rewind",
                RunEvent.event_type == "state_transition",
            )
            .order_by(RunEvent.created_at.asc(), RunEvent.id.asc())
            .all()
        )
        payloads = [json.loads(event.payload) for event in events]
    rewind_payload = next(
        payload for payload in payloads if payload.get("reason") == "rerun_rewind"
    )
    assert rewind_payload["from_state"] == "USER_FIELD_REVIEW"
    assert rewind_payload["to_state"] == "USER_DEEP_DIVE_REVIEW"
    assert rewind_payload["phase"] == "synthesizer"
    assert rewind_payload["source"] == "rerun_phase"


async def test_scout_completion_uses_discovery_sentinel(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Scout writes ``discovery/scout_report.md``, not ``sources/...``.
    The has_completed_output predicate must match the real path."""
    run_dir = _seed_run(app_session, tmp_path, "run_scout", state="USER_SEARCH_REVIEW")
    _touch(run_dir / "discovery" / "scout_report.md", "report")
    from autoessay.models import Run as RunModel
    from autoessay.phase_rerun import has_completed_output

    with app_session() as session:
        run = session.get(RunModel, "run_scout")
        assert has_completed_output(run, "scout") is True


async def test_proposal_completion_uses_versioned_proposal_sentinel(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """Legacy ``proposal/checkpoint.json`` is a run-writer marker, not
    proposal output. Rerun/stale checks must only count proposal_v*.json."""
    run_dir = _seed_run(app_session, tmp_path, "run_proposal_sentinel")
    _touch(run_dir / "proposal" / "checkpoint.json", "{}")
    from autoessay.models import Run as RunModel
    from autoessay.phase_rerun import has_completed_output

    with app_session() as session:
        run = session.get(RunModel, "run_proposal_sentinel")
        assert has_completed_output(run, "proposal") is False
        _touch(run_dir / "proposal" / "proposal_v001.json", "{}")
        assert has_completed_output(run, "proposal") is True


async def test_failed_rerun_restores_pre_rerun_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """If the runner crashes after rerun_phase rewinds run.state, the
    state must be restored. Otherwise the run is left at the input
    state (or a *_RUNNING state) while files are rolled back — state
    and files would disagree."""
    run_dir = _seed_run(app_session, tmp_path, "run_state_restore", state="USER_FIELD_REVIEW")
    _touch(run_dir / "synthesis" / "claims.jsonl", "{}")
    from autoessay import main as main_mod

    def boom_synth(run_id, session=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated agent crash")

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "synthesizer", boom_synth)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError):
            await client.post("/api/runs/run_state_restore/phases/synthesizer/rerun")
    with app_session() as session:
        run = session.get(Run, "run_state_restore")
        assert run.state == "USER_FIELD_REVIEW"


async def test_drafter_rerun_advances_to_user_revision_review(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Drafter normally chains into stylist and ends at DRAFTER_RUNNING.
    A rerun of drafter alone must force the run to USER_REVISION_REVIEW
    so the next stale-banner click is not 409'd as 'currently running'."""
    run_dir = _seed_run(app_session, tmp_path, "run_drafter_post", state="USER_REVISION_REVIEW")
    _touch(run_dir / "drafts" / "v001" / "manuscript.md", "manuscript")
    from autoessay import main as main_mod

    def fake_drafter(run_id, session=None):  # type: ignore[no-untyped-def]
        run = session.get(Run, run_id)
        # Simulate drafter's natural end-state: chains into stylist.
        run.state = "DRAFTER_RUNNING"
        session.flush()

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "drafter", fake_drafter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs/run_drafter_post/phases/drafter/rerun")
    assert resp.status_code == 202, resp.text
    with app_session() as session:
        run = session.get(Run, "run_drafter_post")
        assert run.state == "USER_REVISION_REVIEW"


async def test_stylist_completion_distinguishes_from_drafter(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """has_completed_output must return False for stylist when only
    drafter has written. Otherwise stale_from_phase could advance to
    a phase that has never produced its own output."""
    run_dir = _seed_run(app_session, tmp_path, "run_stylist_check")
    _touch(run_dir / "drafts" / "v001" / "manuscript.md", "x")
    from autoessay.models import Run as RunModel
    from autoessay.phase_rerun import has_completed_output

    with app_session() as session:
        run = session.get(RunModel, "run_stylist_check")
        assert has_completed_output(run, "drafter") is True
        assert has_completed_output(run, "stylist") is False
        # Once stylist also writes, it counts as completed.
        _touch(run_dir / "drafts" / "v001" / "style" / "score.json", "{}")
        assert has_completed_output(run, "stylist") is True


async def test_run_cancelled_during_rerun_keeps_cancelled_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """If the runner raises RunCancelled, the run.state must stay
    CANCELLED. The state-restore branch is for unexpected exceptions
    only — a deliberate cancellation must not be undone."""
    run_dir = _seed_run(app_session, tmp_path, "run_cancel_mid", state="USER_FIELD_REVIEW")
    _touch(run_dir / "synthesis" / "claims.jsonl", "{}")
    from autoessay import main as main_mod
    from autoessay.state_machine import RunCancelled

    def cancelled_synth(run_id, session=None):  # type: ignore[no-untyped-def]
        run = session.get(Run, run_id)
        run.state = "CANCELLED"
        run.cancel_requested_at = utcnow()
        session.flush()
        raise RunCancelled(f"run {run_id} cancelled")

    monkeypatch.setitem(main_mod._PHASE_RUNNERS, "synthesizer", cancelled_synth)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RunCancelled):
            await client.post("/api/runs/run_cancel_mid/phases/synthesizer/rerun")
    with app_session() as session:
        run = session.get(Run, "run_cancel_mid")
        assert run.state == "CANCELLED"
        assert run.cancel_requested_at is not None


async def test_integrity_completion_excludes_drafter_dedup(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """The drafter harness writes integrity/local_dedup.json before
    the integrity phase ever runs. has_completed_output must NOT
    treat that as integrity completion — only an actual integrity
    summary counts."""
    run_dir = _seed_run(app_session, tmp_path, "run_integrity_check")
    _touch(run_dir / "integrity" / "local_dedup.json", "{}")
    from autoessay.models import Run as RunModel
    from autoessay.phase_rerun import has_completed_output

    with app_session() as session:
        run = session.get(RunModel, "run_integrity_check")
        assert has_completed_output(run, "integrity") is False
        # Once integrity actually writes its summary, it counts.
        _touch(run_dir / "integrity" / "integrity_summary.json", "{}")
        assert has_completed_output(run, "integrity") is True
