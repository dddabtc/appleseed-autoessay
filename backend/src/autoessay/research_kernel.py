"""Research-kernel edit + snapshot helpers (PR-C0.b1).

Builds on PR-C0 foundation (#138) which added ``runs.paper_mode``
and ``runs.research_kernel_json``. This module adds:

- ``compute_kernel_hash`` — concurrency token (SHA of normalized
  paper_mode + kernel JSON). Endpoints use this to detect lost
  updates.
- ``write_kernel_snapshot`` — atomic write of
  ``proposal/research_kernel_v{NNN}.json`` alongside
  ``proposal_v{NNN}.{json,md}``. NNN matches
  ``runs.proposal_version`` (codex round-5 amendment 3).
- ``apply_kernel_edit`` — three-branch dispatcher per codex round-1
  amendment 1:
    1. pre-proposal (proposal_version == 0): DB only, no file I/O.
    2. proposal-exists, no downstream completed: route through
       ``save_proposal_version(replace=True)`` → snapshot
       overwritten in place, no version bump.
    3. downstream completed: route through
       ``save_proposal_version(replace=False)`` → bump version,
       clone proposal artifact + write new kernel snapshot,
       mark stale on every non-deleted branch with completed
       downstream output.

Concurrency: callers acquire the ``research_kernel_edit`` short-
lived edit lock via ``phase_lock.claim_phase_lock`` (codex round-2
amendment 2 — closes the TOCTOU between state check and commit).

Codex consensus rounds for this sub-PR: 3 (architectural →
sharpening → implementation refinement). Final design has the 3
amendments from round-3 folded:

- ``GET /api/runs/{id}`` exposes paper_mode + kernel + hash so
  reload-safe editing works (not hash alone).
- Stale propagation walks ``PHASES`` manually (proposal isn't in
  ``PHASES``); mirrors existing proposal-save scan.
- Run.paper_mode + Run.research_kernel_json are assigned BEFORE
  ``save_proposal_version`` so the snapshot captures edited state.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.models import Branch, PhaseVersion, RunHead

# ---------------------------------------------------------------------------
# Hash + normalization (concurrency token)
# ---------------------------------------------------------------------------


def normalize_kernel_for_hash(kernel: Mapping[str, Any]) -> str:
    """Canonical JSON of a kernel object for hashing.

    Codex round-3 answer 1: only canonicalize JSON-level whitespace
    (dict key order, inter-token spacing). Do NOT collapse
    whitespace inside string values — that would lose user intent.
    """
    return json.dumps(
        kernel,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compute_kernel_hash(paper_mode: str, kernel: Mapping[str, Any]) -> str:
    """SHA-256 hex of {paper_mode, kernel}.

    Used by PUT /api/runs/{id}/research_kernel for lost-update
    detection. Two concurrent edits with the same
    base_proposal_version can both succeed in pre-proposal /
    no-downstream cases, which is why proposal_version alone is
    insufficient (codex round-2 amendment 1).
    """
    payload = json.dumps(
        {"paper_mode": paper_mode, "kernel": kernel},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Snapshot file writer
# ---------------------------------------------------------------------------


def kernel_snapshot_path(run_dir: Path, proposal_version: int) -> Path:
    """Standard path for the kernel snapshot file.

    ``NNN`` is ``runs.proposal_version`` zero-padded to 3 digits,
    matching the existing proposal_v{NNN}.{json,md} naming.
    """
    return run_dir / "proposal" / f"research_kernel_v{proposal_version:03d}.json"


def write_kernel_snapshot(
    run_dir: Path,
    proposal_version: int,
    paper_mode: str,
    kernel: Mapping[str, Any],
    *,
    timestamp_utc: str,
) -> Path:
    """Atomically write the kernel snapshot file.

    Codex round-2 answer 1: file-first ordering — caller must
    write the file BEFORE committing the DB so a snapshot-write
    failure aborts the DB commit (no DB-ahead-of-files state).
    Caller is responsible for the orchestration; this function
    only does the atomic write.

    Idempotent: rerunning with the same proposal_version overwrites
    in place. Used by both replace mode (overwrite v{current}) and
    new mode (write v{current+1} after the proposal artifact's
    version bump).

    Returns the path written.
    """
    target = kernel_snapshot_path(run_dir, proposal_version)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "proposal_version": proposal_version,
        "paper_mode": paper_mode,
        "kernel_schema_version": int(kernel.get("kernel_schema_version", 1) or 1),
        "timestamp_utc": timestamp_utc,
        "kernel": dict(kernel),
    }

    # Atomic write: tempfile in same directory, then os.replace().
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(target.parent),
        prefix=".research_kernel.",
        suffix=".tmp",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name

    os.replace(tmp_name, target)
    return target


# ---------------------------------------------------------------------------
# Stale propagation
# ---------------------------------------------------------------------------

# These are the pipeline phases in registry order. proposal is NOT
# in this list (codex round-3 amendment 2: existing proposal-save
# manual scan pattern). Kept in sync with main.py::_PHASE_RUNNERS.
_PIPELINE_PHASES = (
    "scout",
    "curator",
    "synthesizer",
    # PR-C2.a: optional framework_lens phase between synthesizer
    # and ideator. Stale-propagation walks every pipeline phase
    # in registry order so a kernel edit triggers re-running here
    # too.
    "framework_lens",
    "ideator",
    "drafter",
    "stylist",
    "critic",
    "integrity",
    "exports",
)


def _earliest_completed_phase_for_branch(
    session: Session,
    run_id: str,
    branch_id: str,
) -> str | None:
    """Find the earliest pipeline phase with a completed head on
    ``branch_id``. Returns ``None`` if no pipeline phase has run yet
    on this branch."""
    for candidate in _PIPELINE_PHASES:
        head = session.scalar(
            select(RunHead.version_id)
            .where(RunHead.run_id == run_id)
            .where(RunHead.branch_id == branch_id)
            .where(RunHead.phase == candidate)
            .limit(1),
        )
        if head is None:
            continue
        # Confirm the pv it points at is `done` (not failed mid-run).
        pv_status = session.scalar(
            select(PhaseVersion.status).where(PhaseVersion.id == head),
        )
        if pv_status == "done":
            return candidate
    return None


def stale_marks_after_kernel_edit(
    session: Session,
    run_id: str,
) -> list[tuple[str, str]]:
    """Compute (branch_id, earliest_completed_phase) pairs that
    should be staled after a kernel edit, across every non-deleted
    branch (codex round-2 amendment 4: kernel + paper_mode are
    run-level fields, so all branches with completed downstream
    work are affected).

    Returns the list; caller applies the marks via existing
    ``set_branch_stale`` machinery.
    """
    branches = list(
        session.scalars(
            select(Branch).where(Branch.run_id == run_id).where(Branch.deleted_at.is_(None)),
        ),
    )
    marks: list[tuple[str, str]] = []
    for branch in branches:
        earliest = _earliest_completed_phase_for_branch(session, run_id, branch.id)
        if earliest is not None:
            marks.append((branch.id, earliest))
    return marks


def has_any_pipeline_completion(session: Session, run_id: str) -> bool:
    """``True`` iff at least one non-deleted branch has at least
    one pipeline phase with a completed head. Used by
    ``apply_kernel_edit`` to decide between the
    no-downstream-completed and downstream-completed branches."""
    return len(stale_marks_after_kernel_edit(session, run_id)) > 0
