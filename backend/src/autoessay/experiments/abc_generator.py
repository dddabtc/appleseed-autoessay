"""Generation and file-artifact writing for ABC experiment arms."""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import md5, sha256
from pathlib import Path
from typing import Any, Protocol

from autoessay.agents._humanizer import humanizer_directive
from autoessay.config import DEFAULT_EXPRESS_ARS_SKILL_PATH, get_llm_providers
from autoessay.experiments.abc_architecture import (
    EXPERIMENT_ID,
    MANUSCRIPT_MAX_TOKENS,
    PROVIDER_FALLBACK_ALLOWED,
    SELF_CRITIQUE_MAX_TOKENS,
    experiment_script_sha,
    generation_model_id,
    production_commit_sha,
    token_cap_total,
)
from autoessay.experiments.abc_compliance import ComplianceRepairResult, repair_manuscript
from autoessay.experiments.abc_extract import KernelMetadata, load_kernel_metadata, package_sha256
from autoessay.experiments.abc_prompts import (
    PromptBundle,
    build_b_prime_anti_echo_retry_prompt,
    build_b_prime_prompt,
    build_b_prompt,
    build_c_prompt,
    build_e_ars_prompt,
    build_g_ars_front_half_prompt,
)
from autoessay.llm_client import LLMClient


