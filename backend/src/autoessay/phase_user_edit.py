"""User-edit endpoint helpers for phase artifacts (PR-A2).

When the workspace UI sends an edited copy of the agent-produced
artifacts, ``apply_phase_user_edit`` is the single backend entry
point. It enforces every codex amendment from the 2026-05-01
review of issue 1:

- Run must be quiescent (not running / not cancelled) — codex
  amendment 2 ("don't gate ONLY on USER_*_REVIEW; allow editing
  earlier completed phases later in the run").
- Phase must have completed output on the active branch — same
  amendment.
- Branch-scoped stale-phase monotonicity must hold (cannot edit
  upstream of a still-stale phase) — codex amendment 1.
- Optimistic concurrency: caller passes ``base_version_id``; we
  409 if the active head has moved since the caller loaded the
  view — codex amendment 3.
- Reuse ``begin_phase_version`` + ``commit_phase_version`` archive
  layout (``phases/<pv_id>/``); do not invent a parallel
  ``run_dir/<phase>/v###/`` scheme — codex amendment 4.
- Tag the resulting version with ``source='user_edit'`` so the
  history modal labels it (the field added by PR-A1) — codex
  amendments 5 + 6.
- Drafter edits must keep ``manuscript.md`` and ``claim_map.jsonl``
  in sync — codex amendment 7.

Phase-specific validators are intentionally conservative: they
guarantee the file is structurally parseable (JSON / JSONL /
non-empty text). Downstream phases surface their own failures if
the user edited something semantically incompatible.

``proposal`` is *not* served here — it has its own dedicated
``PUT /api/runs/{id}/proposal`` save endpoint (``save_proposal_version``)
predating this module and not part of the versioned-phase
machinery.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from autoessay.models import PhaseVersion, Run
from autoessay.phase_rerun import (
    PHASES,
    RUNNING_STATES,
    first_completed_downstream,
    has_completed_output,
)


class PhaseUserEditError(Exception):
    """Validation errors raised while applying a user edit.

    The endpoint layer translates these into HTTP 4xx responses
    using the ``status_code`` carried by each subclass.
    """

    status_code: int = 400


class PhaseUserEditConflict(PhaseUserEditError):
    status_code = 409


class PhaseUserEditNotAllowed(PhaseUserEditError):
    status_code = 409


# ---------------------------------------------------------------------------
# Per-phase artifact registry. Each entry declares the legacy paths
# the user may edit through this endpoint. Drafter has a "pair_with"
# rule (codex amendment 7): editing the manuscript without also
# updating the claim_map would leave the two artifacts inconsistent.
# ---------------------------------------------------------------------------


# Type tags drive the validator. ``markdown`` allows any non-empty
# string. ``json`` parses with ``json.loads``. ``jsonl`` parses
# each non-empty line.
_MARKDOWN = "markdown"
_JSON = "json"
_JSONL = "jsonl"


# Per-phase editable file map. Key = phase name; value = ordered
# tuple of (logical_path_pattern, kind, required_with_path|None).
#
# Path patterns are EXACT relative paths today. ``drafts/{version}/...``
# is templated at runtime by inspecting the active draft directory
# on disk; non-drafter phases use exact literals.
_PHASE_EDIT_REGISTRY: dict[str, tuple[tuple[str, str, str | None], ...]] = {
    "scout": (("discovery/scout_report.md", _MARKDOWN, None),),
    "curator": (("sources/shortlist.json", _JSON, None),),
    "synthesizer": (
        ("synthesis/claims.jsonl", _JSONL, None),
        ("synthesis/synthesizer_report.md", _MARKDOWN, None),
    ),
    "ideator": (("novelty/angle_cards.json", _JSON, None),),
    "drafter": (
        # Drafter writes to drafts/v###/ — the version segment is
        # resolved at edit time (``_resolve_drafter_paths``). Both
        # files reference each other (claim ids in claim_map index
        # into manuscript paragraphs), so editing one without the
        # other leaves the artifacts inconsistent.
        ("drafts/{version}/manuscript.md", _MARKDOWN, "drafts/{version}/claim_map.jsonl"),
        ("drafts/{version}/claim_map.jsonl", _JSONL, "drafts/{version}/manuscript.md"),
    ),
    # Stylist agent reads/writes ``paper_styled.md`` (not
    # ``manuscript.md``); using the wrong filename would silently
    # write a file no downstream reader picks up — codex audit
    # 2026-05-01.
    "stylist": (("drafts/{version}/style/paper_styled.md", _MARKDOWN, None),),
    "critic": (("reviews/critic_report.json", _JSON, None),),
    "integrity": (("integrity/integrity_summary.json", _JSON, None),),
}


def editable_paths_for_phase(phase: str, run: Run) -> list[tuple[str, str, str | None]]:
    """Resolved tuple list of (path, kind, required_with_path) for ``phase``.

    Templated paths (drafter / stylist) are bound to the most-recent
    drafts/v###/ directory on disk. If no such directory exists the
    entry is dropped — the user can't edit something that hasn't
    been produced yet.
    """
    raw = _PHASE_EDIT_REGISTRY.get(phase)
    if raw is None:
        return []
    if "{version}" not in "".join(p for p, _, _ in raw):
        return list(raw)
    version = _latest_draft_version(Path(run.run_dir))
    if version is None:
        return []
    resolved: list[tuple[str, str, str | None]] = []
    for path, kind, pair in raw:
        resolved_pair = pair.replace("{version}", version) if pair else None
        resolved.append((path.replace("{version}", version), kind, resolved_pair))
    return resolved


def apply_phase_user_edit(
    *,
    session: Session,
    run: Run,
    phase: str,
    base_version_id: str | None,
    files: dict[str, str],
    user_id: str | None,
    mode: str = "new",
) -> dict[str, object]:
    """Apply a user-edit to ``phase`` artifacts.

    ``mode`` is ``"new"`` (default — current behavior: create a new
    pv tagged ``source='user_edit'`` and bump the branch head) or
    ``"replace"`` (codex AGREE 2026-05-01: overwrite the current
    head's archive in place, do NOT create a new pv, only valid
    when no downstream phase has completed AND the head pv is
    exclusive to this branch).

    Returns a payload with ``phase_version_id``, ``version_no``,
    ``branch_id``, ``stale_from_phase`` (post-edit, branch-scoped),
    and ``mode`` (echoed back for the client).

    Raises ``PhaseUserEditError`` (or subclasses) on validation
    failures; the endpoint layer maps those to HTTP 4xx.
    """
    from autoessay.branches import (
        ensure_main_branch,
        get_branch_stale,
        set_branch_stale,
    )
    from autoessay.phase_lock import (
        claim_phase_lock,
        new_lock_token,
        release_phase_lock,
    )
    from autoessay.phase_version import (
        begin_phase_version,
        commit_phase_version,
        fail_phase_version,
        get_run_head,
        replace_phase_version,
    )
    from autoessay.state_machine import append_event

    if mode not in {"new", "replace"}:
        raise PhaseUserEditError(f"unknown save mode: {mode!r}")

    if phase not in PHASES:
        # ``proposal`` is intentionally absent from PHASES (it has
        # its own save endpoint); ``exports`` is the terminal phase
        # and is not user-editable.
        raise PhaseUserEditError(f"phase {phase!r} is not user-editable")
    if phase not in _PHASE_EDIT_REGISTRY:
        raise PhaseUserEditError(f"phase {phase!r} has no editable artifacts registered")
    if run.cancel_requested_at is not None:
        raise PhaseUserEditConflict("this run is cancelled")
    if run.state in RUNNING_STATES:
        raise PhaseUserEditConflict(
            f"another phase is currently running ({run.state}); "
            "wait for it to finish before editing",
        )
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    assert branch_id is not None
    # Codex audit (2026-05-01): use the file-glob path that
    # ``list_phase_versions`` already uses, so vanilla first runs
    # (which do NOT create RunHead rows under stage 2.A semantics)
    # are still considered "has produced output" and editable.
    # Without this, the very first user-edit after the agent's
    # initial run would 409. We still pass branch_id along to
    # honour branch isolation when a RunHead does exist.
    has_output = has_completed_output(
        run,
        phase,
        session=session,
        branch_id=branch_id,
    ) or has_completed_output(run, phase)
    if not has_output:
        raise PhaseUserEditConflict(
            f"phase {phase!r} has not produced any output yet; nothing to edit",
        )
    stale = get_branch_stale(session, run, branch_id=branch_id)
    if stale is not None and stale in PHASES and PHASES.index(phase) > PHASES.index(stale):
        raise PhaseUserEditConflict(
            f"refresh '{stale}' first; can't edit '{phase}' while it is still stale",
        )

    # Optimistic concurrency: only let the caller commit if the head
    # they were viewing is still the head now. Without this two
    # tabs can race and silently overwrite each other. The codex
    # audit flagged a bypass: previously ``base_version_id=None``
    # was accepted even when an active head existed. Only accept
    # ``None`` when the run truly has no head yet (vanilla first
    # run pre-PR-A1 / pre-versioning); any later edit must echo
    # back the head it was looking at.
    current_head = get_run_head(session, run.id, phase, branch_id=branch_id)
    if base_version_id is None:
        if current_head is not None:
            raise PhaseUserEditConflict(
                "this phase already has a tracked version "
                f"({current_head!r}); pass base_version_id from the "
                "editable endpoint and try again",
            )
    elif base_version_id != current_head:
        raise PhaseUserEditConflict(
            "another version of this phase was activated after you loaded "
            "the page; reload to merge before editing",
        )

    registry = editable_paths_for_phase(phase, run)
    if not registry:
        raise PhaseUserEditNotAllowed(
            f"phase {phase!r} has no editable artifact directory yet",
        )
    allowed = {path for path, _, _ in registry}
    kinds = {path: kind for path, kind, _ in registry}
    pairs = {path: pair for path, _, pair in registry if pair}

    if not files:
        raise PhaseUserEditError("at least one file must be provided")

    extra = set(files) - allowed
    if extra:
        raise PhaseUserEditNotAllowed(
            f"phase {phase!r} cannot edit these paths: {sorted(extra)}",
        )

    for path, content in files.items():
        _validate_content(path, kinds[path], content)

    # Codex amendment 7: drafter must pair manuscript and claim_map.
    for path in list(files):
        required = pairs.get(path)
        if required is None:
            continue
        if required not in files:
            raise PhaseUserEditError(
                f"editing {path!r} requires editing {required!r} in the same request "
                "(claim_map must move with manuscript)",
            )

    # Codex AGREE 2026-05-01 amendment 5: replace eligibility check
    # uses the dual branch-aware-with-legacy-fallback pattern that
    # save_proposal already uses, so vanilla first runs whose
    # downstream files exist on disk but don't have RunHead rows do
    # not silently allow replace.
    if mode == "replace":
        downstream_completed = first_completed_downstream(
            run,
            phase,
            session=session,
            branch_id=branch_id,
        )
        if downstream_completed is None:
            # Fall through to file-glob check.
            downstream_completed = first_completed_downstream(run, phase)
        if downstream_completed is not None:
            raise PhaseUserEditConflict(
                f"cannot replace: phase {downstream_completed!r} has already "
                "produced output; save with mode='new' instead",
            )
        if current_head is None:
            raise PhaseUserEditConflict(
                "cannot replace: this phase has no tracked version yet "
                "(vanilla first run pre-versioning). Save with mode='new' "
                "to create the first tracked version.",
            )
        # Codex amendment 7: branch isolation. Reject replace when
        # the head pv is shared (another branch's RunHead points at
        # it OR another branch is forked from it) — overwriting the
        # archive in place would leak into the other branch.
        from autoessay.phase_version import (
            is_pv_branch_exclusive as _is_exclusive,
        )

        if not _is_exclusive(session, current_head, branch_id):
            raise PhaseUserEditConflict(
                "cannot replace: this version is also referenced by "
                "another branch (active head or fork point). Save with "
                "mode='new' instead.",
            )

    # Codex audit (2026-05-01): the begin → write → commit window
    # must be lock-protected against concurrent agent ``start_*`` /
    # ``rerun_phase`` calls. Without the claim, a start can pass its
    # state guard between our quiescence check and our archive
    # capture, then the agent writes legacy files while we are
    # mid-archive, producing a half-edited / half-agent version.
    edit_token = new_lock_token()
    if not claim_phase_lock(session, run, phase, edit_token):
        held = run.active_phase_lock
        raise PhaseUserEditConflict(
            f"another phase is currently running ({held!r}); "
            "wait for it to finish before saving the edit",
        )
    session.commit()

    pv = None
    prior: str | None = None
    run_dir = Path(run.run_dir)
    backup: dict[str, bytes] = {}
    try:
        if mode == "replace":
            # Replace mode: keep the existing head pv id. Backup
            # current legacy files first (rollback target), write
            # new content, then re-archive in place.
            head_pv = session.get(PhaseVersion, current_head)
            if head_pv is None:
                # current_head was set above but the row vanished —
                # treat as concurrency conflict.
                raise PhaseUserEditConflict(
                    "head version no longer exists; reload and retry",
                )
            for path in files:
                target = run_dir / path
                if target.exists():
                    backup[path] = target.read_bytes()
            for path, content in files.items():
                target = run_dir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            replace_phase_version(
                session,
                run,
                head_pv,
                branch_id=branch_id,
                user_id=user_id,
            )
            # Replace doesn't change downstream state — by definition
            # nothing downstream completed, so stale stays None.
            new_stale: str | None = None
            append_event(
                session,
                run,
                "phase_user_edited",
                {
                    "phase": phase,
                    "version_id": head_pv.id,
                    "version_no": head_pv.version_no,
                    "branch_id": branch_id,
                    "user_id": user_id,
                    "files": sorted(files),
                    "mode": "replace",
                },
            )
            release_phase_lock(session, run, phase, edit_token)
            session.commit()
            return {
                "phase_version_id": head_pv.id,
                "version_no": head_pv.version_no,
                "branch_id": branch_id,
                "source": "user_edit",
                "stale_from_phase": new_stale,
                "mode": "replace",
            }
        # Begin a new phase_version, tag it user_edit, write the
        # user's content to the legacy paths, commit_phase_version
        # archives them under phases/<pv_id>/.
        # PR-A4.2 codex amendment 1 (2026-05-02): capture the
        # currently-effective prompt snapshot on this user_edit
        # pv too, so future ``prompt_dirty`` checks against the
        # head pv have a baseline. Without this snapshot, a draft
        # row would always look "different" from a head with no
        # prompt rows, falsely flagging prompt_dirty=true.
        from autoessay.main import _resolve_phase_prompts as _resolve

        resolved_prompts, _ = _resolve(session, run.id, phase, branch_id)
        pv, prior = begin_phase_version(
            session,
            run,
            phase,
            created_by=user_id,
            source="user_edit",
            prompts=resolved_prompts,
            branch_id=branch_id,
        )
        session.commit()
        for path, content in files.items():
            target = run_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup[path] = target.read_bytes()
            target.write_text(content, encoding="utf-8")
        commit_phase_version(session, run, pv, branch_id=branch_id)
        # Branch-scoped stale (codex amendment 1): editing this phase
        # makes the next-completed downstream phase stale, so the
        # stale-banner UI prompts the user to rerun it.
        new_stale = first_completed_downstream(
            run,
            phase,
            session=session,
            branch_id=branch_id,
        )
        set_branch_stale(session, run, new_stale, branch_id=branch_id)
        append_event(
            session,
            run,
            "phase_user_edited",
            {
                "phase": phase,
                "version_id": pv.id,
                "version_no": pv.version_no,
                "branch_id": branch_id,
                "user_id": user_id,
                "files": sorted(files),
                "mode": "new",
            },
        )
        # Release the lock in the same commit so an agent rerun can
        # proceed immediately after.
        release_phase_lock(session, run, phase, edit_token)
        session.commit()
        return {
            "phase_version_id": pv.id,
            "version_no": pv.version_no,
            "branch_id": branch_id,
            "source": "user_edit",
            "stale_from_phase": new_stale,
            "mode": "new",
        }
    except Exception:
        # Restore disk state, mark the pv failed, release the lock,
        # re-raise.
        for path, blob in backup.items():
            target = run_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        for path in files:
            if path not in backup:
                target = run_dir / path
                if target.exists():
                    target.unlink()
        if pv is not None:
            try:
                fail_phase_version(session, run, pv, prior, cancelled=False)
                session.commit()
            except Exception:
                session.rollback()
        try:
            release_phase_lock(session, run, phase, edit_token)
            session.commit()
        except Exception:
            session.rollback()
        raise


def _validate_content(path: str, kind: str, content: str) -> None:
    if not isinstance(content, str):
        raise PhaseUserEditError(f"{path}: content must be a string")
    if kind == _MARKDOWN:
        if not content.strip():
            raise PhaseUserEditError(f"{path}: markdown content must not be empty")
        return
    if kind == _JSON:
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise PhaseUserEditError(f"{path}: invalid JSON ({exc.msg})") from exc
        return
    if kind == _JSONL:
        for line_no, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise PhaseUserEditError(
                    f"{path}: invalid JSONL on line {line_no} ({exc.msg})",
                ) from exc
        return
    # Unreachable in practice — registry only emits known kinds.
    raise PhaseUserEditError(f"{path}: unknown content kind {kind!r}")


def _latest_draft_version(run_dir: Path) -> str | None:
    drafts_dir = run_dir / "drafts"
    if not drafts_dir.is_dir():
        return None
    candidates = sorted(p.name for p in drafts_dir.iterdir() if p.is_dir())
    return candidates[-1] if candidates else None


__all__ = [
    "PhaseUserEditConflict",
    "PhaseUserEditError",
    "PhaseUserEditNotAllowed",
    "apply_phase_user_edit",
    "editable_paths_for_phase",
]
