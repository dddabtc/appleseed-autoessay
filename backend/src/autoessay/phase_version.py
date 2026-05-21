"""Persistent phase version history (codex-AGREEd #2 stage 2.A).

Every rerun is wrapped in a transactional record-keeping flow:

1. Capture the current ``run_head`` for the phase (if any) and copy
   every owned legacy file into a per-pv "prerun backup" staging
   area. The backup is the rollback target on failure — it works
   whether or not a prior pv exists.
2. INSERT a new ``phase_version`` row with status=running.
3. Run the agent — it writes to legacy paths exactly as before.
4. On success: archive a FULL snapshot of every owned legacy file
   under ``runs/<run>/phases/<pv>/...`` (NOT a delta — restore must
   be self-contained), INSERT artifact rows, mark the new version
   ``done``, supersede the prior version, flip ``run_head``, and
   delete the prerun backup.
5. On agent-detected failure (run state ends in a graceful failure
   state, or the runner raised): mark new version ``failed`` /
   ``cancelled``, purge the phase's owned legacy files, and restore
   from the prerun backup. The prior pv (if any) is left untouched
   as the still-active head.

Per-phase ownership (:data:`PHASE_OWNERSHIP`) is explicit. Drafter and
stylist both write under ``drafts/<v>/`` but to disjoint sub-paths;
the ``exclude_under`` field lets one phase's roots subtract another
phase's sub-paths. Files outside any phase's ownership are not tracked.

``parent_pv_id`` reflects the run_head at begin time. Activating an
older version and then re-running creates a new pv whose parent is the
re-activated version, so the chain becomes a tree, not a line — this
is intentional and lets users branch back to a known-good state. The
totally-ordered ``version_no`` (per run+phase) remains the canonical
ordering. True branch labels come in stage 2.C.

Stage 2.A only versions phases on rerun, not on the original first
run via the normal start endpoints. After the first rerun, run_head
is set and full versioning kicks in. The prerun backup ensures that
even a failed first rerun does not destroy the existing legacy output.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Delete

from autoessay.models import (
    Branch,
    PhaseArtifact,
    PhaseVersion,
    PhaseVersionInput,
    PhaseVersionPrompt,
    Run,
    RunHead,
    utcnow,
)
from autoessay.state_machine import RunCancelled

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Ownership:
    """Specifies the rel-paths a phase owns inside ``run_dir``.

    A file at rel-path ``p`` is owned by this phase iff:
    * some ``root`` in :attr:`roots` is a prefix of ``p`` (or equals
      ``p`` for a file root), AND
    * no ``ex`` in :attr:`exclude_under` is a prefix of ``p``.

    Roots and excludes both support a single ``*`` wildcard per path
    segment (no ``**``); see :func:`_segments_match`.
    """

    roots: tuple[str, ...]
    exclude_under: tuple[str, ...] = ()


#: Per-phase file ownership. Each phase walks its roots, excluding any
#: sub-paths owned by another phase. Drafter and stylist both write
#: under ``drafts/<v>/`` so drafter's roots subtract stylist's roots.
PHASE_OWNERSHIP: dict[str, _Ownership] = {
    "proposal": _Ownership(roots=("proposal",)),
    "scout": _Ownership(roots=("discovery",)),
    "curator": _Ownership(
        roots=("sources",),
        exclude_under=(
            "sources/uploads",
            "sources/user_upload_manifest.json",
            "sources/user_upload_sources.json",
        ),
    ),
    # PR-C2.a + PR-C3.a: synthesizer keeps ownership of ``synthesis/``
    # but the framework_lens.json + tension_extraction.json files
    # inside it are owned by their respective phases — exclude them
    # from synthesizer's versioning so re-running synthesizer doesn't
    # double-snapshot them. (Codex round-2 amendment 2: tension owns
    # tension_extraction.json as the single writer; synthesizer never
    # mutates it.)
    "synthesizer": _Ownership(
        roots=("synthesis",),
        exclude_under=(
            "synthesis/framework_lens.json",
            "synthesis/tension_extraction.json",
        ),
    ),
    # PR-C2.a: framework_lens phase. Owns just the single file
    # (a non-glob root). exclude_under not needed since the file
    # is the only thing under this phase's surface.
    "framework_lens": _Ownership(roots=("synthesis/framework_lens.json",)),
    # PR-C3.a: tension_extraction phase. Owns just the single artifact.
    "tension_extraction": _Ownership(roots=("synthesis/tension_extraction.json",)),
    "ideator": _Ownership(roots=("novelty",)),
    "drafter": _Ownership(roots=("drafts",), exclude_under=("drafts/*/style",)),
    "stylist": _Ownership(roots=("drafts/*/style",)),
    "critic": _Ownership(roots=("reviews",)),
    "integrity": _Ownership(roots=("integrity",)),
    "exports": _Ownership(roots=("exports",)),
}

#: Backwards-compatible alias used by older imports / tests.
PHASE_LEGACY_DIRS: dict[str, tuple[str, ...]] = {
    phase: tuple(part.split("/", 1)[0] for part in spec.roots)
    for phase, spec in PHASE_OWNERSHIP.items()
}


def _segments_match(pattern: str, path: str) -> bool:
    """Return True iff ``pattern`` matches ``path`` segment-by-segment.

    Each segment in ``pattern`` is either a literal or a single ``*``
    wildcard matching any non-empty segment. ``pattern`` matches if it
    is equal to ``path`` OR it is a prefix of ``path`` (segment-wise).
    """
    pat_parts = pattern.split("/")
    path_parts = path.split("/")
    if len(pat_parts) > len(path_parts):
        return False
    for pp, ap in zip(pat_parts, path_parts, strict=False):
        if pp == "*":
            continue
        if pp != ap:
            return False
    return True


def _is_owned(phase: str, rel_path: str) -> bool:
    spec = PHASE_OWNERSHIP.get(phase)
    if spec is None:
        return False
    if not any(_segments_match(root, rel_path) for root in spec.roots):
        return False
    return not any(_segments_match(ex, rel_path) for ex in spec.exclude_under)


def _enumerate_owned_files(run_dir: Path, phase: str) -> Iterable[Path]:
    """Yield every file under ``run_dir`` that ``phase`` owns.

    Walks the ownership roots (resolved against the actual filesystem,
    expanding ``*`` against present subdirs) and yields every regular
    file whose rel-path matches the phase's ownership predicate. The
    per-pv archive area (``phases/...``) is never walked.
    """
    spec = PHASE_OWNERSHIP.get(phase)
    if spec is None:
        return
    yielded: set[Path] = set()
    for root in spec.roots:
        for resolved in _resolve_root(run_dir, root):
            if not resolved.exists():
                continue
            if resolved.is_file():
                if resolved in yielded:
                    continue
                rel = str(resolved.relative_to(run_dir)).replace("\\", "/")
                if _is_owned(phase, rel):
                    yielded.add(resolved)
                    yield resolved
                continue
            for path in resolved.rglob("*"):
                if not path.is_file() or path.name.startswith("."):
                    continue
                if path in yielded:
                    continue
                rel = str(path.relative_to(run_dir)).replace("\\", "/")
                if _is_owned(phase, rel):
                    yielded.add(path)
                    yield path


def _resolve_root(run_dir: Path, root: str) -> Iterable[Path]:
    """Yield filesystem paths for ``root`` under ``run_dir``.

    Expands a single ``*`` segment against the actual directory
    listing. Multiple ``*`` segments are not supported; the dataset is
    flat enough that one wildcard per root suffices today.
    """
    if "*" not in root:
        yield run_dir / root
        return
    parts = root.split("/")
    star_idx = parts.index("*")
    parent = run_dir / "/".join(parts[:star_idx]) if star_idx > 0 else run_dir
    if not parent.exists():
        return
    for child in sorted(parent.iterdir()):
        if not child.is_dir():
            continue
        tail = "/".join(parts[star_idx + 1 :])
        yield (child / tail) if tail else child


def _hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _prerun_backup_dir(run_dir: Path, pv_id: str) -> Path:
    return run_dir / "phases" / "_prerun" / pv_id


def _capture_prerun_backup(run_dir: Path, phase: str, pv_id: str) -> None:
    """Copy every owned file into the per-pv prerun backup area.

    Used as the rollback source for :func:`fail_phase_version`. Works
    whether a prior pv exists or not.
    """
    backup = _prerun_backup_dir(run_dir, pv_id)
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True, exist_ok=True)
    for source in _enumerate_owned_files(run_dir, phase):
        rel = source.relative_to(run_dir)
        target = backup / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _restore_from_prerun_backup(run_dir: Path, pv_id: str) -> None:
    """Copy every file in the prerun backup back to its legacy path."""
    backup = _prerun_backup_dir(run_dir, pv_id)
    if not backup.exists():
        return
    for source in backup.rglob("*"):
        if not source.is_file():
            continue
        rel = source.relative_to(backup)
        target = run_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _drop_prerun_backup(run_dir: Path, pv_id: str) -> None:
    backup = _prerun_backup_dir(run_dir, pv_id)
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def _purge_owned_files(run_dir: Path, phase: str) -> None:
    """Delete every owned legacy file for ``phase`` and clean up
    empty parent dirs.

    Empty-dir cleanup matters for drafter/stylist: code like
    ``drafter.py`` picks the highest-numbered ``drafts/v00N`` dir as
    "latest". If we leave an empty ``drafts/v002/`` behind after
    activating ``v001``, readers will still resolve to ``v002``. The
    cleanup walks up only inside the phase's roots and never deletes
    the top-level root itself or per-pv archive areas.
    """
    spec = PHASE_OWNERSHIP.get(phase)
    if spec is None:
        return
    archive_root = run_dir / "phases"
    root_paths = {run_dir / part for part in PHASE_LEGACY_DIRS.get(phase, ())}
    parents: set[Path] = set()
    for path in _enumerate_owned_files(run_dir, phase):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        for parent in path.parents:
            if parent in (run_dir, archive_root) or parent in root_paths:
                break
            try:
                parent.relative_to(archive_root)
                continue  # never touch anything inside phases/
            except ValueError:
                pass
            parents.add(parent)
    # Remove deepest parents first so leaf empties get rmdir-ed before
    # their grandparents are checked.
    for parent in sorted(parents, key=lambda p: -len(p.parts)):
        if not parent.exists():
            continue
        # Not-empty (other phase's files still inside) is OSError —
        # the suppress means "leave the dir alone in that case".
        with contextlib.suppress(OSError):
            parent.rmdir()


def _resolve_branch_id(session: Session, run: Run, branch_id: str | None) -> str:
    """Pick the branch id to use for run_heads / drafts. Defaults to
    the run's active branch and creates a main branch on demand for
    legacy/test runs that don't yet have one (codex-AGREEd #2 stage 2.C)."""
    from autoessay.branches import ensure_main_branch

    if branch_id is not None:
        return branch_id
    if run.active_branch_id is not None:
        return run.active_branch_id
    branch = ensure_main_branch(session, run)
    return branch.id


