"""Production critic-loop helper used after final rewrite polish.

This is the production version of the paired-runner loop: score the current
pipeline manuscript against the frozen shadow baseline once, apply the v3
repair plan for a bounded number of iterations, then select the candidate with
the best north-star-style max-loss.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, StrictStr
from sqlalchemy.orm import Session

from autoessay.agents._critic_polish_loop import (
    CRITIC_LOOP_ACTIVE_DIMS,
    POLISH_BLIND_EVAL_SYSTEM_PROMPT,
    POLISH_BLIND_EVAL_USER_TEMPLATE,
    _candidate_report_from_letter,
    _PolishCritiqueOutput,
    manuscript_eval_metadata,
)
from autoessay.agents.shadow_baseline import load_shadow_baseline
from autoessay.config import get_settings
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    hash_text,
    run_llm_step,
)
from autoessay.models import Project, Run


class CriticLoopRewriteOutput(BaseModel):
    manuscript: StrictStr


@dataclass(frozen=True)
class CriticLoopRunResult:
    manuscript: str
    audit: dict[str, object]


def run_production_critic_loop(
    *,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    manuscript: str,
    rewrite_dir: Path,
    iterations: int,
) -> CriticLoopRunResult:
    """Run the bounded v3 critic loop and return the selected manuscript.

    Failures are non-fatal: the audit records why the loop skipped or stopped,
    and the input manuscript is returned unchanged.
    """

    loop_dir = rewrite_dir / "critic_loop"
    loop_dir.mkdir(parents=True, exist_ok=True)
    audit_path = loop_dir / "critic_loop.json"
    audit: dict[str, object] = {
        "status": "not_started",
        "phase": "final_rewrite",
        "critic_loop_iterations": iterations,
        "audit_path": _relative_to_run(run, audit_path),
        "iterations": [],
        "selected_iter": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    settings = get_settings()
    if not getattr(settings, "critic_loop_enabled", True):
        audit["status"] = "skipped_disabled"
        _write_json(audit_path, audit)
        return CriticLoopRunResult(manuscript=manuscript, audit=audit)
    if settings.critic_stub:
        audit["status"] = "skipped_critic_stub"
        _write_json(audit_path, audit)
        return CriticLoopRunResult(manuscript=manuscript, audit=audit)
    if iterations <= 0:
        audit["status"] = "skipped_zero_iterations"
        _write_json(audit_path, audit)
        return CriticLoopRunResult(manuscript=manuscript, audit=audit)

    baseline_md, baseline_source = _baseline_text(Path(run.run_dir))
    audit["baseline_source"] = baseline_source
    if not baseline_md.strip():
        audit["status"] = "skipped_no_real_baseline"
        _write_json(audit_path, audit)
        return CriticLoopRunResult(manuscript=manuscript, audit=audit)
    if not manuscript.strip():
        audit["status"] = "skipped_empty_pipeline_manuscript"
        _write_json(audit_path, audit)
        return CriticLoopRunResult(manuscript=manuscript, audit=audit)

    baseline_score_result = _score_manuscript(
        run=run,
        project=project,
        session=session,
        manuscript_md=baseline_md,
        label="baseline",
        iteration=None,
    )
    audit["baseline_score"] = baseline_score_result
    _write_json(audit_path, audit)
    if baseline_score_result.get("status") != "scored":
        audit["status"] = "failed_baseline_scoring"
        _write_json(audit_path, audit)
        return CriticLoopRunResult(manuscript=manuscript, audit=audit)

    baseline_scores = _dict_payload(baseline_score_result.get("scores"))
    current_md = manuscript.strip()

    # Round 0 — holistic revision (codex AGREE-WITH-AMENDMENTS 2026-05-12).
    # Score the input manuscript once, then ask the v3 critic to integrate
    # every repair_plan item into one full rewrite before the structured
    # for-loop starts. Round 0 unconditionally accepts the rewrite iff it
    # passes the deterministic sanity gate; failure paths keep ``current_md``
    # unchanged so the structured loop continues to operate on the original
    # input manuscript. ``iterations`` stays at 3 so ``selected_iter``
    # semantics (and the existing take-best invariant on the for-loop)
    # don't shift; the budget impact is a single extra LLM round per run.
    # PR-366 (2026-05-13): switched from env flag to per-run
    # ``run.mathematical_mode``. Same toggle as the polish loop —
    # the user's "数理增强模式" checkbox enables BOTH round-0 loops
    # together (codex PR-366 review). Default false → cheap path.
    mathematical_mode_active = bool(getattr(run, "mathematical_mode", False))
    round0_holistic_audit: dict[str, object] = {
        "enabled": mathematical_mode_active,
        "status": "skipped_disabled",
    }
    audit["round0_holistic"] = round0_holistic_audit
    if mathematical_mode_active:
        round0_holistic_audit["status"] = "attempted"
        round0_score = _score_manuscript(
            run=run,
            project=project,
            session=session,
            manuscript_md=current_md,
            label="round0_input",
            iteration=None,
        )
        round0_holistic_audit["pre_round0_score"] = round0_score
        if round0_score.get("status") != "scored":
            round0_holistic_audit["status"] = "precritique_failed_skipped"
        else:
            round0_text = _holistic_round0_rewrite(
                run=run,
                project=project,
                session=session,
                hooks=hooks,
                manuscript_md=current_md,
                critique_payload=round0_score,
            )
            if round0_text is None:
                round0_holistic_audit["status"] = "failed_skipped"
            else:
                from autoessay.agents import drafter
                from autoessay.agents._round0_helpers import (
                    round0_sanity_check_with_deps,
                )
                from autoessay.agents.final_rewrite import (
                    _controlled_polish_cnki_structure_errors,
                )

                sanity = round0_sanity_check_with_deps(
                    candidate_text=round0_text,
                    incumbent_text=current_md,
                    language=project.language,
                    extract_citations=drafter._extract_inline_citations,
                    cnki_structure_errors=_controlled_polish_cnki_structure_errors,
                )
                round0_holistic_audit["sanity"] = sanity
                if not sanity["ok"]:
                    round0_holistic_audit["status"] = "sanity_failed_skipped"
                else:
                    round0_path = loop_dir / "round0_holistic_manuscript.md"
                    _write_text(round0_path, round0_text + "\n")
                    round0_holistic_audit["round0_manuscript_path"] = _relative_to_run(
                        run, round0_path
                    )
                    current_md = round0_text
                    round0_holistic_audit["status"] = "succeeded"
        _write_json(audit_path, audit)

    scored: list[dict[str, object]] = []
    iterations_payload = audit["iterations"]
    assert isinstance(iterations_payload, list)
    for idx in range(iterations):
        candidate_hash = hash_text(current_md)
        candidate_path = loop_dir / f"candidate_iter{idx}.md"
        _write_text(candidate_path, current_md + "\n")
        score_result = _score_candidate(
            run=run,
            project=project,
            session=session,
            candidate_md=current_md,
            baseline_scores=baseline_scores,
            iteration=idx,
        )
        row: dict[str, object] = {
            "iter": idx,
            "candidate_hash": candidate_hash,
            "candidate_path": _relative_to_run(run, candidate_path),
            **score_result,
        }
        iterations_payload.append(row)
        _write_json(audit_path, audit)
        if row.get("status") != "scored":
            break
        scored.append(row)
        if idx >= iterations - 1:
            break
        repair_plan = row.get("repair_plan_to_full_score")
        if not isinstance(repair_plan, list) or not repair_plan:
            row["rewrite_to_next"] = {
                "status": "no_repair_plan_reused_candidate",
                "next_hash": candidate_hash,
            }
            continue
        rewritten = _rewrite_candidate(
            run=run,
            project=project,
            session=session,
            candidate_md=current_md,
            baseline_md=baseline_md,
            repair_plan=repair_plan,
            iteration=idx,
            hooks=hooks,
        )
        if not rewritten:
            row["rewrite_to_next"] = {"status": "failed"}
            _write_json(audit_path, audit)
            break
        current_md = rewritten.strip()
        row["rewrite_to_next"] = {
            "status": "ok",
            "next_hash": hash_text(current_md),
        }
        _write_json(audit_path, audit)

    if not scored:
        audit["status"] = "failed_no_scored_iterations"
        _write_json(audit_path, audit)
        return CriticLoopRunResult(manuscript=manuscript, audit=audit)

    selected = max(
        scored,
        key=lambda item: (
            _float_metric(item.get("max_loss"), -999.0),
            _float_metric(item.get("sum_delta"), -999.0),
        ),
    )
    selected_iter = _int_metric(selected.get("iter"), 0)
    selected_candidate_path = loop_dir / f"candidate_iter{selected_iter}.md"
    selected_md = selected_candidate_path.read_text(encoding="utf-8").strip()
    selected_path = loop_dir / "selected_manuscript.md"
    _write_text(selected_path, selected_md + "\n")
    audit.update(
        {
            "status": "selected",
            "selected_iter": selected_iter,
            "selected_candidate_hash": selected.get("candidate_hash"),
            "selected_manuscript_path": _relative_to_run(run, selected_path),
            "selected_metrics": {
                "pipeline_quality_scores": selected.get("pipeline_quality_scores"),
                "baseline_quality_scores": selected.get("baseline_quality_scores"),
                "candidate_scores": selected.get("candidate_scores"),
                "baseline_scores": selected.get("baseline_scores"),
                "score_deltas": selected.get("score_deltas"),
                "max_loss": selected.get("max_loss"),
                "sum_delta": selected.get("sum_delta"),
            },
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    _write_json(audit_path, audit)
    return CriticLoopRunResult(manuscript=selected_md, audit=audit)


def _baseline_text(run_dir: Path) -> tuple[str, str | None]:
    baseline = load_shadow_baseline(run_dir)
    if baseline is None:
        return "", None
    baseline_md = baseline.manuscript_markdown.strip()
    if not baseline_md or "stub-mode shadow baseline" in baseline_md[:200]:
        return "", None
    return baseline_md, "shadow_baseline"


def _score_manuscript(
    *,
    run: Run,
    project: Project,
    session: Session,
    manuscript_md: str,
    label: str,
    iteration: int | None,
) -> dict[str, object]:
    user_prompt = (
        POLISH_BLIND_EVAL_USER_TEMPLATE.replace("{{candidate_id}}", "A")
        .replace(
            "{{metadata_json}}",
            json.dumps(
                manuscript_eval_metadata(manuscript_md),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        .replace("{{manuscript}}", manuscript_md)
    )
    suffix = label if iteration is None else f"{label}_iter{iteration}"
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": POLISH_BLIND_EVAL_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.0,
        max_tokens=8000,
        response_format={"type": "json_object"},
        request_id=f"critic_loop_score_{run.id}_{suffix}",
        prompt_template_id="critic_loop.polish_blind_eval.single.v3",
    )
    context = HookContext(
        run_id=run.id,
        phase="final_rewrite",
        step_id=f"final_rewrite.critic_loop.score.{label}",
        user_id=project.user_id,
        attempt=(iteration + 1) if iteration is not None else 1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=project.title,
        run_metadata={"critic_loop_iter": iteration, "score_label": label},
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=HookRegistry(),
                context=context,
                output_schema=_PolishCritiqueOutput,
                audit=AuditWriter(session=session, run_dir=run.run_dir, agent_name="CriticLoop"),
                max_corrective_retries=1,
                llm_optional=True,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - loop is optional.
        return {"status": "critic_failed", "fail_reason": f"{type(exc).__name__}: {exc}"}
    parsed = response.parsed
    if not isinstance(parsed, _PolishCritiqueOutput):
        return {"status": "critic_failed", "fail_reason": "parsed output missing"}
    report = _candidate_report_from_letter(parsed, "a")
    return {
        "status": "scored",
        "scores": _score_payload(report.scores),
        "deduction_ledger": _jsonable(getattr(report, "deduction_ledger", [])),
        "repair_plan_to_full_score": _jsonable(
            getattr(report, "repair_plan_to_full_score", []),
        ),
        "candidate_report": _jsonable(report.dict()),
        "critic_review": _jsonable(parsed.dict()),
    }


def _score_candidate(
    *,
    run: Run,
    project: Project,
    session: Session,
    candidate_md: str,
    baseline_scores: Mapping[str, object],
    iteration: int,
) -> dict[str, object]:
    result = _score_manuscript(
        run=run,
        project=project,
        session=session,
        manuscript_md=candidate_md,
        label="candidate",
        iteration=iteration,
    )
    if result.get("status") != "scored":
        return result
    pipeline_payload = _dict_payload(result.get("scores"))
    baseline_payload = _dict_payload(baseline_scores)
    deltas = {
        dim: _float_metric(pipeline_payload.get(dim), 0.0)
        - _float_metric(baseline_payload.get(dim), 0.0)
        for dim in CRITIC_LOOP_ACTIVE_DIMS
    }
    max_loss = min(deltas.values())
    return {
        "status": "scored",
        "pipeline_quality_scores": pipeline_payload,
        "baseline_quality_scores": baseline_payload,
        "candidate_scores": pipeline_payload,
        "baseline_scores": baseline_payload,
        "score_deltas": deltas,
        "max_loss": max_loss,
        "sum_delta": sum(deltas.values()),
        "repair_plan_to_full_score": result.get("repair_plan_to_full_score") or [],
        "deduction_ledger": result.get("deduction_ledger") or [],
        "pipeline_report": result.get("candidate_report"),
        "baseline_report": None,
        "paired_review": result.get("critic_review"),
    }


def _holistic_round0_rewrite(
    *,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    manuscript_md: str,
    critique_payload: Mapping[str, object],
) -> str | None:
    """Round-0 holistic rewrite for the critic loop.

    Replays the v3 critic's score call as a 4-turn chat-style request
    (system + user with manuscript + assistant with the critique JSON +
    user round-0 directive). Output schema is ``CriticLoopRewriteOutput``
    so it lands as a single ``manuscript`` field. Sanity gating happens
    in the caller; this helper only does the LLM round-trip.

    Codex AGREE-WITH-AMENDMENTS 2026-05-12: critic loop side is fine to
    keep V3 prompt provenance (unlike polish loop, which uses V2). The
    compact-critique preflight prevents stuffing the whole 6-13k-token
    critic JSON when channel context would be tight.
    """
    from autoessay.agents._round0_helpers import (
        ROUND0_DIRECTIVE_USER_TURN,
        build_round0_messages,
        compact_critique_payload,
        should_compact_critique,
    )

    settings = get_settings()
    user_turn_1 = (
        POLISH_BLIND_EVAL_USER_TEMPLATE.replace("{{candidate_id}}", "A")
        .replace(
            "{{metadata_json}}",
            json.dumps(
                manuscript_eval_metadata(manuscript_md),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        .replace("{{manuscript}}", manuscript_md)
    )

    critique_full = critique_payload.get("critic_review") or critique_payload
    try:
        critique_dict = dict(critique_full) if isinstance(critique_full, Mapping) else {}
    except Exception:  # noqa: BLE001 - defensive
        critique_dict = {}
    critique_text_full = json.dumps(critique_dict, ensure_ascii=False, sort_keys=True)

    max_output_tokens = 25000
    if should_compact_critique(
        system_text=POLISH_BLIND_EVAL_SYSTEM_PROMPT,
        user_turn_1_text=user_turn_1,
        critique_json_text=critique_text_full,
        user_turn_2_text=ROUND0_DIRECTIVE_USER_TURN,
        max_output_tokens=max_output_tokens,
        window_tokens=settings.round0_context_window_tokens,
    ):
        critique_assistant: dict[str, object] | str = compact_critique_payload(critique_dict)
        critique_compacted = True
    else:
        critique_assistant = critique_text_full
        critique_compacted = False

    messages = build_round0_messages(
        critique_system_prompt=POLISH_BLIND_EVAL_SYSTEM_PROMPT,
        critique_user_turn_1=user_turn_1,
        critique_assistant_payload=critique_assistant,
    )

    request = LLMCallRequest(
        messages=messages,
        model=settings.one_api_model,
        temperature=0.2,
        max_tokens=max_output_tokens,
        response_format={"type": "json_object"},
        request_id=f"critic_loop_holistic_round0_{run.id}",
        prompt_template_id="critic_loop.holistic_round0.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="final_rewrite",
        step_id="final_rewrite.critic_loop.holistic_round0",
        user_id=project.user_id,
        attempt=0,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=ROUND0_DIRECTIVE_USER_TURN,
        prompt_hash=hash_text(ROUND0_DIRECTIVE_USER_TURN),
        project_title=project.title,
        run_metadata={
            "critic_loop_iter": None,
            "round0_critique_compacted": critique_compacted,
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=CriticLoopRewriteOutput,
                audit=AuditWriter(
                    session=session,
                    run_dir=run.run_dir,
                    agent_name="CriticLoop",
                ),
                max_corrective_retries=1,
                llm_optional=True,
            ),
        )
    except Exception:  # noqa: BLE001 - round 0 must never abort the loop.
        return None
    parsed = response.parsed
    if isinstance(parsed, CriticLoopRewriteOutput):
        text = parsed.manuscript.strip()
        return text or None
    if isinstance(parsed, Mapping):
        try:
            text = CriticLoopRewriteOutput.parse_obj(parsed).manuscript.strip()
        except Exception:  # noqa: BLE001
            return None
        return text or None
    return None


def _rewrite_candidate(
    *,
    run: Run,
    project: Project,
    session: Session,
    candidate_md: str,
    baseline_md: str,
    repair_plan: Sequence[object],
    iteration: int,
    hooks: HookRegistry,
) -> str | None:
    payload = {
        "project_title": project.title,
        "language": project.language,
        "critic_loop_iter": iteration,
        "repair_plan_to_full_score": list(repair_plan),
        "rewrite_contract": {
            "goal": "Revise manuscript A toward the v3 critic full-score repair plan.",
            "allow_new_sections_or_paragraphs": True,
            "preserve_existing_inline_citations_when_still_relevant": True,
            "no_fabricated_sources_authors_years_books_statistics_or_causal_claims": True,
            "citation_changes_allowed_only_when_explicitly_required_by_repair_plan": True,
            "if_required_evidence_is_absent": (
                "downgrade or qualify the claim instead of inventing evidence"
            ),
            "output_full_markdown_only_inside_json_manuscript": True,
        },
        "current_candidate_manuscript": candidate_md,
        "baseline_manuscript_for_context_do_not_copy": baseline_md,
    }
    system_prompt = (
        "你是 production critic loop 的修改执行器。"
        "v3 critic 已经给出 repair_plan_to_full_score；请按该计划修改 A 稿。"
        "这是为了提高 compliance / novelty / completeness / evidence_strength 四维评分。"
        "可以按 repair_plan 新增、删除、移动、重写段落或章节。"
        "不得虚构 source_id、作者、年份、书名、统计数字、档案材料或因果断言；"
        "只有 repair_plan 明确要求且验收标准允许时，才可调整引用。"
        "如果某项 repair_plan 要求的证据在稿中或基线材料中不存在，"
        "请把断言降级为限制、研究设计或待验证路径。"
        "empirical_preservation_guard：保留输入 manuscript 中所有 LaTeX 公式块"
        "（$$...$$、$...$、\\begin{equation}...\\end{equation}）verbatim、"
        "所有 markdown 表格 verbatim、"
        "所有【待填】/【TBD】/【待补】/[FILL] 占位符 verbatim。"
        "占位符是 editorial scaffolding，不是 citation/source_id/bibliography entry，"
        "也不是已经成立的事实断言；不要把占位符填充成具体数字、人名年份、引用编号或新增 claim。"
        "如果原文形如实证结论（'研究表明X''结果显示Y'）但无对应表格、引用或【待填】支撑，"
        "必须降级为'理论预期X''若实证检验支持X''【待填：X 的回归结果】'。"
        "输出必须是严格 JSON，且只包含 manuscript 字段。"
    )
    user_prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.1,
        max_tokens=25000,
        response_format={"type": "json_object"},
        request_id=f"critic_loop_rewrite_{run.id}_iter{iteration}",
        prompt_template_id="critic_loop.rewrite_from_v3_repair_plan.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="final_rewrite",
        step_id="final_rewrite.critic_loop.rewrite",
        user_id=project.user_id,
        attempt=iteration + 1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=project.title,
        run_metadata={
            "critic_loop_iter": iteration,
            "repair_plan_count": len(repair_plan),
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=CriticLoopRewriteOutput,
                audit=AuditWriter(session=session, run_dir=run.run_dir, agent_name="CriticLoop"),
                max_corrective_retries=1,
                llm_optional=True,
            ),
        )
    except Exception:  # noqa: BLE001 - rejected candidate keeps incumbent.
        return None
    parsed = response.parsed
    if isinstance(parsed, CriticLoopRewriteOutput):
        return parsed.manuscript.strip()
    if isinstance(parsed, Mapping):
        try:
            return CriticLoopRewriteOutput.parse_obj(parsed).manuscript.strip()
        except Exception:  # noqa: BLE001
            return None
    return None


def _score_payload(scores: Any) -> dict[str, object]:
    if hasattr(scores, "dict"):
        value = scores.dict()
        return dict(value) if isinstance(value, dict) else {}
    if isinstance(scores, Mapping):
        return {str(key): value for key, value in scores.items()}
    return {}


def _dict_payload(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _float_metric(value: object, default: float) -> float:
    if not isinstance(value, int | float | str):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _int_metric(value: object, default: int) -> int:
    if not isinstance(value, int | float | str):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except TypeError:
        return str(value)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _relative_to_run(run: Run, path: Path) -> str:
    try:
        return str(path.relative_to(Path(run.run_dir)))
    except ValueError:
        return str(path)
