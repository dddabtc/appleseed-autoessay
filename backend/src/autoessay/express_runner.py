"""Express manuscript runner for ADR-0003.

Express is intentionally not a shortcut through drafter/stylist/critic
states. It owns an ``EXPRESS_RUNNING -> EXPRESS_DONE/FAILED`` lifecycle
and reuses shared helpers only for prompt construction, integrity scan
normalization, humanizer directives, and export file rendering.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents import exporter as export_helpers
from autoessay.agents import integrity as integrity_helpers
from autoessay.agents._humanizer import humanizer_directive
from autoessay.config import LLMProviderSpec, get_llm_providers, get_settings
from autoessay.db import SessionLocal
from autoessay.experiments.abc_extract import KernelMetadata
from autoessay.experiments.abc_generator import _load_ars_full_mode_prompt, _required_text
from autoessay.experiments.abc_prompts import PromptBundle, build_e_ars_prompt
from autoessay.harness import HookRegistry
from autoessay.llm_client import LLMClient
from autoessay.models import Project, Run
from autoessay.phase_lock import phase_lock_release_on_exit
from autoessay.state_machine import append_event, transition
from autoessay.telemetry import record_run_telemetry

EXPRESS_FAILURE_BUDGET = "express_budget_exceeded"
EXPRESS_FAILURE_CANCELLED = "express_cancelled"
EXPRESS_FAILURE_TIMEOUT = "express_timeout"
EXPRESS_FAILURE_TRUNCATED = "express_truncated"
EXPRESS_FAILURE_TRANSPORT = "express_transport_error"


class ExpressFailure(Exception):
    failure_code = "express_failed"
    retryable = False


class ExpressBudgetExceeded(ExpressFailure):
    failure_code = EXPRESS_FAILURE_BUDGET


class ExpressCancelled(ExpressFailure):
    failure_code = EXPRESS_FAILURE_CANCELLED


class ExpressTimeout(ExpressFailure):
    failure_code = EXPRESS_FAILURE_TIMEOUT


class ExpressTruncated(ExpressFailure):
    failure_code = EXPRESS_FAILURE_TRUNCATED


class ExpressTransportError(ExpressFailure):
    failure_code = EXPRESS_FAILURE_TRANSPORT
    retryable = True


def _extract_json_object(content: str) -> str:
    """Best-effort recovery of a JSON object from LLM output.

    The audit-only critic asks codex-cli for a JSON document, but
    real LLM output occasionally arrives wrapped in a ```json fence,
    prefixed with a short prose preamble, or followed by trailing
    commentary. Strict ``json.loads`` then fails and the run is
    marked ``express_truncated`` even though the manuscript itself
    is intact. This helper strips Markdown fences and, failing
    that, slices from the first ``{`` to the matching last ``}``
    so a parseable object survives. The retry policy is unchanged:
    if no object can be recovered, ``ExpressTruncated`` is raised.
    """
    stripped = content.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
        stripped = stripped.strip()
    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidate = stripped[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError as exc:
            raise ExpressTruncated(f"codex-cli returned non-JSON audit: {exc}") from exc
    raise ExpressTruncated("codex-cli returned non-JSON audit: no object boundary")


@dataclass(frozen=True)
class ExpressCompletion:
    content: str
    provider: str
    provider_model: str
    usage: Mapping[str, object]
    finish_reason: str | None = None


class ExpressCompletionClient(Protocol):
    def complete(
        self,
        prompt: PromptBundle,
        *,
        timeout_seconds: int,
        max_tokens: int,
        expect_json: bool = False,
    ) -> ExpressCompletion: ...


class HttpExpressClient:
    """OpenAI-compatible HTTP adapter pinned to the express model."""

    def __init__(
        self,
        *,
        model: str | None = None,
        providers: Sequence[LLMProviderSpec] | None = None,
    ) -> None:
        self._model = model or get_settings().express_model
        self._providers = list(providers) if providers is not None else None

    def complete(
        self,
        prompt: PromptBundle,
        *,
        timeout_seconds: int,
        max_tokens: int,
        expect_json: bool = False,
    ) -> ExpressCompletion:
        async def _call() -> dict[str, object]:
            client = LLMClient(
                providers=self._pinned_providers(),
                timeout_seconds=float(timeout_seconds),
            )
            try:
                return await client.chat_completion(
                    prompt.messages,
                    self._model,
                    0.2 if expect_json else 0.7,
                    max_tokens=max_tokens,
                    retries=0,
                    response_format={"type": "json_object"} if expect_json else None,
                    force_no_reasoning=True,
                    validate_json_content=False,
                    stream=not expect_json,
                )
            finally:
                await client.aclose()

        try:
            response = asyncio.run(_call())
        except Exception as exc:  # noqa: BLE001 - normalize all HTTP/provider failures.
            raise ExpressTransportError(
                f"express HTTP completion failed: {type(exc).__name__}: {str(exc)[:500]}"
            ) from exc

        content = str(response.get("content", "")).strip()
        if expect_json:
            content = _extract_json_object(content)
        usage = response.get("usage")
        return ExpressCompletion(
            content=content,
            provider=_string_or(response.get("provider_used"), "llm-http"),
            provider_model=_string_or(response.get("provider_model"), self._model),
            usage=usage if isinstance(usage, Mapping) else {},
            finish_reason=_optional_string(response.get("finish_reason")),
        )

    def _pinned_providers(self) -> list[LLMProviderSpec]:
        providers = list(self._providers) if self._providers is not None else get_llm_providers()
        if not providers:
            raise ExpressTransportError("no LLM providers configured for express mode")
        return [
            LLMProviderSpec(
                name=provider.name,
                base_url=provider.base_url,
                api_key=provider.api_key,
                model=self._model,
            )
            for provider in providers
        ]


class CodexCliExpressClient:
    """Codex CLI adapter pinned to gpt-5.4 by settings."""

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model or get_settings().express_codex_model

    def complete(
        self,
        prompt: PromptBundle,
        *,
        timeout_seconds: int,
        max_tokens: int,
        expect_json: bool = False,
    ) -> ExpressCompletion:
        codex = shutil.which("codex")
        if codex is None:
            raise ExpressTransportError("codex executable is required for express mode")
        with tempfile.NamedTemporaryFile(prefix="express-codex-", suffix=".md") as output_file:
            try:
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
                        self._model,
                        "-c",
                        "model_reasoning_effort=medium",
                        "--output-last-message",
                        output_file.name,
                        "-",
                    ],
                    input=_codex_cli_adapter_prompt(
                        prompt,
                        expect_json=expect_json,
                        max_tokens=max_tokens,
                    ),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise ExpressTimeout(
                    f"codex-cli express call timed out after {timeout_seconds}s"
                ) from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise ExpressTransportError(
                    "codex-cli express call failed"
                    + (
                        f": {detail[:1000]}"
                        if detail
                        else f" with exit code {completed.returncode}"
                    ),
                )
            content = Path(output_file.name).read_text(encoding="utf-8").strip()
        if expect_json:
            content = _extract_json_object(content)
        return ExpressCompletion(
            content=content,
            provider="codex-cli",
            provider_model=self._model,
            usage=_codex_cli_usage(completed.stdout),
        )


def run_express(
    run_id: str,
    db_session: Session | None = None,
    *,
    lock_token: str | None = None,
    completion_client: ExpressCompletionClient | None = None,
    hooks: HookRegistry | None = None,
) -> dict[str, object]:
    client = completion_client or HttpExpressClient()

    def _execute(db: Session) -> dict[str, object]:
        run = db.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        if run.state not in {"DOMAIN_LOADED", "EXPRESS_FAILED", "EXPRESS_RUNNING"}:
            raise ValueError(
                "express runner requires DOMAIN_LOADED/EXPRESS_FAILED/EXPRESS_RUNNING, "
                f"got {run.state}"
            )
        if run.state != "EXPRESS_RUNNING":
            transition(
                run,
                "EXPRESS_RUNNING",
                db,
                reason="express_generation_started",
                payload={"runner": "express"},
            )
        append_event(db, run, "express_generation_started", {"runner": "express"})
        db.commit()
        db.refresh(run)
        try:
            return _run_express_pipeline(run, db, client=client, hooks=hooks or HookRegistry())
        except ExpressFailure as exc:
            return _fail_express(run, db, exc.failure_code, str(exc))

    with phase_lock_release_on_exit(run_id, "express", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def _run_express_pipeline(
    run: Run,
    session: Session,
    *,
    client: ExpressCompletionClient,
    hooks: HookRegistry,
) -> dict[str, object]:
    settings = get_settings()
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run.id}")
    run_dir = Path(run.run_dir)
    express_dir = run_dir / "express"
    express_dir.mkdir(parents=True, exist_ok=True)
    prompt, ars_context = _build_ars_prompt(project, run)
    _write_text(express_dir / "ars_prompt.redacted.md", prompt.as_text())
    prompt_estimate = _estimate_tokens(prompt.as_text())
    requested_estimate = prompt_estimate + settings.express_manuscript_max_tokens
    if requested_estimate > settings.express_token_cap:
        raise ExpressBudgetExceeded(
            f"estimated express request {requested_estimate} tokens exceeds cap "
            f"{settings.express_token_cap}",
        )
    _check_cancelled(run, session)
    ars_response = _complete_with_transport_retry(
        client,
        prompt,
        timeout_seconds=settings.express_timeout_seconds,
        max_tokens=settings.express_manuscript_max_tokens,
        expect_json=False,
    )
    _check_reported_budget(ars_response.usage, settings.express_token_cap, stage="ars")
    manuscript = ars_response.content.strip()
    if _is_truncated(ars_response) or not _looks_like_complete_manuscript(manuscript):
        raise ExpressTruncated("ARS manuscript was empty, truncated, or missing required sections")
    _write_text(express_dir / "ars_manuscript_raw.md", manuscript)
    _write_json(
        express_dir / "ars_usage.json",
        _stage_usage_payload("ars_single_call", ars_response),
    )
    _check_cancelled(run, session)
    audit_prompt = _build_audit_prompt(project, run, manuscript)
    audit_estimate = _estimate_tokens(audit_prompt.as_text()) + settings.express_audit_max_tokens
    ars_total = _usage_total(ars_response.usage) or requested_estimate
    if ars_total + audit_estimate > settings.express_token_cap:
        raise ExpressBudgetExceeded(
            f"estimated express audit total {ars_total + audit_estimate} tokens exceeds cap "
            f"{settings.express_token_cap}",
        )
    audit_payload, audit_response = _run_audit_critic(
        client,
        audit_prompt,
        timeout_seconds=settings.express_timeout_seconds,
        max_tokens=settings.express_audit_max_tokens,
    )
    usage_total = _sum_usage(ars_response.usage, audit_response.usage if audit_response else {})
    _check_reported_budget(usage_total, settings.express_token_cap, stage="total")
    _write_json(express_dir / "audit_critic.json", audit_payload)
    _write_text(express_dir / "audit_critic.md", _render_audit_markdown(audit_payload))
    integrity_summary = _run_integrity_audit_only(
        run=run,
        project=project,
        session=session,
        manuscript=manuscript,
        hooks=hooks,
    )
    humanized = _apply_humanizer(run, project, manuscript)
    _write_draft_compat_artifacts(run_dir, humanized)
    exports = _export_express_manuscript(run=run, project=project, manuscript=humanized)
    provenance = {
        "schema_version": "express_provenance_v1",
        "mode": "express",
        "run_id": run.id,
        "project_id": project.id,
        "provider": ars_response.provider,
        "provider_model": ars_response.provider_model,
        "token_cap": settings.express_token_cap,
        "token_usage": _token_usage_payload(usage_total),
        "ars_skill_sha": ars_context.get("ars_skill_sha"),
        "ars_skill_file_sha256": ars_context.get("ars_skill_file_sha256"),
        "prompt_sha256": prompt.sha256,
        "audit_prompt_sha256": audit_prompt.sha256,
        "integrity_audit": integrity_summary,
        "exports": exports,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(express_dir / "provenance.json", provenance)
    transition(
        run,
        "EXPRESS_DONE",
        session,
        reason="express_generation_completed",
        payload={
            "exports": exports.get("artifacts", {}),
            "token_usage": _token_usage_payload(usage_total),
        },
    )
    append_event(
        session,
        run,
        "express_generation_done",
        {"exports": exports.get("artifacts", {}), "token_usage": _token_usage_payload(usage_total)},
    )
    record_run_telemetry(session, run)
    session.commit()
    return {
        "run_id": run.id,
        "state": "EXPRESS_DONE",
        "exports": exports.get("artifacts", {}),
        "token_usage": _token_usage_payload(usage_total),
    }


def _build_ars_prompt(project: Project, run: Run) -> tuple[PromptBundle, Mapping[str, object]]:
    settings = get_settings()
    ars_context = _load_ars_full_mode_prompt(settings.express_ars_skill_path)
    kernel = KernelMetadata(
        title=project.title,
        research_kernel=dict(run.research_kernel_json or {}),
        target_journal=project.target_journal,
    )
    return (
        build_e_ars_prompt(
            kernel=kernel,
            ars_full_mode_prompt=_required_text(ars_context, "prompt"),
            humanizer_directive=humanizer_directive(project.language),
        ),
        ars_context,
    )


def _build_audit_prompt(project: Project, run: Run, manuscript: str) -> PromptBundle:
    kernel_json = json.dumps(run.research_kernel_json or {}, ensure_ascii=False, sort_keys=True)
    return PromptBundle(
        system=(
            "You are an audit-only academic manuscript critic. Return strict JSON only. "
            "Do not rewrite, repair, or block the manuscript."
        ),
        user="\n\n".join(
            [
                (
                    "Audit the manuscript for citation traceability, target word count, "
                    "structure, and style compliance."
                ),
                (
                    "Return JSON with keys: status, summary, citation_traceability, "
                    "word_count, style_compliance, issues."
                ),
                f"Project title: {project.title}",
                f"Target journal: {project.target_journal or ''}",
                f"Research kernel JSON: {kernel_json}",
                "<MANUSCRIPT>",
                manuscript,
                "</MANUSCRIPT>",
            ],
        ),
    )


def _run_audit_critic(
    client: ExpressCompletionClient,
    prompt: PromptBundle,
    *,
    timeout_seconds: int,
    max_tokens: int,
) -> tuple[dict[str, object], ExpressCompletion | None]:
    response = _complete_with_transport_retry(
        client,
        prompt,
        timeout_seconds=timeout_seconds,
        max_tokens=max_tokens,
        expect_json=True,
    )
    try:
        decoded = json.loads(response.content)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ExpressTruncated(f"audit critic returned invalid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ExpressTruncated("audit critic JSON root must be an object")
    decoded.setdefault("status", "completed")
    decoded["audit_only"] = True
    return {str(k): v for k, v in decoded.items()}, response


def _complete_with_transport_retry(
    client: ExpressCompletionClient,
    prompt: PromptBundle,
    *,
    timeout_seconds: int,
    max_tokens: int,
    expect_json: bool,
) -> ExpressCompletion:
    last_exc: ExpressFailure | None = None
    for attempt in range(2):
        try:
            return client.complete(
                prompt,
                timeout_seconds=timeout_seconds,
                max_tokens=max_tokens,
                expect_json=expect_json,
            )
        except ExpressTransportError as exc:
            last_exc = exc
            if attempt == 0:
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise ExpressTransportError("express completion did not run")


def _run_integrity_audit_only(
    *,
    run: Run,
    project: Project,
    session: Session,
    manuscript: str,
    hooks: HookRegistry,
) -> dict[str, object]:
    run_dir = Path(run.run_dir)
    integrity_dir = run_dir / "integrity"
    raw_dir = integrity_dir / "vendor_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    vendor_text = integrity_helpers.prepare_vendor_text(manuscript)
    scan_kinds = ["plagiarism", "ai_style"]
    settings = get_settings()
    actionable = [
        kind for kind in scan_kinds if integrity_helpers._vendor_keys_present(kind, settings)
    ]
    skipped = [kind for kind in scan_kinds if kind not in actionable]
    results = []
    try:
        if actionable:
            results = asyncio.run(
                integrity_helpers._run_requested_scans_via_harness(
                    text=vendor_text,
                    scan_kinds=actionable,
                    run=run,
                    project=project,
                    session=session,
                    hooks=hooks,
                ),
            )
    except integrity_helpers.IntegrityClientError as exc:
        append_event(
            session,
            run,
            "express_integrity_audit_failed",
            {"failure": str(exc), "audit_only": True},
        )
        results = []
        skipped = scan_kinds
    for skipped_kind in skipped:
        results.append(integrity_helpers._skipped_no_vendor_result(skipped_kind, vendor_text))
    stored_results = []
    for result in results:
        raw_filename = f"{_safe_filename(result.vendor)}_{_safe_filename(result.scan_id)}.json"
        raw_path = raw_dir / raw_filename
        integrity_helpers._write_json(raw_path, result.raw_response)
        stored_results.append(
            result.copy(update={"raw_report_path": str(raw_path.relative_to(run_dir))}),
        )
    plagiarism = [result for result in stored_results if result.scan_type == "plagiarism"]
    ai_style = [result for result in stored_results if result.scan_type == "ai_style"]
    integrity_helpers._write_text(
        integrity_dir / "plagiarism_report.md",
        integrity_helpers._report_markdown("Plagiarism Report", plagiarism),
    )
    integrity_helpers._write_text(
        integrity_dir / "ai_style_report.md",
        integrity_helpers._report_markdown("AI-Style Report", ai_style),
    )
    summary = integrity_helpers._summary_payload(stored_results, "express")
    summary.update(
        {
            "audit_only": True,
            "manuscript_source": "express",
            "manuscript_version": "express",
        },
    )
    integrity_helpers._write_json(integrity_dir / "integrity_summary.json", summary)
    append_event(session, run, "express_integrity_audit_done", summary)
    session.commit()
    return summary


def _apply_humanizer(run: Run, project: Project, manuscript: str) -> str:
    directive = humanizer_directive(project.language)
    express_dir = Path(run.run_dir) / "express"
    _write_json(
        express_dir / "humanizer.json",
        {
            "mode": "directive_applied_in_ars_prompt",
            "directive_sha256": _sha256_text(directive),
            "language": project.language,
        },
    )
    return manuscript


def _write_draft_compat_artifacts(run_dir: Path, manuscript: str) -> None:
    draft_dir = run_dir / "drafts" / "v001"
    style_dir = draft_dir / "style"
    style_dir.mkdir(parents=True, exist_ok=True)
    _write_text(draft_dir / "manuscript.md", manuscript)
    _write_text(style_dir / "paper_styled.md", manuscript)
    _write_text(draft_dir / "claim_map.jsonl", "")
    _write_text(draft_dir / "citations.bib", "")
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    shortlist = sources_dir / "shortlist.json"
    if not shortlist.exists():
        _write_text(shortlist, "[]\n")


def _export_express_manuscript(
    *,
    run: Run,
    project: Project,
    manuscript: str,
) -> dict[str, object]:
    run_dir = Path(run.run_dir)
    exports_dir = run_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    language = (project.language or "en").strip().lower() or "en"
    artifacts: dict[str, str] = {}
    export_helpers._write_text(exports_dir / "manuscript.md", manuscript)
    artifacts["markdown"] = "exports/manuscript.md"
    export_helpers._write_docx(exports_dir / "manuscript.docx", manuscript)
    artifacts["docx"] = "exports/manuscript.docx"
    export_helpers._write_html(exports_dir / "manuscript.html", manuscript, language)
    artifacts["html"] = "exports/manuscript.html"
    export_helpers._write_latex(exports_dir / "manuscript.tex", manuscript, language)
    artifacts["latex"] = "exports/manuscript.tex"
    export_helpers._write_text(exports_dir / "citations.bib", "")
    artifacts["bibtex"] = "exports/citations.bib"
    export_helpers._write_json(exports_dir / "citations.csl.json", [])
    artifacts["csl_json"] = "exports/citations.csl.json"
    manifest = export_helpers._manifest_payload(run_dir, artifacts, language)
    manifest["mode"] = "express"
    manifest["audit_only"] = True
    export_helpers._write_json(exports_dir / "manifest.json", manifest)
    return {"artifacts": artifacts, "manifest": manifest}


def _fail_express(
    run: Run, session: Session, failure_code: str, guidance: str
) -> dict[str, object]:
    if run.state == "EXPRESS_RUNNING":
        transition(
            run,
            "EXPRESS_FAILED",
            session,
            reason=failure_code,
            payload={"failure_code": failure_code, "guidance": guidance},
        )
    append_event(
        session,
        run,
        "express_generation_failed",
        {"failure_code": failure_code, "guidance": guidance},
    )
    express_dir = Path(run.run_dir) / "express"
    express_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        express_dir / "failure.json",
        {
            "failure_code": failure_code,
            "guidance": guidance,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    record_run_telemetry(session, run, audit_status="fail", failure_code=failure_code)
    session.commit()
    return {"run_id": run.id, "state": "EXPRESS_FAILED", "failure_code": failure_code}


def _check_cancelled(run: Run, session: Session) -> None:
    session.refresh(run, ["cancel_requested_at", "state"])
    if run.cancel_requested_at is not None:
        raise ExpressCancelled(f"run {run.id} cancelled during express generation")


def _is_truncated(response: ExpressCompletion) -> bool:
    return (response.finish_reason or "").strip().lower() in {"length", "max_tokens", "truncated"}


def _looks_like_complete_manuscript(manuscript: str) -> bool:
    text = manuscript.strip()
    if len(text) < 1000:
        return False
    lowered = text.lower()
    required_any = [
        ("摘要", "abstract"),
        ("关键词", "keywords"),
        ("结论", "conclusion"),
        ("参考文献", "references", "bibliography"),
    ]
    return all(any(marker in lowered for marker in markers) for markers in required_any)


def _check_reported_budget(usage: Mapping[str, object], cap: int, *, stage: str) -> None:
    total = _usage_total(usage)
    if total > cap:
        raise ExpressBudgetExceeded(f"reported {stage} token usage {total} exceeds cap {cap}")


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _usage_total(usage: Mapping[str, object]) -> int:
    return _int_value(usage.get("total_tokens"))


def _sum_usage(*usages: Mapping[str, object]) -> dict[str, int]:
    prompt = 0
    completion = 0
    total = 0
    for usage in usages:
        prompt += _int_value(usage.get("prompt_tokens"))
        completion += _int_value(usage.get("completion_tokens"))
        total += _int_value(usage.get("total_tokens"))
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _token_usage_payload(usage: Mapping[str, object]) -> dict[str, int]:
    return {
        "prompt_tokens": _int_value(usage.get("prompt_tokens")),
        "completion_tokens": _int_value(usage.get("completion_tokens")),
        "total_tokens": _int_value(usage.get("total_tokens")),
    }


def _stage_usage_payload(stage: str, response: ExpressCompletion) -> dict[str, object]:
    return {
        "stage": stage,
        "provider": response.provider,
        "provider_model": response.provider_model,
        "token_usage": _token_usage_payload(response.usage),
        "finish_reason": response.finish_reason,
    }


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    return 0


def _string_or(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _codex_cli_adapter_prompt(prompt: PromptBundle, *, expect_json: bool, max_tokens: int) -> str:
    suffix = "\nReturn strict JSON only." if expect_json else ""
    return (
        "You are acting as a chat-completion adapter for production express mode.\n"
        "Do not inspect the repository, run shell commands, edit files, or explain the process.\n"
        "Return only the final content requested by the embedded SYSTEM and USER prompts."
        f"{suffix}\n"
        f"Maximum output budget: {max_tokens} tokens.\n\n"
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


def _render_audit_markdown(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "# Express Audit Critic",
            "",
            f"- Status: {payload.get('status', 'unknown')}",
            f"- Audit only: {payload.get('audit_only', True)}",
            f"- Summary: {payload.get('summary', '')}",
            "",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ],
    )


def _safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned.strip("_") or "item"


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)