def compute_input_snapshot_hash(
    session: Session,
    run_id: str,
    phase: str,
    *,
    prompt_hash: str | None = None,
    branch_id: str | None = None,
) -> str:
    """Hash the upstream pv ids on this branch, plus prompt hash.

    The lookup is now branch-scoped: branches each have their own
    ``run_heads`` rows, so an upstream rerun on branch B does not
    accidentally bleed into branch A's input identity.
    """
    from autoessay.phase_rerun import PHASES

    upstream_ids: list[str] = []
    if phase in PHASES:
        idx = PHASES.index(phase)
        for upstream in PHASES[:idx]:
            stmt = (
                select(RunHead.version_id)
                .where(RunHead.run_id == run_id)
                .where(RunHead.phase == upstream)
            )
            if branch_id is not None:
                stmt = stmt.where(RunHead.branch_id == branch_id)
            head = session.scalar(stmt)
            if head:
                upstream_ids.append(head)
    digest = hashlib.sha256()
    digest.update("|".join(upstream_ids).encode())
    if prompt_hash:
        digest.update(b"|prompt=")
        digest.update(prompt_hash.encode())
    return digest.hexdigest()


def get_run_head(
    session: Session, run_id: str, phase: str, *, branch_id: str | None = None
) -> str | None:
    """Return the active pv id for ``(run, phase)`` on a given branch.

    If ``branch_id`` is None, this still returns SOMETHING — but it
    might pick an arbitrary branch's row. Callers that care about
    branch isolation must always pass ``branch_id`` (e.g., the rerun
    endpoint passes the active branch id explicitly).
    """
    stmt = select(RunHead.version_id).where(RunHead.run_id == run_id).where(RunHead.phase == phase)
    if branch_id is not None:
        stmt = stmt.where(RunHead.branch_id == branch_id)
    return session.scalar(stmt)


def _next_version_no(session: Session, run_id: str, phase: str) -> int:
    rows = session.scalars(
        select(PhaseVersion.version_no)
        .where(PhaseVersion.run_id == run_id)
        .where(PhaseVersion.phase == phase)
        .order_by(PhaseVersion.version_no.desc())
        .limit(1),
    ).all()
    return (rows[0] + 1) if rows else 1


@dataclass(frozen=True)
class ResolvedPrompt:
    """One resolved prompt surface for a phase invocation.

    Built by the rerun endpoint: looks up the phase's spec, then picks
    either the user's draft override (source="override") or the
    registry default (source="default"). Persisted by
    :func:`begin_phase_version` into ``phase_version_prompts``.
    """

    prompt_key: str
    source: str
    content: str
    content_hash: str
    template_id: str | None


