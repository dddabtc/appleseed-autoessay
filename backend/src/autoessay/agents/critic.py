"""Critic agent and citation audit gate."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from autoessay.agents._critic_polish_loop import (
        BaselineMode,
        PolishLoopResult,
        PolishStatus,
    )
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._language import language_directive
from autoessay.agents.final_rewrite import (
    complete_downstream_review_fallback,
    load_latest_rewrite_artifact,
)
from autoessay.agents.phase_context import phase_context_prompt_block
from autoessay.clients.common import NormalizedSource
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    HookResult,
    LLMCallRequest,
    SchemaViolationError,
    hash_text,
    run_llm_step,
)
from autoessay.memory import MemoryClient, make_memory_pre_llm_hook
from autoessay.models import Project, Run
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

Severity = Literal["BLOCKER", "HIGH", "MEDIUM", "LOW"]
IssueDimension = Literal["thesis", "structure", "evidence", "prose"]
SuggestedAction = Literal[
    "REWRITE",
    "ADD_EVIDENCE",
    "REMOVE",
    "RESCOPE",
    "VERIFY_CITATION",
]


class CriticIssue(BaseModel):
    issue_id: str
    severity: Severity
    dimension: IssueDimension
    paragraph_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    description: str
    suggested_action: SuggestedAction

    class Config:
        extra = "ignore"


class RawCriticResponse(BaseModel):
    issues: list[CriticIssue] = Field(default_factory=list)

    class Config:
        extra = "ignore"


class CriticReport(BaseModel):
    issues: list[CriticIssue] = Field(default_factory=list)

    class Config:
        extra = "ignore"


class CriticHarnessCitationAudit:
    def __init__(self) -> None:
        self.audit_rows: list[dict[str, object]] = []
        self.blocker_issues: list[CriticIssue] = []


def run_critic(
    run_id: str,
    db_session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run the critic.

    ``prompt_overrides`` is the resolved override map from the rerun
    endpoint (codex-AGREEd #2 stage 2.B). Stage 2.B uses
    ``prompt_overrides["main"]`` as the static instruction block.

    ``lock_token`` (Stage 3.E follow-up P0): owner-checked phase-start
    lock release at exit.

    PR-A4.1b (2026-05-02): wraps in ``maybe_run_with_versioning``.
    """
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_critic_with_session(
                run_id,
                session,
                hooks or HookRegistry(),
                prompt_overrides=prompt_overrides,
            )

        maybe_run_with_versioning(session, run, "critic", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "critic", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def load_critic_payload(run: Run) -> dict[str, object]:
    reviews_dir = Path(run.run_dir) / "reviews"
    latest = _latest_review_path(reviews_dir)
    return {
        "run_id": run.id,
        "critic_report": _read_optional_text(latest) if latest is not None else "",
        "claim_audit": _load_jsonl_objects(reviews_dir / "claim_audit.jsonl"),
        "revision_plan": _read_optional_text(reviews_dir / "revision_plan.md"),
        "blocking_issues": _load_json_mapping(reviews_dir / "blocking_issues.json"),
        "north_star_gate": _load_json_mapping(reviews_dir / "north_star_gate.json"),
    }


def run_citation_audit(run_dir: Path) -> tuple[list[dict[str, object]], list[CriticIssue]]:
    rewrite = load_latest_rewrite_artifact(run_dir)
    if rewrite is not None:
        claim_map = rewrite.claim_map
    else:
        draft_dir = latest_draft_dir(run_dir)
        if draft_dir is None:
            return [], [
                CriticIssue(
                    issue_id="audit_missing_draft",
                    severity="BLOCKER",
                    dimension="evidence",
                    paragraph_id=None,
                    source_ids=[],
                    description="No draft directory was available for citation audit.",
                    suggested_action="VERIFY_CITATION",
                ),
            ]
        claim_map = _load_jsonl_objects(draft_dir / "claim_map.jsonl")
    if not claim_map:
        return [], [
            CriticIssue(
                issue_id="audit_missing_draft",
                severity="BLOCKER",
                dimension="evidence",
                paragraph_id=None,
                source_ids=[],
                description="No draft directory was available for citation audit.",
                suggested_action="VERIFY_CITATION",
            ),
        ]
    shortlist = _read_sources_json(run_dir / "sources" / "shortlist.json")
    return audit_claim_map(claim_map, shortlist)


def audit_claim_map(
    claim_map: Sequence[Mapping[str, object]],
    shortlist: Sequence[NormalizedSource],
) -> tuple[list[dict[str, object]], list[CriticIssue]]:
    sources_by_id = {source.source_id: source for source in shortlist}
    audit_rows: list[dict[str, object]] = []
    blocker_issues: list[CriticIssue] = []
    for index, claim in enumerate(claim_map, start=1):
        paragraph_id = _string_value(claim.get("paragraph_id")) or f"claim-{index:03d}"
        source_ids = _source_ids(claim.get("source_ids"))
        failures: list[dict[str, object]] = []
        evidence_status = claim.get("evidence_status")
        if evidence_status == "model_backed" and source_ids:
            failures.append(
                {
                    "source_id": ",".join(source_ids),
                    "reason": "model_backed claim must not carry source_ids",
                },
            )
        elif not source_ids and evidence_status != "model_backed":
            failures.append(
                {
                    "source_id": None,
                    "reason": "claim has no source_ids",
                },
            )
        for source_id in source_ids:
            source = sources_by_id.get(source_id)
            if source is None:
                failures.append(
                    {
                        "source_id": source_id,
                        "reason": "source_id is not present in shortlist",
                    },
                )
                continue
            if not _source_has_exportable_reference(source):
                failures.append(
                    {
                        "source_id": source_id,
                        "reason": "shortlist source has no DOI, URL, upload status, or user flag",
                    },
                )
        status = "BLOCKER" if failures else "PASS"
        row = {
            "claim_index": index,
            "paragraph_id": paragraph_id,
            "claim_text": _string_value(claim.get("claim_text"))
            or _string_value(claim.get("text"))
            or "",
            "source_ids": source_ids,
            "status": status,
            "failures": failures,
        }
        audit_rows.append(row)
        if failures:
            blocker_issues.append(
                CriticIssue(
                    issue_id=f"audit_{paragraph_id}_{index:03d}",
                    severity="BLOCKER",
                    dimension="evidence",
                    paragraph_id=paragraph_id,
                    source_ids=source_ids,
                    description=_audit_failure_description(paragraph_id, failures),
                    suggested_action="VERIFY_CITATION",
                ),
            )
    return audit_rows, blocker_issues


def latest_draft_dir(run_dir: Path) -> Path | None:
    drafts_dir = run_dir / "drafts"
    if not drafts_dir.exists():
        return None
    candidates = [path for path in drafts_dir.glob("v[0-9][0-9][0-9]") if path.is_dir()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda path: path.name)[-1]


def _run_critic_with_session(
    run_id: str,
    session: Session,
    hooks: HookRegistry,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    _rewrite_fallback_depth: int = 0,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    assert_run_active(run, session)
    if run.state not in {"USER_REVISION_REVIEW", "CRITIC_RUNNING"}:
        raise InvalidTransition(
            f"Critic requires USER_REVISION_REVIEW or CRITIC_RUNNING, got {run.state}"
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    run_dir = Path(run.run_dir)
    draft_dir = latest_draft_dir(run_dir)
    if draft_dir is None:
        return _fail_fixable(run, session, "Critic needs a completed styled draft.")
    rewrite = load_latest_rewrite_artifact(run_dir)
    draft = (
        rewrite.manuscript
        if rewrite is not None
        else _read_optional_text(draft_dir / "style" / "paper_styled.md")
    )
    if not draft.strip():
        return _fail_fixable(run, session, "Critic found an empty styled draft.")

    claim_map = (
        rewrite.claim_map
        if rewrite is not None
        else _load_jsonl_objects(draft_dir / "claim_map.jsonl")
    )
    shortlist = _read_sources_json(run_dir / "sources" / "shortlist.json")
    draft = _normalize_raw_source_id_markers_for_review(draft, claim_map, shortlist)
    claims = _load_jsonl_objects(run_dir / "synthesis" / "claims.jsonl")
    source_notes = _load_source_notes(run_dir / "synthesis" / "source_notes")
    selected_thesis = _load_json_mapping(run_dir / "novelty" / "selected_thesis.json")

    if run.state != "CRITIC_RUNNING":
        transition(run, "CRITIC_RUNNING", session, reason="Critic started")
    append_event(
        session,
        run,
        "phase_started",
        {
            "phase": "critic",
            "run_id": run.id,
            "draft_version": draft_dir.name,
            "manuscript_source": "rewrite" if rewrite is not None else "stylist",
            # PR-369 X-2 (codex review): snapshot mathematical_mode so
            # the audit trail captures the value critic_loop will read.
            "mathematical_mode_snapshot": bool(getattr(run, "mathematical_mode", False)),
            **({"rewrite_version": rewrite.version} if rewrite is not None else {}),
        },
    )
    session.commit()
    session.refresh(run)

    settings = get_settings()
    instructions_override = prompt_overrides.get("main") if prompt_overrides else None
    llm_issues: list[CriticIssue] | None
    if settings.critic_stub:
        llm_issues = _stub_critic_issues(claim_map)
        audit_rows, audit_blockers = audit_claim_map(claim_map, shortlist)
    else:
        audit_state = CriticHarnessCitationAudit()
        llm_issues = _critic_via_harness(
            draft=draft,
            claim_map=claim_map,
            shortlist=shortlist,
            claims=claims,
            source_notes=source_notes,
            selected_thesis=selected_thesis,
            run=run,
            project=project,
            hooks=hooks,
            audit=AuditWriter(session=session, run_dir=run.run_dir, agent_name="Critic"),
            audit_state=audit_state,
            draft_version=rewrite.version if rewrite is not None else draft_dir.name,
            instructions_override=instructions_override,
        )
        if llm_issues is None:
            return _fail_fixable(run, session, "Critic LLM returned invalid JSON after one retry.")
        audit_rows = audit_state.audit_rows
        audit_blockers = audit_state.blocker_issues
    if settings.baseline_as_evidence_test:
        llm_issues = _filter_baseline_as_evidence_test_issues(llm_issues)
    all_issues = [*llm_issues, *audit_blockers]
    blockers = [issue for issue in all_issues if issue.severity == "BLOCKER"]
    next_dimension = _recommended_dimension(all_issues)
    reviews_dir = run_dir / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    report_version = rewrite.version if rewrite is not None else draft_dir.name
    _write_text(
        reviews_dir / f"critic_{report_version}.md",
        _critic_markdown(
            draft_version=report_version,
            issues=all_issues,
            audit_rows=audit_rows,
            next_dimension=next_dimension,
        ),
    )
    _write_jsonl(reviews_dir / "claim_audit.jsonl", audit_rows)
    _write_text(
        reviews_dir / "revision_plan.md",
        _revision_plan_markdown(next_dimension=next_dimension, issues=all_issues),
    )
    _write_json(reviews_dir / "blocking_issues.json", {"issues": _issue_payloads(blockers)})

    if _should_fallback_rewrite_after_critic(
        rewrite=rewrite,
        blockers=blockers,
        settings=settings,
        fallback_depth=_rewrite_fallback_depth,
    ):
        assert rewrite is not None
        complete_downstream_review_fallback(
            run,
            session,
            previous_rewrite=rewrite,
            blockers=blockers,
            draft_version=draft_dir.name,
            reason="downstream_critic_blockers",
        )
        return _run_critic_with_session(
            run_id,
            session,
            hooks,
            prompt_overrides=prompt_overrides,
            _rewrite_fallback_depth=_rewrite_fallback_depth + 1,
        )

    # PR-G-CriticScores wire-up (codex round-3 AGREE on v4 polish-loop
    # design): score the pipeline manuscript against the
    # shadow_baseline (when present) on 3 LLM-judged dims (合规性 /
    # 创新性 / 完整性). When ``Settings.polish_loop_enabled`` is True
    # AND a real (non-stub) baseline exists on disk, run the blind
    # A/B critic call and persist ``reviews/polish_quality.json`` for
    # downstream consumers (acceptance gate, real-paper scorers,
    # human reviewers). Rewrite is deferred — this wire-up adds
    # scoring infrastructure only.
    polish_result = _maybe_run_polish_scoring(
        run=run,
        draft_dir=draft_dir,
        run_dir=run_dir,
        reviews_dir=reviews_dir,
        session=session,
        hooks=hooks,
        project=project,
    )
    if polish_result is not None:
        append_event(
            session,
            run,
            "polish_quality_scored",
            {
                "phase": "critic",
                "draft_version": draft_dir.name,
                "polish_status": polish_result.status,
                "baseline_mode": polish_result.baseline_mode,
                "failed_dims": list(polish_result.failed_dims),
            },
        )
        session.commit()

    north_star_gate = _maybe_run_north_star_gate_sidecar(
        run=run,
        project=project,
        session=session,
        draft=draft,
        reviews_dir=reviews_dir,
    )
    if north_star_gate is not None:
        append_event(
            session,
            run,
            "north_star_gate_scored",
            {
                "phase": "critic",
                "sidecar_only": True,
                "blocking": False,
                "status": north_star_gate.get("status"),
                "pass": north_star_gate.get("pass"),
                "max_loss": north_star_gate.get("max_loss"),
                "n_valid_samples": north_star_gate.get("n_valid_samples"),
            },
        )
        session.commit()

    summary = {
        "phase": "critic",
        "draft_version": draft_dir.name,
        "manuscript_source": "rewrite" if rewrite is not None else "stylist",
        **({"rewrite_version": rewrite.version} if rewrite is not None else {}),
        "issues": len(all_issues),
        "blocking_issues": len(blockers),
        "next_revision_dimension": next_dimension,
        "next_stage": "external_scan_approval",
        "polish_quality": polish_result.to_dict() if polish_result is not None else None,
        "north_star_gate": north_star_gate,
    }
    transition(
        run,
        "USER_EXTERNAL_SCAN_APPROVAL",
        session,
        reason="Critic completed",
        payload=summary,
    )
    append_event(session, run, "phase_done", summary)
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


def _should_fallback_rewrite_after_critic(
    *,
    rewrite: object | None,
    blockers: Sequence[CriticIssue],
    settings: object,
    fallback_depth: int,
) -> bool:
    if rewrite is None or not blockers:
        return False
    if fallback_depth > 0:
        return False
    if not bool(getattr(settings, "final_rewrite_holistic", False)):
        return False
    audit = getattr(rewrite, "audit", {})
    return not (isinstance(audit, Mapping) and audit.get("fallback_to_original") is True)


def _maybe_run_north_star_gate_sidecar(
    *,
    run: Run,
    project: Project,
    session: Session,
    draft: str,
    reviews_dir: Path,
) -> dict[str, object] | None:
    settings = get_settings()
    if not getattr(settings, "north_star_gate_enabled", True):
        return None
    try:
        from autoessay.agents.north_star_gate_runner import run_north_star_gate_sidecar

        return run_north_star_gate_sidecar(
            run=run,
            project=project,
            session=session,
            pipeline_md=draft,
            reviews_dir=reviews_dir,
        )
    except Exception as exc:  # noqa: BLE001 - sidecar observability must not block critic.
        payload: dict[str, object] = {
            "status": "error",
            "phase": "critic",
            "sidecar_only": True,
            "blocking": False,
            "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        _write_json(reviews_dir / "north_star_gate.json", payload)
        return payload


def _fail_fixable(run: Run, session: Session, guidance: str) -> dict[str, object]:
    if run.state != "FAILED_FIXABLE":
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="Critic needs user-fixable input",
            payload={"guidance": guidance},
        )
    append_event(
        session,
        run,
        "phase_failed",
        {
            "phase": "critic",
            "failure_class": "failed_fixable",
            "guidance": guidance,
        },
    )
    session.commit()
    return {"run_id": run.id, "state": run.state, "guidance": guidance}


def _critic_via_harness(
    *,
    draft: str,
    claim_map: Sequence[Mapping[str, object]],
    shortlist: Sequence[NormalizedSource],
    claims: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, object],
    selected_thesis: Mapping[str, object],
    run: Run,
    project: Project,
    hooks: HookRegistry,
    audit: AuditWriter,
    audit_state: CriticHarnessCitationAudit,
    draft_version: str,
    instructions_override: str | None = None,
) -> list[CriticIssue] | None:
    from autoessay.agents._research_kernel_prompt import (
        KERNEL_INJECTION_GUARD,
        research_kernel_for_prompt,
    )

    research_kernel = research_kernel_for_prompt(
        getattr(run, "research_kernel_json", None),
    )
    accumulated_context = phase_context_prompt_block(run.run_dir, "critic")
    prompt = _critic_prompt(
        draft=draft,
        claim_map=claim_map,
        shortlist=shortlist,
        claims=claims,
        source_notes=source_notes,
        selected_thesis=selected_thesis,
        suffix="",
        instructions_override=instructions_override,
        project_title=project.title,
        research_kernel=research_kernel,
        accumulated_context=accumulated_context,
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Critic. Identify unsupported claims, weak transitions, "
                    "missing counterarguments, and novelty overclaims. The user's "
                    "project_title and research_kernel are the substantive ground "
                    "truth — flag any paragraph that drifts from them as a "
                    "``thesis`` or ``structure`` issue (use the description field to "
                    "say 'drifts from project_title/research_kernel'). Do not "
                    "rewrite. "
                    + KERNEL_INJECTION_GUARD
                    + " "
                    + language_directive(project.language)
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.1,
        max_tokens=2200,
        response_format={"type": "json_object"},
        request_id=f"critic_report_{draft_version}",
        prompt_template_id="critic.report.v1",
    )
    call_hooks = _copy_hook_registry(hooks)
    _register_critic_memory_hook(call_hooks)
    call_hooks.register_post_llm(
        "citation_audit",
        _make_critic_citation_audit_hook(
            claim_map=claim_map,
            shortlist=shortlist,
            audit_state=audit_state,
        ),
    )
    context = HookContext(
        run_id=run.id,
        phase="critic",
        step_id="critic.report",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "critic",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "draft_version": draft_version,
            "claim_count": len(claim_map),
            "shortlist_count": len(shortlist),
            "memory_query": (
                f"phase=critic topic={project.title} draft_version={draft_version} "
                f"claim_count={len(claim_map)}"
            ),
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=call_hooks,
                context=context,
                output_schema=CriticReport,
                audit=audit,
                max_corrective_retries=2,
                llm_optional=False,
            ),
        )
    except SchemaViolationError:
        return None
    except Exception:  # noqa: BLE001 - worker records fixable failure.
        return None
    return _critic_issues_from_output(response.parsed)


def _make_critic_citation_audit_hook(
    *,
    claim_map: Sequence[Mapping[str, object]],
    shortlist: Sequence[NormalizedSource],
    audit_state: CriticHarnessCitationAudit,
) -> Callable[[HookContext, object], HookResult]:
    def post_llm(_ctx: HookContext, _response: object) -> HookResult:
        audit_rows, blocker_issues = audit_claim_map(claim_map, shortlist)
        audit_state.audit_rows = audit_rows
        audit_state.blocker_issues = blocker_issues
        return HookResult(
            annotations={
                "claims_checked": len(audit_rows),
                "blockers_injected": len(blocker_issues),
            },
        )

    return post_llm


def _register_critic_memory_hook(hooks: HookRegistry) -> None:
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


def _copy_hook_registry(base_hooks: HookRegistry) -> HookRegistry:
    copied = HookRegistry()
    copied._pre_llm = list(base_hooks._pre_llm)
    copied._post_llm = list(base_hooks._post_llm)
    copied._pre_tool = list(base_hooks._pre_tool)
    copied._post_tool = list(base_hooks._post_tool)
    return copied


def _critic_issues_from_output(parsed: object) -> list[CriticIssue] | None:
    if isinstance(parsed, CriticReport):
        return list(parsed.issues)
    if not isinstance(parsed, Mapping):
        return None
    try:
        raw = RawCriticResponse.parse_obj(parsed)
    except ValidationError:
        return None
    return raw.issues


def _stub_critic_issues(claim_map: Sequence[Mapping[str, object]]) -> list[CriticIssue]:
    first_paragraph = "stub-p001"
    if claim_map:
        first_paragraph = _string_value(claim_map[0].get("paragraph_id")) or first_paragraph
    return [
        CriticIssue(
            issue_id="critic_stub_001",
            severity="LOW",
            dimension="structure",
            paragraph_id=first_paragraph,
            source_ids=[],
            description="Stub critic found no blocking argument defect.",
            suggested_action="REWRITE",
        ),
    ]


def _filter_baseline_as_evidence_test_issues(
    issues: Sequence[CriticIssue],
) -> list[CriticIssue]:
    """Drop critic-only objections to the TEST source being synthetic.

    Deterministic claim audit still runs unchanged. This filter only
    prevents the LLM critic from reclassifying the explicitly enabled
    ``shadow_baseline_v001`` test source as illegal merely because its
    title/source_id says "test". Mixed-source issues are kept unless
    their description is specifically about the shadow/test source
    legality; substantive complaints about unsupported claims still
    pass through.
    """
    filtered: list[CriticIssue] = []
    for issue in issues:
        source_ids = set(issue.source_ids)
        description = issue.description.casefold()
        complains_about_test_source = any(
            marker in description
            for marker in (
                "shadow_baseline_v001",
                "shadow baseline",
                "test source",
                "source status",
                "测试源",
                "测试性",
                "测试",
                "占位",
                "来源状态",
                "核验来源",
            )
        )
        complains_about_missing_shadow_evidence = (
            source_ids == {"shadow_baseline_v001"}
            and issue.suggested_action == "ADD_EVIDENCE"
            and any(
                marker in description
                for marker in (
                    "add evidence",
                    "primary source",
                    "primary sample",
                    "source material",
                    "evidence chain",
                    "first-hand",
                    "一手",
                    "样本",
                    "证据链",
                    "材料链",
                    "缺少",
                    "不足以支撑",
                    "尚不足以支撑",
                )
            )
        )
        if "shadow_baseline_v001" in source_ids and complains_about_test_source:
            continue
        if complains_about_missing_shadow_evidence:
            continue
        filtered.append(issue)
    return filtered


def _critic_prompt(
    *,
    draft: str,
    claim_map: Sequence[Mapping[str, object]],
    shortlist: Sequence[NormalizedSource],
    claims: Sequence[Mapping[str, object]],
    source_notes: Mapping[str, object],
    selected_thesis: Mapping[str, object],
    suffix: str,
    instructions_override: str | None = None,
    project_title: str = "",
    research_kernel: Mapping[str, object] | None = None,
    accumulated_context: str = "",
) -> str:
    """Build the critic's review LLM prompt.

    ``instructions_override`` replaces the static instruction block
    (codex-AGREEd #2 stage 2.B). The dynamic context (draft text,
    claim map, evidence pack, schema spec) is always appended.
    """
    from autoessay.prompts import CRITIC_MAIN_INSTRUCTIONS

    instructions = instructions_override or CRITIC_MAIN_INSTRUCTIONS
    required_schema = {
        "issues": [
            {
                "issue_id": "critic_001",
                "severity": "BLOCKER|HIGH|MEDIUM|LOW",
                "dimension": "thesis|structure|evidence|prose",
                "paragraph_id": "paragraph id or null",
                "source_ids": ["source_id"],
                "description": "specific issue",
                "suggested_action": "REWRITE|ADD_EVIDENCE|REMOVE|RESCOPE|VERIFY_CITATION",
            },
        ],
    }
    evidence_pack = {
        # PR-J7: project_title + research_kernel are the substantive
        # ground truth for drift detection. Empty/missing kernel →
        # empty {} (degrade to title-only anchoring).
        "project_title": project_title,
        "research_kernel": dict(research_kernel) if research_kernel else {},
        "shortlist": [
            {
                "source_id": source.source_id,
                "title": source.title,
                "authors": source.authors,
                "year": source.year,
                "doi": source.doi,
                "url": source.url,
                "access_status": source.access_status,
                "risk_flags": source.risk_flags,
            }
            for source in shortlist
        ],
        "source_notes": source_notes,
        "claims": list(claims),
        "selected_thesis": dict(selected_thesis),
    }
    baseline_as_evidence_policy = ""
    if get_settings().baseline_as_evidence_test and any(
        source.source_id == "shadow_baseline_v001" for source in shortlist
    ):
        baseline_as_evidence_policy = (
            " baseline_as_evidence_test_policy: "
            "AUTOESSAY_BASELINE_AS_EVIDENCE_TEST is enabled. "
            "``shadow_baseline_v001`` is a legal approved TEST-only source "
            "for this run. Do not mark it BLOCKER/HIGH merely because it is "
            "synthetic, internal, or test-labeled. You may still flag actual "
            "copying, ungrounded paraphrase, unsupported claims, or raw "
            "citation-marker defects. If another non-shadow source is weak, "
            "write that as a separate issue instead of bundling it with "
            "shadow_baseline_v001."
        )
    return (
        "You are Critic. "
        f"Draft: {draft}. "
        f"{accumulated_context}"
        f"Claim map: {json.dumps(list(claim_map), sort_keys=True)}. "
        f"Evidence matrix: {json.dumps(evidence_pack, sort_keys=True)}. "
        f"{baseline_as_evidence_policy} "
        f"{instructions} "
        f"Return strict JSON matching this schema: {json.dumps(required_schema, sort_keys=True)}"
        f"{suffix}"
    )


def _parse_critic_response(value: str) -> list[CriticIssue] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    try:
        raw = RawCriticResponse.parse_obj(decoded)
    except ValidationError:
        return None
    return raw.issues


def _source_has_exportable_reference(source: NormalizedSource) -> bool:
    if _has_text(source.doi) or _has_text(source.url):
        return True
    access_status = str(source.access_status)
    if access_status == "user_upload":
        return True
    if source.source_client == "user_upload" or source.license == "user_upload_local_only":
        return True
    return "unverified-user-supplied" in source.risk_flags


def _has_text(value: str | None) -> bool:
    return bool(value and value.strip())


def _audit_failure_description(
    paragraph_id: str,
    failures: Sequence[Mapping[str, object]],
) -> str:
    source_parts: list[str] = []
    for failure in failures:
        raw_source_id = failure.get("source_id")
        source_id = raw_source_id if isinstance(raw_source_id, str) else "missing"
        reason = _string_value(failure.get("reason")) or "invalid reference"
        source_parts.append(f"{source_id}: {reason}")
    return f"Paragraph {paragraph_id} has citation audit blocker(s): {'; '.join(source_parts)}."


def _recommended_dimension(issues: Sequence[CriticIssue]) -> str:
    severity_weight = {"BLOCKER": 8, "HIGH": 4, "MEDIUM": 2, "LOW": 1}
    scores = {"thesis": 0, "structure": 0, "evidence": 0, "prose": 0}
    for issue in issues:
        scores[issue.dimension] += severity_weight[issue.severity]
    return max(scores.items(), key=lambda item: item[1])[0]


def _critic_markdown(
    *,
    draft_version: str,
    issues: Sequence[CriticIssue],
    audit_rows: Sequence[Mapping[str, object]],
    next_dimension: str,
) -> str:
    lines = [
        "# Critic Review",
        "",
        f"- Draft version: {draft_version}",
        f"- Issues: {len(issues)}",
        f"- Blocking issues: {sum(1 for issue in issues if issue.severity == 'BLOCKER')}",
        f"- Recommended next revision dimension: {next_dimension}",
        "",
        "## Issues",
        "",
    ]
    if not issues:
        lines.append("No issues returned.")
    for issue in issues:
        source_ids = ", ".join(issue.source_ids) if issue.source_ids else "none"
        paragraph_id = issue.paragraph_id or "not specified"
        lines.extend(
            [
                f"### {issue.issue_id}",
                "",
                f"- Severity: {issue.severity}",
                f"- Dimension: {issue.dimension}",
                f"- Paragraph: {paragraph_id}",
                f"- Sources: {source_ids}",
                f"- Suggested action: {issue.suggested_action}",
                "",
                issue.description,
                "",
            ],
        )
    lines.extend(["## Citation Audit", ""])
    for row in audit_rows:
        lines.append(
            "- "
            f"{row.get('paragraph_id')}: {row.get('status')} "
            f"({', '.join(_source_ids(row.get('source_ids'))) or 'no source'})"
        )
    return "\n".join(lines).rstrip() + "\n"


def _revision_plan_markdown(*, next_dimension: str, issues: Sequence[CriticIssue]) -> str:
    blockers = [issue for issue in issues if issue.severity == "BLOCKER"]
    lines = [
        "# Revision Plan",
        "",
        f"Recommended next revision dimension: `{next_dimension}`.",
        "",
    ]
    if blockers:
        lines.extend(["## Blocking Issues", ""])
        for issue in blockers:
            paragraph_id = issue.paragraph_id or "not specified"
            lines.append(f"- {issue.issue_id} ({paragraph_id}): {issue.description}")
    else:
        lines.append("No blocking issues were found. Use the highest-severity issue set first.")
    return "\n".join(lines).rstrip() + "\n"


def _issue_payloads(issues: Sequence[CriticIssue]) -> list[dict[str, object]]:
    return [dict(issue.dict()) for issue in issues]


def _source_ids(value: object) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item != "[UNCITED]"]
    if isinstance(value, str) and value and value != "[UNCITED]":
        return [value]
    return []


