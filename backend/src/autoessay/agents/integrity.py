"""Integrity agent for user-approved plagiarism and AI-style scans."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents.critic import latest_draft_dir
from autoessay.agents.final_rewrite import load_latest_rewrite_artifact
from autoessay.clients.integrity import (
    IntegrityClientError,
    NormalizedScanResult,
    copyleaks,
    document_hash,
    gptzero,
    originality,
)
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.harness import (
    AuditVerdict,
    AuditWriter,
    HookContext,
    HookRegistry,
    HookResult,
    ToolCallRequest,
    ToolCallResponse,
    ToolInvocationError,
    hash_text,
    run_tool_step,
)
from autoessay.models import Checkpoint, Project, Run
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

SCAN_KINDS = {"plagiarism", "ai_style"}
INTEGRITY_PAYLOAD_PREVIEW_CHARS = 1200


@dataclass(frozen=True)
class IntegrityVendor:
    provider: str
    endpoint: Callable[[str], str]
    scan: Callable[[str, str], Awaitable[NormalizedScanResult]]


def run_integrity(
    run_id: str,
    db_session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run integrity. Stage 3.E follow-up P0: ``lock_token`` triggers
    owner-checked phase-start lock release at exit. PR-A4.1b
    (2026-05-02): wraps in ``maybe_run_with_versioning``."""
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_integrity_with_session(
                run_id,
                session,
                hooks or HookRegistry(),
            )

        maybe_run_with_versioning(session, run, "integrity", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "integrity", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def load_integrity_payload(run: Run) -> dict[str, object]:
    integrity_dir = Path(run.run_dir) / "integrity"
    return {
        "run_id": run.id,
        "plagiarism_report": _read_optional_text(integrity_dir / "plagiarism_report.md"),
        "ai_style_report": _read_optional_text(integrity_dir / "ai_style_report.md"),
        "integrity_summary": _load_json_mapping(integrity_dir / "integrity_summary.json"),
    }


def latest_external_scan_decision(session: Session, run: Run) -> dict[str, object] | None:
    checkpoint = session.scalar(
        select(Checkpoint)
        .where(Checkpoint.run_id == run.id)
        .where(Checkpoint.checkpoint_type == "USER_EXTERNAL_SCAN_APPROVAL")
        .order_by(Checkpoint.created_at.desc(), Checkpoint.id.desc())
        .limit(1),
    )
    if checkpoint is None:
        return None
    payload = _json_object(checkpoint.decision_payload)
    payload["status"] = checkpoint.status
    return payload


def _run_integrity_with_session(
    run_id: str,
    session: Session,
    hooks: HookRegistry,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    assert_run_active(run, session)
    if run.state not in {"USER_EXTERNAL_SCAN_APPROVAL", "FAILED_VENDOR"}:
        raise InvalidTransition(
            f"Integrity requires USER_EXTERNAL_SCAN_APPROVAL or FAILED_VENDOR, got {run.state}",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    decision = latest_external_scan_decision(session, run)
    scan_kinds = _approved_scan_kinds(decision)
    if not scan_kinds:
        append_event(
            session,
            run,
            "phase_waiting",
            {
                "phase": "integrity",
                "guidance": "External scan needs explicit approval or skip-with-note.",
            },
        )
        session.commit()
        return {
            "run_id": run.id,
            "state": run.state,
            "guidance": "External scan approval is required.",
        }

    run_dir = Path(run.run_dir)
    draft_dir = latest_draft_dir(run_dir)
    rewrite = load_latest_rewrite_artifact(run_dir)
    if draft_dir is None and rewrite is None:
        return _fail_vendor(run, session, "Integrity could not find a draft to scan.")
    if rewrite is not None:
        draft_text = rewrite.manuscript
        manuscript_source = "rewrite"
        manuscript_version = rewrite.version
    elif draft_dir is not None:
        draft_text = _read_optional_text(draft_dir / "style" / "paper_styled.md")
        manuscript_source = "stylist"
        manuscript_version = draft_dir.name
    else:
        return _fail_vendor(run, session, "Integrity could not find a draft to scan.")
    if not draft_text.strip():
        return _fail_vendor(run, session, "Integrity found an empty styled draft.")
    vendor_text = prepare_vendor_text(draft_text)
    if not vendor_text.strip():
        return _fail_vendor(run, session, "Integrity payload was empty after privacy stripping.")

    settings = get_settings()
    actionable_kinds: list[str] = []
    skipped_kinds: list[str] = []
    for scan_kind in scan_kinds:
        if _vendor_keys_present(scan_kind, settings):
            actionable_kinds.append(scan_kind)
        else:
            skipped_kinds.append(scan_kind)

    transition(run, "INTEGRITY_RUNNING", session, reason="Integrity started")
    append_event(
        session,
        run,
        "phase_started",
        {
            "phase": "integrity",
            "run_id": run.id,
            "draft_version": draft_dir.name if draft_dir is not None else None,
            "manuscript_source": manuscript_source,
            "manuscript_version": manuscript_version,
            **({"rewrite_version": rewrite.version} if rewrite is not None else {}),
            "scan_kinds": scan_kinds,
            "actionable_scan_kinds": actionable_kinds,
            "skipped_scan_kinds": skipped_kinds,
        },
    )
    if skipped_kinds:
        append_event(
            session,
            run,
            "scan_kinds_skipped",
            {
                "phase": "integrity",
                "scan_kinds": skipped_kinds,
                "reason": "no_vendor_configured",
                "guidance": (
                    "No integrity vendor key is configured for these scan kinds; "
                    "the run will continue with these scans skipped."
                ),
            },
        )
    session.commit()
    session.refresh(run)

    try:
        if actionable_kinds:
            results = asyncio.run(
                _run_requested_scans_via_harness(
                    text=vendor_text,
                    scan_kinds=actionable_kinds,
                    run=run,
                    project=project,
                    session=session,
                    hooks=hooks,
                ),
            )
        else:
            results = []
    except IntegrityClientError as exc:
        return _fail_vendor(run, session, str(exc))

    for skipped_kind in skipped_kinds:
        results.append(_skipped_no_vendor_result(skipped_kind, vendor_text))

    integrity_dir = run_dir / "integrity"
    raw_dir = integrity_dir / "vendor_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    stored_results: list[NormalizedScanResult] = []
    for result in results:
        raw_filename = f"{_safe_filename(result.vendor)}_{_safe_filename(result.scan_id)}.json"
        raw_path = raw_dir / raw_filename
        _write_json(raw_path, result.raw_response)
        stored_results.append(
            result.copy(update={"raw_report_path": str(raw_path.relative_to(run_dir))}),
        )

    plagiarism_results = [result for result in stored_results if result.scan_type == "plagiarism"]
    ai_style_results = [result for result in stored_results if result.scan_type == "ai_style"]
    _write_text(
        integrity_dir / "plagiarism_report.md",
        _report_markdown("Plagiarism Report", plagiarism_results),
    )
    _write_text(
        integrity_dir / "ai_style_report.md",
        _report_markdown("AI-Style Report", ai_style_results),
    )
    summary = _summary_payload(
        stored_results,
        manuscript_version,
    )
    summary["manuscript_source"] = manuscript_source
    summary["manuscript_version"] = manuscript_version
    if rewrite is not None:
        summary["rewrite_version"] = rewrite.version
    _write_json(integrity_dir / "integrity_summary.json", summary)

    transition(
        run,
        "USER_INTEGRITY_REVIEW",
        session,
        reason="Integrity completed",
        payload=summary,
    )
    append_event(session, run, "phase_done", {"phase": "integrity", **summary})
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


async def _run_requested_scans_via_harness(
    *,
    text: str,
    scan_kinds: Sequence[str],
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
) -> list[NormalizedScanResult]:
    results: list[NormalizedScanResult] = []
    errors: list[str] = []
    for scan_kind in scan_kinds:
        try:
            result = await _scan_with_fallback_via_harness(
                text=text,
                scan_kind=scan_kind,
                run=run,
                project=project,
                session=session,
                hooks=hooks,
            )
        except IntegrityClientError as exc:
            errors.append(str(exc))
            continue
        results.append(result)
    if errors:
        raise IntegrityClientError("; ".join(errors))
    return results


async def _scan_with_fallback_via_harness(
    *,
    text: str,
    scan_kind: str,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
) -> NormalizedScanResult:
    errors: list[str] = []
    for vendor in _vendors_for_scan_kind(scan_kind):
        try:
            return await _integrity_vendor_via_harness(
                text=text,
                scan_kind=scan_kind,
                vendor=vendor,
                run=run,
                project=project,
                session=session,
                hooks=hooks,
            )
        except ToolInvocationError as exc:
            errors.append(f"{vendor.provider}: {exc.failure_class}: {exc}")
        except Exception as exc:  # noqa: BLE001 - caller needs aggregate vendor failure.
            errors.append(f"{vendor.provider}: {exc}")
    raise IntegrityClientError(f"all vendors failed for {scan_kind}: {'; '.join(errors)}")


async def _integrity_vendor_via_harness(
    *,
    text: str,
    scan_kind: str,
    vendor: IntegrityVendor,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
) -> NormalizedScanResult:
    endpoint = vendor.endpoint(scan_kind)
    request_payload = _bounded_integrity_payload(text, scan_kind)
    request = ToolCallRequest(
        provider=vendor.provider,
        endpoint=endpoint,
        payload=request_payload,
        request_id=f"integrity_{scan_kind}_{vendor.provider}",
        prompt_template_id="integrity.vendor_scan.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="integrity",
        step_id="integrity.vendor_scan",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=json.dumps(request_payload, sort_keys=True),
        prompt_hash=hash_text(json.dumps(request_payload, sort_keys=True)),
        project_title=project.title,
        run_metadata={
            "agent_phase": "integrity",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "vendor": vendor.provider,
            "endpoint": endpoint,
            "scan_kind": scan_kind,
            "request_payload": request_payload,
            "response_schema": "NormalizedScanResult",
        },
    )
    call_hooks = _make_integrity_tool_hooks(
        base_hooks=hooks,
        vendor=vendor.provider,
        endpoint=endpoint,
        scan_kind=scan_kind,
        expected_document_hash=document_hash(text),
    )

    async def call_vendor() -> NormalizedScanResult:
        return await vendor.scan(text, scan_kind)

    response = await run_tool_step(
        request=request,
        hooks=call_hooks,
        context=context,
        tool=call_vendor,
        output_schema=NormalizedScanResult,
        audit=AuditWriter(
            session=session,
            run_dir=run.run_dir,
            agent_name="Integrity",
            provider=vendor.provider,
        ),
        max_transient_retries=1,
    )
    if isinstance(response.parsed, NormalizedScanResult):
        return response.parsed
    if isinstance(response.parsed, Mapping):
        return NormalizedScanResult.parse_obj(response.parsed)
    raise IntegrityClientError(f"{vendor.provider} returned an invalid integrity result")


def _vendor_keys_present(scan_kind: str, settings: object) -> bool:
    """Return True if at least one vendor for ``scan_kind`` is usable.

    Stub mode counts as "usable" because vendor adapters short-circuit to
    deterministic fixtures without contacting the real APIs.
    """
    if getattr(settings, "integrity_stub", False):
        return True
    if scan_kind == "plagiarism":
        if getattr(settings, "originality_api_key", None):
            return True
        return bool(
            getattr(settings, "copyleaks_email", None)
            and getattr(settings, "copyleaks_api_key", None)
        )
    if scan_kind == "ai_style":
        if getattr(settings, "originality_api_key", None):
            return True
        return bool(getattr(settings, "gptzero_api_key", None))
    return False


def _skipped_no_vendor_result(scan_kind: str, text: str) -> NormalizedScanResult:
    return NormalizedScanResult(
        vendor="none",
        scan_type=scan_kind,
        document_hash=document_hash(text),
        status="skipped_no_vendor",
        score=None,
        spans=[],
        scan_id=f"skipped_{scan_kind}",
        raw_response={
            "reason": "no_vendor_configured",
            "scan_kind": scan_kind,
            "note": (
                "No integrity vendor for this scan kind has a key configured; "
                "the run continued without this scan."
            ),
        },
    )


def _vendors_for_scan_kind(scan_kind: str) -> tuple[IntegrityVendor, ...]:
    if scan_kind == "plagiarism":
        return (
            IntegrityVendor(
                provider="originality",
                endpoint=_originality_endpoint,
                scan=originality.scan,
            ),
            IntegrityVendor(
                provider="copyleaks",
                endpoint=_copyleaks_endpoint,
                scan=copyleaks.scan,
            ),
        )
    if scan_kind == "ai_style":
        return (
            IntegrityVendor(
                provider="originality",
                endpoint=_originality_endpoint,
                scan=originality.scan,
            ),
            IntegrityVendor(
                provider="gptzero",
                endpoint=_gptzero_endpoint,
                scan=gptzero.scan,
            ),
        )
    raise IntegrityClientError(f"unsupported scan kind: {scan_kind}")


def _originality_endpoint(scan_kind: str) -> str:
    return "/api/v1/scan/ai" if scan_kind == "ai_style" else "/api/v1/scan/plagiarism"


def _gptzero_endpoint(_scan_kind: str) -> str:
    return "/v2/predict/text"


def _copyleaks_endpoint(_scan_kind: str) -> str:
    return "/v3/scans/submit/file/{scan_id}"


def _bounded_integrity_payload(text: str, scan_kind: str) -> dict[str, object]:
    return {
        "scan_type": scan_kind,
        "document_hash": document_hash(text),
        "text_length": len(text),
        "text_preview": text[:INTEGRITY_PAYLOAD_PREVIEW_CHARS],
    }


def _make_integrity_tool_hooks(
    *,
    base_hooks: HookRegistry,
    vendor: str,
    endpoint: str,
    scan_kind: str,
    expected_document_hash: str,
) -> HookRegistry:
    hooks = _copy_hook_registry(base_hooks)
    hooks.register_pre_tool(
        "integrity_request_log",
        _make_integrity_request_log_hook(vendor=vendor, endpoint=endpoint),
    )
    hooks.register_post_tool(
        "normalized_scan_result",
        _make_normalized_scan_result_hook(
            provider=vendor,
            scan_kind=scan_kind,
            expected_document_hash=expected_document_hash,
        ),
    )
    return hooks


def _copy_hook_registry(base_hooks: HookRegistry) -> HookRegistry:
    copied = HookRegistry()
    copied._pre_llm = list(base_hooks._pre_llm)
    copied._post_llm = list(base_hooks._post_llm)
    copied._pre_tool = list(base_hooks._pre_tool)
    copied._post_tool = list(base_hooks._post_tool)
    return copied


def _make_integrity_request_log_hook(
    *,
    vendor: str,
    endpoint: str,
) -> Callable[[HookContext], HookContext]:
    def pre_tool(ctx: HookContext) -> HookContext:
        metadata = dict(ctx.run_metadata)
        metadata["pre_tool"] = {
            "vendor": vendor,
            "endpoint": endpoint,
            "request_payload": metadata.get("request_payload", {}),
        }
        return replace(ctx, run_metadata=metadata)

    return pre_tool


def _make_normalized_scan_result_hook(
    *,
    provider: str,
    scan_kind: str,
    expected_document_hash: str,
) -> Callable[[HookContext, ToolCallResponse], HookResult]:
    def post_tool(_ctx: HookContext, response: ToolCallResponse) -> HookResult:
        parsed = response.parsed
        if not isinstance(parsed, NormalizedScanResult):
            return HookResult(
                annotations={"errors": ["response is not a NormalizedScanResult"]},
                verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
            )
        errors: list[str] = []
        if parsed.scan_type != scan_kind:
            errors.append(f"scan_type expected {scan_kind}, got {parsed.scan_type}")
        if parsed.document_hash != expected_document_hash:
            errors.append("document_hash did not match approved scan payload")
        if not _vendor_matches_provider(parsed.vendor, provider):
            errors.append(f"vendor expected {provider}, got {parsed.vendor}")
        if errors:
            return HookResult(
                annotations={"errors": errors},
                verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
            )
        return HookResult(
            annotations={
                "schema": "NormalizedScanResult",
                "scan_type": parsed.scan_type,
                "vendor": parsed.vendor,
                "span_count": len(parsed.spans),
            },
        )

    return post_tool


def _vendor_matches_provider(vendor: str, provider: str) -> bool:
    aliases = {"originality": {"originality", "originality_ai"}}
    return vendor == provider or vendor in aliases.get(provider, set())


def prepare_vendor_text(draft_text: str) -> str:
    lines = draft_text.splitlines()
    kept: list[str] = []
    in_bibliography = False
    for line in lines:
        if _is_bibliography_heading(line):
            in_bibliography = True
            continue
        if in_bibliography:
            continue
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip() + "\n"


def _is_bibliography_heading(line: str) -> bool:
    stripped = line.strip().lower()
    return bool(re.match(r"^#{1,6}\s+(bibliography|references|works cited)\s*$", stripped))


def _approved_scan_kinds(decision: Mapping[str, object] | None) -> list[str]:
    if decision is None or decision.get("status") != "ACCEPTED":
        return []
    if decision.get("approve") is not True:
        return []
    raw_kinds = decision.get("scan_kinds")
    if not isinstance(raw_kinds, list):
        return []
    kinds: list[str] = []
    for item in raw_kinds:
        if isinstance(item, str) and item in SCAN_KINDS and item not in kinds:
            kinds.append(item)
    return kinds


def _fail_vendor(run: Run, session: Session, guidance: str) -> dict[str, object]:
    if run.state != "FAILED_VENDOR":
        transition(
            run,
            "FAILED_VENDOR",
            session,
            reason="Integrity vendor failure",
            payload={"guidance": guidance, "resume_options": ["retry_later", "skip_with_note"]},
        )
    append_event(
        session,
        run,
        "phase_failed",
        {
            "phase": "integrity",
            "failure_class": "failed_vendor",
            "guidance": guidance,
            "resume_options": ["retry_later", "skip_with_note"],
        },
    )
    session.commit()
    return {
        "run_id": run.id,
        "state": run.state,
        "guidance": guidance,
        "resume_options": ["retry_later", "skip_with_note"],
    }


def _report_markdown(title: str, results: Sequence[NormalizedScanResult]) -> str:
    lines = ["# " + title, ""]
    if not results:
        lines.append("No scan was requested for this category.")
        return "\n".join(lines).rstrip() + "\n"
    for result in results:
        score = "n/a" if result.score is None else f"{result.score:.3f}"
        lines.extend(
            [
                f"## {result.vendor}",
                "",
                f"- Scan ID: {result.scan_id}",
                f"- Status: {result.status}",
                f"- Score: {score}",
                f"- Document hash: {result.document_hash}",
                f"- Raw report: {result.raw_report_path or 'not stored'}",
                "",
                "### Spans",
                "",
            ],
        )
        if not result.spans:
            lines.append("No spans returned.")
            lines.append("")
            continue
        for span in result.spans:
            confidence = "n/a" if span.confidence is None else f"{span.confidence:.3f}"
            source_url = f" ({span.source_url})" if span.source_url else ""
            lines.append(
                f"- `{span.span_id}` {span.start}-{span.end}: {span.label}; "
                f"confidence {confidence}{source_url}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _summary_payload(
    results: Sequence[NormalizedScanResult],
    draft_version: str,
) -> dict[str, object]:
    by_kind: dict[str, dict[str, object]] = {}
    for result in results:
        by_kind[result.scan_type] = {
            "vendor": result.vendor,
            "scan_id": result.scan_id,
            "score": result.score,
            "status": result.status,
            "span_count": len(result.spans),
            "spans": [dict(span.dict()) for span in result.spans],
            "raw_report_path": result.raw_report_path,
        }
    return {
        "draft_version": draft_version,
        "scans": by_kind,
        "span_counts": {kind: payload["span_count"] for kind, payload in by_kind.items()},
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "mode": "soft_signal_no_auto_revision",
    }


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: value for key, value in decoded.items() if isinstance(key, str)}


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


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "scan"