def _combined_prompt_hash(prompts: list[ResolvedPrompt]) -> str | None:
    """Stable hash across every prompt surface for a phase, used as
    the ``prompt_hash`` column on ``phase_versions`` and as input to
    ``compute_input_snapshot_hash``. ``None`` if no surfaces."""
    if not prompts:
        return None
    digest = hashlib.sha256()
    for p in sorted(prompts, key=lambda r: r.prompt_key):
        digest.update(p.prompt_key.encode())
        digest.update(b"=")
        digest.update(p.content_hash.encode())
        digest.update(b"|")
    return digest.hexdigest()


def begin_phase_version(
    session: Session,
    run: Run,
    phase: str,
    *,
    created_by: str | None = None,
    source: str = "agent",
    prompts: list[ResolvedPrompt] | None = None,
    branch_id: str | None = None,
) -> tuple[PhaseVersion, str | None]:
    """INSERT a new phase_version with status=running.

    The new version is tagged with ``created_on_branch_id`` and its
    explicit upstream lineage (the branch's run_heads at begin time)
    is recorded in ``phase_version_inputs``. Without this, a
    downstream pv on branch B could later silently inherit branch A's
    upstream via the ambient run_head pointer (codex round-1 #2 stage
    2.C concern).

    Captures a prerun backup of every owned legacy file. The backup
    is the unconditional rollback target on failure, so a missing
    prior pv still preserves the pre-existing legacy output untouched.

    ``source`` defaults to ``'agent'`` — every existing caller is an
    agent invocation. PR-A2's user-edit endpoints will pass
    ``source='user_edit'`` to mark inline-edited versions in the
    history modal.
    """
    from autoessay.models import PhaseVersionInput
    from autoessay.phase_rerun import PHASES

    branch_id = _resolve_branch_id(session, run, branch_id)
    prior = get_run_head(session, run.id, phase, branch_id=branch_id)
    pv_id = f"pv_{uuid4().hex}"
    prompt_list = prompts or []
    prompt_hash = _combined_prompt_hash(prompt_list)
    pv = PhaseVersion(
        id=pv_id,
        run_id=run.id,
        phase=phase,
        version_no=_next_version_no(session, run.id, phase),
        parent_pv_id=prior,
        status="running",
        artifacts_dir=f"phases/{pv_id}",
        input_snapshot_hash=compute_input_snapshot_hash(
            session, run.id, phase, prompt_hash=prompt_hash, branch_id=branch_id
        ),
        prompt_hash=prompt_hash,
        created_on_branch_id=branch_id,
        created_by=created_by,
        source=source,
    )
    session.add(pv)
    session.flush()
    # Record explicit upstream lineage (codex non-negotiable).
    if phase in PHASES:
        idx = PHASES.index(phase)
        for upstream_phase in PHASES[:idx]:
            upstream_pv_id = get_run_head(session, run.id, upstream_phase, branch_id=branch_id)
            if upstream_pv_id is not None:
                session.add(
                    PhaseVersionInput(
                        phase_version_id=pv_id,
                        upstream_phase=upstream_phase,
                        upstream_pv_id=upstream_pv_id,
                    )
                )
    for resolved in prompt_list:
        session.add(
            PhaseVersionPrompt(
                phase_version_id=pv_id,
                prompt_key=resolved.prompt_key,
                phase=phase,
                source=resolved.source,
                content=resolved.content,
                content_hash=resolved.content_hash,
                template_id=resolved.template_id,
            )
        )
    session.flush()
    _capture_prerun_backup(Path(run.run_dir), phase, pv_id)
    return pv, prior


def commit_phase_version(
    session: Session,
    run: Run,
    pv: PhaseVersion,
    *,
    branch_id: str | None = None,
) -> list[PhaseArtifact]:
    """Archive a FULL snapshot of the phase's owned files, flip the
    branch's head (codex-AGREEd #2 stage 2.C: head is per branch).

    Stage 2.A used to mark the prior pv as ``superseded``; codex
    round-1 review (#2 stage 2.C) flagged that as wrong with
    branches: a pv that's the head of branch A shouldn't be marked
    superseded just because branch B's head moved. ``status`` stays
    ``done`` for every successful pv; activeness is determined
    entirely by the per-branch ``run_heads`` row.
    """
    branch_id = _resolve_branch_id(session, run, branch_id)
    run_dir = Path(run.run_dir)
    archive_root = run_dir / pv.artifacts_dir
    archive_root.mkdir(parents=True, exist_ok=True)
    archived: list[PhaseArtifact] = []
    for source in _enumerate_owned_files(run_dir, pv.phase):
        rel = source.relative_to(run_dir)
        rel_str = str(rel).replace("\\", "/")
        target = archive_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        sha, size = _hash_and_size(target)
        artifact = PhaseArtifact(
            id=f"art_{uuid4().hex}",
            phase_version_id=pv.id,
            kind=rel_str.split("/", 1)[0] if "/" in rel_str else rel_str,
            logical_path=rel_str,
            blob_path=str(target.relative_to(run_dir)).replace("\\", "/"),
            sha256=sha,
            size_bytes=size,
        )
        session.add(artifact)
        archived.append(artifact)
    pv.status = "done"
    pv.completed_at = utcnow()
    head = session.scalar(
        select(RunHead)
        .where(RunHead.run_id == run.id)
        .where(RunHead.branch_id == branch_id)
        .where(RunHead.phase == pv.phase),
    )
    if head is None:
        session.add(
            RunHead(
                run_id=run.id,
                branch_id=branch_id,
                phase=pv.phase,
                version_id=pv.id,
            )
        )
    else:
        head.version_id = pv.id
        head.updated_at = utcnow()
    _drop_prerun_backup(run_dir, pv.id)
    session.flush()

    # PR-A4.3 codex round-2 review (2026-05-02): a successful
    # rerun changes the upstream vector for downstream phases.
    # Per rule 2 / rule 5 of the codex-AGREEd version model, the
    # downstream RunHeads must be re-evaluated against the new
    # upstream vector — the same cascade that ``activate_version``
    # performs. Without this, rerunning scout leaves curator/
    # synthesizer/... heads on pvs whose recorded lineage no
    # longer matches scout's head, and the modal would show them
    # as ``lineage_dirty=true`` indefinitely without an automatic
    # restore path.
    from autoessay.phase_rerun import PHASES

    if pv.phase in PHASES and PHASES.index(pv.phase) + 1 < len(PHASES):
        for downstream_phase in PHASES[PHASES.index(pv.phase) + 1 :]:
            _cascade_phase_after_upstream_change(
                session,
                run,
                downstream_phase,
                branch_id,
            )
        # PR-A4.3 codex round-3 (2026-05-02): rematerialize legacy
        # files for the cascade-touched DOWNSTREAM phases so disk-
        # backed bundle endpoints (``/sources``, ``/synthesis``,
        # ...) reflect the new head set. Without this, head was
        # deleted but old artifacts stayed on disk, violating
        # "head missing == ungenerated" from the user's view.
        #
        # Important: only materialize downstream phases. The full-
        # branch ``materialize_branch_legacy_paths`` is too
        # aggressive here because it would purge upstream phases
        # that have on-disk artifacts but no RunHead row
        # (pre-migration vanilla runs, test fixtures that seed
        # files without pv rows). Those orphan files must
        # survive — only the just-now-cascaded downstream gets
        # cleaned up.
        run_dir = Path(run.run_dir)
        for downstream_phase in PHASES[PHASES.index(pv.phase) + 1 :]:
            _purge_owned_files(run_dir, downstream_phase)
            head_pv_id = session.scalar(
                select(RunHead.version_id)
                .where(RunHead.run_id == run.id)
                .where(RunHead.branch_id == branch_id)
                .where(RunHead.phase == downstream_phase),
            )
            if head_pv_id is not None:
                _restore_legacy_paths_from(session, run, head_pv_id)
        session.flush()
    return archived


