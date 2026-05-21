"""PR-382 (2026-05-13): one-click full-auto pilot coordinator.

When ``run.auto_advance`` is true, this module is the single source
of truth for "where should this run go next?" — a table-driven
dispatcher from each ``USER_*_REVIEW`` state to (a) the checkpoint
payload that auto-accepts it and (b) the next phase to enqueue.

Codex AGREE-WITH-AMENDMENTS 2026-05-13 PR-382:
- Table-driven coordinator (don't scatter logic into every phase
  handler — amendment X).
- Approve all qualified-and-deduped sources at the search /
  deep-dive gates (amendment 2 — prevents the synthesizer<3 failure
  the mathematical_mode canary hit).
- Read ``recommended_angle_id`` from the ideator artifact instead
  of hardcoding ``angle_001`` (amendment 3 — defaults to the first
  card today but the field gives us a smarter slot later).
- ``FAILED_FIXABLE`` / ``FAILED_POLICY`` / ``FAILED_NEEDS_USER`` /
  ``FAILED_VENDOR`` always pause; only ``auto_advance_paused``
  event + UI banner today; email/push notification is opt-in
  follow-up work (amendment 4).
- Idempotent: every handler ends with ``session.commit()`` and the
  RQ enqueue (if any) happens AFTER the commit, reusing the
  existing enqueue-failure unlock path. Calling ``maybe_advance``
  on a state that doesn't match the table returns ``False`` cleanly.

Entry points (every place a run might land in a new ``USER_*_REVIEW``
state):
- ``state_machine.transition`` — after a ``phase_done`` event
- ``main.update_run_settings`` — when ``auto_advance`` flips on
- ``zombie_reaper`` — after recovering a stuck phase
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from autoessay.models import Run

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# States where we deliberately STOP and surface to the user — never
# auto-advance through these. ``FAILED_VENDOR`` included per codex
# amendment 4.
_PAUSE_STATES: frozenset[str] = frozenset(
    {
        "FAILED_FIXABLE",
        "FAILED_POLICY",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "CANCELLED",
    },
)

# Internal default skip reason for USER_EXTERNAL_SCAN_APPROVAL when
# auto-pilot fires. Keeps the safety-gate happy (the gate refuses
# test-like internal labels — must read scholarly).
_AUTO_SKIP_EXTERNAL_SCAN_REASON = (
    "本稿处于自动化生产流程，外部 plagiarism / AI 扫描在本阶段不强制执行，"
    "保留供用户在终稿前手动复核。"
)


def maybe_advance(
    session: Session,
    run: Run,
    *,
    source: str = "phase_done",
) -> bool:
    """Single entry point. Idempotent. Return ``True`` iff the run
    actually transitioned (or fired a phase-start) on this call.

    ``source`` is recorded in the audit event so post-mortems can
    tell which trigger fired the coordinator.

    Never raises — handlers catch their own exceptions and emit
    ``auto_advance_error`` events instead, so calling this from a
    state-machine ``transition`` hook can't crash the request.
    """
    if not getattr(run, "auto_advance", False):
        return False
    if getattr(run, "generation_mode", "deep") != "deep":
        return False
    if run.state in _PAUSE_STATES:
        _emit_paused(session, run, source=source, reason=f"state={run.state}")
        return False
    handler = _DISPATCH.get(run.state)
    if handler is None:
        # Not a user-review state we know how to advance. RUNNING_*
        # states drop here (the worker is mid-flight, nothing to do)
        # and post-EXPORTS_DONE drops here (terminal).
        return False
    try:
        return handler(session, run, source)
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        logger.exception(
            "auto_advance handler raised for run=%s state=%s source=%s",
            run.id,
            run.state,
            source,
        )
        _emit_error(session, run, source=source, error=exc)
        with _suppress_db_errors():
            session.rollback()
        return False


# ---------------------------------------------------------------------
# Per-state handlers. Each returns ``True`` if it actually advanced.
#
# Pattern (codex AGREE-WITH-AMENDMENTS B1 idempotent service):
#   1. Build the CheckpointDecisionRequest payload.
#   2. Call the same ``_record_*_checkpoint`` function the HTTP route
#      uses (so the state machine + lock + audit are all in one
#      transaction — no second source of truth).
#   3. ``session.commit()`` to flush.
#   4. Call the next phase's ``start_*`` function which handles its
#      own commit + RQ enqueue + lock claim.
# ---------------------------------------------------------------------


def _advance_domain_loaded(session: Session, run: Run, source: str) -> bool:
    """PR-386: kick off the proposal phase from the fresh-run state.

    Mirrors what ``start_proposal`` would do for an empty user draft —
    claim the proposal phase lock, enqueue the job (or run sync), let
    the worker land the run in ``USER_PROPOSAL_REVIEW`` where the
    regular ``_advance_proposal_review`` handler picks it up.

    Without this handler PR-382's auto-pilot left the run stuck at
    ``DOMAIN_LOADED`` because the dispatch table only covered
    ``USER_*_REVIEW`` states; the user perceived "completely did
    not auto-run" because the very first kick had to be manual.
    """
    from autoessay.config import get_settings
    from autoessay.main import (
        _claim_or_409,
        _release_after_enqueue_failure,
    )
    from autoessay.worker import enqueue_proposal_job

    token = _claim_or_409(session, run, "proposal")
    _emit_advanced(session, run, source=source, from_state="DOMAIN_LOADED")
    session.commit()
    session.refresh(run)
    settings = get_settings()
    if settings.sync_worker:
        from autoessay.agents.proposal import run_proposal_draft

        run_proposal_draft(run.id, session, user_draft=None, lock_token=token)
    else:
        try:
            enqueue_proposal_job(run.id, user_draft=None, lock_token=token)
        except Exception:
            _release_after_enqueue_failure(session, run, "proposal", token)
            raise
    return True


def _phase_finished_without_transition(session: Session, run: Run, phase: str) -> bool:
    """PR-393: True iff the run's latest event chain for ``phase``
    contains a ``phase_done`` *after* its latest ``phase_started`` —
    i.e. the worker did its work and released the lock, but the state
    machine never advanced. Used by ``_advance_drafter_running`` to
    distinguish "drafter is still drafting" (no chain action needed)
    from "drafter finished cleanly but nobody kicked stylist".

    Walks events DESC, returns:
    - ``True`` on encountering ``phase_done`` for this phase first
    - ``False`` on encountering ``phase_started`` / ``phase_failed``
      for this phase first (in flight or already failed)
    - ``False`` when no event matches at all
    """
    import json as _json

    from sqlalchemy import select as _select

    from autoessay.models import RunEvent

    events = session.scalars(
        _select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.created_at.desc())
    ).all()
    for ev in events:
        try:
            payload_phase = _json.loads(ev.payload or "{}").get("phase")
        except _json.JSONDecodeError:
            payload_phase = None
        if payload_phase != phase:
            continue
        if ev.event_type == "phase_done":
            return True
        if ev.event_type in {"phase_started", "phase_failed"}:
            return False
    return False


def _advance_drafter_running(session: Session, run: Run, source: str) -> bool:
    """PR-393: bridge the drafter→stylist gap in auto-pilot.

    The drafter agent writes ``phase_done`` on success but **does not
    transition state out of DRAFTER_RUNNING** (see ``agents/drafter.py``
    around the ``phase_done`` emission — only ``next_stage:
    'stylist_pending'`` is in the payload, with no consumer). In the
    UI flow a user clicks ``phase-action-stylist`` to enqueue stylist;
    in auto-pilot, nothing did. Result: the run sat at DRAFTER_RUNNING
    with ``active_phase_lock IS NULL`` forever.

    Trigger condition (codex amendment 3 — explicit, not 409-driven):
    - ``run.active_phase_lock IS NULL`` (drafter actually finished)
    - latest event chain has ``phase_done(drafter)`` since the latest
      ``phase_started(drafter)`` — verified via
      ``_phase_finished_without_transition``

    When both hold, claim the stylist lock and enqueue it (or run
    sync). Stylist's own success path transitions to
    ``USER_REVISION_REVIEW`` where ``_advance_revision_review``
    picks the chain back up — so we deliberately do NOT add similar
    bridges for STYLIST/REWRITE/CRITIC (they already self-transition
    or are wrapped).
    """
    if run.active_phase_lock is not None:
        return False
    if not _phase_finished_without_transition(session, run, "drafter"):
        return False
    from autoessay.config import get_settings
    from autoessay.main import (
        _claim_or_409,
        _release_after_enqueue_failure,
    )
    from autoessay.worker import enqueue_stylist_job

    token = _claim_or_409(session, run, "stylist")
    _emit_advanced(session, run, source=source, from_state="DRAFTER_RUNNING")
    session.commit()
    session.refresh(run)
    settings = get_settings()
    if settings.sync_worker:
        from autoessay.agents.stylist import run_stylist

        run_stylist(run.id, session, lock_token=token)
    else:
        try:
            enqueue_stylist_job(run.id, lock_token=token)
        except Exception:
            _release_after_enqueue_failure(session, run, "stylist", token)
            raise
    return True


def _advance_proposal_review(session: Session, run: Run, source: str) -> bool:
    from autoessay.main import (
        CheckpointDecisionRequest,
        _record_proposal_checkpoint,
        start_scout,
    )

    _record_proposal_checkpoint(
        run,
        # PR-387: the proposal checkpoint reads ``accept`` (not
        # ``approve``); the original PR-382 typo raised HTTPException
        # 400 ("accept must be true or false") at every USER_PROPOSAL_
        # REVIEW gate, caught only when a live run exposed it.
        CheckpointDecisionRequest(status="ACCEPTED", accept=True),
        session,
    )
    _emit_advanced(session, run, source=source, from_state="USER_PROPOSAL_REVIEW")
    session.commit()
    session.refresh(run)
    start_scout(run.id, session)
    return True


def _advance_search_review(session: Session, run: Run, source: str) -> bool:
    """Codex amendment 2: approve all qualified-and-deduped skim
    candidates so the curator gets the full input set. Avoids the
    synthesizer<3 failure the mathematical_mode canary hit."""
    from autoessay.agents.curator import load_sources_payload
    from autoessay.main import (
        CheckpointDecisionRequest,
        _record_source_review_checkpoint,
        start_curator,
    )

    source_ids = _all_source_ids_from_skim(load_sources_payload(run))
    _record_source_review_checkpoint(
        run,
        "USER_SEARCH_REVIEW",
        CheckpointDecisionRequest(
            status="ACCEPTED",
            decision_payload={
                "source_ids": source_ids,
                "approved_source_ids": source_ids,
                "rejected_source_ids": [],
                "pinned_source_ids": [],
                "review_scope": "search_review",
            },
        ),
        session,
    )
    _emit_advanced(
        session,
        run,
        source=source,
        from_state="USER_SEARCH_REVIEW",
        extra={"approved_count": len(source_ids)},
    )
    session.commit()
    session.refresh(run)
    start_curator(run.id, session)
    return True


def _advance_deep_dive_review(session: Session, run: Run, source: str) -> bool:
    from autoessay.agents.curator import load_sources_payload
    from autoessay.main import (
        CheckpointDecisionRequest,
        _record_source_review_checkpoint,
        start_synthesizer,
    )

    source_ids = _all_source_ids_from_shortlist(load_sources_payload(run))
    _record_source_review_checkpoint(
        run,
        "USER_DEEP_DIVE_REVIEW",
        CheckpointDecisionRequest(
            status="ACCEPTED",
            decision_payload={
                "source_ids": source_ids,
                "approved_source_ids": source_ids,
                "rejected_source_ids": [],
                "pinned_source_ids": [],
                "review_scope": "deep_dive_review",
            },
        ),
        session,
    )
    _emit_advanced(
        session,
        run,
        source=source,
        from_state="USER_DEEP_DIVE_REVIEW",
        extra={"approved_count": len(source_ids)},
    )
    session.commit()
    session.refresh(run)
    start_synthesizer(run.id, session)
    return True


def _advance_field_review(session: Session, run: Run, source: str) -> bool:
    """USER_FIELD_REVIEW → start framework_lens. No checkpoint
    payload required; the phase API itself transitions the state."""
    from autoessay.main import start_framework_lens

    _emit_advanced(session, run, source=source, from_state="USER_FIELD_REVIEW")
    session.commit()
    session.refresh(run)
    start_framework_lens(run.id, session)
    return True


def _advance_lens_review(session: Session, run: Run, source: str) -> bool:
    from autoessay.main import start_ideator

    _emit_advanced(session, run, source=source, from_state="USER_LENS_REVIEW")
    session.commit()
    session.refresh(run)
    start_ideator(run.id, session)
    return True


def _advance_novelty_review(session: Session, run: Run, source: str) -> bool:
    """Codex amendment 3: read ``recommended_angle_id`` from the
    ideator artifact rather than hardcoding ``angle_001``. The
    artifact may not yet expose this field; default to the first
    angle card's id so behavior is deterministic."""
    from autoessay.agents.ideator import load_novelty_payload
    from autoessay.main import (
        CheckpointDecisionRequest,
        _record_novelty_checkpoint,
        start_drafter,
    )

    payload = load_novelty_payload(run)
    angle_id = _pick_recommended_angle(payload)
    if angle_id is None:
        _emit_paused(
            session,
            run,
            source=source,
            reason="no angle cards available — auto-pilot cannot pick one",
        )
        session.commit()
        return False
    _record_novelty_checkpoint(
        run,
        CheckpointDecisionRequest(
            status="ACCEPTED",
            selected_angle_id=angle_id,
            decision_payload={"selected_angle_id": angle_id},
        ),
        session,
    )
    _emit_advanced(
        session,
        run,
        source=source,
        from_state="USER_NOVELTY_REVIEW",
        extra={"selected_angle_id": angle_id},
    )
    session.commit()
    session.refresh(run)
    start_drafter(run.id, session)
    return True