def _normalize_raw_source_id_markers_for_review(
    manuscript: str,
    claim_map: Sequence[Mapping[str, object]],
    shortlist: Sequence[NormalizedSource],
) -> str:
    cited_ids: set[str] = set()
    for claim in claim_map:
        cited_ids.update(_source_ids(claim.get("source_ids")))
    if not manuscript.strip() or not cited_ids:
        return manuscript
    cited_sources = [source for source in shortlist if source.source_id in cited_ids]
    source_to_tag = {
        source.source_id: f"[{index}]" for index, source in enumerate(cited_sources, 1)
    }
    if not source_to_tag:
        return manuscript
    normalized = manuscript
    bracket_patterns = (
        r"\[\s*([^\]]+?)\s*\]",
        r"［\s*([^］]+?)\s*］",
        r"【\s*([^】]+?)\s*】",
        r"〔\s*([^〕]+?)\s*〕",
        r"[（(]\s*([^）)]+?)\s*[）)]",
    )
    for pattern in bracket_patterns:
        normalized = re.sub(
            pattern,
            lambda match: _replace_composite_source_id_marker(match, source_to_tag),
            normalized,
        )
    ordered_sources = sorted(source_to_tag.items(), key=lambda item: len(item[0]), reverse=True)
    for source_id, tag in ordered_sources:
        escaped = re.escape(source_id)
        normalized = re.sub(rf"[\[［【]\s*{escaped}\s*[\]］】]", tag, normalized)
        normalized = re.sub(rf"[（(]\s*{escaped}\s*[）)]", tag, normalized)
    return normalized


