"""Phase-rerun support (codex-AGREEd #2 stage 1).

Stage 1 lets the user re-run any single phase that has already
completed at least once. Artifacts are overwritten in place — no
version history retained yet (that's stage 2).

Critical invariant: when an upstream phase is rerun, all *completed*
downstream phases are now stale relative to the new upstream output.
``Run.stale_from_phase`` names the **earliest** such stale phase —
i.e. the next phase the user must rerun. The API enforces a monotonic
refresh order: while ``stale_from_phase`` is set, only that exact
phase or an upstream phase may be rerun. This prevents the user from
"refreshing" a downstream phase based on still-stale upstream data.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from autoessay.models import Run, RunEvent, utcnow

#: Canonical pipeline order. ``proposal`` is excluded — its rerun is
#: already handled via the existing accept-and-redraft flow.
#: PR-C2.b: ``framework_lens`` slots between synthesizer and ideator;
#: rerun chain handles it like any other phase regardless of whether
#: ``should_run_framework_lens`` would skip on a fresh run.
PHASES: tuple[str, ...] = (
    "scout",
    "curator",
    "synthesizer",
    # PR-C3.a: tension_extraction sits BEFORE framework_lens (codex
    # round-2 amendment 3 — lens consumes compact tensions in C3.b).
    # When ``Settings.tension_taxonomy_enabled`` is False (default
    # until C3.b lands), the tension state never enters the run flow.
    "tension_extraction",
    "framework_lens",
    "ideator",
    "drafter",
    "stylist",
    "critic",
    "integrity",
    "exports",
)

#: For each rerunnable phase, the canonical run state the agent
#: requires before it will run. After a successful first run the run
#: has progressed past this state, so the rerun endpoint must rewind
#: ``run.state`` to this value before invoking the agent.
#: PR-C2.b: framework_lens runs from USER_FIELD_REVIEW. ideator
#: stays anchored on USER_FIELD_REVIEW (the "skip-lens" common
#: case); when an ideator agent runs from USER_LENS_REVIEW
#: (post-lens path), it accepts both states explicitly via
#: ``IDEATOR_VALID_INPUT_STATES`` below.
PHASE_INPUT_STATES: dict[str, str] = {
    "scout": "USER_PROPOSAL_REVIEW",
    "curator": "USER_SEARCH_REVIEW",
    "synthesizer": "USER_DEEP_DIVE_REVIEW",
    # PR-C3.a: tension_extraction starts from USER_FIELD_REVIEW. Lens /
    # ideator stay anchored at USER_FIELD_REVIEW too (the
    # tension-skipped common case); when these phases run AFTER
    # tension extraction, they accept USER_TENSION_REVIEW too — see
    # ``LENS_VALID_INPUT_STATES`` / ``IDEATOR_VALID_INPUT_STATES``.
    "tension_extraction": "USER_FIELD_REVIEW",
    "framework_lens": "USER_FIELD_REVIEW",
    "ideator": "USER_FIELD_REVIEW",
    "drafter": "USER_NOVELTY_REVIEW",
    "stylist": "USER_REVISION_REVIEW",
    "critic": "USER_REVISION_REVIEW",
    "integrity": "USER_EXTERNAL_SCAN_APPROVAL",
    "exports": "USER_FINAL_ACCEPTANCE",
}


def resolve_rewind_state(phase: str, run: Run) -> str | None:
    """Return the state a rerun should rewind to for this run.

    Most phases rewind to their canonical input state. Scout is the
    exception for proposal-less runs: a research-kernel-only run has
    ``proposal_version == 0`` and no proposal artifact, so rewinding
    scout to USER_PROPOSAL_REVIEW strands the frontend on a proposal
    review tab that has nothing to load. In that case the valid input
    state is DOMAIN_LOADED; scout already accepts it.
    """
    target_state = PHASE_INPUT_STATES.get(phase)
    if (
        phase == "scout"
        and target_state == "USER_PROPOSAL_REVIEW"
        and int(run.proposal_version or 0) < 1
    ):
        return "DOMAIN_LOADED"
    return target_state


def rewind_for_rerun(
    run: Run,
    phase: str,
    target_state: str,
    session: Session,
    *,
    source: str,
) -> RunEvent:
    """Apply the rerun back-edge and write an auditable transition event."""
    from autoessay.state_machine import append_event

    from_state = run.state
    to_state = target_state
    run.state = to_state
    run.updated_at = utcnow()
    event = append_event(
        session,
        run,
        "state_transition",
        {
            "from_state": from_state,
            "to_state": to_state,
            "phase": phase,
            "reason": "rerun_rewind",
            "source": source,
        },
    )
    session.flush()
    return event


#: PR-C3.a + codex round-2 amendment 4: framework_lens may run from
#: ``USER_FIELD_REVIEW`` (tension-skipped path) or ``USER_TENSION_REVIEW``
#: (post-tension path). Symmetric to IDEATOR_VALID_INPUT_STATES.
LENS_VALID_INPUT_STATES: frozenset[str] = frozenset(
    {"USER_FIELD_REVIEW", "USER_TENSION_REVIEW"},
)

#: PR-C2.b + PR-C3.a: ideator can be started from
#: USER_FIELD_REVIEW (lens + tension both skipped),
#: USER_LENS_REVIEW (post-lens), OR USER_TENSION_REVIEW (post-tension
#: with lens skipped). codex round-2 amendment 4.
IDEATOR_VALID_INPUT_STATES: frozenset[str] = frozenset(
    {"USER_FIELD_REVIEW", "USER_LENS_REVIEW", "USER_TENSION_REVIEW"},
)

#: After a successful rerun, force the run into a quiescent user-
#: review state. Drafter normally chains into stylist and ends at
#: ``DRAFTER_RUNNING``; without this fix-up, ``assert_can_rerun``
#: would reject the next stale-banner click as "phase currently
#: running". Phases that already end at a quiescent state on their
#: own are not listed here.
PHASE_POST_RERUN_STATE: dict[str, str] = {
    "drafter": "USER_REVISION_REVIEW",
}

#: Run states that mean "an agent is currently writing artifacts".
#: A rerun must wait until the run is quiescent.
RUNNING_STATES: frozenset[str] = frozenset(
    {
        "PROPOSAL_DRAFTING",
        "EXPRESS_RUNNING",
        "SCOUT_RUNNING",
        "CURATOR_RUNNING",
        "SYNTHESIZER_RUNNING",
        # PR-C3.a: tension_extraction phase between synthesizer and lens.
        "TENSION_EXTRACTION_RUNNING",
        # PR-C2.b: framework_lens phase between synthesizer and ideator.
        "FRAMEWORK_LENS_RUNNING",
        "IDEATOR_RUNNING",
        "DRAFTER_RUNNING",
        "STYLIST_RUNNING",
        # Slice E final_rewrite phase between stylist and critic
        # (gated by AUTOESSAY_FINAL_REWRITE_ENABLED, default ON).
        "REWRITE_RUNNING",
        "CRITIC_RUNNING",
        "INTEGRITY_RUNNING",
        "EXPORTS_RUNNING",
    }
)


def downstream_of(phase: str) -> list[str]:
    """Phases that come strictly after ``phase`` in pipeline order."""
    if phase not in PHASES:
        return []
    idx = PHASES.index(phase)
    return list(PHASES[idx + 1 :])


#: Per-phase glob sentinels for "has the phase produced output?"
#: Each pattern is matched relative to ``run.run_dir`` via
#: :meth:`Path.glob`. The phase is "completed" iff at least one
#: pattern matches at least one regular file. Drafter and stylist
#: share ``drafts/`` but their sentinels differ to avoid false
#: positives (a drafter run alone must NOT make stylist look
#: completed — that would let stale_from_phase advance to a phase
#: that has never produced its own output).
PHASE_COMPLETION_GLOBS: dict[str, tuple[str, ...]] = {
    "proposal": ("proposal/proposal_v*.json",),
    "scout": ("discovery/scout_report.md",),
    "curator": ("sources/shortlist.json",),
    "synthesizer": ("synthesis/claims.jsonl",),
    "tension_extraction": ("synthesis/tension_extraction.json",),
    # PR-C2.a: framework_lens artifact lives next to synthesizer.json.
    "framework_lens": ("synthesis/framework_lens.json",),
    "ideator": ("novelty/angle_cards.json",),
    "drafter": ("drafts/*/manuscript.md",),
    "stylist": ("drafts/*/style/*",),
    "final_rewrite": ("rewrite/v*/manuscript.md",),
    "rewrite": ("rewrite/v*/manuscript.md",),
    "critic": ("reviews/*",),
    # Integrity-specific sentinel — drafter's harness writes
    # integrity/local_dedup.json before the integrity phase ever runs,
    # so a wildcard would falsely advertise integrity as completed.
    "integrity": ("integrity/integrity_summary.json",),
    "exports": ("exports/manifest.json",),
}


def has_completed_output(
    run: Run, phase: str, *, session: Session | None = None, branch_id: str | None = None
) -> bool:
    """``True`` iff ``phase`` has previously produced output reachable
    from the (run, branch).

    When a session+branch_id are passed, checks ``run_heads`` —
    branches each maintain their own heads, so this avoids the
    cross-branch-leak codex round-2 flagged: branch B's downstream
    files on disk would otherwise make branch A's stale-marker
    advance even when A has no head for that phase.

    When called without session/branch (legacy callers, or pre-
    branch-rollout test code), falls back to the file-glob check
    (still correct on the active branch since materialization keeps
    legacy paths in sync).
    """
    if session is not None and branch_id is not None:
        from sqlalchemy import select as _select

        from autoessay.models import RunHead

        return (
            session.scalar(
                _select(RunHead.version_id)
                .where(RunHead.run_id == run.id)
                .where(RunHead.branch_id == branch_id)
                .where(RunHead.phase == phase),
            )
            is not None
        )
    patterns = PHASE_COMPLETION_GLOBS.get(phase)
    if not patterns:
        return False
    run_dir = Path(run.run_dir)
    for pattern in patterns:
        for match in run_dir.glob(pattern):
            if match.is_file() and match.stat().st_size > 0:
                return True
    return False


def first_completed_downstream(
    run: Run, phase: str, *, session: Session | None = None, branch_id: str | None = None
) -> str | None:
    """Earliest completed downstream phase of ``phase`` on this branch."""
    for downstream in downstream_of(phase):
        if has_completed_output(run, downstream, session=session, branch_id=branch_id):
            return downstream
    return None


def assert_can_rerun(
    run: Run, phase: str, *, session: Session, branch_id: str | None = None
) -> None:
    """Validate every precondition codex required.

    ``branch_id`` defaults to the run's active branch. Stale-phase
    monotonicity is checked against the BRANCH's stale, not the run's,
    so two branches can independently be stale at different phases
    (codex-AGREEd #2 stage 2.C).
    """
    from autoessay.branches import get_branch_stale

    if phase not in PHASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown phase: {phase}",
        )
    if run.cancel_requested_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this run is cancelled",
        )
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before triggering a rerun"
            ),
        )
    if not has_completed_output(run, phase, session=session, branch_id=branch_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"phase {phase!r} has not produced any output yet; "
                "use the normal start button instead of rerun"
            ),
        )
    stale = get_branch_stale(session, run, branch_id=branch_id)
    if stale is not None and stale in PHASES:
        stale_idx = PHASES.index(stale)
        phase_idx = PHASES.index(phase)
        if phase_idx > stale_idx:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(f"refresh '{stale}' first; can't rerun '{phase}' while it is still stale"),
            )


def update_stale_marker_after_success(
    session: Session, run: Run, phase: str, *, branch_id: str | None = None
) -> None:
    """Recompute the active branch's ``stale_from_phase`` after a
    successful rerun (codex-AGREEd #2 stage 2.C: per-branch stale).
    Logic is unchanged; just the storage moved.
    """
    from autoessay.branches import set_branch_stale

    new_stale = first_completed_downstream(run, phase, session=session, branch_id=branch_id)
    set_branch_stale(session, run, new_stale, branch_id=branch_id)


__all__ = [
    "PHASES",
    "PHASE_COMPLETION_GLOBS",
    "PHASE_INPUT_STATES",
    "PHASE_POST_RERUN_STATE",
    "RUNNING_STATES",
    "assert_can_rerun",
    "downstream_of",
    "first_completed_downstream",
    "has_completed_output",
    "resolve_rewind_state",
    "rewind_for_rerun",
    "update_stale_marker_after_success",
]
