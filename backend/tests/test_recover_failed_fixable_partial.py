"""PR-I3.b — `_recover_failed_fixable_for_phase` partial-output cases.

When a worker dies mid-phase (SIGKILL inside an LLM call, container
restart, RQ work-horse killed), the phase may have written a sentinel
completion glob (e.g. synthesizer's ``claims.jsonl``) but NOT
finished. PR-I1 / I2 / I3 zombie recovery moves the run to
``FAILED_FIXABLE``. The user clicks "重试该步骤" → frontend calls
``start_<phase>`` → `_recover_failed_fixable_for_phase` is supposed
to rewind state so the guard accepts.

Before PR-I3.b, the helper early-returned on
``has_completed_output=True`` and the user got a 409 they couldn't
escape from. PR-I3.b distinguishes "clean completion that hit a
policy block" (skip rewind) from "worker died mid-phase" (rewind +
re-run end to end) via the latest ``phase_failed`` event's
``failure_class``.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from autoessay.main import (
    _PARTIAL_FAILURE_CLASSES,
    _latest_phase_failed_payload,
    _recover_failed_fixable_for_phase,
)
from autoessay.models import Domain, Project, Run, RunEvent, User
from autoessay.state_machine import append_event, transition


def _ensure_user_project(session) -> Project:
    user = session.scalar(select(User).where(User.id == "user_pri3b"))
    if user is None:
        session.add(
            User(
                id="user_pri3b",
                oidc_subject="subject-pri3b",
                oidc_issuer="https://auth.example.test/casdoor",
                email="pri3b@example.test",
                display_name="PR-I3.b",
            ),
        )
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
    project = session.scalar(select(Project).where(Project.id == "proj_pri3b"))
    if project is None:
        project = Project(
            id="proj_pri3b",
            user_id="user_pri3b",
            title="PR-I3.b",
            domain_id="general_academic",
            domain_version="0.0",
            language="en",
            status="ACTIVE",
        )
        session.add(project)
        session.flush()
    return project


def _seed_synthesizer_failed_fixable_run(
    session,
    run_id: str,
    tmp_path: Path,
    *,
    write_claims: bool,
    failure_class: str | None,
) -> Run:
    """Insert a synthesizer-FAILED_FIXABLE run with optional partial
    artifact + a phase_failed event of the given failure_class.

    `write_claims=True` simulates the SIGKILL-inside-material_diagnostic
    case: the rerun-stale completion glob (``synthesis/claims.jsonl``)
    was satisfied before the worker died, so ``has_completed_output``
    will return True and the helper has to consult the failure class.

    `failure_class=None` skips event emission entirely; lets us cover
    the "no phase_failed history" branch.
    """
    _ensure_user_project(session)
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if write_claims:
        synthesis_dir = run_dir / "synthesis"
        synthesis_dir.mkdir(parents=True, exist_ok=True)
        (synthesis_dir / "claims.jsonl").write_text(
            '{"claim_id":"c1","text":"partial output before SIGKILL"}\n',
            encoding="utf-8",
        )
    run = Run(
        id=run_id,
        project_id="proj_pri3b",
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
            {"phase": "synthesizer", "failure_class": failure_class},
        )
    session.commit()
    return run


def test_partial_failure_classes_set_locked() -> None:
    """Regression guard against accidental class additions /
    removals. zombie_recovered comes from PR-I1 + PR-I2.a reaper;
    phase_runtime_error comes from PR-I2.b common failure boundary.
    Adding a graceful failure class here would cause user retries
    to silently overwrite genuine completed artifacts."""
    expected = frozenset({"zombie_recovered", "phase_runtime_error"})
    assert expected == _PARTIAL_FAILURE_CLASSES


def test_zombie_recovered_with_partial_claims_rewinds_state(
    app_session,
    tmp_path: Path,
) -> None:
    """SIGKILL victim: claims.jsonl written, then worker died inside
    material_diagnostic. Latest phase_failed event is
    failure_class=zombie_recovered. Helper MUST rewind state to
    USER_DEEP_DIVE_REVIEW so start_synthesizer guard accepts."""
    with app_session() as session:
        run = _seed_synthesizer_failed_fixable_run(
            session,
            "run_pri3b_zombie",
            tmp_path,
            write_claims=True,
            failure_class="zombie_recovered",
        )
        run_id = run.id

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"
        _recover_failed_fixable_for_phase(session, run, "synthesizer")
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == run_id))
        assert run is not None
        assert run.state == "USER_DEEP_DIVE_REVIEW"


def test_phase_runtime_error_with_partial_output_rewinds_state(
    app_session,
    tmp_path: Path,
) -> None:
    """PR-I2.b common failure boundary catches an unhandled exception
    inside the agent and emits failure_class=phase_runtime_error.
    Same rewind semantics as zombie_recovered."""
    with app_session() as session:
        _seed_synthesizer_failed_fixable_run(
            session,
            "run_pri3b_runtime",
            tmp_path,
            write_claims=True,
            failure_class="phase_runtime_error",
        )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_runtime"))
        assert run is not None
        _recover_failed_fixable_for_phase(session, run, "synthesizer")
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_runtime"))
        assert run is not None
        assert run.state == "USER_DEEP_DIVE_REVIEW"


def test_graceful_policy_failure_with_completed_output_no_op(
    app_session,
    tmp_path: Path,
) -> None:
    """Synthesizer cleanly produced output but agent then emitted
    failed_fixable (e.g. 'only 0 of 6 sources processed'). User
    should call rerun_phase, not start_<phase>. Helper must NOT
    rewind state."""
    with app_session() as session:
        _seed_synthesizer_failed_fixable_run(
            session,
            "run_pri3b_graceful",
            tmp_path,
            write_claims=True,
            failure_class="failed_fixable",
        )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_graceful"))
        assert run is not None
        _recover_failed_fixable_for_phase(session, run, "synthesizer")
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_graceful"))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"


def test_first_attempt_failure_no_partial_output_still_rewinds(
    app_session,
    tmp_path: Path,
) -> None:
    """Existing PR-I1 first-attempt path: no claims.jsonl yet, any
    failure_class. Helper rewinds (the original behavior — must
    not regress)."""
    with app_session() as session:
        _seed_synthesizer_failed_fixable_run(
            session,
            "run_pri3b_first_attempt",
            tmp_path,
            write_claims=False,
            failure_class="phase_runtime_error",
        )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_first_attempt"))
        assert run is not None
        _recover_failed_fixable_for_phase(session, run, "synthesizer")
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_first_attempt"))
        assert run is not None
        assert run.state == "USER_DEEP_DIVE_REVIEW"


def test_zombie_event_for_different_phase_no_op(
    app_session,
    tmp_path: Path,
) -> None:
    """Run is FAILED_FIXABLE because of a synthesizer zombie, but the
    user clicked retry on a different phase (drafter). The helper
    must NOT rewind drafter state — the latest phase_failed event's
    payload.phase is synthesizer, not drafter.

    Codex amendment#2: this is exactly why we look at the latest
    phase_failed globally and check payload.phase, not "latest
    matching phase".
    """
    with app_session() as session:
        run = _seed_synthesizer_failed_fixable_run(
            session,
            "run_pri3b_other_phase",
            tmp_path,
            write_claims=True,
            failure_class="zombie_recovered",
        )
        # The run is at FAILED_FIXABLE with a synthesizer zombie
        # event; user (or buggy frontend) calls start_drafter.
        # We need drafter to ALSO look "completed" (so the
        # has_completed_output branch is exercised); the
        # PHASE_COMPLETION_GLOBS["drafter"] glob is
        # "drafts/*/manuscript.md", so write to a versioned subdir.
        drafter_dir = Path(run.run_dir) / "drafts" / "v001"
        drafter_dir.mkdir(parents=True, exist_ok=True)
        (drafter_dir / "manuscript.md").write_text("fake", encoding="utf-8")

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_other_phase"))
        assert run is not None
        _recover_failed_fixable_for_phase(session, run, "drafter")
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_other_phase"))
        assert run is not None
        # Drafter was NOT rewound — the zombie event was for
        # synthesizer, drafter has its own (faked) completed output.
        assert run.state == "FAILED_FIXABLE"


def test_no_phase_failed_history_with_partial_output_no_op(
    app_session,
    tmp_path: Path,
) -> None:
    """Edge case: state is FAILED_FIXABLE + has_completed_output but
    no phase_failed event exists yet (could happen if events were
    pruned, or in a hand-crafted DB state). Helper must NOT rewind —
    we have no signal that this was a partial failure."""
    with app_session() as session:
        _seed_synthesizer_failed_fixable_run(
            session,
            "run_pri3b_no_event",
            tmp_path,
            write_claims=True,
            failure_class=None,  # don't emit any phase_failed event
        )

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_no_event"))
        assert run is not None
        _recover_failed_fixable_for_phase(session, run, "synthesizer")
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_no_event"))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"


def test_latest_phase_failed_payload_uses_most_recent(
    app_session,
    tmp_path: Path,
) -> None:
    """Helper returns the MOST recent phase_failed event globally,
    not filtered by phase. If a run had a synthesizer zombie that
    was fixed (newer phase_done event) and then a graceful drafter
    failure, the helper must return the drafter event."""
    with app_session() as session:
        run = _seed_synthesizer_failed_fixable_run(
            session,
            "run_pri3b_history",
            tmp_path,
            write_claims=True,
            failure_class="zombie_recovered",
        )
        # Append a newer phase_failed for a different phase. Helper
        # must return this newer event, not the older synthesizer
        # zombie one.
        append_event(
            session,
            run,
            "phase_failed",
            {"phase": "drafter", "failure_class": "failed_fixable"},
        )
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_history"))
        assert run is not None
        payload = _latest_phase_failed_payload(session, run)
        assert payload is not None
        assert payload.get("phase") == "drafter"
        assert payload.get("failure_class") == "failed_fixable"


def test_latest_phase_failed_payload_invalid_json_returns_none(
    app_session,
    tmp_path: Path,
) -> None:
    """If the most recent phase_failed event somehow has invalid
    JSON in payload, helper returns None rather than raising —
    the rewind path then takes the safe no-op branch."""
    with app_session() as session:
        run = _seed_synthesizer_failed_fixable_run(
            session,
            "run_pri3b_bad_json",
            tmp_path,
            write_claims=True,
            failure_class="zombie_recovered",
        )
        # Manually corrupt the payload of the latest event.
        latest = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.event_type == "phase_failed")
            .order_by(RunEvent.created_at.desc()),
        )
        assert latest is not None
        latest.payload = "{not-valid-json"
        session.commit()

    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_bad_json"))
        assert run is not None
        payload = _latest_phase_failed_payload(session, run)
        assert payload is None
        # And the rewind helper takes the safe no-op branch.
        _recover_failed_fixable_for_phase(session, run, "synthesizer")
        session.commit()
    with app_session() as session:
        run = session.scalar(select(Run).where(Run.id == "run_pri3b_bad_json"))
        assert run is not None
        assert run.state == "FAILED_FIXABLE"
