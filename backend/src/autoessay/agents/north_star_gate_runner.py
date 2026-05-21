"""North-star gate sidecar for the critic phase.

The gate is observability only in production: it writes an audit payload and
event data, but never changes run state or blocks the critic phase.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from autoessay.agents._north_star_gate import (
    NORTH_STAR_GATE_SYSTEM_PROMPT,
    NorthStarGateOutput,
    aggregate_gate_samples,
    build_north_star_gate_user_prompt,
    coin_flip_slots,
    evaluate_gate_sample,
    should_resample_gate,
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


def run_north_star_gate_sidecar(
    *,
    run: Run,
    project: Project,
    session: Session,
    pipeline_md: str,
    reviews_dir: Path,
) -> dict[str, object]:
    audit_path = reviews_dir / "north_star_gate.json"
    audit: dict[str, object] = {
        "status": "not_started",
        "phase": "critic",
        "sidecar_only": True,
        "blocking": False,
        "audit_path": _relative_to_run(run, audit_path),
        "samples": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    settings = get_settings()
    if not getattr(settings, "north_star_gate_enabled", True):
        audit["status"] = "skipped_disabled"
        _write_json(audit_path, audit)
        return audit
    if settings.critic_stub:
        audit["status"] = "skipped_critic_stub"
        _write_json(audit_path, audit)
        return audit
    if not pipeline_md.strip():
        audit["status"] = "skipped_empty_pipeline_manuscript"
        _write_json(audit_path, audit)
        return audit
    baseline = load_shadow_baseline(Path(run.run_dir))
    if baseline is None or not baseline.manuscript_markdown.strip():
        audit["status"] = "skipped_no_baseline"
        _write_json(audit_path, audit)
        return audit
    baseline_md = baseline.manuscript_markdown.strip()
    if "stub-mode shadow baseline" in baseline_md[:200]:
        audit["status"] = "skipped_stub_baseline"
        _write_json(audit_path, audit)
        return audit

    try:
        first = _run_gate_sample(
            run=run,
            project=project,
            session=session,
            pipeline_md=pipeline_md,
            baseline_md=baseline_md,
            sample_idx=0,
        )
        samples = [first]
        forced_samples = int(getattr(settings, "north_star_gate_force_samples", 0) or 0)
        target_samples = (
            max(1, forced_samples)
            if forced_samples > 0
            else 3
            if should_resample_gate(first)
            else 1
        )
        audit.update(
            {
                "status": "sampling",
                "forced_samples": forced_samples if forced_samples > 0 else None,
                "target_samples": target_samples,
                "samples": samples,
            },
        )
        _write_json(audit_path, audit)
        while len(samples) < target_samples:
            samples.append(
                _run_gate_sample(
                    run=run,
                    project=project,
                    session=session,
                    pipeline_md=pipeline_md,
                    baseline_md=baseline_md,
                    sample_idx=len(samples),
                ),
            )
            audit["samples"] = samples
            _write_json(audit_path, audit)
        result = aggregate_gate_samples(
            samples,
            forced_samples=forced_samples if forced_samples > 0 else None,
        )
        audit.update(
            {
                "status": "scored" if result.get("max_loss") is not None else "unscorable",
                "pass": result.get("pass"),
                "reason": result.get("reason"),
                "max_loss": result.get("max_loss"),
                "median_item_delta": result.get("median_item_delta"),
                "n_samples": result.get("n_samples"),
                "n_valid_samples": result.get("n_valid_samples"),
                "samples": result.get("samples"),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - observability sidecar must not block critic.
        audit.update(
            {
                "status": "error",
                "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    _write_json(audit_path, audit)
    return audit


def _run_gate_sample(
    *,
    run: Run,
    project: Project,
    session: Session,
    pipeline_md: str,
    baseline_md: str,
    sample_idx: int,
) -> dict[str, object]:
    pipeline_slot, baseline_slot = coin_flip_slots(random.SystemRandom())
    manuscript_a = pipeline_md if pipeline_slot == "A" else baseline_md
    manuscript_b = baseline_md if baseline_slot == "B" else pipeline_md
    user_prompt = build_north_star_gate_user_prompt(
        manuscript_a=manuscript_a,
        manuscript_b=manuscript_b,
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": NORTH_STAR_GATE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.0,
        max_tokens=7000,
        response_format={"type": "json_object"},
        request_id=f"critic_north_star_gate_{run.id}_sample{sample_idx}",
        prompt_template_id="north_star_gate.paired_blind_box_ledger.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="critic",
        step_id="critic.north_star_gate",
        user_id=project.user_id,
        attempt=sample_idx + 1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=project.title,
        run_metadata={
            "north_star_gate_sample": sample_idx,
            "pipeline_slot": pipeline_slot,
            "baseline_slot": baseline_slot,
            "sidecar_only": True,
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=HookRegistry(),
                context=context,
                output_schema=NorthStarGateOutput,
                audit=AuditWriter(session=session, run_dir=run.run_dir, agent_name="NorthStarGate"),
                max_corrective_retries=1,
                llm_optional=True,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "can_score": False,
            "checksum_failed": False,
            "validation_errors": [f"{type(exc).__name__}: {exc}"],
            "coin": [f"{pipeline_slot}=pipeline", f"{baseline_slot}=baseline"],
            "pipeline_slot": pipeline_slot,
            "baseline_slot": baseline_slot,
            "sample_idx": sample_idx,
        }
    parsed = response.parsed
    if not isinstance(parsed, NorthStarGateOutput):
        return {
            "can_score": False,
            "checksum_failed": False,
            "validation_errors": ["parsed output missing"],
            "coin": [f"{pipeline_slot}=pipeline", f"{baseline_slot}=baseline"],
            "pipeline_slot": pipeline_slot,
            "baseline_slot": baseline_slot,
            "sample_idx": sample_idx,
        }
    sample = evaluate_gate_sample(
        output=parsed,
        pipeline_slot=pipeline_slot,
        baseline_slot=baseline_slot,
    )
    sample["sample_idx"] = sample_idx
    return sample


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _relative_to_run(run: Run, path: Path) -> str:
    try:
        return str(path.relative_to(Path(run.run_dir)))
    except ValueError:
        return str(path)
