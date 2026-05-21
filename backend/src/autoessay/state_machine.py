"""Run state transitions and event recording for the v1 pipeline."""

import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from autoessay.models import Run, RunEvent, utcnow
from autoessay.run_writer import ensure_phase_checkpoint, record_run_event_payload


class RunCancelled(Exception):
    """Raised when an agent detects ``run.cancel_requested_at`` mid-flight.

    The expected use is for the agent's entry point to call
    :func:`assert_run_active` at the top of every phase and inside any
    long per-section / per-source loop. When this exception fires the
    state has already been transitioned to ``CANCELLED`` and a
    ``run_cancelled`` event recorded — the caller should just stop
    cleanly, not attempt cleanup.
    """


def assert_run_active(run: Run, session: Session) -> None:
    """If a cancel was requested on this run, transition to CANCELLED
    and raise :class:`RunCancelled`.

    Workers must call this before each artifact write. The check uses
    ``session.refresh`` so it picks up cancel intent set by the API in
    a different transaction.
    """
    session.refresh(run, ["cancel_requested_at", "state"])
    if run.cancel_requested_at is None:
        return
    if run.state == "CANCELLED":
        raise RunCancelled(f"run {run.id} already cancelled")
    try:
        transition(
            run,
            "CANCELLED",
            session,
            reason="Run cancelled (essay deleted or user cancellation)",
            payload={"cancel_requested_at": run.cancel_requested_at.isoformat()},
        )
    except InvalidTransition:
        # Run is already in a terminal state — just bail.
        raise RunCancelled(f"run {run.id} cancel intent honored late") from None
    append_event(
        session,
        run,
        "run_cancelled",
        {"cancel_requested_at": run.cancel_requested_at.isoformat()},
    )
    session.commit()
    raise RunCancelled(f"run {run.id} cancelled")


PIPELINE_STATES: tuple[str, ...] = (
    "TOPIC_ENTERED",
    "DOMAIN_LOADED",
    "EXPRESS_RUNNING",
    "EXPRESS_DONE",
    "EXPRESS_FAILED",
    "PROPOSAL_DRAFTING",
    "USER_PROPOSAL_REVIEW",
    "SCOUT_RUNNING",
    "USER_SEARCH_REVIEW",
    "CURATOR_RUNNING",
    "USER_DEEP_DIVE_REVIEW",
    "SYNTHESIZER_RUNNING",
    "USER_FIELD_REVIEW",
    # PR-C3.a: optional tension-extraction phase between synthesizer
    # and lens/ideator. Skipped when ``Settings.tension_taxonomy_enabled``
    # is False (default until C3.b) OR when ``should_run_tension_extraction``
    # returns False — in that case the run goes USER_FIELD_REVIEW ->
    # FRAMEWORK_LENS_RUNNING / IDEATOR_RUNNING directly. Lives BEFORE
    # framework_lens so the lens prompt can consume compact tension
    # signals when both phases are on (codex round-2 amendment 3).
    "TENSION_EXTRACTION_RUNNING",
    "USER_TENSION_REVIEW",
    # PR-C2.a: optional framework-lens phase between synthesizer and
    # ideator. Skipped when no theoretical_lens inputs exist AND
    # paper_mode != theory_article — in that case the run goes
    # USER_FIELD_REVIEW -> IDEATOR_RUNNING directly.
    "FRAMEWORK_LENS_RUNNING",
    "USER_LENS_REVIEW",
    "IDEATOR_RUNNING",
    "USER_NOVELTY_REVIEW",
    "DRAFTER_RUNNING",
    "STYLIST_RUNNING",
    "REWRITE_RUNNING",
    "CRITIC_RUNNING",
    "USER_REVISION_REVIEW",
    "USER_EXTERNAL_SCAN_APPROVAL",
    "INTEGRITY_RUNNING",
    "USER_INTEGRITY_REVIEW",
    "USER_FINAL_ACCEPTANCE",
    "EXPORTS_RUNNING",
    "EXPORTS_DONE",
)

ERROR_STATES: tuple[str, ...] = (
    "FAILED_FIXABLE",
    "FAILED_NEEDS_USER",
    "FAILED_VENDOR",
    "FAILED_POLICY",
    "CANCELLED",
)

