"""PR-C3.b — tension_extraction phase runner integration tests.

Covers the runner path that C3.b adds on top of C3.a's stub-only
``extract_tensions``:

  * ``run_tension_extraction`` transitions USER_FIELD_REVIEW →
    TENSION_EXTRACTION_RUNNING → USER_TENSION_REVIEW with appropriate
    audit events
  * Missing synthesizer.json → FAILED_FIXABLE
  * Stub mode produces deterministic 2-tension artifact +
    ``synthesis/tension_extraction.json`` written
  * Reruns from USER_TENSION_REVIEW (rerun path)
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from autoessay.agents.tension_extraction import run_tension_extraction
from autoessay.config import get_settings
from autoessay.models import Domain, Project, Run, RunEvent, User
from autoessay.state_machine import transition


def _seed_run(session, tmp_path: Path, run_id: str = "run_tension_runner") -> Run:
    user = session.scalar(select(User).where(User.id == "user_tension_runner"))
    if user is None:
        user = User(
            id="user_tension_runner",
            oidc_subject="subject-tension-runner",
            oidc_issuer="https://auth.example.test/casdoor",
            email="tension@example.test",
            display_name="Tension",
        )
        session.add(user)
        session.flush()
    domain = session.scalar(select(Domain).where(Domain.id == "general_academic"))
    if domain is None:
        domain = Domain(id="general_academic", display_name="General", version="0.0")
        session.add(domain)
        session.flush()
    project = session.scalar(select(Project).where(Project.id == "proj_tension_runner"))
    if project is None:
        project = Project(
            id="proj_tension_runner",
            user_id=user.id,
            title="Tension runner test",
            domain_id="general_academic",
            domain_version="0.0",
            language="en",
            status="ACTIVE",
        )
        session.add(project)
        session.flush()
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "synthesis").mkdir(exist_ok=True)
    run = Run(
        id=run_id,
        project_id="proj_tension_runner",
        run_dir=str(run_dir),
        state="TOPIC_ENTERED",
        baseline_hash="0" * 64,
        domain_version="0.0",
        paper_mode="case_analysis",
        research_kernel_json={"tentative_question": "Test question"},
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
        "USER_FIELD_REVIEW",
    ):
        transition(run, state, session, reason="test setup")
    session.commit()
    return run


def _write_synthesizer(run_dir: Path) -> None:
    payload = {
        "schema_version": 1,
        "primary_track": [
            {"source_id": "src_b", "claim_id": "c_b", "text": "primary claim"},
        ],
        "secondary_track": [
            {"source_id": "src_a", "claim_id": "c_a", "text": "secondary claim"},
        ],
        "theoretical_lens_track": [],
        "methodological_track": [],
    }
    (run_dir / "synthesis" / "synthesizer.json").write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )


def test_run_tension_extraction_stub_path_lands_user_tension_review(
    app_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOESSAY_TENSION_EXTRACTION_STUB", "1")
    get_settings.cache_clear()
    with app_session() as session:
        run = _seed_run(session, tmp_path)
        run_id = run.id
        _write_synthesizer(Path(run.run_dir))

    with app_session() as session:
        run_tension_extraction(run_id, session)

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "USER_TENSION_REVIEW"
        artifact = Path(run.run_dir) / "synthesis" / "tension_extraction.json"
        assert artifact.exists()
        decoded = json.loads(artifact.read_text(encoding="utf-8"))
        assert decoded["schema_version"] == 1
        assert len(decoded["tensions"]) == 2

        # Audit events: phase_started + phase_done.
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at.asc()),
        ).all()
        event_types = [(e.event_type, json.loads(e.payload or "{}").get("phase")) for e in events]
        assert ("phase_started", "tension_extraction") in event_types
        assert ("phase_done", "tension_extraction") in event_types
    monkeypatch.delenv("AUTOESSAY_TENSION_EXTRACTION_STUB", raising=False)
    get_settings.cache_clear()


def test_run_tension_extraction_missing_synthesizer_fails_fixable(
    app_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AUTOESSAY_TENSION_EXTRACTION_STUB", "1")
    get_settings.cache_clear()
    with app_session() as session:
        run = _seed_run(session, tmp_path, run_id="run_no_synth")
        run_id = run.id
        # Deliberately do NOT write synthesizer.json.

    with app_session() as session:
        run_tension_extraction(run_id, session)

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at.desc()),
        ).all()
        latest = events[0]
        payload = json.loads(latest.payload)
        assert latest.event_type == "phase_failed"
        assert payload["phase"] == "tension_extraction"
        assert payload["failure_class"] == "failed_fixable"
    monkeypatch.delenv("AUTOESSAY_TENSION_EXTRACTION_STUB", raising=False)
    get_settings.cache_clear()


def test_run_tension_extraction_rerun_from_user_tension_review(
    app_session, tmp_path: Path, monkeypatch
) -> None:
    """Codex round-2 #3 — USER_TENSION_REVIEW supports tension rerun."""
    monkeypatch.setenv("AUTOESSAY_TENSION_EXTRACTION_STUB", "1")
    get_settings.cache_clear()
    with app_session() as session:
        run = _seed_run(session, tmp_path, run_id="run_tension_rerun")
        run_id = run.id
        _write_synthesizer(Path(run.run_dir))

    # First run.
    with app_session() as session:
        run_tension_extraction(run_id, session)

    # Now state is USER_TENSION_REVIEW; rerun from there.
    with app_session() as session:
        run_tension_extraction(run_id, session)

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "USER_TENSION_REVIEW"
    monkeypatch.delenv("AUTOESSAY_TENSION_EXTRACTION_STUB", raising=False)
    get_settings.cache_clear()
