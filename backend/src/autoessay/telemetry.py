"""ADR-0003 P3 production telemetry for generation-mode comparison."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoessay.generation_modes import DEEP_MODE, EXPRESS_MODE
from autoessay.models import ProviderCall, Run, RunTelemetry, utcnow

DEEP_TELEMETRY_STATES: frozenset[str] = frozenset(
    {
        "EXPORTS_DONE",
        "FAILED_FIXABLE",
        "FAILED_NEEDS_USER",
        "FAILED_VENDOR",
        "FAILED_POLICY",
        "CANCELLED",
    },
)


def record_deep_transition_telemetry(
    session: Session,
    run: Run,
    *,
    to_state: str,
    reason: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> RunTelemetry | None:
    if _run_mode(run) != DEEP_MODE or to_state not in DEEP_TELEMETRY_STATES:
        return None
    failure_code: str | None = None
    audit_status = "unknown"
    if to_state == "EXPORTS_DONE":
        audit_status = "pass"
    elif to_state == "CANCELLED":
        audit_status = "cancelled"
        failure_code = "cancelled"
    elif to_state.startswith("FAILED_"):
        audit_status = "fail"
        failure_code = _failure_code_from_transition(
            state=to_state,
            reason=reason,
            payload=payload,
        )
    return record_run_telemetry(
        session,
        run,
        audit_status=audit_status,
        failure_code=failure_code,
    )


def record_run_telemetry(
    session: Session,
    run: Run,
    *,
    finished_at: datetime | None = None,
    audit_status: str | None = None,
    failure_code: str | None = None,
) -> RunTelemetry:
    finished = finished_at or utcnow()
    created = _datetime_or_now(getattr(run, "created_at", None), fallback=finished)
    mode = _run_mode(run)
    telemetry = session.get(RunTelemetry, run.id)
    if telemetry is None:
        telemetry = RunTelemetry(
            run_id=run.id,
            mode=mode,
            created_at=created,
            finished_at=finished,
        )
    telemetry.mode = mode
    telemetry.total_tokens = _collect_total_tokens(session, run, mode=mode)
    telemetry.latency_ms = _latency_ms(created, finished)
    telemetry.audit_status = (
        audit_status or _derive_audit_status(run, mode=mode)
    ).strip() or "unknown"
    telemetry.manuscript_chars = _manuscript_chars(run, mode=mode)
    telemetry.created_at = created
    telemetry.finished_at = finished
    telemetry.failure_code = (
        failure_code if failure_code is not None else _derive_failure_code(run, mode=mode)
    )
    session.add(telemetry)
    return telemetry


def _run_mode(run: Run) -> str:
    mode = getattr(run, "generation_mode", None)
    return EXPRESS_MODE if mode == EXPRESS_MODE else DEEP_MODE


def _collect_total_tokens(session: Session, run: Run, *, mode: str) -> int | None:
    if mode == EXPRESS_MODE:
        provenance = _read_json_object(Path(run.run_dir) / "express" / "provenance.json")
        usage = provenance.get("token_usage") if provenance else None
        if isinstance(usage, Mapping):
            total = _int_or_none(usage.get("total_tokens"))
            if total is not None:
                return total
    total = session.scalar(
        select(func.sum(ProviderCall.units)).where(
            ProviderCall.run_id == run.id,
            ProviderCall.units.is_not(None),
        ),
    )
    return _int_or_none(total)


def _derive_audit_status(run: Run, *, mode: str) -> str:
    if mode == EXPRESS_MODE:
        if Path(run.run_dir, "express", "failure.json").is_file() or run.state == "EXPRESS_FAILED":
            return "fail"
        audit_payload = _read_json_object(Path(run.run_dir) / "express" / "audit_critic.json")
        return _express_audit_status(audit_payload)
    if run.state == "EXPORTS_DONE":
        return "pass"
    if run.state == "CANCELLED":
        return "cancelled"
    if run.state.startswith("FAILED_"):
        return "fail"
    return "unknown"


def _express_audit_status(payload: Mapping[str, Any]) -> str:
    raw_status = str(payload.get("status") or "").strip().lower()
    if raw_status in {"pass", "passed", "ok", "accepted", "clean"}:
        return "pass"
    if raw_status in {"fail", "failed", "block", "blocked", "rejected", "error"}:
        return "fail"
    if _contains_blocking_issue(payload):
        return "fail"
    if payload:
        return "pass"
    return "unknown"


def _contains_blocking_issue(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in {"severity", "status", "verdict"} and str(item).strip().lower() in {
                "blocker",
                "critical",
                "high",
                "fail",
                "failed",
                "blocked",
                "reject",
                "rejected",
            }:
                return True
            if _contains_blocking_issue(item):
                return True
    elif isinstance(value, list):
        return any(_contains_blocking_issue(item) for item in value)
    return False


def _manuscript_chars(run: Run, *, mode: str) -> int | None:
    run_dir = Path(run.run_dir)
    candidates = [
        run_dir / "exports" / "manuscript.md",
        run_dir / "drafts" / "v001" / "manuscript.md",
    ]
    if mode == EXPRESS_MODE:
        candidates.append(run_dir / "express" / "ars_manuscript_raw.md")
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        return len(text)
    return None


def _derive_failure_code(run: Run, *, mode: str) -> str | None:
    if mode == EXPRESS_MODE:
        failure = _read_json_object(Path(run.run_dir) / "express" / "failure.json")
        code = _string_or_none(failure.get("failure_code"))
        if code:
            return code
    if run.state == "CANCELLED":
        return "cancelled"
    if run.state.startswith("FAILED_") or run.state == "EXPRESS_FAILED":
        return run.state
    return None


def _failure_code_from_transition(
    *,
    state: str,
    reason: str | None,
    payload: Mapping[str, Any] | None,
) -> str:
    if payload is not None:
        for key in ("failure_code", "failure_class", "code"):
            code = _string_or_none(payload.get(key))
            if code:
                return code
    if reason:
        return _slug_reason(reason)
    return state


def _slug_reason(reason: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in reason.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:128] or "failed"


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _latency_ms(created: datetime, finished: datetime) -> int:
    delta = finished - created
    return max(int(delta.total_seconds() * 1000), 0)


def _datetime_or_now(value: object, *, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return fallback


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float | Decimal):
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None
    return max(parsed, 0)


def _string_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
