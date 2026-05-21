"""PR-I5 — POST /api/runs/{run_id}/phases/{phase}/retry resolver.

Backend authority over the retry decision tree (codex 2-round
consensus). Replaces PR-I4.a's frontend smartRetry static heuristic.

Decision tree under test:

  1. unknown phase                               → 404 unknown_phase
  2. state != FAILED_FIXABLE                     → 422 not_failed_fixable
  3. latest phase_failed.phase != requested phase → 422 phase_mismatch
  4. failure_class in PARTIAL                    → start (rewind)
  5. has_completed_output                        → rerun (overwrite)
  6. failure_class in GRACEFUL (no output)       → 422 guidance_required
  7. fallback (unknown class, no output)         → start

Plus partial-failure-class branch must take priority over has_output
(codex round-2 Q2: worker can die after sentinel was written;
sentinel presence does not imply phase completed cleanly).
"""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.main import _GRACEFUL_FAILURE_CLASSES, _PARTIAL_FAILURE_CLASSES, app
from autoessay.models import Domain, Project, Run, User
from autoessay.state_machine import append_event, transition


def _ensure_project(session) -> None:
    user = session.scalar(select(User).where(User.id == "single-user"))
    if user is None:
        session.add(User(id="single-user", display_name="Single User"))
        session.flush()
    domain = session.scalar(select(Domain).where(Domain.id == "general_academic"))
    if domain is None:
        session.add(
            Domain(
                id="general_academic",
                display_name="General Academic",
                version="0.0",
            ),
        )
        session.flush()
    project = session.scalar(select(Project).where(Project.id == "proj_pri5"))
    if project is None:
        session.add(
            Project(
                id="proj_pri5",
                user_id="single-user",
                title="PR-I5",
                domain_id="general_academic",
                domain_version="0.0",
                language="en",
                status="ACTIVE",
            ),
        )
        session.flush()


def _seed_failed_synthesizer_run(
    session,
    run_id: str,
    tmp_path: Path,
    *,
    write_claims: bool,
    failure_class: str | None,
    failed_phase: str | None = None,
) -> Run:
    """Seed a SYNTHESIZER FAILED_FIXABLE run.
    `failed_phase` overrides the phase recorded in the latest
    phase_failed event payload — used for the phase_mismatch tests.
    `failure_class=None` skips the event entirely.
    """
    _ensure_project(session)
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # Synthesizer's assert_phase_ready needs a non-empty shortlist.json
    # to pass the readiness gate. Seed a minimal one so the start
    # path can dispatch through to the runner.
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / "shortlist.json").write_text(
        '[{"source_id":"crossref:10.1/x","title":"Stub","authors":[],"year":2020,'
        '"venue":"Test","url":null,"abstract":"","access_status":"open",'
        '"risk_flags":[],"score_breakdown":{},"weight":1.0,"rank_score":1.0,'
        '"research_role":"empirical","relevance_reason":"stub","retain_reason":"stub"}]',
        encoding="utf-8",
    )
    if write_claims:
        synthesis_dir = run_dir / "synthesis"
        synthesis_dir.mkdir(parents=True, exist_ok=True)
        (synthesis_dir / "claims.jsonl").write_text(
            '{"claim_id":"c1","text":"x"}\n', encoding="utf-8"
        )
    run = Run(
        id=run_id,
        project_id="proj_pri5",
        run_dir=str(run_dir),
        state="TOPIC_ENTERED",
        baseline_hash="0" * 64,
        domain_version="0.0",
    )
    session.add(run)
    session.flush()
    for state in (
        "DOMAIN_LOADED",
        "PROPOSAL_DRAFTING",
        "USER_PROPOSAL_REVIEW",
        "SCOUT_RUNNING",
        "USER_SEARCH_REVIEW",
        "CURATOR_RUNNING",
        "USER_DEEP_DIVE_REVIEW",
        "SYNTHESIZER_RUNNING",
        "FAILED_FIXABLE",
    ):
        transition(run, state, session, reason="test fixture")
    if failure_class is not None:
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": failed_phase or "synthesizer",
                "failure_class": failure_class,
            },
        )
    session.commit()
    return run


# --- Constants sanity --------------------------------------------------


def test_partial_classes_set_locked() -> None:
    expected = frozenset({"zombie_recovered", "phase_runtime_error"})
    assert expected == _PARTIAL_FAILURE_CLASSES


def test_graceful_classes_set_locked() -> None:
    expected = frozenset({"failed_fixable", "failed_vendor", "failed_policy"})
    assert expected == _GRACEFUL_FAILURE_CLASSES


# --- Validation gates --------------------------------------------------


async def test_unknown_phase_returns_404(app_session) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_does_not_exist/phases/material_diagnostic/retry",
        )
    # phase check happens before run lookup; 404 with unknown_phase code
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "unknown_phase"
    assert detail["phase"] == "material_diagnostic"