def is_pv_branch_exclusive(session: Session, pv_id: str, branch_id: str) -> bool:
    """``True`` iff overwriting ``pv_id`` in place will not leak into
    any other branch.

    A pv is shared (NOT exclusive) when ANY of the following holds:

    - Another branch's ``run_heads`` row points at it (the same pv is
      currently active on a different branch).
    - Another branch's ``forked_from_pv_id`` references it (a fork
      point that future branches still depend on).
    - Another branch was *created on* this pv (``created_on_branch_id``
      values pointing to children of this pv) — covered indirectly
      because every such child pv has its own row, not via this pv.

    Codex AGREE 2026-05-01 amendment 7: replace mode must check
    exclusivity before overwriting the on-disk archive at
    ``phases/<pv_id>/`` because the same archive is the canonical
    snapshot for every branch that resolves to this pv.
    """
    other_head = session.scalar(
        select(RunHead.branch_id)
        .where(RunHead.version_id == pv_id)
        .where(RunHead.branch_id != branch_id)
        .limit(1),
    )
    if other_head is not None:
        return False
    other_fork = session.scalar(
        select(Branch.id)
        .where(Branch.forked_from_pv_id == pv_id)
        .where(Branch.deleted_at.is_(None))
        .limit(1),
    )
    return other_fork is None


def replace_phase_version(
    session: Session,
    run: Run,
    pv: PhaseVersion,
    *,
    branch_id: str | None = None,
    user_id: str | None = None,
) -> list[PhaseArtifact]:
    """Re-archive the new on-disk content into the existing pv's
    ``artifacts_dir`` and refresh ``phase_artifacts`` rows in place.

    Pre-conditions (caller-enforced; we assert here as a defense):

    - The legacy paths under ``run.run_dir`` already hold the NEW
      content the user wants archived.
    - ``pv`` is the current head of ``branch_id`` (or the active
      branch if ``branch_id`` is None).
    - :func:`is_pv_branch_exclusive` returned ``True`` for ``pv.id``.
    - The caller holds the ``phase_lock`` for ``pv.phase`` so a
      concurrent agent run cannot interleave.

    Atomicity: writes the new archive into a sibling temp directory,
    deletes prior ``PhaseArtifact`` rows + inserts new ones in the
    same flush, then atomically swaps the temp dir into place. On
    exception the partial temp dir is removed and the DB rollback
    handled by the caller's session boundary leaves the old archive
    + old artifact rows intact.

    If ``pv.source`` was ``'agent'``, bumps it to ``'user_edit'`` so
    the history modal stops labeling the row as agent output (codex
    amendment 4).
    """
    branch_id = _resolve_branch_id(session, run, branch_id)
    run_dir = Path(run.run_dir)
    archive_root = run_dir / pv.artifacts_dir
    temp_root = archive_root.with_suffix(archive_root.suffix + ".replace_tmp")
    if temp_root.exists():
        shutil.rmtree(temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    new_artifacts: list[PhaseArtifact] = []
    try:
        for source in _enumerate_owned_files(run_dir, pv.phase):
            rel = source.relative_to(run_dir)
            rel_str = str(rel).replace("\\", "/")
            target = temp_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            sha, size = _hash_and_size(target)
            new_artifacts.append(
                PhaseArtifact(
                    id=f"art_{uuid4().hex}",
                    phase_version_id=pv.id,
                    kind=rel_str.split("/", 1)[0] if "/" in rel_str else rel_str,
                    logical_path=rel_str,
                    # blob_path will be rewritten after rename, see
                    # below — we cannot compute the final path until
                    # we know the swap succeeded.
                    blob_path=str(target.relative_to(run_dir)).replace("\\", "/"),
                    sha256=sha,
                    size_bytes=size,
                ),
            )
        # Refresh artifact rows for this pv: drop the old rows, add
        # the new rows. Done before the rename so a session rollback
        # leaves the archive on disk in old-state and the rows in
        # old-state. Codex amendment 2.
        session.execute(
            _delete_phase_artifact_for_pv(pv.id),
        )
        for art in new_artifacts:
            session.add(art)
        session.flush()
        # Atomic swap: move old archive aside, rename temp into
        # place, then drop the old. shutil.move within the same
        # filesystem is atomic enough for our purposes; on the same
        # device it uses ``os.rename``.
        backup_root = archive_root.with_suffix(archive_root.suffix + ".old")
        if backup_root.exists():
            shutil.rmtree(backup_root)
        if archive_root.exists():
            archive_root.rename(backup_root)
        temp_root.rename(archive_root)
        # Now rewrite blob_path on each row to point at the swapped-
        # in archive (the temp_root path no longer exists).
        for art in new_artifacts:
            tail = art.blob_path
            # ``tail`` was built relative to run_dir using temp_root;
            # strip the ``.replace_tmp`` suffix so it now points at
            # archive_root.
            art.blob_path = tail.replace(
                temp_root.relative_to(run_dir).as_posix(),
                archive_root.relative_to(run_dir).as_posix(),
                1,
            )
        session.flush()
        if backup_root.exists():
            shutil.rmtree(backup_root)
    except Exception:
        if temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)
        raise
    if pv.source != "user_edit":
        pv.source = "user_edit"
    if user_id is not None:
        pv.created_by = user_id
    session.flush()
    return new_artifacts


def _delete_phase_artifact_for_pv(pv_id: str) -> Delete:
    """sqlalchemy DELETE for a pv's artifacts. Returns a Core
    statement so the caller can wrap in ``session.execute(...)``."""
    from sqlalchemy import delete as _delete

    return _delete(PhaseArtifact).where(PhaseArtifact.phase_version_id == pv_id)


