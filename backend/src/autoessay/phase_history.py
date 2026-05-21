"""Phase-history modal data contract (PR-A4.2, codex AGREE-with-amendments 2026-05-02).

Computes the per-phase card payload the frontend consumes to render
the modal: current head, state flags (head_missing / prompt_dirty /
lineage_dirty), upstream summary, version list with lineage.

State flags follow codex amendment 1: compute from
(head, drafts, lineage) instead of storing a single enum. The three
flags can coexist, and the UI is responsible for prioritizing how
to present them in a single state pill (mobile) vs detailed reasons
(desktop).

This module is read-only — it queries the existing phase_versions /
phase_version_inputs / run_heads / phase_prompt_drafts /
phase_version_prompts tables but never writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.models import (
    Branch,
    PhasePromptDraft,
    PhaseVersion,
    PhaseVersionInput,
    PhaseVersionPrompt,
    Run,
    RunHead,
)
from autoessay.phase_rerun import PHASE_INPUT_STATES, PHASES
from autoessay.phase_version import reachable_pv_ids_for_branch


@dataclass(frozen=True)
class StateFlags:
    head_missing: bool
    prompt_dirty: bool
    lineage_dirty: bool


@dataclass(frozen=True)
class UpstreamHeadEntry:
    upstream_phase: str
    head_version_no: int | None
    head_pv_id: str | None
    matches_my_lineage: bool


@dataclass(frozen=True)
class VersionLineageEntry:
    upstream_phase: str
    upstream_pv_id: str
    upstream_version_no: int


@dataclass(frozen=True)
class PhaseVersionEntry:
    pv_id: str
    version_no: int
    source: str
    status: str
    created_at: str
    is_head: bool
    upstream_lineage: list[VersionLineageEntry]
    has_downstream_dependents: bool
    dependent_summary: str | None
    # PR-A4.4 codex amendment 5 (2026-05-02): backend computes
    # the delete-block decision so the modal does not have to
    # reimplement the rule-4 reverse-dependency check. ``True``
    # iff a DELETE on this pv would 409.
    delete_blocked: bool
    delete_block_reason: str | None


@dataclass(frozen=True)
class PhaseHistoryEntry:
    phase: str
    state_flags: StateFlags
    head_pv_id: str | None
    head_version_no: int | None
    upstream_summary: list[UpstreamHeadEntry]
    versions: list[PhaseVersionEntry]
    # PR-A4.4 codex amendment 4 (2026-05-02): only surface a
    # "run this phase now" CTA when the phase can actually be
    # started from the run's current state. Pre-computed
    # server-side so the modal doesn't reimplement
    # ALLOWED_TRANSITIONS / phase-readiness logic.
    runnable_now: bool


@dataclass(frozen=True)
class PhaseHistoryResponse:
    run_id: str
    branch_id: str
    phases: list[PhaseHistoryEntry] = field(default_factory=list)


def compute_phase_history(
    session: Session,
    run: Run,
    branch_id: str,
) -> PhaseHistoryResponse:
    """Build the per-phase card payload for ``run`` on ``branch_id``.

    See module docstring for state-flag semantics.
    """
    # Pre-load every pv on the run (cheap relative to the per-phase
    # iteration; avoids N+1 queries). Filter by branch_id later.
    all_pvs = session.scalars(
        select(PhaseVersion).where(PhaseVersion.run_id == run.id),
    ).all()
    pvs_by_id = {pv.id: pv for pv in all_pvs}

    # Pre-load lineage rows for every pv on this run.
    all_inputs = (
        session.scalars(
            select(PhaseVersionInput).where(
                PhaseVersionInput.phase_version_id.in_(pvs_by_id.keys()),
            ),
        ).all()
        if pvs_by_id
        else []
    )
    inputs_by_pv: dict[str, list[PhaseVersionInput]] = {}
    for inp in all_inputs:
        inputs_by_pv.setdefault(inp.phase_version_id, []).append(inp)
    # Reverse-index: for each pv, who references it as upstream?
    referenced_by: dict[str, list[PhaseVersionInput]] = {}
    for inp in all_inputs:
        referenced_by.setdefault(inp.upstream_pv_id, []).append(inp)

    # Pre-load run_heads for this branch.
    heads = session.scalars(
        select(RunHead).where(RunHead.run_id == run.id).where(RunHead.branch_id == branch_id),
    ).all()
    head_by_phase: dict[str, RunHead] = {h.phase: h for h in heads}

    # Pre-load PhaseVersionPrompt rows for every head pv.
    head_pv_ids = [h.version_id for h in heads]
    all_pv_prompts = (
        session.scalars(
            select(PhaseVersionPrompt).where(
                PhaseVersionPrompt.phase_version_id.in_(head_pv_ids),
            ),
        ).all()
        if head_pv_ids
        else []
    )
    head_prompts_by_pv: dict[str, dict[str, str]] = {}
    for row in all_pv_prompts:
        head_prompts_by_pv.setdefault(row.phase_version_id, {})[row.prompt_key] = row.content_hash

    # Pre-load PhasePromptDraft rows for this branch.
    drafts = session.scalars(
        select(PhasePromptDraft)
        .where(PhasePromptDraft.run_id == run.id)
        .where(PhasePromptDraft.branch_id == branch_id),
    ).all()
    drafts_by_phase: dict[str, list[PhasePromptDraft]] = {}
    for d in drafts:
        drafts_by_phase.setdefault(d.phase, []).append(d)

    entries: list[PhaseHistoryEntry] = []
    for phase in PHASES:
        head = head_by_phase.get(phase)
        head_pv = pvs_by_id.get(head.version_id) if head is not None else None

        # ----- Head summary ----------------------------------------------
        head_pv_id = head_pv.id if head_pv else None
        head_version_no = head_pv.version_no if head_pv else None

        # ----- Upstream summary + lineage_dirty --------------------------
        upstream_summary: list[UpstreamHeadEntry] = []
        lineage_dirty = False
        if phase in PHASES:
            phase_idx = PHASES.index(phase)
            for upstream_phase in PHASES[:phase_idx]:
                up_head = head_by_phase.get(upstream_phase)
                up_head_pv = pvs_by_id.get(up_head.version_id) if up_head else None
                # What does THIS phase's head pv think its upstream
                # was when it ran?
                my_lineage_for_upstream: str | None = None
                if head_pv:
                    for inp in inputs_by_pv.get(head_pv.id, []):
                        if inp.upstream_phase == upstream_phase:
                            my_lineage_for_upstream = inp.upstream_pv_id
                            break
                matches = up_head_pv is not None and my_lineage_for_upstream == up_head_pv.id
                if head_pv is not None and not matches:
                    # head exists but its lineage record disagrees
                    # with current upstream head → upstream advanced
                    lineage_dirty = True
                upstream_summary.append(
                    UpstreamHeadEntry(
                        upstream_phase=upstream_phase,
                        head_version_no=up_head_pv.version_no if up_head_pv else None,
                        head_pv_id=up_head_pv.id if up_head_pv else None,
                        matches_my_lineage=matches,
                    ),
                )

        # ----- prompt_dirty ---------------------------------------------
        # prompt_dirty: a draft row exists AND its content_hash differs
        # from the head pv's snapshot for the same prompt_key. If the
        # head has no snapshot for a key but a draft exists, treat as
        # dirty (the draft would change the next run's behavior).
        phase_drafts = drafts_by_phase.get(phase, [])
        head_prompts = head_prompts_by_pv.get(head_pv_id, {}) if head_pv_id else {}
        prompt_dirty = False
        for d in phase_drafts:
            if d.content_hash != head_prompts.get(d.prompt_key):
                prompt_dirty = True
                break

        head_missing = head_pv is None

        # ----- All versions reachable from this branch ------------------
        # PR-A4.4 codex amendment 6 (2026-05-02): use the same
        # reachability rule as activation visibility, so forked
        # branches see inherited versions from their fork point's
        # parent-walk. "created_on_branch_id == branch_id" alone
        # would hide them.
        reachable = reachable_pv_ids_for_branch(session, run.id, phase, branch_id)
        my_pvs = [pv for pv in all_pvs if pv.id in reachable]
        my_pvs.sort(key=lambda pv: pv.version_no, reverse=True)
        version_entries: list[PhaseVersionEntry] = []
        for pv in my_pvs:
            lineage = [
                VersionLineageEntry(
                    upstream_phase=inp.upstream_phase,
                    upstream_pv_id=inp.upstream_pv_id,
                    upstream_version_no=(
                        pvs_by_id[inp.upstream_pv_id].version_no
                        if inp.upstream_pv_id in pvs_by_id
                        else 0
                    ),
                )
                for inp in inputs_by_pv.get(pv.id, [])
            ]
            dependents = referenced_by.get(pv.id, [])
            has_dependents = len(dependents) > 0
            dependent_summary: str | None = None
            if has_dependents:
                # "被 ideator v1, drafter v2 引用" — render the
                # downstream phase + version_no for the most-recent
                # dependent (one is enough for the UI tooltip; the
                # full set is implied by phase order).
                first = dependents[0]
                downstream_pv = pvs_by_id.get(first.phase_version_id)
                if downstream_pv:
                    dependent_summary = f"{downstream_pv.phase} v{downstream_pv.version_no}"
            # PR-A4.4 codex amendment 5 (2026-05-02): backend
            # computes the delete-block decision so the modal
            # never rolls its own copy of the rule-4 reverse-
            # dependency check. Mirrors
            # ``phase_version.delete_phase_version`` reject
            # conditions exactly:
            delete_blocked, delete_reason = _compute_delete_block(
                session,
                pv,
                has_dependents,
                dependent_summary,
                head_by_phase,
            )
            version_entries.append(
                PhaseVersionEntry(
                    pv_id=pv.id,
                    version_no=pv.version_no,
                    source=pv.source,
                    status=pv.status,
                    created_at=pv.created_at.isoformat() if pv.created_at else "",
                    is_head=pv.id == head_pv_id,
                    upstream_lineage=lineage,
                    has_downstream_dependents=has_dependents,
                    dependent_summary=dependent_summary,
                    delete_blocked=delete_blocked,
                    delete_block_reason=delete_reason,
                ),
            )

        # PR-A4.4 codex amendment 4 (2026-05-02): only set
        # ``runnable_now`` true when the phase can actually be
        # started from the run's current state. The modal uses
        # this to gray out "run this phase now" CTAs for
        # downstream-but-not-ready phases.
        runnable_now = _phase_runnable_now(run, phase)

        entries.append(
            PhaseHistoryEntry(
                phase=phase,
                state_flags=StateFlags(
                    head_missing=head_missing,
                    prompt_dirty=prompt_dirty,
                    lineage_dirty=lineage_dirty,
                ),
                head_pv_id=head_pv_id,
                head_version_no=head_version_no,
                upstream_summary=upstream_summary,
                versions=version_entries,
                runnable_now=runnable_now,
            ),
        )

    return PhaseHistoryResponse(
        run_id=run.id,
        branch_id=branch_id,
        phases=entries,
    )


def _compute_delete_block(
    session: Session,
    pv: PhaseVersion,
    has_dependents: bool,
    dependent_summary: str | None,
    head_by_phase: dict[str, RunHead],
) -> tuple[bool, str | None]:
    """Mirror :func:`phase_version.delete_phase_version`'s reject
    conditions and return ``(blocked, reason)``.

    Codex amendment 5: ``has_downstream_dependents`` alone is
    insufficient — delete also rejects when the pv is an active
    head on ANY branch, has a lineage child via parent_pv_id, or
    is the fork-point of any branch (incl. soft-deleted).
    """
    # Reject 1: active head on any branch.
    is_head_anywhere = (
        session.scalar(
            select(RunHead.version_id).where(RunHead.version_id == pv.id).limit(1),
        )
        is not None
    )
    if is_head_anywhere:
        return True, "active_head"
    # Reject 2: downstream lineage references this pv.
    if has_dependents:
        return True, dependent_summary or "downstream_dependent"
    # Reject 3: lineage child (parent_pv_id reference).
    has_child = (
        session.scalar(
            select(PhaseVersion.id).where(PhaseVersion.parent_pv_id == pv.id).limit(1),
        )
        is not None
    )
    if has_child:
        return True, "lineage_child"
    # Reject 4: branch fork-point. Codex round-4 #4 (2026-05-03):
    # only ACTIVE branches' fork points block delete. PR #155 made
    # delete_phase_version permissive on soft-deleted-branch fork
    # points (NULLing the dead reference); phase-history must
    # mirror that filter so the modal doesn't show "fork_point:foo"
    # blocked when the backend would actually accept the delete.
    fork_branch_name = session.scalar(
        select(Branch.name)
        .where(Branch.forked_from_pv_id == pv.id)
        .where(Branch.deleted_at.is_(None))
        .limit(1),
    )
    if fork_branch_name is not None:
        return True, f"fork_point:{fork_branch_name}"
    return False, None


def _phase_runnable_now(run: Run, phase: str) -> bool:
    """``True`` iff ``phase`` can be started from ``run.state``
    via the standard ``start_*`` endpoint.

    Codex amendment 4 + round-2 follow-up (2026-05-02):
    don't enable "run this phase now" for phases the run isn't
    currently positioned for — the start_* endpoint would 409
    anyway, and the modal CTA would mislead the user into
    clicking dead buttons. ALSO false during any RUNNING_STATES
    (codex round-2): even if state == DRAFTER_RUNNING and
    phase == drafter, surfacing "run drafter now" while the
    drafter agent is in flight is misleading; that handoff
    state is the post-drafter / pre-stylist quiescent point in
    practice, not a "you can drafter again" state.

    A phase is runnable iff ``run.state`` equals its
    ``PHASE_INPUT_STATES`` predecessor state. The predecessors
    are USER_*_REVIEW values which are quiescent by
    construction, so this also implicitly excludes RUNNING_STATES.

    PR-C2.b: ideator is a special case — it can run from
    either ``USER_FIELD_REVIEW`` (lens-skipped path) or
    ``USER_LENS_REVIEW`` (post-lens path). See
    ``IDEATOR_VALID_INPUT_STATES``.

    Round-1 audit #6 (2026-05-03): for framework_lens, the raw
    PHASE_INPUT_STATES match is necessary but not sufficient. The
    phase has a ``should_run_framework_lens`` decision that returns
    False when there are no lens inputs AND paper_mode is not
    theory_article — in that case the start_framework_lens endpoint
    would FAIL_FIXABLE or transition directly to IDEATOR_RUNNING,
    so the phase-history "run now" affordance must reflect that.
    """
    if phase == "ideator":
        from autoessay.phase_rerun import IDEATOR_VALID_INPUT_STATES

        if run.state not in IDEATOR_VALID_INPUT_STATES:
            return False
        # Codex round-4 #1 (2026-05-03): theory_article must
        # traverse framework_lens; skip-direct-to-ideator from
        # USER_FIELD_REVIEW is rejected by start_ideator. Mirror
        # that here so the modal CTA isn't a dead button.
        return not (run.paper_mode == "theory_article" and run.state == "USER_FIELD_REVIEW")
    if phase == "framework_lens":
        from autoessay.phase_rerun import LENS_VALID_INPUT_STATES

        # PR-C3.b codex round-2 amendment 4: lens may run from
        # USER_FIELD_REVIEW (tension-skipped path) or USER_TENSION_REVIEW
        # (post-tension path).
        if run.state not in LENS_VALID_INPUT_STATES:
            return False
        return _framework_lens_should_run(run)
    if phase == "tension_extraction":
        # PR-C3.a + C3.b: gated by Settings.tension_taxonomy_enabled +
        # synthesizer claim presence (delegate to should_run helper).
        return _tension_extraction_should_run(run)
    expected_state = PHASE_INPUT_STATES.get(phase)
    if expected_state is None:
        return False
    return run.state == expected_state


def _tension_extraction_should_run(run: Run) -> bool:
    """Helper for PR-C3.b: read the run's synthesizer artifact +
    consult ``should_run_tension_extraction``. Operational gate
    (``Settings.tension_taxonomy_enabled``) and synthesizer-claim
    presence both gate the phase. Errors reading the artifact fall
    back to False (don't surface a dead button)."""
    import json
    from pathlib import Path

    from autoessay.agents.tension_extraction import should_run_tension_extraction

    valid_inputs = {"USER_FIELD_REVIEW", "USER_TENSION_REVIEW"}
    if run.state not in valid_inputs:
        return False
    run_dir = Path(run.run_dir)
    synth_path = run_dir / "synthesis" / "synthesizer.json"
    try:
        synth_payload = (
            json.loads(synth_path.read_text(encoding="utf-8")) if synth_path.exists() else None
        )
    except (OSError, json.JSONDecodeError):
        return False
    if synth_payload is not None and not isinstance(synth_payload, dict):
        synth_payload = None
    paper_mode = str(run.paper_mode or "case_analysis")
    return should_run_tension_extraction(
        paper_mode=paper_mode,
        synthesizer_payload=synth_payload,
    )


def _framework_lens_should_run(run: Run) -> bool:
    """Helper for round-1 audit #6: read the run's shortlist +
    synthesizer artifact and consult ``should_run_framework_lens``.

    Returns True when the phase MUST or SHOULD run, False when it
    can be skipped (in which case the run would go straight to
    IDEATOR_RUNNING, so framework_lens isn't a valid "run now"
    target). Errors reading the artifacts (e.g. legacy run with
    no synthesis dir) fall back to True so the user is not
    silently locked out of running lens.
    """
    import json
    from pathlib import Path

    from autoessay.framework_lens import should_run_framework_lens

    run_dir = Path(run.run_dir)
    paper_mode = str(run.paper_mode or "case_analysis")
    shortlist_path = run_dir / "sources" / "shortlist.json"
    synth_path = run_dir / "synthesis" / "synthesizer.json"
    try:
        shortlist = (
            json.loads(shortlist_path.read_text(encoding="utf-8"))
            if shortlist_path.exists()
            else []
        )
    except (OSError, json.JSONDecodeError):
        return True
    try:
        dual_track = (
            json.loads(synth_path.read_text(encoding="utf-8")) if synth_path.exists() else None
        )
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(shortlist, list):
        shortlist = []
    if dual_track is not None and not isinstance(dual_track, dict):
        dual_track = None
    return should_run_framework_lens(
        paper_mode=paper_mode,
        dual_track=dual_track,
        shortlist=shortlist,
    )


def _serialize_state_flags(flags: StateFlags) -> Mapping[str, bool]:
    return {
        "head_missing": flags.head_missing,
        "prompt_dirty": flags.prompt_dirty,
        "lineage_dirty": flags.lineage_dirty,
    }


def serialize_response(response: PhaseHistoryResponse) -> Mapping[str, object]:
    """Convert the dataclass response into the JSON shape the
    endpoint serializes. Kept separate from the dataclasses so the
    in-memory representation stays type-rich."""
    return {
        "run_id": response.run_id,
        "branch_id": response.branch_id,
        "phases": [
            {
                "phase": e.phase,
                "state_flags": _serialize_state_flags(e.state_flags),
                "head_pv_id": e.head_pv_id,
                "head_version_no": e.head_version_no,
                "upstream_summary": [
                    {
                        "upstream_phase": u.upstream_phase,
                        "head_version_no": u.head_version_no,
                        "head_pv_id": u.head_pv_id,
                        "matches_my_lineage": u.matches_my_lineage,
                    }
                    for u in e.upstream_summary
                ],
                "versions": [
                    {
                        "pv_id": v.pv_id,
                        "version_no": v.version_no,
                        "source": v.source,
                        "status": v.status,
                        "created_at": v.created_at,
                        "is_head": v.is_head,
                        "upstream_lineage": [
                            {
                                "upstream_phase": ln.upstream_phase,
                                "upstream_pv_id": ln.upstream_pv_id,
                                "upstream_version_no": ln.upstream_version_no,
                            }
                            for ln in v.upstream_lineage
                        ],
                        "has_downstream_dependents": v.has_downstream_dependents,
                        "dependent_summary": v.dependent_summary,
                        "delete_blocked": v.delete_blocked,
                        "delete_block_reason": v.delete_block_reason,
                    }
                    for v in e.versions
                ],
                "runnable_now": e.runnable_now,
            }
            for e in response.phases
        ],
    }


__all__ = [
    "PhaseHistoryEntry",
    "PhaseHistoryResponse",
    "PhaseVersionEntry",
    "StateFlags",
    "UpstreamHeadEntry",
    "VersionLineageEntry",
    "compute_phase_history",
    "serialize_response",
]