def _replace_composite_source_id_marker(
    match: re.Match[str],
    source_to_tag: Mapping[str, str],
) -> str:
    inner = match.group(1).strip()
    pieces = _split_source_marker_pieces(inner)
    if len(pieces) < 2:
        return match.group(0)
    tags: list[str] = []
    for piece in pieces:
        tag = source_to_tag.get(piece)
        if tag is None:
            return match.group(0)
        tags.append(tag)
    return "".join(tags)


def _split_source_marker_pieces(inner: str) -> list[str]:
    return [
        piece.strip() for piece in re.split(r"\s*(?:[;；,，、]|\s+)\s*", inner) if piece.strip()
    ]


def _string_value(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _latest_review_path(reviews_dir: Path) -> Path | None:
    if not reviews_dir.exists():
        return None
    candidates = sorted(reviews_dir.glob("critic_v[0-9][0-9][0-9].md"))
    return candidates[-1] if candidates else None


def _load_source_notes(path: Path) -> dict[str, object]:
    notes: dict[str, object] = {}
    if not path.exists():
        return notes
    for note_path in sorted(path.glob("*.json")):
        note = _load_json_mapping(note_path)
        source_id = note.get("source_id")
        if isinstance(source_id, str) and source_id:
            notes[source_id] = note
    return notes


def _read_sources_json(path: Path) -> list[NormalizedSource]:
    records = _load_json_array(path)
    sources: list[NormalizedSource] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            sources.append(NormalizedSource.parse_obj(record))
        except ValidationError:
            continue
    return sources


def _load_json_array(path: Path) -> list[object]:
    if not path.exists():
        return []
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _load_json_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: value for key, value in decoded.items() if isinstance(key, str)}


