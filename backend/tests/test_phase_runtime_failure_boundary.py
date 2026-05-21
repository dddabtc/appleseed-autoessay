"""PR-I2.b — common failure boundary tests for ``run_with_versioning``.

Codex Q3 amendment: when an agent runner raises a non-RunCancelled
exception, ``run_with_versioning`` must (1) mark the pv failed,
(2) transition the run to FAILED_FIXABLE, (3) append a ``phase_failed``
event with ``failure_class=phase_runtime_error`` so the
FailureResolutionBanner picks the run up — same UX as J5/PR-I1 zombie
recovery. The wrapper then re-raises so RQ records the worker failure
for retry/observability.

Without this layer, ordinary exceptions silently leave the run in
``*_RUNNING`` with no banner — indistinguishable from a SIGKILL zombie
to the user.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from autoessay.models import Domain, Project, Run, RunEvent, User
from autoessay.phase_version import run_with_versioning
from autoessay.state_machine import RunCancelled, transition


def _seed_run(session, tmp_path: Path, run_id: str = "run_runtime_fail") -> Run:
    user = session.scalar(select(User).where(User.id == "user_failure_boundary"))
    if user is None:
        user = User(
            id="user_failure_boundary",
            oidc_subject="subject-failure",
            oidc_issuer="https://auth.example.test/casdoor",
            email="failure@example.test",
            display_name="Failure",
        )
        session.add(user)
        session.flush()
    domain = session.scalar(select(Domain).where(Domain.id == "general_academic"))
    if domain is None:
        domain = Domain(id="general_academic", display_name="General", version="0.0")
        session.add(domain)
        session.flush()
    project = session.scalar(select(Project).where(Project.id == "proj_failure"))
    if project is None:
        project = Project(
            id="proj_failure",
            user_id="user_failure_boundary",
            title="Failure boundary",
            domain_id="general_academic",
            domain_version="0.0",
            language="en",
            status="ACTIVE",
        )
        session.add(project)
        session.flush()
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run = Run(
        id=run_id,
        project_id="proj_failure",
        run_dir=str(run_dir),
        state="TOPIC_ENTERED",
        baseline_hash="0" * 64,
        domain_version="0.0",
    )
    session.add(run)
    session.flush()
    # March to SCOUT_RUNNING so the wrapper has a *_RUNNING state to
    # transition out of.
    for state in (
        "DOMAIN_LOADED",
        "PROPOSAL_DRAFTING",
        "USER_PROPOSAL_REVIEW",
        "SCOUT_RUNNING",
    ):
        transition(run, state, session, reason="test setup")
    session.commit()
    return run


def test_runtime_exception_transitions_to_failed_fixable(app_session, tmp_path: Path) -> None:
    """The dominant case codex Q3 flagged: a non-RunCancelled
    exception in an agent runner must NOT leave the run in
    SCOUT_RUNNING — it should land on FAILED_FIXABLE so the banner
    surfaces a retry button."""
    with app_session() as session:
        run = _seed_run(session, tmp_path)
        run_id = run.id

    def runner_call() -> None:
        raise RuntimeError("simulated agent crash")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        with pytest.raises(RuntimeError, match="simulated agent crash"):
            run_with_versioning(session, run, "scout", runner_call)
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at.desc()),
        ).all()
        # The latest event must be phase_failed with the boundary marker
        # so the FailureResolutionBanner picks up the phase correctly.
        latest = events[0]
        assert latest.event_type == "phase_failed"
        import json

        payload = json.loads(latest.payload)
        assert payload["phase"] == "scout"
        assert payload["failure_class"] == "phase_runtime_error"
        assert payload["error_class"] == "RuntimeError"
        assert payload["boundary"] == "phase_version_wrapper"


def test_run_cancelled_skips_failure_boundary(app_session, tmp_path: Path) -> None:
    """RunCancelled must NOT trigger the failure boundary — it's the
    user-initiated cancel path, which has its own state-transition
    handling and shouldn't write a phase_failed audit."""
    with app_session() as session:
        run = _seed_run(session, tmp_path, run_id="run_cancelled")
        run_id = run.id

    def runner_call() -> None:
        raise RunCancelled("user cancelled")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        with pytest.raises(RunCancelled):
            run_with_versioning(session, run, "scout", runner_call)
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        # RunCancelled does NOT route through the failure boundary,
        # so the run state stays in SCOUT_RUNNING (the run.json
        # cancel path resets via a separate API).
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id),
        ).all()
        # No phase_runtime_error event from this codepath.
        for ev in events:
            import json

            payload = json.loads(ev.payload or "{}")
            assert payload.get("failure_class") != "phase_runtime_error"


def test_runtime_exception_appends_audit_when_already_in_failure_state(
    app_session, tmp_path: Path
) -> None:
    """If the agent itself wrote a graceful failure transition before
    the exception bubbled (rare but possible), the boundary skips the
    transition but still appends the audit event so the trail records
    where the exception originated."""
    with app_session() as session:
        run = _seed_run(session, tmp_path, run_id="run_already_failed")
        # Force the run into FAILED_FIXABLE before invoking — simulates
        # the agent emitting a graceful transition then raising for
        # observability.
        transition(run, "FAILED_FIXABLE", session, reason="agent self-fail")
        session.commit()
        run_id = run.id

    def runner_call() -> None:
        raise RuntimeError("post-graceful crash")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        with pytest.raises(RuntimeError):
            run_with_versioning(session, run, "scout", runner_call)
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"  # unchanged
        events = session.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id),
        ).all()
        boundary_events = []
        for ev in events:
            import json

            payload = json.loads(ev.payload or "{}")
            if payload.get("boundary") == "phase_version_wrapper":
                boundary_events.append(ev)
        assert len(boundary_events) == 1