def fail_phase_version(
    session: Session,
    run: Run,
    pv: PhaseVersion,
    prior_active_pv_id: str | None,
    *,
    cancelled: bool = False,
) -> None:
    """Mark the version failed/cancelled and roll back legacy paths.

    When ``prior_active_pv_id`` is set, purges the failed pv's
    writes and restores the prior pv's content from the prerun
    backup. When prior is None (vanilla first run wrapped by
    ``maybe_run_with_versioning`` per PR-A4.1b 2026-05-02), there
    is no good state to restore TO — the agent's writes ARE the
    only artifacts, often the user's only debugging evidence (e.g.
    ``discovery/warnings.jsonl``). Skip purge/restore in that
    case; the pv row records the failure status, the files
    survive on disk for inspection.

    The prior pv stays as the active head; the failed pv is
    recorded but never gets run_head.
    """
    pv.status = "cancelled" if cancelled else "failed"
    pv.completed_at = utcnow()
    run_dir = Path(run.run_dir)
    if prior_active_pv_id is not None:
        _purge_owned_files(run_dir, pv.phase)
        _restore_from_prerun_backup(run_dir, pv.id)
    _drop_prerun_backup(run_dir, pv.id)
    session.flush()


def _restore_legacy_paths_from(session: Session, run: Run, source_pv_id: str) -> None:
    """Copy every artifact of ``source_pv_id`` back into its legacy
    path. Caller is responsible for purging stale files first."""
    run_dir = Path(run.run_dir)
    rows = session.scalars(
        select(PhaseArtifact).where(PhaseArtifact.phase_version_id == source_pv_id),
    ).all()
    for art in rows:
        blob = run_dir / art.blob_path
        if not blob.exists():
            continue
        target = run_dir / art.logical_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blob, target)


def activate_version(
    session: Session,
    run: Run,
    phase: str,
    pv_id: str,
    *,
    branch_id: str | None = None,
) -> PhaseVersion:
    """Switch the branch's ``run_head`` to ``pv_id`` and cascade
    through every downstream phase per the rule-5 lineage match
    (codex AGREE 2026-05-02 amendment 2 to PR-A4).

    The cascade is the difference between this function and the
    pre-PR-A4 ``activate_version``: changing one phase's head
    invalidates downstream pvs whose recorded lineage references
    the old upstream pv. We walk pipeline order; for each
    downstream phase whose current head pv's full upstream
    vector no longer matches the current heads, we look for a
    same-branch pv whose lineage DOES match. If found we flip
    the RunHead; if not we DELETE the RunHead row entirely
    (``RunHead.version_id`` is NOT NULL, so we cannot represent
    "ungenerated" by setting it to None — the row's absence
    carries that meaning per codex amendment 2).

    Diamond case: when a downstream phase has multiple upstream
    phases (every pipeline phase except scout has two or more
    upstreams via lineage), we require ALL recorded
    ``upstream_pv_id`` values to match the corresponding current
    head pvs. A partial match still counts as lineage_dirty.

    After all RunHead updates, calls
    ``materialize_branch_legacy_paths`` to rewrite legacy
    artifacts on disk so subsequent reads see the new head set.

    Stage 2.C drops the global "superseded" status — a pv keeps
    ``status='done'`` regardless of which branch's head it is.
    The target pv must satisfy ``status='done'`` to be
    activatable.

    Codex round-1 visibility check: the caller is expected to
    verify the target pv is reachable from ``branch_id`` (e.g.,
    it was created on this branch or on an ancestor branch up
    to the fork point). This function only validates run/phase
    membership.
    """
    from autoessay.phase_rerun import PHASES

    pv = session.get(PhaseVersion, pv_id)
    if pv is None or pv.run_id != run.id or pv.phase != phase:
        raise ValueError("phase_version not found for this run/phase")
    if pv.status != "done":
        raise ValueError(f"cannot activate version with status={pv.status}; need 'done'")
    branch_id = _resolve_branch_id(session, run, branch_id)

    # Step 1: flip the activated phase's RunHead.
    head = session.scalar(
        select(RunHead)
        .where(RunHead.run_id == run.id)
        .where(RunHead.branch_id == branch_id)
        .where(RunHead.phase == phase),
    )
    if head is None:
        session.add(RunHead(run_id=run.id, branch_id=branch_id, phase=phase, version_id=pv_id))
    else:
        head.version_id = pv_id
        head.updated_at = utcnow()
    session.flush()

    # Step 2: cascade through downstream pipeline phases.
    if phase in PHASES:
        downstream_phases = PHASES[PHASES.index(phase) + 1 :]
        for downstream_phase in downstream_phases:
            _cascade_phase_after_upstream_change(
                session,
                run,
                downstream_phase,
                branch_id,
            )

    # Step 3: materialize the activated phase + every cascaded
    # downstream phase onto disk. Codex amendment 2 said
    # "materialize ALL changed phases"; we narrow that to "the
    # activated phase + downstream phases" so we don't disturb
    # upstream files (codex round-3 review 2026-05-02 — full-
    # branch materialize purged orphan-but-real upstream files
    # from pre-migration vanilla runs and test fixtures that
    # seed files without pv rows).
    run_dir = Path(run.run_dir)
    affected_phases = [phase]
    if phase in PHASES:
        affected_phases.extend(PHASES[PHASES.index(phase) + 1 :])
    for affected in affected_phases:
        _purge_owned_files(run_dir, affected)
        head_pv_id = session.scalar(
            select(RunHead.version_id)
            .where(RunHead.run_id == run.id)
            .where(RunHead.branch_id == branch_id)
            .where(RunHead.phase == affected),
        )
        if head_pv_id is not None:
            _restore_legacy_paths_from(session, run, head_pv_id)
    session.flush()
    return pv