def _advance_revision_review(session: Session, run: Run, source: str) -> bool:
    from autoessay.main import start_critic

    _emit_advanced(session, run, source=source, from_state="USER_REVISION_REVIEW")
    session.commit()
    session.refresh(run)
    start_critic(run.id, session)
    return True


def _advance_external_scan_approval(session: Session, run: Run, source: str) -> bool:
    from autoessay.main import (
        CheckpointDecisionRequest,
        _record_external_scan_checkpoint,
    )

    _record_external_scan_checkpoint(
        run,
        CheckpointDecisionRequest(
            status="ACCEPTED",
            decision_payload={
                "approve": False,
                "skip_reason": _AUTO_SKIP_EXTERNAL_SCAN_REASON,
            },
        ),
        session,
    )
    _emit_advanced(
        session,
        run,
        source=source,
        from_state="USER_EXTERNAL_SCAN_APPROVAL",
        extra={"action": "skipped"},
    )
    session.commit()
    return True


def _advance_integrity_review(session: Session, run: Run, source: str) -> bool:
    from autoessay.main import (
        CheckpointDecisionRequest,
        _record_integrity_review_checkpoint,
    )

    _record_integrity_review_checkpoint(
        run,
        # PR-387: integrity_review checkpoint also reads ``accept``,
        # not ``approve``. Same PR-382 typo as the proposal handler.
        CheckpointDecisionRequest(status="ACCEPTED", accept=True),
        session,
    )
    _emit_advanced(session, run, source=source, from_state="USER_INTEGRITY_REVIEW")
    session.commit()
    return True


