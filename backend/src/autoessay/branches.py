"""Branch-aware helpers (codex-AGREEd #2 stage 2.C).

A run can have many named branches; each branch carries its own
``run_heads``, ``phase_prompt_drafts``, and ``stale_from_phase``.
``Run.active_branch_id`` points at the branch the workspace is
currently scoped to. Most code paths default to the active branch
unless an explicit ``branch_id`` is passed (e.g., the rerun endpoint
forwards a query param so the user can rerun on a non-active branch
without switching the workspace).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.models import Branch, Run, utcnow

#: Stable id format for the implicit ``main`` branch. Matches the
#: backfill in alembic 013 so the migration and runtime agree on
#: which row to look up.
MAIN_BRANCH_NAME = "main"


def main_branch_id_for(run_id: str) -> str:
    return f"br_main_{run_id}"


def ensure_main_branch(session: Session, run: Run) -> Branch:
    """Return the run's ``main`` branch, creating it if missing.

    The migration backfills one for every existing run; this is a
    safety net for runs that were created in tests or scripts that
    skipped the normal create-run path.
    """
    main_id = main_branch_id_for(run.id)
    branch = session.get(Branch, main_id)
    if branch is None:
        branch = Branch(id=main_id, run_id=run.id, name=MAIN_BRANCH_NAME)
        session.add(branch)
        session.flush()
        if run.active_branch_id is None:
            run.active_branch_id = main_id
            session.flush()
    elif run.active_branch_id is None:
        run.active_branch_id = main_id
        session.flush()
    return branch


def get_branch(session: Session, run: Run, branch_id: str | None = None) -> Branch:
    """Resolve ``branch_id`` (or the run's active branch) to a row.

    Raises ``ValueError`` if the branch does not exist or belongs to
    another run; the API layer translates that to 404. Soft-deleted
    branches still resolve (so a final rerun-on-a-deleted-branch
    request can fail with a clear "branch deleted" message instead
    of a generic 404), but :func:`get_active_branch` is the right
    helper for "what branch is the user looking at right now".
    """
    target_id = branch_id or run.active_branch_id
    if target_id is None:
        return ensure_main_branch(session, run)
    branch = session.get(Branch, target_id)
    if branch is None or branch.run_id != run.id:
        raise ValueError(f"branch not found for this run: {target_id!r}")
    return branch


def get_active_branch(session: Session, run: Run) -> Branch:
    return get_branch(session, run, branch_id=run.active_branch_id)


def get_branch_stale(session: Session, run: Run, branch_id: str | None = None) -> str | None:
    return get_branch(session, run, branch_id).stale_from_phase


def set_branch_stale(
    session: Session,
    run: Run,
    value: str | None,
    branch_id: str | None = None,
) -> None:
    branch = get_branch(session, run, branch_id)
    branch.stale_from_phase = value
    session.flush()


def mark_branch_stale_at_or_earlier(
    session: Session,
    run: Run,
    candidate_phase: str,
    branch_id: str | None = None,
) -> str:
    """Mark the branch stale starting at ``candidate_phase`` OR an
    earlier phase if one is already recorded.

    Round-1 audit codex DISAGREE #3 (2026-05-03): unconditional
    ``set_branch_stale(..., "synthesizer")`` was overwriting the
    earlier stale marker (e.g. ``curator``) and letting the user
    skip the true earliest stale phase. This helper preserves the
    earliest marker by comparing against the canonical PHASES order.

    Returns the phase that was actually written (either the
    pre-existing earlier marker, or the candidate). Callers should
    use this return value when emitting events.
    """
    from autoessay.phase_rerun import PHASES

    branch = get_branch(session, run, branch_id)
    existing = branch.stale_from_phase
    if existing is None:
        branch.stale_from_phase = candidate_phase
        session.flush()
        return candidate_phase
    try:
        existing_idx = PHASES.index(existing)
    except ValueError:
        # Unknown legacy value — keep existing rather than risk
        # losing an earlier-than-PHASES sentinel.
        return existing
    try:
        candidate_idx = PHASES.index(candidate_phase)
    except ValueError as exc:
        raise ValueError(
            f"unknown candidate phase: {candidate_phase!r}",
        ) from exc
    if candidate_idx < existing_idx:
        branch.stale_from_phase = candidate_phase
        session.flush()
        return candidate_phase
    return existing


def list_active_branches(session: Session, run: Run) -> list[Branch]:
    """Branches not soft-deleted, oldest first (so ``main`` shows up
    first in dropdowns)."""
    return list(
        session.scalars(
            select(Branch)
            .where(Branch.run_id == run.id)
            .where(Branch.deleted_at.is_(None))
            .order_by(Branch.created_at.asc())
        ).all()
    )


def create_branch(
    session: Session,
    run: Run,
    *,
    name: str,
    base_branch: Branch,
    forked_from_pv_id: str,
    forked_phase: str,
    created_by: str | None = None,
) -> Branch:
    """Create a new branch by forking from a phase_version.

    The new branch inherits ``base_branch``'s heads for every
    upstream phase (the rerun endpoint copies them at first use, not
    here, to keep this function focused on the metadata row).
    """
    if base_branch.run_id != run.id:
        raise ValueError("base_branch belongs to a different run")
    existing = session.scalar(
        select(Branch)
        .where(Branch.run_id == run.id)
        .where(Branch.name == name)
        .where(Branch.deleted_at.is_(None))
    )
    if existing is not None:
        raise ValueError(f"branch name already in use: {name!r}")
    branch = Branch(
        id=f"br_{uuid4().hex}",
        run_id=run.id,
        name=name,
        parent_branch_id=base_branch.id,
        forked_from_pv_id=forked_from_pv_id,
        forked_phase=forked_phase,
        stale_from_phase=None,
        created_by=created_by,
    )
    session.add(branch)
    session.flush()
    return branch


def materialize_branch_legacy_paths(session: Session, run: Run, branch: Branch) -> None:
    """Restore every owned file to disk for every phase whose branch
    head is set, so the legacy path readers see THIS branch's
    artifacts after a switch.

    Without this, switching from B to A leaves B's last-restored
    files on disk, and bundle endpoints (which read disk, not
    run_heads) keep showing B's content. Codex round-2 #2 stage 2.C
    flagged this — it also lets reruns on A consume B's bytes.
    """
    from autoessay.models import RunHead
    from autoessay.phase_rerun import PHASES
    from autoessay.phase_version import (
        _purge_owned_files,
        _restore_legacy_paths_from,
    )

    heads = {
        row.phase: row.version_id
        for row in session.scalars(
            select(RunHead).where(RunHead.run_id == run.id).where(RunHead.branch_id == branch.id)
        ).all()
    }
    # For phases the branch has a head for, purge then restore from
    # that head's archive. For phases the branch has no head for, we
    # also purge so the legacy path doesn't keep showing another
    # branch's files (nothing to restore — the user has to rerun on
    # this branch first).
    run_dir = Path(run.run_dir)
    for phase in (*PHASES, "proposal"):
        _purge_owned_files(run_dir, phase)
        head_pv_id = heads.get(phase)
        if head_pv_id is not None:
            _restore_legacy_paths_from(session, run, head_pv_id)


def soft_delete_branch(session: Session, run: Run, branch: Branch) -> None:
    """Mark a branch deleted. Refuses the run's main branch.

    If the deleted branch was active, falls back to ``main`` AND
    materializes main's heads to disk (codex round-3 #2 stage 2.C:
    without the materialize step, legacy bundle endpoints keep
    showing the deleted branch's last-restored files until the user
    explicitly re-switches).
    """
    if branch.id == main_branch_id_for(run.id):
        raise ValueError("cannot delete the main branch of a run")
    if branch.deleted_at is not None:
        return
    was_active = run.active_branch_id == branch.id
    branch.deleted_at = utcnow()
    session.flush()
    if was_active:
        main_id = main_branch_id_for(run.id)
        run.active_branch_id = main_id
        session.flush()
        main_branch = session.get(Branch, main_id)
        if main_branch is not None:
            materialize_branch_legacy_paths(session, run, main_branch)


__all__ = [
    "MAIN_BRANCH_NAME",
    "create_branch",
    "ensure_main_branch",
    "get_active_branch",
    "get_branch",
    "get_branch_stale",
    "list_active_branches",
    "main_branch_id_for",
    "materialize_branch_legacy_paths",
    "set_branch_stale",
    "soft_delete_branch",
]