def _cascade_phase_after_upstream_change(
    session: Session,
    run: Run,
    phase: str,
    branch_id: str,
) -> None:
    """Re-evaluate ``phase``'s RunHead given a now-current upstream
    head set. Walked once per downstream phase from
    :func:`activate_version`'s cascade.

    Algorithm:

    1. Compute the current upstream-vector dict ``{upstream_phase
       → current_head_pv_id}`` for every phase strictly upstream
       of ``phase``.
    2. If the existing head pv's recorded lineage matches the
       full vector, leave it alone.
    3. Else, scan every same-branch pv for ``phase`` (status
       'done') and pick the first whose recorded lineage matches
       the full vector.
    4. If found: flip RunHead. If not found: DELETE the RunHead
       row (codex amendment 2: ``RunHead.version_id`` is NOT
       NULL, absence == "ungenerated").
    """
    from autoessay.phase_rerun import PHASES

    if phase not in PHASES:
        return

    # Build current upstream vector for ``phase``.
    phase_idx = PHASES.index(phase)
    current_upstream: dict[str, str] = {}
    for upstream_phase in PHASES[:phase_idx]:
        up_head = session.scalar(
            select(RunHead.version_id)
            .where(RunHead.run_id == run.id)
            .where(RunHead.branch_id == branch_id)
            .where(RunHead.phase == upstream_phase),
        )
        if up_head is not None:
            current_upstream[upstream_phase] = up_head

    head_row = session.scalar(
        select(RunHead)
        .where(RunHead.run_id == run.id)
        .where(RunHead.branch_id == branch_id)
        .where(RunHead.phase == phase),
    )

    # Codex 2026-05-02 review: even when ``head_row is None``
    # (downstream is currently ungenerated, e.g. from an earlier
    # cascade that found no match), a NEW activation may bring
    # the upstream vector back to a state where a historical
    # candidate matches. Don't return early — always run the
    # candidate scan and restore a head if possible.

    if head_row is not None:
        # Fast path: current head's lineage already matches the
        # new upstream vector. Leave it alone.
        head_lineage = _lineage_dict(session, head_row.version_id)
        if _lineage_matches(head_lineage, current_upstream):
            return

    # Codex 2026-05-02 review: candidate scan must use the same
    # branch-reachability rule as activation visibility, not just
    # ``created_on_branch_id == branch_id``. A pv created on the
    # source branch before a fork is still reachable from the
    # fork via parent_pv_id, and the cascade should consider it.
    reachable = reachable_pv_ids_for_branch(session, run.id, phase, branch_id)
    if not reachable:
        # No candidates at all on this branch.
        if head_row is not None:
            session.delete(head_row)
            session.flush()
        return
    candidates = session.scalars(
        select(PhaseVersion)
        .where(PhaseVersion.id.in_(reachable))
        .where(PhaseVersion.status == "done")
        .order_by(PhaseVersion.version_no.desc()),
    ).all()
    matched: PhaseVersion | None = None
    for cand in candidates:
        cand_lineage = _lineage_dict(session, cand.id)
        if _lineage_matches(cand_lineage, current_upstream):
            matched = cand
            break

    if matched is not None:
        if head_row is None:
            # Restore a head row that was previously deleted.
            session.add(
                RunHead(
                    run_id=run.id,
                    branch_id=branch_id,
                    phase=phase,
                    version_id=matched.id,
                ),
            )
        else:
            head_row.version_id = matched.id
            head_row.updated_at = utcnow()
    elif head_row is not None:
        # No compatible pv → DELETE RunHead row entirely (codex
        # amendment 2: NOT NULL on version_id, absence is the
        # "ungenerated" signal).
        session.delete(head_row)
    session.flush()


def _lineage_dict(session: Session, pv_id: str) -> dict[str, str]:
    """Return ``{upstream_phase: upstream_pv_id}`` for the given pv."""
    rows = session.scalars(
        select(PhaseVersionInput).where(PhaseVersionInput.phase_version_id == pv_id),
    ).all()
    return {row.upstream_phase: row.upstream_pv_id for row in rows}


def _lineage_matches(
    lineage: dict[str, str],
    expected: dict[str, str],
) -> bool:
    """Diamond-case lineage check: full equality required.

    Codex 2026-05-02 amendment: previously this checked
    ``expected ⊆ lineage`` — too loose. A candidate with
    ``{scout: pv_x, curator: pv_old}`` would pass against
    ``expected = {scout: pv_x}`` (when curator's RunHead was
    absent), incorrectly accepting a candidate whose curator
    lineage is now stale. Require ``lineage == expected`` so
    extra entries in either side disqualify the candidate.

    If ``expected`` is empty (e.g. scout with no upstreams),
    only candidates with empty lineage pass.
    """
    return lineage == expected


def delete_phase_version(
    session: Session,
    run: Run,
    phase: str,
    pv_id: str,
) -> None:
    """Delete a phase_version + all of its child rows + on-disk
    archive (PR-A4.3, codex AGREE 2026-05-02 amendment 7).

    Rejects with ``ValueError`` (caller maps to 409) when the pv
    is still referenced by:

    - any ``run_heads`` row (active head on some branch — must
      activate a different version first)
    - any ``phase_version_inputs.upstream_pv_id`` (downstream pv
      lineage — caller must delete the downstream first per
      rule-4 reverse-dependency order)
    - any ``phase_versions.parent_pv_id`` (lineage child)
    - any ``branches.forked_from_pv_id`` INCLUDING soft-deleted
      branches (deleted branches' fork-point still pins
      historical reachability)

    On success deletes:

    1. ``phase_version_prompts`` rows
    2. ``phase_artifacts`` (artifacts_v2) rows
    3. ``phase_version_inputs`` rows owned by this pv
    4. The pv row itself
    5. The on-disk archive at ``run.run_dir / pv.artifacts_dir``

    Note: ``phase_versions`` has no ``deleted_at`` column, so
    "deleted" means "row gone". Callers should not rely on the
    pv being recoverable; record audit elsewhere (e.g.
    RunEvent) if needed.
    """
    pv = session.get(PhaseVersion, pv_id)
    if pv is None or pv.run_id != run.id or pv.phase != phase:
        raise ValueError("phase_version not found for this run/phase")

    # Reject conditions (FK-style).
    if session.scalar(
        select(RunHead.version_id).where(RunHead.version_id == pv_id).limit(1),
    ):
        raise ValueError(
            "version is the active head on some branch; activate a different version first",
        )
    referencing_input = session.scalar(
        select(PhaseVersionInput.phase_version_id)
        .where(PhaseVersionInput.upstream_pv_id == pv_id)
        .limit(1),
    )
    if referencing_input is not None:
        # Build a helpful message naming the dependent (one example).
        dep_pv = session.get(PhaseVersion, referencing_input)
        dep_label = (
            f"{dep_pv.phase} v{dep_pv.version_no}" if dep_pv is not None else "another version"
        )
        raise ValueError(
            f"version is referenced as upstream by {dep_label}; "
            "delete the downstream version first (reverse-dependency order)",
        )
    child_pv = session.scalar(
        select(PhaseVersion.id).where(PhaseVersion.parent_pv_id == pv_id).limit(1),
    )
    if child_pv is not None:
        raise ValueError(
            "version has a lineage child (parent_pv_id reference); delete the child first",
        )
    # Round-1 audit #16 (2026-05-03): replace mode (via
    # is_pv_branch_exclusive) only blocks on fork points held by
    # *non-deleted* branches. Delete used to also block on
    # soft-deleted branches' fork points, creating an asymmetry
    # where replace could mutate a pv that delete then refused to
    # remove. Soft-delete is permanent (no restore path), so the
    # fork-point of a deleted branch is a dead reference. Filter
    # the same way replace does so the two operations are
    # consistent.
    fork_branch = session.scalar(
        select(Branch.name)
        .where(Branch.forked_from_pv_id == pv_id)
        .where(Branch.deleted_at.is_(None))
        .limit(1),
    )
    if fork_branch is not None:
        raise ValueError(
            f"branch '{fork_branch}' was forked from this version; cannot delete",
        )

    # If any soft-deleted branch still references this pv as its
    # fork point, NULL out that reference so the FK doesn't reject
    # the row delete. The branch is already deleted_at != None so
    # users cannot fork further from it; losing the lineage pointer
    # is acceptable (the branch is dead).
    from sqlalchemy import update as _sql_update

    session.execute(
        _sql_update(Branch)
        .where(Branch.forked_from_pv_id == pv_id)
        .where(Branch.deleted_at.is_not(None))
        .values(forked_from_pv_id=None),
    )

    # Cascade delete child rows.
    session.execute(
        _delete_stmt_for_phase_version_prompts(pv_id),
    )
    session.execute(
        _delete_stmt_for_phase_artifact(pv_id),
    )
    session.execute(
        _delete_stmt_for_phase_version_inputs(pv_id),
    )

    # On-disk archive cleanup. Best-effort: missing dir is fine
    # (e.g. a backfilled v001 has no archive).
    archive = Path(run.run_dir) / pv.artifacts_dir
    if archive.exists():
        shutil.rmtree(archive, ignore_errors=True)

    session.delete(pv)
    session.flush()