def _advance_final_acceptance(session: Session, run: Run, source: str) -> bool:
    from autoessay.main import (
        CheckpointDecisionRequest,
        _record_final_acceptance_checkpoint,
        start_exports,
    )

    _record_final_acceptance_checkpoint(
        run,
        CheckpointDecisionRequest(
            status="ACCEPTED",
            accept=True,
            decision_payload={
                "accept": True,
                "approve": True,
                "export_formats": [
                    "markdown",
                    "docx",
                    "html",
                    "latex",
                    "bibtex",
                    "csl_json",
                ],
            },
        ),
        session,
    )
    _emit_advanced(session, run, source=source, from_state="USER_FINAL_ACCEPTANCE")
    session.commit()
    session.refresh(run)
    start_exports(run.id, session)
    return True


# State → handler table (codex amendment X). Adding a new gate is
# one entry here + one handler function; no other module touches
# the dispatch.
_DISPATCH: dict[str, Callable[[Session, Run, str], bool]] = {
    # PR-386: cover the fresh-run kickoff. Without this the run sits
    # at DOMAIN_LOADED until the user manually clicks "Generate
    # Initial Proposal" — defeating the point of auto-pilot.
    "DOMAIN_LOADED": _advance_domain_loaded,
    # PR-393: bridge the drafter→stylist gap. The drafter agent emits
    # ``phase_done`` but never transitions state, so the run sat at
    # DRAFTER_RUNNING. Handler is guarded by
    # ``_phase_finished_without_transition`` so it's a no-op while
    # drafter is still in flight.
    "DRAFTER_RUNNING": _advance_drafter_running,
    "USER_PROPOSAL_REVIEW": _advance_proposal_review,
    "USER_SEARCH_REVIEW": _advance_search_review,
    "USER_DEEP_DIVE_REVIEW": _advance_deep_dive_review,
    "USER_FIELD_REVIEW": _advance_field_review,
    "USER_LENS_REVIEW": _advance_lens_review,
    "USER_NOVELTY_REVIEW": _advance_novelty_review,
    "USER_REVISION_REVIEW": _advance_revision_review,
    "USER_EXTERNAL_SCAN_APPROVAL": _advance_external_scan_approval,
    "USER_INTEGRITY_REVIEW": _advance_integrity_review,
    "USER_FINAL_ACCEPTANCE": _advance_final_acceptance,
}