class ChatCompletionClient(Protocol):
    async def chat_completion(
        self,
        messages: Sequence[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int = 4000,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        force_no_reasoning: bool = False,
        validate_json_content: bool = False,
        stream: bool = False,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ArmGenerationResult:
    manuscript_path: Path
    provenance_path: Path
    prompt_path: Path | None


class TokenBudgetExceededError(RuntimeError):
    """Raised when a generated ABC arm exceeds the configured token cap."""


_KNOWN_PROVIDER_MODELS = {
    "rightcode": "gpt-5.4",
    "apiport": "gpt-5.4",
    "minimax": "MiniMax-M2.7",
}

ARS_SKILL_PATH_ENV = "AUTOESSAY_ABC_ARS_SKILL_PATH"
GENERATION_ADAPTER_ENV = "AUTOESSAY_ABC_GENERATION_ADAPTER"
CODEX_GENERATION_MODEL_ENV = "AUTOESSAY_ABC_CODEX_GENERATION_MODEL"
CODEX_GENERATION_TIMEOUT_ENV = "AUTOESSAY_ABC_CODEX_GENERATION_TIMEOUT_SECONDS"
REQUIRE_CODEX_GPT54_EG_ENV = "AUTOESSAY_ABC_REQUIRE_CODEX_GPT54_EG"
E_STREAM_ENV = "AUTOESSAY_ABC_E_STREAM"
F_STREAM_ENV = "AUTOESSAY_ABC_F_STREAM"
G_STREAM_ENV = "AUTOESSAY_ABC_G_STREAM"
F_STAGE_MAX_TOKENS_ENV = "AUTOESSAY_ABC_F_STAGE_MAX_TOKENS"
DEFAULT_ARS_SKILL_PATH = DEFAULT_EXPRESS_ARS_SKILL_PATH
DEFAULT_ARS_PIPELINE_PATH = Path(
    "/tmp/ars-experiment/academic-research-skills/academic-pipeline/SKILL.md"
)
F_STAGE_ORDER: tuple[str, ...] = (
    "intake",
    "structure",
    "draft",
    "integrity",
    "review",
    "revise",
)
F_REVIEWER_ROLES: tuple[str, ...] = ("compliance", "novelty", "completeness")
A_PHASE_ORDER: tuple[str, ...] = (
    "proposal",
    "scout",
    "curator",
    "synthesizer",
    "tension_extraction",
    "framework_lens",
    "ideator",
    "drafter",
    "stylist",
    "final_rewrite",
    "critic",
    "integrity",
    "exports",
)
A_PHASE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "proposal": ("proposal/proposal_v001.json", "proposal/proposal_v001.md"),
    "scout": ("discovery/scout_report.md",),
    "curator": ("sources/shortlist.json", "sources/curation_report.md"),
    "synthesizer": ("synthesis/synthesizer.json", "synthesis/synthesizer_report.md"),
    "tension_extraction": ("synthesis/tension_extraction.json",),
    "framework_lens": ("synthesis/framework_lens.json", "framework_lens/checkpoint.json"),
    "ideator": ("novelty/selected_thesis.json", "novelty/ideator_report.md"),
    "drafter": ("drafts/v001/manuscript.md", "drafter/checkpoint.json"),
    "stylist": ("stylist/checkpoint.json",),
    "final_rewrite": ("rewrite/checkpoint.json", "final_rewrite/llm_calls.jsonl"),
    "critic": ("critic/checkpoint.json", "reviews/critic_v001.md"),
    "integrity": ("reviews/claim_audit.jsonl", "integrity/checkpoint.json"),
    "exports": ("exports/manuscript.md", "exports/manifest.json"),
}


async def generate_arm(
    *,
    kernel_id: str,
    arm: str,
    results_dir: str | Path,
    gateway: ChatCompletionClient | None = None,
) -> ArmGenerationResult:
    if arm == "A":
        return copy_arm_a_submission(kernel_id=kernel_id, results_dir=results_dir)
    if arm == "B":
        return await generate_b(kernel_id=kernel_id, results_dir=results_dir, gateway=gateway)
    if arm == "B_prime":
        return await generate_b_prime(kernel_id=kernel_id, results_dir=results_dir, gateway=gateway)
    if arm == "C":
        return await generate_c(kernel_id=kernel_id, results_dir=results_dir, gateway=gateway)
    if arm == "E":
        return await generate_e(kernel_id=kernel_id, results_dir=results_dir, gateway=gateway)
    if arm == "F":
        return await generate_f(kernel_id=kernel_id, results_dir=results_dir, gateway=gateway)
    if arm == "G":
        return await generate_g(kernel_id=kernel_id, results_dir=results_dir, gateway=gateway)
    raise ValueError(f"Unsupported ABC arm: {arm}")


async def generate_b(
    *,
    kernel_id: str,
    results_dir: str | Path,
    gateway: ChatCompletionClient | None = None,
) -> ArmGenerationResult:
    root = Path(results_dir)
    kernel = load_kernel_metadata(root, kernel_id)
    package_md = _read_text(root / kernel_id / "front_half" / "package.md")
    source_hash = package_sha256(root, kernel_id)
    prompt = build_b_prompt(
        kernel=kernel,
        package_md=package_md,
        humanizer_directive=humanizer_directive("zh"),
    )
    response = await _run_generation_call(
        prompt,
        gateway=gateway,
        max_tokens=MANUSCRIPT_MAX_TOKENS,
    )
    repair = repair_manuscript(response.content)
    provenance = _base_provenance(
        kernel_id=kernel_id,
        arm="B",
        prompt_sha256=prompt.sha256,
        provider=response.provider,
        provider_model=response.provider_model,
        token_usage=response.usage,
        source_package_sha256=source_hash,
        compliance_repair=repair,
    )
    arm_dir = root / kernel_id / "B"
    _write_text(arm_dir / "manuscript.md", repair.manuscript)
    _write_text(arm_dir / "prompt.redacted.txt", prompt.as_text())
    _write_json(arm_dir / "provenance.json", provenance)
    return ArmGenerationResult(
        manuscript_path=arm_dir / "manuscript.md",
        provenance_path=arm_dir / "provenance.json",
        prompt_path=arm_dir / "prompt.redacted.txt",
    )


async def generate_b_prime(
    *,
    kernel_id: str,
    results_dir: str | Path,
    gateway: ChatCompletionClient | None = None,
) -> ArmGenerationResult:
    root = Path(results_dir)
    package_md = _read_text(root / kernel_id / "front_half" / "package.md")
    source_hash = package_sha256(root, kernel_id)
    base_b_manuscript = _read_text(root / kernel_id / "B" / "manuscript.md")
    base_b_hash = _sha256_text(base_b_manuscript)
    prompt = build_b_prime_prompt(
        package_md=package_md,
        base_b_manuscript=base_b_manuscript,
        humanizer_directive=humanizer_directive("zh"),
    )
    initial_prompt_sha = prompt.sha256
    response = await _run_generation_call(
        prompt,
        gateway=gateway,
        max_tokens=SELF_CRITIQUE_MAX_TOKENS,
    )
    initial_usage = response.usage
    repair = repair_manuscript(response.content)
    retry_prompt: PromptBundle | None = None
    retry_response: _GenerationResponse | None = None
    retry_repair: ComplianceRepairResult | None = None
    if repair.manuscript == base_b_manuscript:
        retry_prompt = build_b_prime_anti_echo_retry_prompt(
            package_md=package_md,
            base_b_manuscript=base_b_manuscript,
            humanizer_directive=humanizer_directive("zh"),
        )
        retry_response = await _run_generation_call(
            retry_prompt,
            gateway=gateway,
            max_tokens=SELF_CRITIQUE_MAX_TOKENS,
        )
        retry_repair = repair_manuscript(retry_response.content)
        if retry_repair.manuscript == base_b_manuscript:
            raise RuntimeError(
                "B_prime self-critique returned a byte-identical manuscript to base B "
                "after anti-echo retry; "
                f"kernel_id={kernel_id} base_b_manuscript_sha256={base_b_hash}"
            )
        prompt = retry_prompt
        response = retry_response
        repair = retry_repair
    token_usage = response.usage
    if retry_response is not None:
        token_usage = _sum_token_usage(initial_usage, retry_response.usage)
    provenance = _base_provenance(
        kernel_id=kernel_id,
        arm="B_prime",
        prompt_sha256=prompt.sha256,
        provider=response.provider,
        provider_model=response.provider_model,
        token_usage=token_usage,
        source_package_sha256=source_hash,
        compliance_repair=repair,
    )
    provenance["base_b_manuscript_sha256"] = base_b_hash
    provenance["self_critique_prompt_sha256"] = prompt.sha256
    provenance["self_critique"] = {
        "attempted": True,
        "prompt_sha256": prompt.sha256,
        "base_b_manuscript_sha256": base_b_hash,
        "output_equal_to_base": False,
        "anti_echo_retry_attempted": retry_response is not None,
        "attempt_count": 2 if retry_response is not None else 1,
        "attempt_prompt_sha256s": (
            [initial_prompt_sha, prompt.sha256] if retry_response is not None else [prompt.sha256]
        ),
    }
    arm_dir = root / kernel_id / "B_prime"
    _write_text(arm_dir / "manuscript.md", repair.manuscript)
    _write_text(arm_dir / "prompt.redacted.txt", prompt.as_text())
    _write_json(arm_dir / "provenance.json", provenance)
    return ArmGenerationResult(
        manuscript_path=arm_dir / "manuscript.md",
        provenance_path=arm_dir / "provenance.json",
        prompt_path=arm_dir / "prompt.redacted.txt",
    )


async def generate_c(
    *,
    kernel_id: str,
    results_dir: str | Path,
    gateway: ChatCompletionClient | None = None,
) -> ArmGenerationResult:
    root = Path(results_dir)
    kernel = load_kernel_metadata(root, kernel_id)
    prompt = build_c_prompt(
        kernel=kernel,
        humanizer_directive=humanizer_directive("zh"),
    )
    response = await _run_generation_call(
        prompt,
        gateway=gateway,
        max_tokens=MANUSCRIPT_MAX_TOKENS,
    )
    provenance = _base_provenance(
        kernel_id=kernel_id,
        arm="C",
        prompt_sha256=prompt.sha256,
        provider=response.provider,
        provider_model=response.provider_model,
        token_usage=response.usage,
        source_package_sha256=None,
        compliance_repair=None,
    )
    arm_dir = root / kernel_id / "C"
    _write_text(arm_dir / "manuscript.md", response.content)
    _write_text(arm_dir / "prompt.redacted.txt", prompt.as_text())
    _write_json(arm_dir / "provenance.json", provenance)
    return ArmGenerationResult(
        manuscript_path=arm_dir / "manuscript.md",
        provenance_path=arm_dir / "provenance.json",
        prompt_path=arm_dir / "prompt.redacted.txt",
    )


async def generate_e(
    *,
    kernel_id: str,
    results_dir: str | Path,
    gateway: ChatCompletionClient | None = None,
    ars_skill_path: str | Path | None = None,
) -> ArmGenerationResult:
    root = Path(results_dir)
    kernel = load_kernel_metadata(root, kernel_id)
    ars_context = _load_ars_full_mode_prompt(
        Path(ars_skill_path or os.getenv(ARS_SKILL_PATH_ENV, "").strip() or DEFAULT_ARS_SKILL_PATH)
    )
    prompt = build_e_ars_prompt(
        kernel=kernel,
        ars_full_mode_prompt=_required_text(ars_context, "prompt"),
        humanizer_directive=humanizer_directive("zh"),
    )
    response = await _run_generation_call(
        prompt,
        gateway=gateway,
        max_tokens=MANUSCRIPT_MAX_TOKENS,
        stream=_env_flag(E_STREAM_ENV, default=True),
    )
    _enforce_codex_gpt54_eg(response, arm="E")
    arm_dir = root / kernel_id / "E"
    state_path = arm_dir / "state.json"
    final_manuscript = response.content
    md5_comparison = _manuscript_md5_comparison(root, kernel_id, final_manuscript, current_arm="E")
    provenance = _base_provenance(
        kernel_id=kernel_id,
        arm="E",
        prompt_sha256=prompt.sha256,
        provider=response.provider,
        provider_model=response.provider_model,
        token_usage=response.usage,
        source_package_sha256=None,
        compliance_repair=None,
    )
    provenance.update(
        {
            "ars_skill_sha": ars_context["ars_skill_sha"],
            "ars_skill_file_sha256": ars_context["ars_skill_file_sha256"],
            "ars_mode": "academic-paper/full/single-call",
            "ars_prompt_extraction": ars_context["manifest"],
            "ars_single_call": {
                "phase_count": 1,
                "llm_call_count": 1,
                "stage_order": ["ars_single_call"],
                "stage_calls": [
                    {
                        "stage": "ars_single_call",
                        "prompt_sha256": prompt.sha256,
                        "provider": response.provider,
                        "provider_model": response.provider_model,
                        "token_usage": _token_usage_payload(response.usage),
                    }
                ],
                "peer_review_structure": {
                    "enabled": False,
                    "reason": "single-call ARS arm; reviewer panel intentionally omitted",
                },
                "revision_loop_count": 0,
                "claim_audit": {
                    "enabled": True,
                    "mode": "single_call_prompt_internal_citation_self_audit",
                    "separate_llm_call": False,
                    "external_scan": False,
                    "scope": [
                        "citation_reference_alignment",
                        "unsupported_claim_downgrade_or_delete",
                        "no_fabricated_doi",
                    ],
                },
                "state_path": str(state_path),
                "raw_response_sha256": _sha256_text(response.content),
                "final_manuscript_sha256": _sha256_text(final_manuscript),
                "final_manuscript_md5": _md5_text(final_manuscript),
                "md5_distinctness": md5_comparison,
            },
        }
    )
    _write_text(arm_dir / "manuscript.md", final_manuscript)
    _write_text(arm_dir / "prompt.redacted.txt", prompt.as_text())
    _write_json(arm_dir / "provenance.json", provenance)
    _write_json(
        state_path,
        _e_state_payload(
            kernel_id=kernel_id,
            prompt=prompt,
            response=response,
            final_manuscript=final_manuscript,
        ),
    )
    return ArmGenerationResult(
        manuscript_path=arm_dir / "manuscript.md",
        provenance_path=arm_dir / "provenance.json",
        prompt_path=arm_dir / "prompt.redacted.txt",
    )


async def generate_g(
    *,
    kernel_id: str,
    results_dir: str | Path,
    gateway: ChatCompletionClient | None = None,
    ars_skill_path: str | Path | None = None,
) -> ArmGenerationResult:
    """Generate arm G: appleseed front-half source pool + ARS single-call writer.

    G is the cut-point arm. It may read only the protocol-approved front-half
    evidence package and the ARS writing spec excerpts; it does not run
    appleseed ideator/drafter/stylist/final_rewrite/critic/integrity/export.
    """
    root = Path(results_dir)
    kernel = load_kernel_metadata(root, kernel_id)
    package_md = _read_text(root / kernel_id / "front_half" / "package.md")
    source_hash = package_sha256(root, kernel_id)
    ars_context = _load_ars_full_mode_prompt(
        Path(ars_skill_path or os.getenv(ARS_SKILL_PATH_ENV, "").strip() or DEFAULT_ARS_SKILL_PATH)
    )
    prompt = build_g_ars_front_half_prompt(
        kernel=kernel,
        package_md=package_md,
        ars_full_mode_prompt=_required_text(ars_context, "prompt"),
        humanizer_directive=humanizer_directive("zh"),
    )
    response = await _run_generation_call(
        prompt,
        gateway=gateway,
        max_tokens=MANUSCRIPT_MAX_TOKENS,
        stream=_env_flag(G_STREAM_ENV, default=True),
    )
    _enforce_codex_gpt54_eg(response, arm="G")
    repair = repair_manuscript(response.content)
    final_manuscript = repair.manuscript
    arm_dir = root / kernel_id / "G"
    state_path = arm_dir / "state.json"
    token_usage = response.usage
    md5_comparison = _manuscript_md5_comparison(root, kernel_id, final_manuscript, current_arm="G")
    provenance = _base_provenance(
        kernel_id=kernel_id,
        arm="G",
        prompt_sha256=prompt.sha256,
        provider=response.provider,
        provider_model=response.provider_model,
        token_usage=token_usage,
        source_package_sha256=source_hash,
        compliance_repair=repair,
    )
    provenance.update(
        {
            "ars_skill_sha": ars_context["ars_skill_sha"],
            "ars_skill_file_sha256": ars_context["ars_skill_file_sha256"],
            "ars_mode": "academic-paper/full/single-call+appleseed-front-half-source-pool",
            "ars_prompt_extraction": ars_context["manifest"],
            "appleseed_cut_policy": {
                "retained": ["front_half evidence package"],
                "retained_artifacts": [
                    "discovery/scout_report.md",
                    "sources/shortlist.json",
                    "synthesis/claims.jsonl",
                    "synthesis/synthesizer.json",
                    "synthesis/tension_extraction.json",
                    "synthesis/framework_lens.json",
                ],
                "cut": [
                    "ideator",
                    "drafter",
                    "stylist",
                    "final_rewrite",
                    "critic",
                    "integrity",
                    "exports",
                    "external_scan",
                ],
            },
            "ars_single_call": {
                "phase_count": 1,
                "llm_call_count": 1,
                "stage_order": ["ars_front_half_single_call"],
                "stage_calls": [
                    {
                        "stage": "ars_front_half_single_call",
                        "prompt_sha256": prompt.sha256,
                        "provider": response.provider,
                        "provider_model": response.provider_model,
                        "token_usage": _token_usage_payload(token_usage),
                    }
                ],
                "peer_review_structure": {
                    "enabled": False,
                    "reason": "single-call cut-point arm; reviewer panel intentionally omitted",
                },
                "revision_loop_count": 0,
                "claim_audit": {
                    "enabled": True,
                    "mode": "single_call_prompt_internal_self_audit",
                    "separate_llm_call": False,
                    "external_scan": False,
                    "scope": [
                        "source_pool_only",
                        "citation_reference_alignment",
                        "unsupported_claim_downgrade_or_delete",
                    ],
                },
                "state_path": str(state_path),
                "raw_response_sha256": _sha256_text(response.content),
                "final_manuscript_sha256": _sha256_text(final_manuscript),
                "final_manuscript_md5": _md5_text(final_manuscript),
                "md5_distinctness": md5_comparison,
            },
        }
    )
    state_payload = _g_state_payload(
        kernel_id=kernel_id,
        prompt=prompt,
        response=response,
        repair=repair,
        source_package_sha256=source_hash,
        final_manuscript=final_manuscript,
    )
    _write_text(arm_dir / "manuscript.md", final_manuscript)
    _write_text(arm_dir / "prompt.redacted.txt", prompt.as_text())
    _write_json(arm_dir / "provenance.json", provenance)
    _write_json(state_path, state_payload)
    return ArmGenerationResult(
        manuscript_path=arm_dir / "manuscript.md",
        provenance_path=arm_dir / "provenance.json",
        prompt_path=arm_dir / "prompt.redacted.txt",
    )


async def generate_f(
    *,
    kernel_id: str,
    results_dir: str | Path,
    gateway: ChatCompletionClient | None = None,
    ars_skill_path: str | Path | None = None,
) -> ArmGenerationResult:
    """Generate arm F with a real multi-call ARS core pipeline.

    F deliberately differs from E: it does not render one giant ARS prompt.
    It executes separate stage prompts, persists every checkpoint artifact,
    runs a three-role reviewer panel, and revises from actual review text.
    """
    root = Path(results_dir)
    kernel = load_kernel_metadata(root, kernel_id)
    arm_dir = root / kernel_id / "F"
    artifact_dir = arm_dir / "stage_artifacts"
    ars_context = _load_ars_multi_stage_context(
        Path(ars_skill_path or os.getenv(ARS_SKILL_PATH_ENV, "").strip() or DEFAULT_ARS_SKILL_PATH)
    )
    stage_max_tokens = _env_int(F_STAGE_MAX_TOKENS_ENV, default=30_000)
    stream = _env_flag(F_STREAM_ENV, default=True)
    stage_records: list[dict[str, object]] = []
    prompts: list[tuple[str, PromptBundle]] = []
    artifacts: dict[str, str] = {}
    passport: dict[str, object] = _initial_f_passport(
        kernel_id=kernel_id,
        kernel=kernel,
        ars_context=ars_context,
    )

    async def run_stage(
        stage: str, prompt: PromptBundle, *, max_tokens: int
    ) -> _GenerationResponse:
        prompts.append((stage, prompt))
        response = await _run_generation_call(
            prompt,
            gateway=gateway,
            max_tokens=max_tokens,
            stream=stream,
        )
        prompt_tokens, completion_tokens, total_tokens = _usage_numbers(response.usage)
        stage_records.append(
            {
                "stage": stage,
                "prompt_sha256": prompt.sha256,
                "provider": response.provider,
                "provider_model": response.provider_model,
                "token_usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            }
        )
        return response

    intake_prompt = _build_f_intake_prompt(kernel=kernel, ars_context=ars_context)
    intake = await run_stage("intake", intake_prompt, max_tokens=12_000)
    _write_f_stage_artifact(
        artifact_dir=artifact_dir,
        passport=passport,
        stage="intake",
        content=intake.content,
        next_stage="structure",
    )
    artifacts["intake"] = intake.content

    structure_prompt = _build_f_structure_prompt(
        kernel=kernel,
        ars_context=ars_context,
        intake_artifact=artifacts["intake"],
    )
    structure = await run_stage("structure", structure_prompt, max_tokens=18_000)
    _write_f_stage_artifact(
        artifact_dir=artifact_dir,
        passport=passport,
        stage="structure",
        content=structure.content,
        next_stage="draft",
    )
    artifacts["structure"] = structure.content

    draft_prompt = _build_f_draft_prompt(
        kernel=kernel,
        ars_context=ars_context,
        intake_artifact=artifacts["intake"],
        structure_artifact=artifacts["structure"],
        humanizer_directive=humanizer_directive("zh"),
    )
    draft = await run_stage("draft", draft_prompt, max_tokens=stage_max_tokens)
    _write_f_stage_artifact(
        artifact_dir=artifact_dir,
        passport=passport,
        stage="draft",
        content=draft.content,
        next_stage="integrity",
    )
    artifacts["draft"] = draft.content

    integrity_prompt = _build_f_integrity_prompt(
        kernel=kernel,
        ars_context=ars_context,
        intake_artifact=artifacts["intake"],
        structure_artifact=artifacts["structure"],
        draft_artifact=artifacts["draft"],
    )
    integrity = await run_stage("integrity", integrity_prompt, max_tokens=22_000)
    _write_f_stage_artifact(
        artifact_dir=artifact_dir,
        passport=passport,
        stage="integrity",
        content=integrity.content,
        next_stage="review",
    )
    artifacts["integrity"] = integrity.content

    reviewer_outputs: dict[str, str] = {}
    for role in F_REVIEWER_ROLES:
        review_prompt = _build_f_review_prompt(
            kernel=kernel,
            ars_context=ars_context,
            reviewer_role=role,
            intake_artifact=artifacts["intake"],
            structure_artifact=artifacts["structure"],
            draft_artifact=artifacts["draft"],
            integrity_artifact=artifacts["integrity"],
        )
        review_response = await run_stage(f"review_{role}", review_prompt, max_tokens=12_000)
        reviewer_outputs[role] = review_response.content
        _write_text(artifact_dir / f"review_{role}.md", review_response.content)

    combined_review = _combined_review_artifact(reviewer_outputs)
    _write_f_stage_artifact(
        artifact_dir=artifact_dir,
        passport=passport,
        stage="review",
        content=combined_review,
        next_stage="revise",
    )
    artifacts["review"] = combined_review

    revise_prompt = _build_f_revise_prompt(
        kernel=kernel,
        ars_context=ars_context,
        intake_artifact=artifacts["intake"],
        structure_artifact=artifacts["structure"],
        draft_artifact=artifacts["draft"],
        integrity_artifact=artifacts["integrity"],
        review_artifact=artifacts["review"],
        humanizer_directive=humanizer_directive("zh"),
    )
    revise = await run_stage("revise", revise_prompt, max_tokens=stage_max_tokens)
    _write_f_stage_artifact(
        artifact_dir=artifact_dir,
        passport=passport,
        stage="revise",
        content=revise.content,
        next_stage="complete",
    )
    artifacts["revise"] = revise.content

    final_manuscript = _strip_ars_internal_markers(_extract_final_manuscript(revise.content))
    if not final_manuscript.strip():
        raise RuntimeError(f"F arm {kernel_id} revise stage did not produce a final manuscript")
    token_usage = _sum_token_usage(*_stage_record_token_usages(stage_records))
    provider, provider_model = _f_provider_model(stage_records)
    prompt_manifest = _f_prompt_manifest(prompts)
    prompt_sha256 = _sha256_text(json.dumps(prompt_manifest, ensure_ascii=False, sort_keys=True))
    provenance = _base_provenance(
        kernel_id=kernel_id,
        arm="F",
        prompt_sha256=prompt_sha256,
        provider=provider,
        provider_model=provider_model,
        token_usage=token_usage,
        source_package_sha256=None,
        compliance_repair=None,
    )
    provenance.update(
        {
            "ars_skill_sha": ars_context["ars_skill_sha"],
            "ars_pipeline_sha": ars_context["ars_pipeline_sha"],
            "ars_skill_file_sha256": ars_context["ars_skill_file_sha256"],
            "ars_pipeline_file_sha256": ars_context["ars_pipeline_file_sha256"],
            "ars_mode": "academic-pipeline/v3.8.2+academic-paper/full/multi-stage-core",
            "ars_prompt_extraction": ars_context["manifest"],
            "ars_multi_stage": {
                "stage_order": list(F_STAGE_ORDER),
                "independent_llm_call_count": len(stage_records),
                "stage_calls": stage_records,
                "reviewer_roles": list(F_REVIEWER_ROLES),
                "revision_loop_count": 1,
                "claim_audit": {
                    "enabled": True,
                    "stage": "integrity",
                    "agent": "claim_ref_alignment_audit_agent",
                    "checks": [
                        "citation_existence",
                        "reference_list_crosswalk",
                        "locator_anchor_presence",
                        "claim_reference_alignment",
                    ],
                },
                "material_passport_path": str(artifact_dir / "material_passport.json"),
                "state_path": str(artifact_dir / "state.json"),
                "artifact_dir": str(artifact_dir),
                "final_manuscript_sha256": _sha256_text(final_manuscript),
                "draft_artifact_sha256": _sha256_text(artifacts["draft"]),
                "revise_artifact_sha256": _sha256_text(artifacts["revise"]),
            },
        }
    )
    _write_text(arm_dir / "manuscript.md", final_manuscript)
    _write_text(arm_dir / "prompt.redacted.txt", _render_f_prompt_manifest(prompts))
    _write_json(arm_dir / "provenance.json", provenance)
    _write_json(artifact_dir / "state.json", _f_state_payload(passport, stage_records))
    _write_json(artifact_dir / "prompt_manifest.json", prompt_manifest)
    return ArmGenerationResult(
        manuscript_path=arm_dir / "manuscript.md",
        provenance_path=arm_dir / "provenance.json",
        prompt_path=arm_dir / "prompt.redacted.txt",
    )


def copy_arm_a_submission(
    *,
    kernel_id: str,
    results_dir: str | Path,
) -> ArmGenerationResult:
    root = Path(results_dir)
    kernel_payload = _load_json_object(root / kernel_id / "kernel.json")
    run_dir_value = kernel_payload.get("a_run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        raise ValueError(f"Missing a_run_dir in {root / kernel_id / 'kernel.json'}")
    run_dir = Path(run_dir_value)
    manuscript_source, submitted_source, exception_reason = _resolve_arm_a_manuscript(run_dir)
    token_usage, llm_audit = _collect_run_llm_usage_with_audit(run_dir)
    phase_audit = _collect_a_phase_audit(run_dir)
    arm_dir = root / kernel_id / "A"
    provenance = _base_provenance(
        kernel_id=kernel_id,
        arm="A",
        prompt_sha256=None,
        provider="production",
        provider_model="production-configured",
        token_usage=token_usage,
        source_package_sha256=None,
        compliance_repair=None,
        submitted_manuscript_source=submitted_source,
    )
    if exception_reason:
        provenance["submitted_manuscript_source_reason"] = exception_reason
    provenance["appleseed_pipeline"] = {
        "phase_order": list(A_PHASE_ORDER),
        "phase_count_expected": len(A_PHASE_ORDER),
        "phase_count_observed_with_artifacts": phase_audit["phase_count_observed_with_artifacts"],
        "terminal_submission_source": submitted_source,
        "phase_artifacts": phase_audit["phase_artifacts"],
        "llm_call_count": llm_audit["llm_call_count"],
        "per_stage_token_usage": llm_audit["per_stage_token_usage"],
        "state_tracking": {
            "run_dir": str(run_dir),
            "run_json": str(run_dir / "run.json"),
            "checkpoint_files": phase_audit["checkpoint_files"],
        },
    }
    if manuscript_source is not None:
        arm_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manuscript_source, arm_dir / "manuscript.md")
    else:
        arm_dir.mkdir(parents=True, exist_ok=True)
        _write_text(arm_dir / "manuscript.md", "")
    _write_json(arm_dir / "provenance.json", provenance)
    return ArmGenerationResult(
        manuscript_path=arm_dir / "manuscript.md",
        provenance_path=arm_dir / "provenance.json",
        prompt_path=None,
    )


@dataclass(frozen=True)
class _GenerationResponse:
    content: str
    provider: str
    provider_model: str
    usage: Mapping[str, object]


async def _run_generation_call(
    prompt: PromptBundle,
    *,
    gateway: ChatCompletionClient | None,
    max_tokens: int,
    stream: bool = False,
) -> _GenerationResponse:
    if gateway is None and os.getenv(GENERATION_ADAPTER_ENV, "").strip() == "codex-cli":
        return _run_codex_cli_generation(prompt)
    model_id = generation_model_id()
    owned_gateway: LLMClient | None = None
    client: ChatCompletionClient
    if gateway is None:
        owned_gateway = _make_pinned_llm_client(model_id)
        client = owned_gateway
    else:
        client = gateway
    try:
        response = await client.chat_completion(
            prompt.messages,
            model_id,
            0.2,
            max_tokens=max_tokens,
            retries=0,
            force_no_reasoning=False,  # Align with prod agents: let the model reason naturally.
            stream=stream,
        )
    finally:
        if owned_gateway is not None:
            await owned_gateway.aclose()
    usage = response.get("usage")
    usage_mapping = usage if isinstance(usage, Mapping) else {}
    provider = response.get("provider_used") or response.get("provider") or "unknown"
    provider_name = str(provider)
    provider_model = _provider_model(response, provider_name)
    content = str(response.get("content") or "")
    _require_usage(
        usage_mapping,
        kernel_id=None,
        arm=None,
        provider=provider_name,
        provider_model=provider_model,
    )
    if not content.strip():
        raise RuntimeError(
            f"ABC generation provider {provider_name}/{provider_model} returned empty content"
        )
    return _GenerationResponse(
        content=content,
        provider=provider_name,
        provider_model=provider_model,
        usage=usage_mapping,
    )


def _run_codex_cli_generation(prompt: PromptBundle) -> _GenerationResponse:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError(f"{GENERATION_ADAPTER_ENV}=codex-cli requires the codex executable")
    model = os.getenv(CODEX_GENERATION_MODEL_ENV, "").strip() or "gpt-5.4"
    timeout = _env_int(CODEX_GENERATION_TIMEOUT_ENV, default=3600)
    with tempfile.NamedTemporaryFile(prefix="abc-codex-generation-", suffix=".md") as output_file:
        adapter_prompt = _codex_cli_adapter_prompt(prompt)
        completed = subprocess.run(
            [
                codex,
                "exec",
                "--json",
                "--ephemeral",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--model",
                model,
                "-c",
                "model_reasoning_effort=medium",
                "--output-last-message",
                output_file.name,
                "-",
            ],
            input=adapter_prompt,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                "codex-cli generation failed"
                + (f": {detail[:1000]}" if detail else f" with exit code {completed.returncode}")
            )
        content = Path(output_file.name).read_text(encoding="utf-8").strip()
    usage = _codex_cli_usage(completed.stdout)
    _require_usage(
        usage,
        kernel_id=None,
        arm=None,
        provider="codex-cli",
        provider_model=model,
    )
    if not content:
        raise RuntimeError(
            f"codex-cli generation provider codex-cli/{model} returned empty content"
        )
    return _GenerationResponse(
        content=content,
        provider="codex-cli",
        provider_model=model,
        usage=usage,
    )


def _codex_cli_adapter_prompt(prompt: PromptBundle) -> str:
    return (
        "You are acting as a chat-completion adapter for an experiment.\n"
        "Do not inspect the repository, run shell commands, edit files, or explain the process.\n"
        "Return only the final content requested by the embedded SYSTEM and USER prompts.\n\n"
        "<SYSTEM>\n"
        f"{prompt.system}\n"
        "</SYSTEM>\n\n"
        "<USER>\n"
        f"{prompt.user}\n"
        "</USER>\n"
    )


def _codex_cli_usage(jsonl: str) -> dict[str, int]:
    usage: Mapping[str, object] = {}
    for line in jsonl.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
            continue
        raw_usage = event.get("usage")
        if isinstance(raw_usage, Mapping):
            usage = raw_usage
    input_tokens = _int_value(usage.get("input_tokens"))
    output_tokens = _int_value(usage.get("output_tokens"))
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _build_f_intake_prompt(
    *, kernel: KernelMetadata, ars_context: Mapping[str, object]
) -> PromptBundle:
    return PromptBundle(
        system=(
            "你是 ARS academic-paper 的 intake_agent，只执行 Phase 0 配置与素材护照初始化。"
            "本调用是独立 checkpoint，不得写正文，不得模拟后续阶段。"
        ),
        user="\n\n".join(
            [
                "任务：根据研究核生成 Paper Configuration Record 与 Material Passport 初始状态。",
                "实验 arm：F = 真 ARS multi-stage core；此阶段只能做 intake。",
                _f_kernel_block(kernel),
                "约束：",
                "- 输出中文 Markdown。",
                "- 明确 operational mode=full，claim_audit=enabled。",
                "- 根据学科选择合理论文类型、引用格式、目标字数、章节预期。",
                "- 因本实验不得向用户追问，所有缺失项采用保守默认值并标为 assumed。",
                "- 不读取 ABC front_half evidence package，不读取 A/B/B_prime/C/E 稿件。",
                "- 末尾必须有 `## Mandatory Checkpoint`，列出本阶段产物与下一阶段输入。",
                "ARS active spec excerpt:",
                _ars_excerpt(ars_context, "intake_agent"),
            ]
        ),
    )


def _build_f_structure_prompt(
    *,
    kernel: KernelMetadata,
    ars_context: Mapping[str, object],
    intake_artifact: str,
) -> PromptBundle:
    return PromptBundle(
        system=(
            "你是 ARS academic-paper 的 structure_architect_agent。"
            "只执行结构设计、证据地图和候选来源语料规划；不得写完整论文。"
        ),
        user="\n\n".join(
            [
                "任务：基于 intake checkpoint 设计完整论文结构、论证蓝图、证据地图和来源候选表。",
                _f_kernel_block(kernel),
                "硬性要求：",
                "- 输出 Paper Outline、Argument Blueprint、Evidence Map、Candidate Source Corpus。",
                "- Candidate Source Corpus 至少 10 条，编号 `src-001` 起；每条包含可见引用编号、作者/题名/年份、用途、预计 locator anchor（page/section/quote/paragraph）。",
                "- 不编造 DOI；不确定 DOI 时留空。",
                "- 结构必须适配目标中文期刊风格，保留摘要、关键词、正文、结论、参考文献和声明类末节。",
                "- 末尾必须有 `## Mandatory Checkpoint`。",
                "Intake checkpoint:",
                "<INTAKE_ARTIFACT>",
                intake_artifact,
                "</INTAKE_ARTIFACT>",
                "ARS active spec excerpt:",
                _ars_excerpt(ars_context, "structure_architect_agent"),
            ]
        ),
    )


def _build_f_draft_prompt(
    *,
    kernel: KernelMetadata,
    ars_context: Mapping[str, object],
    intake_artifact: str,
    structure_artifact: str,
    humanizer_directive: str,
) -> PromptBundle:
    return PromptBundle(
        system=(
            "你是 ARS academic-paper 的 draft_writer_agent。"
            "本调用只执行 drafting checkpoint，必须使用上游 structure 产物，"
            "并按 v3.7.3/v3.8 规则发出 citation anchors 与 claim intent manifest。"
        ),
        user="\n\n".join(
            [
                "任务：写出初稿，但保留 ARS 内部审计标记供下一阶段 integrity 使用。",
                _f_kernel_block(kernel),
                "硬性要求：",
                "- 先输出 `## Claim Intent Manifest`，用 JSON 代码块列出主要 substantive claims、planned_refs、negative_constraints。",
                "- 再输出 `## Draft Body`，正文必须是完整中文学术论文草稿。",
                "- 所有可引用事实或文献判断都用形如 `[1]<!--ref:src-001--><!--anchor:section:...-->` 的三层 citation emission。",
                "- 每个 citation 必须有 `<kind>` 非 none 的 anchor；若无法定位，宁可减少该 claim，不要写 anchor:none。",
                "- 参考文献必须只列正文实际引用来源，并保留 src slug 映射。",
                "- 不读取 A/B/B_prime/C/E 稿件，不读取 ABC front_half evidence package。",
                "- 不输出最终 formatted manuscript；这是待 integrity/review 的 draft artifact。",
                "- 末尾必须有 `## Mandatory Checkpoint`。",
                "人类化写作指令：",
                humanizer_directive,
                "Intake checkpoint:",
                "<INTAKE_ARTIFACT>",
                intake_artifact,
                "</INTAKE_ARTIFACT>",
                "Structure checkpoint:",
                "<STRUCTURE_ARTIFACT>",
                structure_artifact,
                "</STRUCTURE_ARTIFACT>",
                "ARS active spec excerpt:",
                _ars_excerpt(ars_context, "draft_writer_agent"),
            ]
        ),
    )


def _build_f_integrity_prompt(
    *,
    kernel: KernelMetadata,
    ars_context: Mapping[str, object],
    intake_artifact: str,
    structure_artifact: str,
    draft_artifact: str,
) -> PromptBundle:
    return PromptBundle(
        system=(
            "你是 ARS academic-pipeline 的 integrity_verification_agent 与 "
            "claim_ref_alignment_audit_agent 组合审计层。"
            "本调用只做 integrity checkpoint；不得重写论文，不得代替 reviewer。"
        ),
        user="\n\n".join(
            [
                "任务：执行 v3.8 L3 claim-faithfulness audit + 引用/locator 完整性检查。",
                _f_kernel_block(kernel),
                "审计要求：",
                "- 扫描 draft 中每个 citation marker；逐条建立 Citation Audit Table。",
                "- 每条 citation 都必须检查：visible citation、ref slug、reference-list crosswalk、anchor kind/value、anchor 是否非 none、locator 是否可供读者追踪、该引用是否真实/疑似真实/疑似伪造、claim 是否被该来源支持。",
                "- 不能实际检索到全文时，明确标记 retrieval_method=model_knowledge_or_metadata_only，并给 LOW/MED/HIGH 风险，不得声称已读全文。",
                "- 对疑似 fabricated reference、anchorless citation、claim-not-supported、negative-constraint violation 给出 HIGH-WARN。",
                "- 输出 revision instructions：哪些 claim 删除、降格、换源、补 locator、或移入 limitations。",
                "- 末尾必须有 `## Mandatory Checkpoint`。",
                "Intake checkpoint:",
                "<INTAKE_ARTIFACT>",
                intake_artifact,
                "</INTAKE_ARTIFACT>",
                "Structure checkpoint:",
                "<STRUCTURE_ARTIFACT>",
                structure_artifact,
                "</STRUCTURE_ARTIFACT>",
                "Draft checkpoint:",
                "<DRAFT_ARTIFACT>",
                draft_artifact,
                "</DRAFT_ARTIFACT>",
                "ARS active spec excerpts:",
                _ars_excerpt(ars_context, "integrity_verification_agent"),
                _ars_excerpt(ars_context, "claim_ref_alignment_audit_agent"),
            ]
        ),
    )


def _build_f_review_prompt(
    *,
    kernel: KernelMetadata,
    ars_context: Mapping[str, object],
    reviewer_role: str,
    intake_artifact: str,
    structure_artifact: str,
    draft_artifact: str,
    integrity_artifact: str,
) -> PromptBundle:
    role_spec = {
        "compliance": (
            "你是合规审稿人，只关注格式、引用、声明、学术规范和目标中文期刊适配。"
            "优先检查 CNKI/中文期刊体例、参考文献、AI disclosure、COI、funding、ethics/data availability。"
        ),
        "novelty": (
            "你是创新性审稿人，只关注问题意识、材料新意、理论视角、方法/论证的新贡献。"
            "不要用格式问题替代创新性判断。"
        ),
        "completeness": (
            "你是完整性审稿人，只关注摘要-正文-结论闭环、章节覆盖、论证链、局限性、跨节连贯性。"
            "不要用创新性问题替代完整性判断。"
        ),
    }[reviewer_role]
    return PromptBundle(
        system=(
            "你是 ARS reviewer panel 的一个独立 reviewer。"
            "本调用只输出你的角色报告；不得协商、不得看其他 reviewer 输出。"
            f"{role_spec}"
        ),
        user="\n\n".join(
            [
                f"任务：输出 `{reviewer_role}` reviewer report。",
                _f_kernel_block(kernel),
                "输出要求：",
                "- Markdown 报告，包含 score、major issues、minor issues、must-fix revision instructions。",
                "- 每条批评必须引用 draft 中具体 anchor/章节/短语。",
                "- 明确哪些 integrity findings 必须进入 revision。",
                "- 不重写论文。",
                "Intake checkpoint:",
                "<INTAKE_ARTIFACT>",
                intake_artifact,
                "</INTAKE_ARTIFACT>",
                "Structure checkpoint:",
                "<STRUCTURE_ARTIFACT>",
                structure_artifact,
                "</STRUCTURE_ARTIFACT>",
                "Draft checkpoint:",
                "<DRAFT_ARTIFACT>",
                draft_artifact,
                "</DRAFT_ARTIFACT>",
                "Integrity checkpoint:",
                "<INTEGRITY_ARTIFACT>",
                integrity_artifact,
                "</INTEGRITY_ARTIFACT>",
                "ARS active spec excerpt:",
                _ars_excerpt(ars_context, "peer_reviewer_agent"),
            ]
        ),
    )


def _build_f_revise_prompt(
    *,
    kernel: KernelMetadata,
    ars_context: Mapping[str, object],
    intake_artifact: str,
    structure_artifact: str,
    draft_artifact: str,
    integrity_artifact: str,
    review_artifact: str,
    humanizer_directive: str,
) -> PromptBundle:
    return PromptBundle(
        system=(
            "你是 ARS academic-paper 的 draft_writer_agent revision mode。"
            "必须根据 integrity audit 和三名 reviewer 的真实反馈进行实质修订。"
            "不得 echo 初稿，不得输出过程解释作为最终稿。"
        ),
        user="\n\n".join(
            [
                "任务：执行 revision loop round 1，输出修订记录和最终可盲评 manuscript。",
                _f_kernel_block(kernel),
                "硬性要求：",
                "- 必须先输出 `## Revision Log`，逐条说明如何处理 integrity + compliance/novelty/completeness feedback。",
                "- 再输出 `## Final Manuscript`；后面只能是最终完整 Markdown 论文正文。",
                "- 最终稿不要包含 ARS、arm F、checkpoint、provenance、stage_artifacts、Material Passport 等实验标记。",
                "- 最终稿不要保留 `<!--ref:...-->` 或 `<!--anchor:...-->` HTML comments；保留正常可见引用 `[1]`。",
                "- 对 HIGH-WARN 的引用或 claim：删除、降格、换成更保守表述，或明确列为局限，不得原样保留。",
                "- 包含题名、摘要、关键词、正文、结论、参考文献、Data Availability Statement、Ethics Declaration、Author Contributions (CRediT)、Conflict of Interest Statement、Funding Acknowledgment、AI disclosure statement。",
                "- 参考文献只列最终正文实际引用来源；不要编造 DOI。",
                "人类化写作指令：",
                humanizer_directive,
                "Intake checkpoint:",
                "<INTAKE_ARTIFACT>",
                intake_artifact,
                "</INTAKE_ARTIFACT>",
                "Structure checkpoint:",
                "<STRUCTURE_ARTIFACT>",
                structure_artifact,
                "</STRUCTURE_ARTIFACT>",
                "Draft checkpoint:",
                "<DRAFT_ARTIFACT>",
                draft_artifact,
                "</DRAFT_ARTIFACT>",
                "Integrity checkpoint:",
                "<INTEGRITY_ARTIFACT>",
                integrity_artifact,
                "</INTEGRITY_ARTIFACT>",
                "Reviewer panel reports:",
                "<REVIEW_ARTIFACT>",
                review_artifact,
                "</REVIEW_ARTIFACT>",
                "ARS active spec excerpt:",
                _ars_excerpt(ars_context, "draft_writer_agent"),
            ]
        ),
    )


def _write_f_stage_artifact(
    *,
    artifact_dir: Path,
    passport: dict[str, object],
    stage: str,
    content: str,
    next_stage: str,
) -> None:
    artifact_path = artifact_dir / f"{stage}.md"
    checkpoint = (
        "\n\n---\n\n"
        "## Mandatory Checkpoint\n\n"
        f"- Stage: {stage}\n"
        f"- Artifact: {artifact_path}\n"
        f"- Status: complete\n"
        f"- Next stage: {next_stage}\n"
    )
    _write_text(artifact_path, content.rstrip() + checkpoint)
    completed = passport.setdefault("completed_stages", [])
    if isinstance(completed, list):
        completed.append(
            {
                "stage": stage,
                "artifact_path": str(artifact_path),
                "completed_at": _utc_now(),
                "next_stage": next_stage,
                "sha256": _sha256_text(content),
            }
        )
    artifacts = passport.setdefault("artifacts", {})
    if isinstance(artifacts, dict):
        artifacts[stage] = str(artifact_path)
    _write_json(artifact_dir / "material_passport.json", passport)


def _initial_f_passport(
    *,
    kernel_id: str,
    kernel: KernelMetadata,
    ars_context: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "abc_f_ars_material_passport_v1",
        "kernel_id": kernel_id,
        "title": kernel.title,
        "target_journal": kernel.target_journal,
        "research_kernel": kernel.research_kernel,
        "ars_pipeline_version": "3.8.2",
        "ars_skill_sha": ars_context["ars_skill_sha"],
        "ars_pipeline_sha": ars_context["ars_pipeline_sha"],
        "claim_audit_enabled": True,
        "stage_order": list(F_STAGE_ORDER),
        "reviewer_roles": list(F_REVIEWER_ROLES),
        "completed_stages": [],
        "artifacts": {},
        "created_at": _utc_now(),
    }


def _combined_review_artifact(reviewer_outputs: Mapping[str, str]) -> str:
    parts = ["# Reviewer Panel Reports"]
    for role in F_REVIEWER_ROLES:
        parts.extend([f"## Reviewer: {role}", reviewer_outputs.get(role, "").strip()])
    return "\n\n".join(parts).strip() + "\n"


def _extract_final_manuscript(value: str) -> str:
    marker = re.search(r"^##\s+Final Manuscript\s*$", value, flags=re.IGNORECASE | re.MULTILINE)
    if marker is None:
        return value.strip()
    text = value[marker.end() :].strip()
    checkpoint = re.search(
        r"^##\s+Mandatory Checkpoint\s*$", text, flags=re.IGNORECASE | re.MULTILINE
    )
    if checkpoint is not None:
        text = text[: checkpoint.start()].strip()
    return text


def _strip_ars_internal_markers(value: str) -> str:
    text = re.sub(r"<!--\s*(?:ref|anchor):.*?-->", "", value)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _f_provider_model(stage_records: Sequence[Mapping[str, object]]) -> tuple[str, str]:
    providers = {str(record.get("provider") or "unknown") for record in stage_records}
    models = {str(record.get("provider_model") or "unknown") for record in stage_records}
    provider = next(iter(providers)) if len(providers) == 1 else "mixed"
    model = next(iter(models)) if len(models) == 1 else "mixed"
    return provider, model


def _f_prompt_manifest(prompts: Sequence[tuple[str, PromptBundle]]) -> dict[str, object]:
    return {
        "schema_version": "abc_f_prompt_manifest_v1",
        "stages": [
            {
                "stage": stage,
                "prompt_sha256": prompt.sha256,
                "system_chars": len(prompt.system),
                "user_chars": len(prompt.user),
            }
            for stage, prompt in prompts
        ],
    }


def _render_f_prompt_manifest(prompts: Sequence[tuple[str, PromptBundle]]) -> str:
    parts = ["# ABC F Multi-Stage Prompt Manifest"]
    for stage, prompt in prompts:
        parts.extend(
            [
                f"## Stage: {stage}",
                f"- Prompt SHA-256: {prompt.sha256}",
                f"- System chars: {len(prompt.system)}",
                f"- User chars: {len(prompt.user)}",
            ]
        )
    return "\n\n".join(parts) + "\n"


def _f_state_payload(
    passport: Mapping[str, object],
    stage_records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": "abc_f_state_v1",
        "updated_at": _utc_now(),
        "stage_order": list(F_STAGE_ORDER),
        "reviewer_roles": list(F_REVIEWER_ROLES),
        "completed_stages": passport.get("completed_stages", []),
        "stage_calls": list(stage_records),
        "token_usage": _sum_token_usage(*_stage_record_token_usages(stage_records)),
    }


def _g_state_payload(
    *,
    kernel_id: str,
    prompt: PromptBundle,
    response: _GenerationResponse,
    repair: ComplianceRepairResult,
    source_package_sha256: str,
    final_manuscript: str,
) -> dict[str, object]:
    return {
        "schema_version": "abc_g_state_v1",
        "kernel_id": kernel_id,
        "updated_at": _utc_now(),
        "status": "complete",
        "cut_point": "after_appleseed_front_half",
        "stage_order": ["ars_front_half_single_call"],
        "states": [
            {
                "state": "initialized",
                "artifact": "front_half/package.md",
                "source_package_sha256": source_package_sha256,
            },
            {
                "state": "generated",
                "stage": "ars_front_half_single_call",
                "prompt_sha256": prompt.sha256,
                "provider": response.provider,
                "provider_model": response.provider_model,
                "token_usage": _token_usage_payload(response.usage),
            },
            {
                "state": "deterministic_compliance_repair",
                "attempted": True,
                "status": repair.status,
                "changed": repair.changed,
                "operations": list(repair.operations),
                "blockers": list(repair.blockers),
            },
            {
                "state": "complete",
                "final_manuscript_sha256": _sha256_text(final_manuscript),
                "final_manuscript_md5": _md5_text(final_manuscript),
            },
        ],
        "audit_points": {
            "phase_count": 1,
            "llm_call_count": 1,
            "per_stage_token": [
                {
                    "stage": "ars_front_half_single_call",
                    "token_usage": _token_usage_payload(response.usage),
                }
            ],
            "peer_review_structure": "none",
            "revision_loop": "none",
            "claim_audit": "prompt-internal source-pool self-audit; no separate LLM call",
            "state_tracking": "state.json",
        },
    }


def _e_state_payload(
    *,
    kernel_id: str,
    prompt: PromptBundle,
    response: _GenerationResponse,
    final_manuscript: str,
) -> dict[str, object]:
    return {
        "schema_version": "abc_e_state_v1",
        "kernel_id": kernel_id,
        "updated_at": _utc_now(),
        "status": "complete",
        "stage_order": ["ars_single_call"],
        "states": [
            {
                "state": "initialized",
                "input": "kernel_only",
            },
            {
                "state": "generated",
                "stage": "ars_single_call",
                "prompt_sha256": prompt.sha256,
                "provider": response.provider,
                "provider_model": response.provider_model,
                "token_usage": _token_usage_payload(response.usage),
            },
            {
                "state": "complete",
                "final_manuscript_sha256": _sha256_text(final_manuscript),
                "final_manuscript_md5": _md5_text(final_manuscript),
            },
        ],
        "audit_points": {
            "phase_count": 1,
            "llm_call_count": 1,
            "per_stage_token": [
                {
                    "stage": "ars_single_call",
                    "token_usage": _token_usage_payload(response.usage),
                }
            ],
            "peer_review_structure": "none",
            "revision_loop": "none",
            "claim_audit": "prompt-internal citation/reference self-audit; no separate LLM call",
            "state_tracking": "state.json",
        },
    }


def _token_usage_payload(usage: Mapping[str, object]) -> dict[str, int]:
    prompt_tokens, completion_tokens, total_tokens = _usage_numbers(usage)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Expected nonempty text field in ARS context: {key}")
    return value


def _stage_record_token_usages(
    stage_records: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    usages: list[Mapping[str, object]] = []
    for record in stage_records:
        usage = record.get("token_usage")
        if not isinstance(usage, Mapping):
            raise RuntimeError("F stage record is missing token_usage")
        usages.append(usage)
    return usages


def _manuscript_md5_comparison(
    root: Path,
    kernel_id: str,
    manuscript: str,
    *,
    current_arm: str,
) -> dict[str, object]:
    current = _md5_text(manuscript)
    comparisons: dict[str, object] = {}
    kernel_dir = root / kernel_id
    for arm_dir in sorted(path for path in kernel_dir.iterdir() if path.is_dir()):
        arm = arm_dir.name
        if arm in {current_arm, "front_half", "blinded"}:
            continue
        manuscript_path = arm_dir / "manuscript.md"
        if not manuscript_path.is_file():
            continue
        other_md5 = _md5_text(manuscript_path.read_text(encoding="utf-8"))
        comparisons[arm] = {
            "other_md5": other_md5,
            "distinct": other_md5 != current,
        }
    return {
        "current_md5": current,
        "comparisons": comparisons,
        "all_known_existing_distinct": all(
            bool(entry.get("distinct"))
            for entry in comparisons.values()
            if isinstance(entry, Mapping)
        ),
    }


def _ars_excerpt(ars_context: Mapping[str, object], key: str) -> str:
    excerpts = ars_context.get("excerpts")
    if isinstance(excerpts, Mapping):
        value = excerpts.get(key)
        if isinstance(value, str):
            return value
    return ""


def _f_kernel_block(kernel: KernelMetadata) -> str:
    return "\n".join(
        [
            "论文输入：",
            f"- 题目：{kernel.title}",
            f"- 目标期刊：{kernel.target_journal or '未指定'}",
            "- 研究核：",
            "```json",
            json.dumps(kernel.research_kernel, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )


def _env_int(name: str, *, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _make_pinned_llm_client(model_id: str) -> LLMClient:
    providers = get_llm_providers()
    if not providers:
        raise RuntimeError("No LLM providers configured for ABC generation")
    # Use all providers as fallback chain, preserve each provider's own model.
    # Do not override model_id; provenance records the actual provider+model used.
    return LLMClient(providers=providers, timeout_seconds=900.0)


def _provider_model(response: Mapping[str, object], provider: str) -> str:
    for key in ("provider_model", "model_used", "model"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _KNOWN_PROVIDER_MODELS.get(provider, "unknown")


def _base_provenance(
    *,
    kernel_id: str,
    arm: str,
    prompt_sha256: str | None,
    provider: str,
    provider_model: str,
    token_usage: Mapping[str, object],
    source_package_sha256: str | None,
    compliance_repair: ComplianceRepairResult | None,
    submitted_manuscript_source: str | None = None,
) -> dict[str, object]:
    prompt_tokens, completion_tokens, total_tokens = _usage_numbers(token_usage)
    cap = token_cap_total()
    _require_usage(
        token_usage,
        kernel_id=kernel_id,
        arm=arm,
        provider=provider,
        provider_model=provider_model,
    )
    if total_tokens > cap:
        raise TokenBudgetExceededError(
            f"ABC arm {kernel_id}/{arm} used {total_tokens} tokens, exceeding cap {cap}"
        )
    if compliance_repair is None:
        repair_payload: dict[str, object] = {
            "attempted": False,
            "mode": "none",
            "status": "skipped",
        }
    else:
        repair_payload = {
            "attempted": True,
            "mode": "deterministic",
            "status": compliance_repair.status,
            "changed": compliance_repair.changed,
            "blockers": list(compliance_repair.blockers),
            "operations": list(compliance_repair.operations),
        }
    return {
        "experiment_id": EXPERIMENT_ID,
        "kernel_id": kernel_id,
        "arm": arm,
        "model_id": generation_model_id(),
        "provider": provider,
        "provider_model": provider_model,
        "provider_fallback_allowed": PROVIDER_FALLBACK_ALLOWED,
        "production_commit_sha": production_commit_sha(),
        "experiment_script_sha": experiment_script_sha(),
        "submitted_manuscript_source": submitted_manuscript_source,
        "prompt_sha256": prompt_sha256,
        "source_package_sha256": source_package_sha256,
        "token_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "budget_exceeded": False,
        },
        "generated_at": _utc_now(),
        "compliance_repair": repair_payload,
        "self_critique": {
            "attempted": False,
            "prompt_sha256": None,
            "base_b_manuscript_sha256": None,
        },
        "external_scan": {
            "attempted": False,
            "mode": "audit_only",
            "status": "skipped",
        },
    }


def _usage_numbers(usage: Mapping[str, object]) -> tuple[int, int, int]:
    prompt_tokens = _int_value(usage.get("prompt_tokens"))
    completion_tokens = _int_value(usage.get("completion_tokens"))
    total_tokens = _int_value(usage.get("total_tokens"))
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


def _require_usage(
    usage: Mapping[str, object],
    *,
    kernel_id: str | None,
    arm: str | None,
    provider: str,
    provider_model: str,
) -> None:
    _, _, total_tokens = _usage_numbers(usage)
    if total_tokens > 0:
        return
    scope = f"{kernel_id}/{arm}" if kernel_id and arm else "generation call"
    raise RuntimeError(
        f"ABC {scope} from provider {provider}/{provider_model} returned missing token usage"
    )


def _sum_token_usage(*usages: Mapping[str, object]) -> dict[str, int]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    for usage in usages:
        prompt, completion, total = _usage_numbers(usage)
        prompt_tokens += prompt
        completion_tokens += completion
        total_tokens += total
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _collect_run_llm_usage(run_dir: Path) -> dict[str, int]:
    usage, _audit = _collect_run_llm_usage_with_audit(run_dir)
    return usage


def _collect_run_llm_usage_with_audit(run_dir: Path) -> tuple[dict[str, int], dict[str, object]]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    call_count = 0
    seen_calls: set[str] = set()
    per_stage: dict[str, dict[str, int]] = {}
    for path in sorted(run_dir.rglob("llm_calls.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, Mapping):
                continue
            call_key = str(
                payload.get("provider_call_id")
                or payload.get("request_hash")
                or f"{path}:{line_number}"
            )
            if call_key in seen_calls:
                continue
            seen_calls.add(call_key)
            prompt, completion, total = _usage_numbers(payload)
            prompt_tokens += prompt
            completion_tokens += completion
            total_tokens += total
            call_count += 1
            stage = _stage_name_for_llm_log(run_dir, path)
            stage_usage = per_stage.setdefault(
                stage,
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "llm_call_count": 0,
                },
            )
            stage_usage["prompt_tokens"] += prompt
            stage_usage["completion_tokens"] += completion
            stage_usage["total_tokens"] += total
            stage_usage["llm_call_count"] += 1
    if call_count == 0:
        raise RuntimeError(f"No A-path llm_calls.jsonl usage records found below {run_dir}")
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    audit = {
        "llm_call_count": call_count,
        "per_stage_token_usage": [
            {"stage": stage, **usage_payload} for stage, usage_payload in sorted(per_stage.items())
        ],
    }
    return usage, audit


def _stage_name_for_llm_log(run_dir: Path, path: Path) -> str:
    try:
        relative = path.relative_to(run_dir)
    except ValueError:
        return path.parent.name
    parts = relative.parts
    if parts[:1] == ("phases",) and len(parts) >= 3:
        return parts[2]
    return parts[0] if parts else path.parent.name


def _collect_a_phase_audit(run_dir: Path) -> dict[str, object]:
    phase_artifacts: list[dict[str, object]] = []
    observed_count = 0
    checkpoint_files: list[str] = []
    for checkpoint in sorted(run_dir.rglob("checkpoint.json")):
        checkpoint_files.append(str(checkpoint))
    for phase in A_PHASE_ORDER:
        candidates = A_PHASE_ARTIFACTS.get(phase, ())
        present = [relative for relative in candidates if (run_dir / relative).exists()]
        if present:
            observed_count += 1
        phase_artifacts.append(
            {
                "phase": phase,
                "expected_artifacts": list(candidates),
                "present_artifacts": present,
                "observed": bool(present),
            }
        )
    return {
        "phase_count_observed_with_artifacts": observed_count,
        "phase_artifacts": phase_artifacts,
        "checkpoint_files": checkpoint_files,
    }


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    return 0


def _env_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _enforce_codex_gpt54_eg(response: _GenerationResponse, *, arm: str) -> None:
    if not _env_flag(REQUIRE_CODEX_GPT54_EG_ENV, default=False):
        return
    if response.provider == "codex-cli" and response.provider_model == "gpt-5.4":
        return
    raise RuntimeError(
        f"ABC arm {arm} requires codex-cli/gpt-5.4 under {REQUIRE_CODEX_GPT54_EG_ENV}=1; "
        f"got {response.provider}/{response.provider_model}"
    )


def _resolve_arm_a_manuscript(run_dir: Path) -> tuple[Path | None, str, str | None]:
    exports = run_dir / "exports" / "manuscript.md"
    if exports.is_file() and exports.read_text(encoding="utf-8").strip():
        return exports, "exports_done", None
    candidates = sorted(run_dir.glob("rewrite/v*/critic_loop/selected_manuscript.md"))
    for candidate in reversed(candidates):
        if candidate.is_file() and candidate.read_text(encoding="utf-8").strip():
            return candidate, "critic_selected", None
    return None, "other", "No exports manuscript or critic-selected manuscript found."


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _load_json_object(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return data


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _md5_text(value: str) -> str:
    return md5(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _load_ars_full_mode_prompt(skill_path: Path) -> dict[str, object]:
    if not skill_path.is_file():
        raise FileNotFoundError(f"ARS academic-paper SKILL.md not found: {skill_path}")
    skill_text = skill_path.read_text(encoding="utf-8")
    skill_dir = skill_path.parent
    agent_dir = skill_dir / "agents"
    writer_contract_path = skill_dir.parent / "shared" / "contracts" / "writer" / "full.json"
    parts: list[str] = []
    manifest: list[dict[str, object]] = []

    def add(label: str, path: Path, text: str) -> None:
        parts.append(f"## {label}\n\n{text.strip()}")
        manifest.append(
            {
                "label": label,
                "path": str(path),
                "sha256": _sha256_text(text),
                "chars": len(text),
            }
        )

    add(
        "SKILL.md mode/workflow excerpt",
        skill_path,
        "\n\n".join(
            _extract_skill_sections(
                skill_text,
                [
                    "Quick Start",
                    "Agent Team (12 Agents)",
                    "Orchestration Workflow (8 Phases)",
                    "Operational Modes (10 Modes)",
                    "Anti-Patterns",
                    "Quality Standards",
                    "Output Language",
                ],
            )
        ),
    )
    add(
        "intake_agent defaults excerpt",
        agent_dir / "intake_agent.md",
        "\n\n".join(
            _extract_markdown_sections(
                (agent_dir / "intake_agent.md").read_text(encoding="utf-8"),
                ["Interview Protocol", "Output Format"],
                level=2,
            )
        ),
    )
    add(
        "structure_architect_agent excerpt",
        agent_dir / "structure_architect_agent.md",
        "\n\n".join(
            _extract_markdown_sections(
                (agent_dir / "structure_architect_agent.md").read_text(encoding="utf-8"),
                [
                    "Role Definition",
                    "Core Principles",
                    "Structure Selection",
                    "Outline Construction Process",
                    "Output Format",
                ],
                level=2,
            )
        ),
    )
    add(
        "draft_writer_agent excerpt",
        agent_dir / "draft_writer_agent.md",
        "\n\n".join(
            _extract_markdown_sections(
                (agent_dir / "draft_writer_agent.md").read_text(encoding="utf-8"),
                [
                    "Role Definition",
                    "Core Principles",
                    "Writing Process",
                    "Writing Style Guidelines",
                    "Output Format",
                    "Quality Gates",
                ],
                level=2,
            )
            + _extract_markdown_sections(
                (agent_dir / "draft_writer_agent.md").read_text(encoding="utf-8"),
                ["Phase 4b — Writer paper-visible drafting + self-scoring"],
                level=3,
            )
        ),
    )
    if writer_contract_path.is_file():
        add(
            "writer_full contract JSON",
            writer_contract_path,
            writer_contract_path.read_text(encoding="utf-8"),
        )

    prompt = "\n\n---\n\n".join(parts)
    return {
        "prompt": prompt,
        "ars_skill_sha": _ars_git_sha(skill_path),
        "ars_skill_file_sha256": _sha256_file(skill_path),
        "manifest": manifest,
    }


def _load_ars_multi_stage_context(skill_path: Path) -> dict[str, object]:
    if not skill_path.is_file():
        raise FileNotFoundError(f"ARS academic-paper SKILL.md not found: {skill_path}")
    skill_dir = skill_path.parent
    repo_root = skill_dir.parent
    pipeline_path = repo_root / "academic-pipeline" / "SKILL.md"
    if not pipeline_path.is_file():
        pipeline_path = DEFAULT_ARS_PIPELINE_PATH
    if not pipeline_path.is_file():
        raise FileNotFoundError(f"ARS academic-pipeline SKILL.md not found: {pipeline_path}")

    agent_dir = skill_dir / "agents"
    pipeline_agent_dir = pipeline_path.parent / "agents"
    files: dict[str, tuple[Path, str]] = {
        "academic_paper_skill": (
            skill_path,
            "\n\n".join(
                _extract_skill_sections(
                    skill_path.read_text(encoding="utf-8"),
                    [
                        "Quick Start",
                        "Agent Team (12 Agents)",
                        "Orchestration Workflow (8 Phases)",
                        "Checkpoint Rules",
                    ],
                )
            ),
        ),
        "academic_pipeline_skill": (
            pipeline_path,
            "\n\n".join(
                _extract_skill_sections(
                    pipeline_path.read_text(encoding="utf-8"),
                    [
                        "Pipeline Stages (10 Stages)",
                        "Pipeline State Machine",
                        "Adaptive Checkpoint System",
                        "Agent Team (5 Agents)",
                    ],
                )
            ),
        ),
        "intake_agent": (
            agent_dir / "intake_agent.md",
            "\n\n".join(
                _extract_markdown_sections(
                    (agent_dir / "intake_agent.md").read_text(encoding="utf-8"),
                    ["Role Definition", "Core Principles", "Interview Protocol", "Output Format"],
                    level=2,
                )
            ),
        ),
        "structure_architect_agent": (
            agent_dir / "structure_architect_agent.md",
            "\n\n".join(
                _extract_markdown_sections(
                    (agent_dir / "structure_architect_agent.md").read_text(encoding="utf-8"),
                    [
                        "Role Definition",
                        "Core Principles",
                        "Structure Selection",
                        "Outline Construction Process",
                        "Output Format",
                        "Quality Gates",
                    ],
                    level=2,
                )
            ),
        ),
        "draft_writer_agent": (
            agent_dir / "draft_writer_agent.md",
            "\n\n".join(
                _extract_markdown_sections(
                    (agent_dir / "draft_writer_agent.md").read_text(encoding="utf-8"),
                    [
                        "Role Definition",
                        "Core Principles",
                        "Writing Process",
                        "Revision Protocol",
                        "Output Format",
                        "Three-Layer Citation Emission (v3.7.3)",
                        "Claim Intent Manifest Emission (v3.8)",
                    ],
                    level=2,
                )
            ),
        ),
        "peer_reviewer_agent": (
            agent_dir / "peer_reviewer_agent.md",
            "\n\n".join(
                _extract_markdown_sections(
                    (agent_dir / "peer_reviewer_agent.md").read_text(encoding="utf-8"),
                    [
                        "Role Definition",
                        "Core Principles",
                        "Five-Dimension Scoring Rubric",
                        "Review Process",
                        "Revision Loop Protocol",
                        "Output Format",
                    ],
                    level=2,
                )
            ),
        ),
        "integrity_verification_agent": (
            pipeline_agent_dir / "integrity_verification_agent.md",
            "\n\n".join(
                _extract_markdown_sections(
                    (pipeline_agent_dir / "integrity_verification_agent.md").read_text(
                        encoding="utf-8"
                    ),
                    [
                        "Role Definition",
                        "Anti-Hallucination Mandate",
                        "Verification Protocol",
                        "Output Format",
                    ],
                    level=2,
                )
            ),
        ),
        "claim_ref_alignment_audit_agent": (
            pipeline_agent_dir / "claim_ref_alignment_audit_agent.md",
            "\n\n".join(
                _extract_markdown_sections(
                    (pipeline_agent_dir / "claim_ref_alignment_audit_agent.md").read_text(
                        encoding="utf-8"
                    ),
                    [
                        "Role Definition",
                        "Input contract",
                        "Audit pipeline (6 steps)",
                        "Manifest cross-reference (D6)",
                    ],
                    level=2,
                )
            ),
        ),
    }
    manifest: list[dict[str, object]] = []
    excerpts: dict[str, str] = {}
    for label, (path, text) in files.items():
        if not path.is_file():
            raise FileNotFoundError(f"ARS spec file not found: {path}")
        excerpts[label] = text.strip()
        raw = path.read_text(encoding="utf-8")
        manifest.append(
            {
                "label": label,
                "path": str(path),
                "sha256": _sha256_text(raw),
                "excerpt_sha256": _sha256_text(text),
                "excerpt_chars": len(text),
            }
        )
    return {
        "excerpts": excerpts,
        "ars_skill_sha": _ars_git_sha(skill_path),
        "ars_pipeline_sha": _ars_git_sha(pipeline_path),
        "ars_skill_file_sha256": _sha256_file(skill_path),
        "ars_pipeline_file_sha256": _sha256_file(pipeline_path),
        "manifest": manifest,
    }


def _extract_skill_sections(text: str, headings: Sequence[str]) -> list[str]:
    sections: list[str] = []
    for heading in headings:
        extracted = _extract_markdown_section(text, heading, level=2)
        if extracted:
            sections.append(extracted)
    return sections


def _extract_markdown_sections(text: str, headings: Sequence[str], *, level: int) -> list[str]:
    sections: list[str] = []
    for heading in headings:
        extracted = _extract_markdown_section(text, heading, level=level)
        if extracted:
            sections.append(extracted)
    return sections


def _extract_markdown_section(text: str, heading: str, *, level: int) -> str:
    marker = "#" * level
    pattern = re.compile(
        rf"^{re.escape(marker)}\s+{re.escape(heading)}\s*$",
        flags=re.MULTILINE,
    )
    match = pattern.search(text)
    if match is None:
        return ""
    next_heading = re.search(
        rf"^#{{1,{level}}}\s+",
        text[match.end() :],
        flags=re.MULTILINE,
    )
    end = match.end() + next_heading.start() if next_heading else len(text)
    return text[match.start() : end].strip()


def _ars_git_sha(skill_path: Path) -> str:
    repo = skill_path.parents[1] if len(skill_path.parents) > 1 else skill_path.parent
    manifest_path = repo / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        upstream = manifest.get("upstream") if isinstance(manifest, dict) else None
        upstream_commit = upstream.get("commit") if isinstance(upstream, dict) else None
        if isinstance(upstream_commit, str) and upstream_commit.strip():
            return upstream_commit.strip()
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return _sha256_file(skill_path)
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else _sha256_file(skill_path)


def _utc_now() -> str:
    override = os.getenv("AUTOESSAY_EXPERIMENT_ABC_GENERATED_AT", "").strip()
    if override:
        return override
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
