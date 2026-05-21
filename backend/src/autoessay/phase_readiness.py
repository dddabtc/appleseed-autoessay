"""Phase readiness checks shared by ``start_*`` endpoints and the
``rerun_phase`` API.

Codex AGREE-with-amendments (system-wide audit):
> ``start_*`` and ``rerunPhase`` should share a ``phase_ready(run, phase)``
> registry, failing 409 on missing deterministic preconditions before
> any state mutation. The agent-level checks remain (they guarantee
> data integrity) but the API surface should reject earlier so a
> mis-click never leaves the run in FAILED_FIXABLE.

Each ``<phase>_ready(...)`` returns ``(ok, reason)`` where ``ok`` is
True iff the deterministic input artifacts the agent needs are
present on disk / in the database. ``reason`` is None when ready,
otherwise a short user-facing string the API echoes in the 409
detail.

We intentionally do NOT check things that can only be evaluated
once the agent runs (LLM output validity, policy filters, retry
budgets, etc.). Those remain inside the agent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from autoessay.agents.drafter import has_selected_angle
from autoessay.agents.integrity import latest_external_scan_decision
from autoessay.agents.stylist import _latest_draft_dir, stylist_artifacts_ready
from autoessay.models import Run

ReadinessResult = tuple[bool, str | None]


def proposal_ready(run: Run, session: Session) -> ReadinessResult:
    """Proposal needs only a domain-loaded state. No file deps."""
    return True, None


def scout_ready(run: Run, session: Session) -> ReadinessResult:
    """Scout needs an accepted proposal. State guarantees this."""
    return True, None


def curator_ready(run: Run, session: Session) -> ReadinessResult:
    """Curator needs at least one source candidate (scout output OR
    manual upload). Agent fails fixable when neither exists.
    """
    run_dir = Path(run.run_dir)
    skim_path = run_dir / "discovery" / "skim_candidates.jsonl"
    upload_path = run_dir / "sources" / "shortlist.json"
    has_skim = skim_path.exists() and skim_path.stat().st_size > 0
    has_upload = upload_path.exists() and upload_path.stat().st_size > 0
    if not has_skim and not has_upload:
        return (
            False,
            "Curator needs at least one source. Review the Scout output or "
            "upload a manual source first.",
        )
    return True, None


def synthesizer_ready(run: Run, session: Session) -> ReadinessResult:
    """Synthesizer needs a non-empty shortlist."""
    shortlist_path = Path(run.run_dir) / "sources" / "shortlist.json"
    if not shortlist_path.exists():
        return False, "Synthesizer needs Curator's shortlist. Run Curator first."
    try:
        data = json.loads(shortlist_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "Synthesizer cannot read the shortlist. Re-run Curator."
    if not isinstance(data, list) or len(data) == 0:
        return False, "Synthesizer's shortlist is empty. Approve sources or rerun Curator."
    return True, None


def ideator_ready(run: Run, session: Session) -> ReadinessResult:
    """Ideator needs Synthesizer's claims."""
    claims_path = Path(run.run_dir) / "synthesis" / "claims.jsonl"
    if not claims_path.exists() or claims_path.stat().st_size == 0:
        return False, "Ideator needs Synthesizer's claims. Run Synthesizer first."
    return True, None


def drafter_ready(run: Run, session: Session) -> ReadinessResult:
    """Drafter needs a selected novelty angle."""
    if not has_selected_angle(run, session):
        return (
            False,
            "Drafter requires a selected novelty angle. "
            "Pick an angle card on the Novelty tab first.",
        )
    return True, None


def stylist_ready(run: Run, session: Session) -> ReadinessResult:
    """Stylist needs Drafter's manuscript artifacts."""
    return stylist_artifacts_ready(run)


def critic_ready(run: Run, session: Session) -> ReadinessResult:
    """Critic needs a non-empty styled draft."""
    draft_dir = _latest_draft_dir(Path(run.run_dir))
    if draft_dir is None:
        return False, "Critic needs a completed styled draft. Run Stylist first."
    styled_path = draft_dir / "style" / "paper_styled.md"
    if not styled_path.exists() or not styled_path.read_text(encoding="utf-8").strip():
        return False, "Critic found an empty styled draft. Re-run Stylist."
    return True, None


def integrity_ready(run: Run, session: Session) -> ReadinessResult:
    """Integrity needs an approved external-scan decision."""
    decision = latest_external_scan_decision(session, run)
    if decision is None or decision.get("approve") is not True:
        return False, "Integrity requires an approved external scan checkpoint."
    return True, None


def exports_ready(run: Run, session: Session) -> ReadinessResult:
    """Exports needs a styled draft directory + a final manuscript."""
    draft_dir = _latest_draft_dir(Path(run.run_dir))
    if draft_dir is None:
        return False, "Exports needs a completed draft. Run Stylist first."
    final_manuscript = draft_dir / "style" / "paper_styled.md"
    if not final_manuscript.exists() or not final_manuscript.read_text(encoding="utf-8").strip():
        return False, "Exports could not find a final manuscript. Re-run Stylist."
    return True, None


_REGISTRY: dict[str, Callable[[Run, Session], ReadinessResult]] = {
    "proposal": proposal_ready,
    "scout": scout_ready,
    "curator": curator_ready,
    "synthesizer": synthesizer_ready,
    "ideator": ideator_ready,
    "drafter": drafter_ready,
    "stylist": stylist_ready,
    "critic": critic_ready,
    "integrity": integrity_ready,
    "exports": exports_ready,
}


def phase_ready(run: Run, phase: str, session: Session) -> ReadinessResult:
    """Dispatch helper used by ``start_*`` and ``assert_can_rerun``."""
    check = _REGISTRY.get(phase)
    if check is None:
        return True, None
    return check(run, session)


def assert_phase_ready(run: Run, phase: str, session: Session) -> None:
    """Raise 409 if the deterministic preconditions for ``phase`` are
    unmet. Mirrors the agent's own checks so the API rejects
    up-front and the run state is left untouched.
    """
    is_ready, reason = phase_ready(run, phase, session)
    if not is_ready:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=reason or f"Phase {phase!r} is not ready to run.",
        )


__all__ = [
    "ReadinessResult",
    "assert_phase_ready",
    "phase_ready",
]