# PR-388 (codex amendment 4): exported view of states the coordinator
# can resume from after an interruption. Not just ``USER_*_REVIEW`` —
# also includes ``USER_EXTERNAL_SCAN_APPROVAL`` / ``USER_FINAL_ACCEPTANCE``
# (which don't fit the ``USER_*_REVIEW`` naming convention) and
# ``DOMAIN_LOADED`` (fresh-run kickoff state).
RESUMABLE_STATES: frozenset[str] = frozenset(_DISPATCH.keys())


def resume_auto_advance_idle_runs(
    session_factory: Callable[[], Any] | None = None,
) -> dict[str, int]:
    """PR-388: best-effort resume sweep for auto_advance runs left
    stranded by container restart, RQ enqueue failure, or any other
    interruption that broke the ``phase_lock_release_on_exit`` chain.

    Scope (codex amendment):
    - ``auto_advance = True``
    - ``state in RESUMABLE_STATES``
    - ``active_phase_lock IS NULL`` (worker isn't mid-flight; if a
      stale lock exists ``zombie_reaper`` handles it separately).
    - ``deleted_at IS NULL`` and the parent project is also not soft-
      deleted (don't auto-resume runs the user just trashed).
    - ``cancel_requested_at IS NULL`` (user explicitly stopped it).

    Idempotent — calling ``maybe_advance`` on a state already in
    progress is a no-op. Per-run errors are isolated; a bad run does
    not stop the sweep.

    Returns ``{"candidates": N, "resumed": M, "errors": E}`` for
    audit / metrics. Synchronous; the caller (zombie reaper coroutine)
    runs it in ``asyncio.to_thread``.
    """
    from sqlalchemy import select as _select

    from autoessay.db import SessionLocal
    from autoessay.models import Project

    factory = session_factory or SessionLocal
    counters = {"candidates": 0, "resumed": 0, "errors": 0}
    with factory() as session:
        rows = session.execute(
            _select(Run)
            .join(Project, Project.id == Run.project_id)
            .where(
                Run.auto_advance.is_(True),
                Run.state.in_(RESUMABLE_STATES),
                Run.active_phase_lock.is_(None),
                Run.deleted_at.is_(None),
                Project.deleted_at.is_(None),
                Run.cancel_requested_at.is_(None),
            ),
        ).all()
        runs = [row[0] for row in rows]
        counters["candidates"] = len(runs)
        for run in runs:
            try:
                if maybe_advance(session, run, source="reaper_resume"):
                    counters["resumed"] += 1
            except Exception:  # noqa: BLE001 - one bad run can't stop the sweep
                counters["errors"] += 1
                logger.exception(
                    "resume_auto_advance_idle_runs failed for run=%s state=%s",
                    run.id,
                    run.state,
                )
                with _suppress_db_errors():
                    session.rollback()
    if counters["candidates"]:
        logger.info(
            "auto_advance resume sweep: candidates=%d resumed=%d errors=%d",
            counters["candidates"],
            counters["resumed"],
            counters["errors"],
        )
    return counters


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _all_source_ids_from_skim(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("skim_candidates") or payload.get("rows") or []
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            sid = row.get("source_id")
            if isinstance(sid, str) and sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


def _all_source_ids_from_shortlist(payload: object) -> list[str]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("shortlist") or payload.get("rows") or []
    else:
        rows = []
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            sid = row.get("source_id")
            if isinstance(sid, str) and sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


def _pick_recommended_angle(payload: object) -> str | None:
    """Codex amendment 3: prefer ``recommended_angle_id`` if the
    ideator artifact exposes it; else first ``angle_cards[*].angle_id``.
    """
    if not isinstance(payload, dict):
        return None
    rec = payload.get("recommended_angle_id")
    if isinstance(rec, str) and rec:
        return rec
    cards = payload.get("angle_cards") or []
    if isinstance(cards, list):
        for card in cards:
            if isinstance(card, dict):
                aid = card.get("angle_id") or card.get("id")
                if isinstance(aid, str) and aid:
                    return aid
    return None


def _emit_advanced(
    session: Session,
    run: Run,
    *,
    source: str,
    from_state: str,
    extra: dict[str, object] | None = None,
) -> None:
    from autoessay.state_machine import append_event

    payload: dict[str, object] = {
        "from_state": from_state,
        "to_state_after": run.state,  # filled by caller's commit; best-effort
        "source": source,
    }
    if extra:
        payload.update(extra)
    append_event(session, run, "auto_advance", payload)


def _emit_paused(
    session: Session,
    run: Run,
    *,
    source: str,
    reason: str,
) -> None:
    from autoessay.state_machine import append_event

    append_event(
        session,
        run,
        "auto_advance_paused",
        {"state": run.state, "source": source, "reason": reason},
    )


def _emit_error(
    session: Session,
    run: Run,
    *,
    source: str,
    error: Exception,
) -> None:
    from autoessay.state_machine import append_event

    append_event(
        session,
        run,
        "auto_advance_error",
        {
            "state": run.state,
            "source": source,
            "error": f"{type(error).__name__}: {str(error)[:300]}",
        },
    )


def _suppress_db_errors():  # type: ignore[no-untyped-def]
    """Tiny contextlib.suppress so the error path doesn't need
    contextlib at the top of the module."""
    import contextlib

    return contextlib.suppress(Exception)


__all__ = ["maybe_advance"]
