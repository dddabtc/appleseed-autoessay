from uuid import uuid4

import pytest
from conftest import seed_project
from sqlalchemy.orm import Session

from autoessay.models import Run
from autoessay.state_machine import ALLOWED_TRANSITIONS, RUN_STATES, InvalidTransition, transition


def test_proposal_stage_transitions_are_declared() -> None:
    assert "PROPOSAL_DRAFTING" in RUN_STATES
    assert "USER_PROPOSAL_REVIEW" in RUN_STATES
    assert "PROPOSAL_DRAFTING" in ALLOWED_TRANSITIONS["DOMAIN_LOADED"]
    assert "SCOUT_RUNNING" in ALLOWED_TRANSITIONS["DOMAIN_LOADED"]
    assert "USER_PROPOSAL_REVIEW" in ALLOWED_TRANSITIONS["PROPOSAL_DRAFTING"]
    assert "PROPOSAL_DRAFTING" in ALLOWED_TRANSITIONS["USER_PROPOSAL_REVIEW"]
    assert "SCOUT_RUNNING" in ALLOWED_TRANSITIONS["USER_PROPOSAL_REVIEW"]


def test_every_allowed_transition_succeeds(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        seed_project(session)
        for from_state, allowed_states in ALLOWED_TRANSITIONS.items():
            for to_state in allowed_states:
                run = _seed_run(session, tmp_path, from_state)

                event = transition(run, to_state, session, reason="test")

                assert run.state == to_state
                assert event.event_type == "state_transition"
                assert f'"to_state": "{to_state}"' in event.payload
                if to_state.endswith("_RUNNING"):
                    phase = to_state.removesuffix("_RUNNING").lower()
                    assert (tmp_path / run.id / phase / "checkpoint.json").is_file()


def test_every_disallowed_transition_raises(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        seed_project(session)
        for from_state in RUN_STATES:
            allowed = set(ALLOWED_TRANSITIONS[from_state])
            for to_state in RUN_STATES:
                if to_state in allowed:
                    continue
                run = _seed_run(session, tmp_path, from_state)

                with pytest.raises(InvalidTransition):
                    transition(run, to_state, session, reason="test")

                assert run.state == from_state


def _seed_run(session: Session, tmp_path, state: str) -> Run:  # type: ignore[no-untyped-def]
    run_id = f"run_{uuid4().hex}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    run = Run(
        id=run_id,
        project_id="proj_test",
        domain_version="0.1.0",
        run_dir=str(run_dir),
        state=state,
        baseline_hash="test",
    )
    session.add(run)
    session.flush()
    return run