async def test_unknown_run_returns_404(app_session) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_does_not_exist/phases/synthesizer/retry",
        )
    assert resp.status_code == 404


async def test_not_failed_fixable_returns_422(app_session, tmp_path: Path) -> None:
    """Run is in DRAFTER_RUNNING (any non-FAILED_FIXABLE state) →
    422 with current_state discriminator."""
    with app_session() as session:
        run = _seed_failed_synthesizer_run(
            session,
            "run_pri5_running",
            tmp_path,
            write_claims=False,
            failure_class=None,
        )
        # Manually push past FAILED_FIXABLE for the test.
        run.state = "DRAFTER_RUNNING"
        session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_pri5_running/phases/synthesizer/retry",
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "not_failed_fixable"
    assert detail["current_state"] == "DRAFTER_RUNNING"


async def test_phase_mismatch_returns_422(app_session, tmp_path: Path) -> None:
    """Latest phase_failed is for synthesizer but user calls /retry
    for drafter → 422 phase_mismatch with both phase names."""
    with app_session() as session:
        _seed_failed_synthesizer_run(
            session,
            "run_pri5_mismatch",
            tmp_path,
            write_claims=False,
            failure_class="zombie_recovered",
            failed_phase="synthesizer",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_pri5_mismatch/phases/drafter/retry",
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "phase_mismatch"
    assert detail["requested_phase"] == "drafter"
    assert detail["actual_failed_phase"] == "synthesizer"


# --- Decision tree branches -------------------------------------------


async def test_partial_with_no_output_dispatches_start(
    app_session, tmp_path: Path, monkeypatch
) -> None:
    """failure_class=zombie_recovered + no claims.jsonl → start path
    (rewind via _recover_failed_fixable_for_phase + claim + run)."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    with app_session() as session:
        _seed_failed_synthesizer_run(
            session,
            "run_pri5_partial_no_output",
            tmp_path,
            write_claims=False,
            failure_class="zombie_recovered",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_pri5_partial_no_output/phases/synthesizer/retry",
        )
    # synthesizer stub will run sync end-to-end and finish; the
    # response from the resolver still reports the start path it took.
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["action"] == "start"
    assert body["phase"] == "synthesizer"
    assert body["expected_state"] == "SYNTHESIZER_RUNNING"


async def test_partial_with_output_dispatches_start(
    app_session, tmp_path: Path, monkeypatch
) -> None:
    """failure_class=zombie_recovered + claims.jsonl exists → still
    start path (codex round-2 Q2: partial branch takes priority over
    has_output; sentinel doesn't imply clean completion). Mirrors
    the run_032695 incident shape."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    with app_session() as session:
        _seed_failed_synthesizer_run(
            session,
            "run_pri5_partial_with_output",
            tmp_path,
            write_claims=True,
            failure_class="zombie_recovered",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_pri5_partial_with_output/phases/synthesizer/retry",
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["action"] == "start"


async def test_graceful_with_output_dispatches_rerun(
    app_session, tmp_path: Path, monkeypatch
) -> None:
    """failure_class=failed_fixable + claims.jsonl exists → rerun
    path (overwrite). Synthesizer "0 of 6" graceful failure shape."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    with app_session() as session:
        _seed_failed_synthesizer_run(
            session,
            "run_pri5_graceful_with_output",
            tmp_path,
            write_claims=True,
            failure_class="failed_fixable",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_pri5_graceful_with_output/phases/synthesizer/retry",
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["action"] == "rerun"


async def test_graceful_with_no_output_returns_422_guidance(app_session, tmp_path: Path) -> None:
    """failure_class=failed_fixable + no claims.jsonl → 422
    guidance_required. User must fix input before retry can succeed."""
    with app_session() as session:
        _seed_failed_synthesizer_run(
            session,
            "run_pri5_graceful_no_output",
            tmp_path,
            write_claims=False,
            failure_class="failed_fixable",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_pri5_graceful_no_output/phases/synthesizer/retry",
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "guidance_required"
    assert detail["phase"] == "synthesizer"
    assert detail["failure_class"] == "failed_fixable"


async def test_unknown_class_no_output_dispatches_start(
    app_session, tmp_path: Path, monkeypatch
) -> None:
    """Unknown failure_class + no output → fallback start (safest;
    rewind covers no-output via _recover_failed_fixable_for_phase
    branch when failure_class doesn't match anything)."""
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    with app_session() as session:
        _seed_failed_synthesizer_run(
            session,
            "run_pri5_unknown_no_output",
            tmp_path,
            write_claims=False,
            failure_class="brand_new_unmapped_class",
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/runs/run_pri5_unknown_no_output/phases/synthesizer/retry",
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["action"] == "start"