def _load_jsonl_objects(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                records.append(decoded)
    return records


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _write_jsonl(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n",
            )
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


# -----------------------------------------------------------------
# PR-G-CriticScores wire-up — polish-loop scoring (no rewrite yet)
# -----------------------------------------------------------------


def _maybe_run_polish_scoring(
    *,
    run: Run,
    draft_dir: Path,
    run_dir: Path,
    reviews_dir: Path,
    session: Session,
    hooks: HookRegistry,
    project: Project,
) -> PolishLoopResult | None:
    """PR-G-CriticScores wire-up: blind A/B LLM eval of pipeline
    vs shadow_baseline manuscript. Returns ``PolishLoopResult`` or
    ``None`` when the gate decides not to run.

    Status decision tree:
    - ``Settings.polish_loop_enabled = False`` → skipped_disabled
    - ``Settings.critic_stub = True`` → no LLM call, return None
    - shadow_baseline artifact missing or stub mode → skipped_no_real_baseline
    - LLM call fails or invalid → return None (caller skips event)
    - Else: status = passed / failed_to_beat based on margin
    """
    settings = get_settings()
    if not settings.polish_loop_enabled:
        return _polish_skipped_result("skipped_disabled", "missing")
    if settings.critic_stub:
        # Stub mode: no LLM, no event — keep CI deterministic.
        return None

    from autoessay.agents._critic_polish_loop import (
        DEFAULT_PASS_MARGIN,
        POLISH_BLIND_EVAL_SYSTEM_PROMPT,
        POLISH_BLIND_EVAL_USER_TEMPLATE,
        PolishLoopResult,
        QualityScoreSet,
        _candidate_report_from_letter,
        _PolishCritiqueOutput,
        evaluate_pass_margin,
        manuscript_eval_metadata,
    )
    from autoessay.agents._final_manuscript_resolver import (
        read_final_manuscript,
    )
    from autoessay.agents.shadow_baseline import load_shadow_baseline

    baseline = load_shadow_baseline(run_dir)
    if baseline is None:
        return _polish_skipped_result("skipped_no_real_baseline", "missing")
    baseline_md = baseline.manuscript_markdown.strip()
    if not baseline_md:
        return _polish_skipped_result("skipped_no_real_baseline", "missing")
    # Stub detection: ``shadow_baseline._stub_output`` produces a
    # manuscript that opens with the literal sentinel "stub-mode
    # shadow baseline". No ``metadata`` field on the schema, so
    # this content-marker is the cleanest stub-vs-real signal.
    if "stub-mode shadow baseline" in baseline_md[:200]:
        return _polish_skipped_result("skipped_no_real_baseline", "stub")

    rewrite = load_latest_rewrite_artifact(run_dir)
    if rewrite is not None:
        pipeline_md = rewrite.manuscript
        claim_map = rewrite.claim_map
        source = "rewrite"
    else:
        pipeline_md, _, source = read_final_manuscript(run_dir, draft_dir.name)
        claim_map = _load_jsonl_objects(draft_dir / "claim_map.jsonl")
    if not pipeline_md.strip():
        return _polish_skipped_result("skipped_no_real_baseline", "missing")
    shortlist = _read_sources_json(run_dir / "sources" / "shortlist.json")
    pipeline_md = _normalize_raw_source_id_markers_for_review(pipeline_md, claim_map, shortlist)

    system_prompt = POLISH_BLIND_EVAL_SYSTEM_PROMPT
    pass_margin = DEFAULT_PASS_MARGIN
    if settings.baseline_as_evidence_test:
        pass_margin = 0.0
        system_prompt += (
            "\n\nAUTOESSAY_BASELINE_AS_EVIDENCE_TEST 已启用："
            "本轮评分用于验证 synthesizer/drafter/stylist/final_rewrite/"
            "critic/integrity 等后续阶段是否健康，不用于验证源池获取能力。"
            "若候选稿引用 shadow_baseline_v001 或 Shadow Baseline Evidence "
            "Dossier，把它视为合法的已知良好测试源；不要因其 synthetic/"
            "internal/test-only 属性扣合规分。仍需按正文质量扣分："
            "逐字照抄、未引用断言、TODO/[UNCITED]/原始 cite marker、"
            "CNKI 体例缺失、论证不完整或自相矛盾都照常扣分。"
        )
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Critic")
    # PR-G-CriticScores wireup fix: use a fresh HookRegistry for the
    # polish blind-eval call. The critic's main hooks include the
    # harness output sanity gate (forbids ``[UNCITED]``,
    # ``TODO_EVIDENCE`` etc. as drafter-output sentinels), but the
    # critic's blind-eval *judges* whether the manuscripts contain
    # those sentinels — its justification text says "未见
    # [UNCITED]" / "未见 TODO_EVIDENCE" verbatim, which the gate
    # then rejects as a sentinel violation. Real-paper round 4
    # surfaced this: the polish LLM call retried twice with both
    # attempts rejected on this exact pattern. Polish blind-eval
    # is purely an evaluator — none of the drafter sanity gates
    # apply to it.
    polish_hooks = HookRegistry()

    def score_one(
        *,
        label: str,
        manuscript_md: str,
    ) -> (
        tuple[
            QualityScoreSet,
            object,
            _PolishCritiqueOutput,
            dict[str, object],
        ]
        | None
    ):
        metadata = manuscript_eval_metadata(manuscript_md)
        user_prompt = (
            POLISH_BLIND_EVAL_USER_TEMPLATE.replace("{{candidate_id}}", "A")
            .replace(
                "{{metadata_json}}",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            )
            .replace("{{manuscript}}", manuscript_md)
        )
        request = LLMCallRequest(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=settings.one_api_model,
            temperature=0.0,
            max_tokens=8000,
            response_format={"type": "json_object"},
            request_id=f"critic_polish_blind_eval_{run.id}_{label}",
            prompt_template_id="critic.polish_blind_eval.single.v3",
        )
        context = HookContext(
            run_id=run.id,
            phase="critic",
            step_id=f"critic.polish_blind_eval.{label}",
            user_id=project.user_id,
            attempt=1,
            prompt_template_id=request.prompt_template_id,
            prompt_filled=user_prompt,
            prompt_hash=hash_text(user_prompt),
            project_title=project.title,
            run_metadata={"candidate_label": label},
        )
        try:
            response = asyncio.run(
                run_llm_step(
                    request=request,
                    hooks=polish_hooks,
                    context=context,
                    output_schema=_PolishCritiqueOutput,
                    audit=audit,
                    max_corrective_retries=1,
                    llm_optional=True,
                ),
            )
        except Exception:  # noqa: BLE001
            return None
        parsed = response.parsed
        if not isinstance(parsed, _PolishCritiqueOutput):
            return None
        report = _candidate_report_from_letter(parsed, "a")
        return report.scores, report, parsed, metadata

    baseline_result = score_one(label="baseline", manuscript_md=baseline_md)
    pipeline_result = score_one(label="pipeline", manuscript_md=pipeline_md)
    if baseline_result is None or pipeline_result is None:
        return None

    baseline_scores, baseline_report, baseline_parsed, baseline_metadata = baseline_result
    pipeline_scores, pipeline_report, pipeline_parsed, pipeline_metadata = pipeline_result
    schema_partial_fields = [
        f"baseline.{field}" for field in getattr(baseline_parsed, "schema_partial_fields", []) or []
    ]
    schema_partial_fields.extend(
        f"pipeline.{field}" for field in getattr(pipeline_parsed, "schema_partial_fields", []) or []
    )
    for report in (pipeline_report, baseline_report):
        for field in getattr(report, "schema_partial_fields", []) or []:
            schema_partial_fields.append(
                f"{field}",
            )
    score_clipped = bool(
        getattr(pipeline_scores, "score_clipped", False)
        or getattr(baseline_scores, "score_clipped", False)
    )
    passed, failed_dims = evaluate_pass_margin(
        pipeline=pipeline_scores,
        baseline=baseline_scores,
        margin=pass_margin,
    )
    status: PolishStatus = "passed" if passed else "failed_to_beat"
    result = PolishLoopResult(
        status=status,
        baseline_mode="real",
        pipeline_quality_scores=pipeline_scores,
        baseline_quality_scores=baseline_scores,
        failed_dims=failed_dims,
        polish_attempts=0,
        margin=pass_margin,
        label_mapping={"baseline": "single", "pipeline": "single"},
        paired_review={
            "mode": "single_candidate_cached_baseline_ready",
            "baseline": baseline_parsed.dict(),
            "pipeline": pipeline_parsed.dict(),
            "baseline_metadata": baseline_metadata,
            "pipeline_metadata": pipeline_metadata,
        },
        schema_partial_fields=schema_partial_fields,
        score_clipped=score_clipped,
    )
    polish_quality_path = reviews_dir / "polish_quality.json"
    polish_quality_path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def _polish_skipped_result(
    status: PolishStatus,
    baseline_mode: BaselineMode,
) -> PolishLoopResult:
    """Convenience constructor for the skipped statuses that do
    not need a real LLM call."""
    from autoessay.agents._critic_polish_loop import PolishLoopResult

    return PolishLoopResult(
        status=status,
        baseline_mode=baseline_mode,
        pipeline_quality_scores=None,
        baseline_quality_scores=None,
        failed_dims=[],
        polish_attempts=0,
    )