def _delete_stmt_for_phase_version_prompts(pv_id: str) -> Delete:
    from sqlalchemy import delete as _delete

    return _delete(PhaseVersionPrompt).where(
        PhaseVersionPrompt.phase_version_id == pv_id,
    )


def _delete_stmt_for_phase_artifact(pv_id: str) -> Delete:
    from sqlalchemy import delete as _delete

    return _delete(PhaseArtifact).where(PhaseArtifact.phase_version_id == pv_id)


def _delete_stmt_for_phase_version_inputs(pv_id: str) -> Delete:
    from sqlalchemy import delete as _delete

    return _delete(PhaseVersionInput).where(
        PhaseVersionInput.phase_version_id == pv_id,
    )


def reachable_pv_ids_for_branch(
    session: Session, run_id: str, phase: str, branch_id: str
) -> set[str]:
    """Every pv id of ``phase`` reachable from ``branch_id``.

    Reachable means: created on this branch, OR currently the
    branch's head, OR in the ``parent_pv_id`` ancestor chain of any
    pv created on this branch, OR — for forked branches —
    the upstream pv recorded in ``branch.forked_from_pv_id``'s
    lineage for the requested phase (PR-A4.3 codex round-2 review
    2026-05-02).

    Without the fork-lineage seed, a forked branch loses access
    to the upstream heads it inherited from the fork pv after a
    cascade deletes them: the fork pv records lineage like
    ``{scout: scout_v1_id, curator: curator_v1_id, ...}``, but
    those upstream pvs were never "created on this branch" and
    aren't in the parent chain of any branch-local pv (the fork
    pv is the chain root). Seeding from
    ``branch.forked_from_pv_id``'s lineage fixes the
    "reactivate-restores-nothing" bug codex reproduced.

    Codex round-4 #2 stage 2.C: without the ancestor walk, the
    fork-base pv (created on the source branch) drops out of the
    branch's view as soon as the first divergent rerun happens (head
    moves to the new pv). The fork base is still ``parent_pv_id``
    of the new pv, so a parent walk recovers it.
    """
    seen: set[str] = set()
    head = get_run_head(session, run_id, phase, branch_id=branch_id)
    if head is not None:
        seen.add(head)
    on_branch = session.scalars(
        select(PhaseVersion.id)
        .where(PhaseVersion.run_id == run_id)
        .where(PhaseVersion.phase == phase)
        .where(PhaseVersion.created_on_branch_id == branch_id),
    ).all()
    seen.update(on_branch)

    # PR-A4.3 round-2: fork-inherited seed. If this branch was
    # forked from a downstream pv, that pv's lineage names the
    # upstream pv ids the fork inherited. For the requested
    # phase, pull that pv id into ``seen`` so the parent walk
    # below picks up its ancestors and so cascade restoration
    # can find this pv as a candidate.
    branch = session.get(Branch, branch_id)
    if branch is not None and branch.forked_from_pv_id is not None:
        fork_pv = session.get(PhaseVersion, branch.forked_from_pv_id)
        if fork_pv is not None:
            if fork_pv.phase == phase:
                # Forked from THIS phase's pv — seed it.
                seen.add(fork_pv.id)
            else:
                # Forked from a downstream pv — pull its
                # ``PhaseVersionInput`` row for the requested
                # upstream phase if recorded.
                for inp in session.scalars(
                    select(PhaseVersionInput).where(
                        PhaseVersionInput.phase_version_id == fork_pv.id,
                    ),
                ).all():
                    if inp.upstream_phase == phase:
                        seen.add(inp.upstream_pv_id)

    frontier = list(seen)
    while frontier:
        next_id = frontier.pop()
        pv = session.get(PhaseVersion, next_id)
        if pv is None or pv.parent_pv_id is None or pv.parent_pv_id in seen:
            continue
        # Only follow the chain within the same phase. parent_pv_id is
        # always same-phase by construction, but the explicit check
        # documents the invariant.
        parent = session.get(PhaseVersion, pv.parent_pv_id)
        if parent is None or parent.phase != phase:
            continue
        seen.add(parent.id)
        frontier.append(parent.id)
    return seen


def list_versions(
    session: Session,
    run_id: str,
    phase: str,
    *,
    branch_id: str | None = None,
) -> list[tuple[PhaseVersion, bool]]:
    """Every version of ``phase`` reachable from ``branch_id``, newest
    first. See :func:`reachable_pv_ids_for_branch` for the
    reachability rule. ``branch_id=None`` returns every version on
    the run (legacy behavior for tests/callers without branch
    context)."""
    head = get_run_head(session, run_id, phase, branch_id=branch_id)
    stmt = (
        select(PhaseVersion).where(PhaseVersion.run_id == run_id).where(PhaseVersion.phase == phase)
    )
    if branch_id is not None:
        reachable = reachable_pv_ids_for_branch(session, run_id, phase, branch_id)
        if not reachable:
            return []
        stmt = stmt.where(PhaseVersion.id.in_(reachable))
    stmt = stmt.order_by(PhaseVersion.version_no.desc())
    rows = session.scalars(stmt).all()
    return [(pv, pv.id == head) for pv in rows]


#: States the runner may leave the run in to mean "I bailed without
#: producing valid output". Multiple agents transition into one of
#: these without raising; without this set the corresponding pv would
#: commit as ``done`` and flip ``run_head`` to a half-written output.
_GRACEFUL_FAILURE_STATES: frozenset[str] = frozenset(
    {"FAILED_FIXABLE", "FAILED_VENDOR", "FAILED_POLICY"}
)


