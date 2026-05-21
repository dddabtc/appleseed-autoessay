"""User-forced approval at failure-state nodes (Stage 3.E follow-up).

Codex AGREE-with-amendments: every FAILED_* state (except CANCELLED,
which is terminal by design) needs a user-controlled override path.
The exporter citation gate in particular was diagnosed as
permanently-terminal by mistake — codex flagged that it's actually
recoverable, and prod hit this within hours.

This module exposes:

- ``compute_force_target(run, session)``: returns ``ForceTargetInfo``
  describing whether force-approve is applicable from the run's
  current state, what target state it would land at, and a
  short consequence string for the UI to display before the user
  confirms. Computed from persisted phase metadata, not just from
  the failure state name.
- ``force_approve(run, session, reason)``: single-transaction
  mutation. For FAILED_POLICY, marks all unresolved blockers in
  ``reviews/blocking_issues.json`` as user-resolved, then returns
  to the review state for the failed phase. Exporter policy failures
  still advance to final acceptance. For all states, transitions
  and emits a ``force_approve`` audit event carrying the cleared
  blockers snapshot.

The audit event includes a SHA-256 of the pre-mutation
``blocking_issues.json`` so the trail can be reconstructed even
if the file is later edited.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from autoessay.models import Run
from autoessay.state_machine import append_event, transition


@dataclass(frozen=True)
class ForceTargetInfo:
    """What force-approve would do if invoked right now."""

    applicable: bool
    target_state: str | None
    consequence: str | None
    """Human-readable summary the UI shows before the user confirms."""
    blockers_to_resolve: int = 0


# Phase → USER_*_REVIEW mapping for the back-edge.
PHASE_REVIEW_STATE: dict[str, str] = {
    "proposal": "USER_PROPOSAL_REVIEW",
    "scout": "USER_SEARCH_REVIEW",
    "curator": "USER_DEEP_DIVE_REVIEW",
    "synthesizer": "USER_FIELD_REVIEW",
    "tension_extraction": "USER_TENSION_REVIEW",
    "framework_lens": "USER_LENS_REVIEW",
    "ideator": "USER_NOVELTY_REVIEW",
    "drafter": "USER_REVISION_REVIEW",
    "stylist": "USER_REVISION_REVIEW",
    "final_rewrite": "USER_REVISION_REVIEW",
    "rewrite": "USER_REVISION_REVIEW",
    "critic": "USER_EXTERNAL_SCAN_APPROVAL",
    "integrity": "USER_FINAL_ACCEPTANCE",
    "exports": "USER_FINAL_ACCEPTANCE",
}


def compute_force_target(run: Run, session: Session) -> ForceTargetInfo:
    """Decide whether force-approve is applicable + what it would do.

    The frontend reads this from the RunResponse and uses it to
    decide whether to render the "Force approve and continue"
    button + which consequence label to show.
    """
    state = run.state

    if state == "FAILED_POLICY":
        phase = _last_failed_phase(run, session)
        blockers = _load_unresolved_blockers(run)
        if phase == "exports":
            # Exporter citation/structure gate. Force-approve marks
            # blockers user-resolved, advances to USER_FINAL_ACCEPTANCE.
            return ForceTargetInfo(
                applicable=True,
                target_state="USER_FINAL_ACCEPTANCE",
                consequence=(
                    f"Will mark {len(blockers)} blocker(s) as user-resolved "
                    "and advance to final acceptance. Critic/audit findings "
                    "stay on record for audit purposes."
                ),
                blockers_to_resolve=len(blockers),
            )
        return _phase_review_force_target(
            run,
            phase,
            blockers_to_resolve=len(blockers),
            policy_failure=True,
        )

    if state == "FAILED_VENDOR":
        # Codex amendment: route through the same path as Skip integrity.
        return ForceTargetInfo(
            applicable=True,
            target_state="USER_FINAL_ACCEPTANCE",
            consequence=(
                "Skips integrity (the vendor scan was unavailable) and "
                "advances to final acceptance."
            ),
        )

    if state in {"FAILED_FIXABLE", "FAILED_NEEDS_USER"}:
        # Find the failed phase from the most recent phase_failed event,
        # then look up the corresponding USER_*_REVIEW state.
        phase = _last_failed_phase(run, session)
        return _phase_review_force_target(run, phase)

    # CANCELLED, EXPORTS_DONE, all running states, all USER_*_REVIEW —
    # force-approve is not applicable.
    return ForceTargetInfo(applicable=False, target_state=None, consequence=None)


def _phase_review_force_target(
    run: Run,
    phase: str | None,
    *,
    blockers_to_resolve: int = 0,
    policy_failure: bool = False,
) -> ForceTargetInfo:
    if phase is None:
        return ForceTargetInfo(
            applicable=False,
            target_state=None,
            consequence=(
                "No failed-phase context recorded; force-approve has "
                "no obvious target. Use the phase history modal to "
                "rerun a specific phase manually."
            ),
            blockers_to_resolve=blockers_to_resolve,
        )
    target = PHASE_REVIEW_STATE.get(phase)
    if target is None:
        supported = ", ".join(sorted(PHASE_REVIEW_STATE))
        return ForceTargetInfo(
            applicable=False,
            target_state=None,
            consequence=(
                f"Unknown phase {phase!r}; cannot compute target. Supported phases: {supported}."
            ),
            blockers_to_resolve=blockers_to_resolve,
        )
    # Codex: reject if target review state has no minimum artifact
    # to show. We check that the phase has produced at least the
    # sentinel file ``has_completed_output`` would look for; that
    # way the next review tab actually has something to display.
    if not _phase_has_some_output(run, phase):
        return ForceTargetInfo(
            applicable=False,
            target_state=None,
            consequence=(
                f"Phase {phase!r} has not produced any artifacts; "
                "force-approve would leave you on a blank review "
                "screen. Rerun the phase first."
            ),
            blockers_to_resolve=blockers_to_resolve,
        )
    if policy_failure:
        consequence = (
            f"Will mark {blockers_to_resolve} blocker(s) as user-resolved "
            f"and return to {target} for the failed {phase!r} phase. "
            "Critic/audit findings stay on record for audit purposes."
        )
    else:
        consequence = (
            f"Accepts the current partial output of {phase!r} as-is "
            f"and advances to {target}. You can edit prompts and "
            "rerun the phase from there if you want a fresh attempt."
        )
    return ForceTargetInfo(
        applicable=True,
        target_state=target,
        consequence=consequence,
        blockers_to_resolve=blockers_to_resolve,
    )


def force_approve(
    run: Run,
    session: Session,
    reason: str,
) -> dict[str, Any]:
    """Perform the force-approve transition under a single
    transaction. Returns a payload dict suitable for serializing
    into the API response.
    """
    info = compute_force_target(run, session)
    if not info.applicable or info.target_state is None:
        from fastapi import HTTPException
        from fastapi import status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                "Force-approve is not applicable in the run's current "
                f"state ({run.state!r}). " + (info.consequence or "")
            ),
        )

    cleared_blockers: list[Mapping[str, Any]] = []
    blocking_issues_hash: str | None = None
    blocking_issues_path: str | None = None
    if run.state == "FAILED_POLICY":
        cleared_blockers, blocking_issues_hash, blocking_issues_path = _resolve_blocking_issues(
            run, reason
        )

    prior_state = run.state
    transition(
        run,
        info.target_state,
        session,
        reason=f"user force-approve: {reason}",
        payload={
            "force_approve": True,
            "force_reason": reason,
            "cleared_blocker_count": len(cleared_blockers),
        },
    )
    append_event(
        session,
        run,
        "force_approve",
        {
            "prior_state": prior_state,
            "new_state": info.target_state,
            "reason": reason,
            "cleared_blockers": list(cleared_blockers),
            "blocking_issues_path": blocking_issues_path,
            "blocking_issues_sha256_pre": blocking_issues_hash,
        },
    )
    return {
        "prior_state": prior_state,
        "new_state": info.target_state,
        "reason": reason,
        "cleared_blocker_count": len(cleared_blockers),
        "consequence": info.consequence,
    }


def _load_unresolved_blockers(run: Run) -> list[Mapping[str, Any]]:
    path = Path(run.run_dir) / "reviews" / "blocking_issues.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    issues = data.get("issues") if isinstance(data, dict) else None
    if not isinstance(issues, list):
        return []
    return [
        issue for issue in issues if isinstance(issue, dict) and not bool(issue.get("resolved"))
    ]


def _resolve_blocking_issues(
    run: Run, reason: str
) -> tuple[list[Mapping[str, Any]], str | None, str | None]:
    """Mark all unresolved BLOCKERs in blocking_issues.json as
    user-resolved. Returns (cleared, sha256_of_pre_image, path).
    """
    path = Path(run.run_dir) / "reviews" / "blocking_issues.json"
    if not path.exists():
        return [], None, None
    raw = path.read_text(encoding="utf-8")
    sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        data = json.loads(raw)
    except (OSError, ValueError):
        return [], sha256, str(path)
    if not isinstance(data, dict):
        return [], sha256, str(path)
    issues = data.get("issues")
    if not isinstance(issues, list):
        return [], sha256, str(path)

    cleared: list[Mapping[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if bool(issue.get("resolved")):
            continue
        cleared.append({**issue})
        issue["resolved"] = True
        issue["resolved_by"] = "user_force_approve"
        issue["resolved_at"] = now_iso
        issue["resolved_reason"] = reason

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cleared, sha256, str(path)


def _last_failed_phase(run: Run, session: Session) -> str | None:
    from sqlalchemy import select

    from autoessay.models import RunEvent

    event = session.scalar(
        select(RunEvent)
        .where(RunEvent.run_id == run.id)
        .where(RunEvent.event_type == "phase_failed")
        .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
        .limit(1),
    )
    if event is None:
        return None
    try:
        payload = json.loads(event.payload)
    except (TypeError, ValueError):
        return None
    phase = payload.get("phase") if isinstance(payload, dict) else None
    return phase if isinstance(phase, str) else None


def _phase_has_some_output(run: Run, phase: str) -> bool:
    """Cheap check: does the phase have at least one artifact on
    disk? Used to reject force-approve when the target review tab
    would be empty.
    """
    from autoessay.phase_rerun import PHASE_COMPLETION_GLOBS

    patterns = PHASE_COMPLETION_GLOBS.get(phase, ())
    if not patterns:
        return False
    run_dir = Path(run.run_dir)
    for pattern in patterns:
        for match in run_dir.glob(pattern):
            if match.is_file() and match.stat().st_size > 0:
                return True
    return False


__all__ = [
    "ForceTargetInfo",
    "compute_force_target",
    "force_approve",
]