RUN_STATES: tuple[str, ...] = (*PIPELINE_STATES, *ERROR_STATES)

ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    "TOPIC_ENTERED": [
        "DOMAIN_LOADED",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "DOMAIN_LOADED": [
        "EXPRESS_RUNNING",
        "PROPOSAL_DRAFTING",
        "SCOUT_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "EXPRESS_RUNNING": [
        "EXPRESS_DONE",
        "EXPRESS_FAILED",
        "CANCELLED",
    ],
    "EXPRESS_DONE": [],
    "EXPRESS_FAILED": [
        "EXPRESS_RUNNING",
    ],
    "PROPOSAL_DRAFTING": [
        "USER_PROPOSAL_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_PROPOSAL_REVIEW": [
        "PROPOSAL_DRAFTING",
        "SCOUT_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "SCOUT_RUNNING": [
        "USER_SEARCH_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_SEARCH_REVIEW": [
        "CURATOR_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "CURATOR_RUNNING": [
        "USER_DEEP_DIVE_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_DEEP_DIVE_REVIEW": [
        "SYNTHESIZER_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "SYNTHESIZER_RUNNING": [
        "USER_FIELD_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_FIELD_REVIEW": [
        # PR-C3.a: optional tension-extraction phase first.
        # PR-C2.a: optional lens phase OR direct ideator. Caller picks
        # based on tension/lens availability + paper_mode +
        # Settings.tension_taxonomy_enabled.
        "TENSION_EXTRACTION_RUNNING",
        "FRAMEWORK_LENS_RUNNING",
        "IDEATOR_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "TENSION_EXTRACTION_RUNNING": [
        "USER_TENSION_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_TENSION_REVIEW": [
        # Codex round-2 amendment 3: tension before lens. Lens / ideator
        # may both fire after USER_TENSION_REVIEW depending on
        # framework_lens applicability; lens-prompt consumption of
        # tensions lands in C3.b.
        "FRAMEWORK_LENS_RUNNING",
        "IDEATOR_RUNNING",
        "TENSION_EXTRACTION_RUNNING",  # rerun tension
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "FRAMEWORK_LENS_RUNNING": [
        "USER_LENS_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_LENS_REVIEW": [
        "IDEATOR_RUNNING",
        "FRAMEWORK_LENS_RUNNING",  # rerun lens
        # Codex round-2 amendment 4: USER_LENS_REVIEW may rewind to
        # tension when the user wants to refine tensions after seeing
        # the lens output. Conservative addition; the UI will gate.
        "TENSION_EXTRACTION_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "IDEATOR_RUNNING": [
        "USER_NOVELTY_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_NOVELTY_REVIEW": [
        "DRAFTER_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "DRAFTER_RUNNING": [
        "STYLIST_RUNNING",
        "USER_REVISION_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "STYLIST_RUNNING": [
        "USER_REVISION_REVIEW",
        "CRITIC_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "CRITIC_RUNNING": [
        "USER_EXTERNAL_SCAN_APPROVAL",
        "USER_REVISION_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_REVISION_REVIEW": [
        "REWRITE_RUNNING",
        "CRITIC_RUNNING",
        "USER_EXTERNAL_SCAN_APPROVAL",
        "DRAFTER_RUNNING",
        "STYLIST_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "REWRITE_RUNNING": [
        "CRITIC_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_POLICY",
        "FAILED_VENDOR",
        "CANCELLED",
    ],
    "USER_EXTERNAL_SCAN_APPROVAL": [
        "INTEGRITY_RUNNING",
        "USER_FINAL_ACCEPTANCE",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "INTEGRITY_RUNNING": [
        "USER_INTEGRITY_REVIEW",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_INTEGRITY_REVIEW": [
        "USER_FINAL_ACCEPTANCE",
        "DRAFTER_RUNNING",
        "STYLIST_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "USER_FINAL_ACCEPTANCE": [
        "EXPORTS_RUNNING",
        "DRAFTER_RUNNING",
        "STYLIST_RUNNING",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "EXPORTS_RUNNING": [
        "EXPORTS_DONE",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    ],
    "EXPORTS_DONE": [],
    # Stage 3.E follow-up: failure states gain user-forced back-edges
    # via ``POST /api/runs/{id}/force-approve`` (codex AGREE-with-
    # amendments). The endpoint is the only legitimate caller of
    # ``transition`` for these edges; regular state guards still
    # bar them. Each from-state lists every USER_*_REVIEW it could
    # plausibly map back to depending on which phase failed; the
    # endpoint computes the actual target from persisted artifacts
    # and rejects 409 when no minimum artifact is on disk.
    "FAILED_FIXABLE": [
        "USER_PROPOSAL_REVIEW",
        "USER_SEARCH_REVIEW",
        "USER_DEEP_DIVE_REVIEW",
        "USER_FIELD_REVIEW",
        "USER_TENSION_REVIEW",
        "USER_LENS_REVIEW",
        "USER_NOVELTY_REVIEW",
        "USER_REVISION_REVIEW",
        "USER_EXTERNAL_SCAN_APPROVAL",
        "USER_INTEGRITY_REVIEW",
        "USER_FINAL_ACCEPTANCE",
    ],
    "FAILED_NEEDS_USER": [
        "USER_PROPOSAL_REVIEW",
        "USER_SEARCH_REVIEW",
        "USER_DEEP_DIVE_REVIEW",
        "USER_FIELD_REVIEW",
        "USER_TENSION_REVIEW",
        "USER_LENS_REVIEW",
        "USER_NOVELTY_REVIEW",
        "USER_REVISION_REVIEW",
        "USER_EXTERNAL_SCAN_APPROVAL",
        "USER_INTEGRITY_REVIEW",
        "USER_FINAL_ACCEPTANCE",
    ],
    "FAILED_VENDOR": [
        "INTEGRITY_RUNNING",
        "USER_EXTERNAL_SCAN_APPROVAL",
        "USER_FINAL_ACCEPTANCE",
    ],
    "FAILED_POLICY": [
        # FAILED_POLICY is phase-aware. Exporter citation/structure
        # blockers can still proceed to final acceptance, while policy
        # failures from earlier phases return to that phase's review
        # state after the user records a force-approve reason.
        "USER_PROPOSAL_REVIEW",
        "USER_SEARCH_REVIEW",
        "USER_DEEP_DIVE_REVIEW",
        "USER_FIELD_REVIEW",
        "USER_TENSION_REVIEW",
        "USER_LENS_REVIEW",
        "USER_NOVELTY_REVIEW",
        "USER_REVISION_REVIEW",
        "USER_EXTERNAL_SCAN_APPROVAL",
        "USER_INTEGRITY_REVIEW",
        "USER_FINAL_ACCEPTANCE",
    ],
    "CANCELLED": [],
}


class InvalidTransition(ValueError):
    """Raised when a run cannot move from its current state to the requested state."""


def transition(
    run: Run,
    to_state: str,
    session: Session,
    *,
    reason: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> RunEvent:
    allowed = ALLOWED_TRANSITIONS.get(run.state, [])
    if to_state not in allowed:
        raise InvalidTransition(f"cannot transition run {run.id} from {run.state} to {to_state}")

    from_state = run.state
    run.state = to_state
    run.updated_at = utcnow()
    ensure_phase_checkpoint(run.run_dir, to_state)
    event_payload: dict[str, Any] = {
        "from_state": from_state,
        "to_state": to_state,
    }
    if reason is not None:
        event_payload["reason"] = reason
    if payload is not None:
        event_payload["payload"] = dict(payload)
    event = append_event(session, run, "state_transition", event_payload)
    from autoessay.telemetry import record_deep_transition_telemetry

    record_deep_transition_telemetry(
        session,
        run,
        to_state=to_state,
        reason=reason,
        payload=payload,
    )
    return event


def append_event(
    session: Session,
    run: Run,
    event_type: str,
    payload: Mapping[str, Any],
) -> RunEvent:
    event_payload = dict(payload)
    event = RunEvent(
        id=f"event_{uuid4().hex}",
        run_id=run.id,
        event_type=event_type,
        payload=json.dumps(event_payload, sort_keys=True),
        created_at=utcnow(),
    )
    session.add(event)
    session.flush()
    record_run_event_payload(run.run_dir, event_payload)
    return event