def run_with_versioning(
    session: Session,
    run: Run,
    phase: str,
    runner_call: Callable[[], None],
    *,
    created_by: str | None = None,
    prompts: list[ResolvedPrompt] | None = None,
    branch_id: str | None = None,
) -> PhaseVersion:
    """Wrap an agent invocation with the begin / commit / fail cycle.

    ``branch_id`` defaults to the run's active branch (codex-AGREEd
    #2 stage 2.C: per-branch heads). Three failure modes are handled:

    * ``RunCancelled`` raised → record cancelled, restore prerun.
    * Any other exception → PR-I2.b common failure boundary
      (codex Q3) — record pv failed, transition run to FAILED_FIXABLE,
      append a ``phase_failed`` event so the FailureResolutionBanner
      picks the run up; then re-raise so RQ records the worker
      failure too.
    * Runner returns normally but transitioned the run to a
      :data:`_GRACEFUL_FAILURE_STATES` state → record failed.
    """
    branch_id = _resolve_branch_id(session, run, branch_id)
    pv, prior = begin_phase_version(
        session,
        run,
        phase,
        created_by=created_by,
        prompts=prompts,
        branch_id=branch_id,
    )
    session.commit()
    try:
        runner_call()
    except RunCancelled:
        fail_phase_version(session, run, pv, prior, cancelled=True)
        session.commit()
        raise
    except Exception as exc:
        fail_phase_version(session, run, pv, prior, cancelled=False)
        # PR-I2.b: if the runner didn't transition the run to a
        # graceful-failure state itself (e.g. exception leaked past
        # the agent's own try/except), promote the run to
        # FAILED_FIXABLE here — otherwise the run sits in *_RUNNING
        # forever with no banner, indistinguishable from a SIGKILL
        # zombie. Codex Q3: this is the public-layer fix; agents
        # don't each need their own try/finally.
        _record_runtime_failure(session, run, phase, exc)
        session.commit()
        raise
    if run.state in _GRACEFUL_FAILURE_STATES:
        fail_phase_version(session, run, pv, prior, cancelled=False)
        session.commit()
        return pv
    commit_phase_version(session, run, pv, branch_id=branch_id)
    session.commit()
    return pv


def _record_runtime_failure(
    session: Session,
    run: Run,
    phase: str,
    exc: BaseException,
) -> None:
    """PR-I2.b common failure boundary helper.

    When an agent runner raises a non-``RunCancelled`` exception, we
    must leave the run in a state the user can recover from:

    1. Run state moves to FAILED_FIXABLE (so the banner renders + the
       PR-I1 retry path triggers ``_recover_failed_fixable_for_phase``
       on the next ``start_<phase>``).
    2. A ``phase_failed`` event is appended carrying ``phase`` +
       ``failure_class=phase_runtime_error`` + ``error_class`` so the
       audit trail explains what blew up.

    No-op if the run is already in a terminal / failure state — don't
    fight transitions the agent already wrote. Best-effort: any
    failure inside this helper is logged + swallowed so the original
    exception still surfaces to the RQ worker for retry accounting.
    """
    # Local imports to avoid circular import (state_machine ⇢ models ⇢
    # phase_version on cold-start).
    from autoessay.state_machine import (
        InvalidTransition,
        append_event,
        transition,
    )

    if run.state in _GRACEFUL_FAILURE_STATES:
        # Agent already wrote a graceful-failure transition (e.g.
        # FAILED_FIXABLE / CANCELLED). Just append the audit event.
        try:
            append_event(
                session,
                run,
                "phase_failed",
                {
                    "phase": phase,
                    "failure_class": "phase_runtime_error",
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:280],
                    "boundary": "phase_version_wrapper",
                },
            )
        except Exception:  # noqa: BLE001 — best-effort audit
            logger.exception(
                "PR-I2.b: failed to append phase_failed audit event for run=%s phase=%s",
                run.id,
                phase,
            )
        return

    guidance = (
        f"{phase} 阶段意外失败 — worker 进程在内部抛出未处理异常。"
        f"点击下方「重试该步骤」重新启动该阶段。"
        f"The {phase} phase failed with an unhandled "
        f"exception ({type(exc).__name__}). Click 'Retry phase' to restart."
    )
    try:
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason=f"phase_runtime_error:{phase}",
            payload={"phase": phase, "guidance": guidance},
        )
    except InvalidTransition:
        # Run was already moved to a terminal state by something else.
        # Don't escalate; the audit append below still records context.
        pass
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception(
            "PR-I2.b: transition to FAILED_FIXABLE failed for run=%s phase=%s",
            run.id,
            phase,
        )
        return
    try:
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": phase,
                "failure_class": "phase_runtime_error",
                "error_class": type(exc).__name__,
                "error_message": str(exc)[:280],
                "guidance": guidance,
                "boundary": "phase_version_wrapper",
            },
        )
    except Exception:  # noqa: BLE001 — best-effort audit
        logger.exception(
            "PR-I2.b: failed to append phase_failed audit event for run=%s phase=%s",
            run.id,
            phase,
        )


def maybe_run_with_versioning(
    session: Session,
    run: Run,
    phase: str,
    runner_call: Callable[[], None],
    *,
    created_by: str | None = None,
    prompts: list[ResolvedPrompt] | None = None,
    branch_id: str | None = None,
) -> PhaseVersion | None:
    """Wrap ``runner_call`` with versioning if no pv is already
    in flight for this (run, phase, branch); otherwise execute
    ``runner_call`` directly so we don't double-wrap.

    PR-A4.1b: each agent's ``run_<phase>`` calls this to ensure
    future first-runs always create a pv row. The /rerun_phase
    endpoint already wraps explicitly with
    :func:`run_with_versioning`; that path lands here with a
    pv whose ``status='running'`` already exists, and we run
    inline to avoid creating a nested pv.

    Returns the pv created (or None if the call ran inline).
    """
    branch_id = _resolve_branch_id(session, run, branch_id)
    has_running = (
        session.scalar(
            select(PhaseVersion.id)
            .where(PhaseVersion.run_id == run.id)
            .where(PhaseVersion.phase == phase)
            .where(PhaseVersion.created_on_branch_id == branch_id)
            .where(PhaseVersion.status == "running"),
        )
        is not None
    )
    if has_running:
        runner_call()
        return None
    return run_with_versioning(
        session,
        run,
        phase,
        runner_call,
        created_by=created_by,
        prompts=prompts,
        branch_id=branch_id,
    )


__all__ = [
    "PHASE_LEGACY_DIRS",
    "PHASE_OWNERSHIP",
    "ResolvedPrompt",
    "activate_version",
    "begin_phase_version",
    "commit_phase_version",
    "compute_input_snapshot_hash",
    "delete_phase_version",
    "fail_phase_version",
    "get_run_head",
    "is_pv_branch_exclusive",
    "list_versions",
    "maybe_run_with_versioning",
    "reachable_pv_ids_for_branch",
    "replace_phase_version",
    "run_with_versioning",
]
