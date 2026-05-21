"""Generate ADR-0003 mode telemetry comparison reports."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from autoessay.db import SessionLocal
from autoessay.generation_modes import DEEP_MODE, EXPRESS_MODE
from autoessay.models import Run, RunTelemetry, utcnow

MODES: tuple[str, str] = (EXPRESS_MODE, DEEP_MODE)
_SINCE_RE = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[dhw])$")


def build_report(session: Session, *, since: datetime) -> dict[str, Any]:
    since = _ensure_aware_utc(since)
    telemetry_rows = list(
        session.scalars(
            select(RunTelemetry)
            .where(RunTelemetry.finished_at >= since)
            .order_by(RunTelemetry.finished_at.asc()),
        ),
    )
    created_distribution = _created_run_distribution(session, since=since)
    return {
        "schema_version": "mode_telemetry_report_v1",
        "generated_at": utcnow().isoformat(),
        "since": since.isoformat(),
        "telemetry_run_count": len(telemetry_rows),
        "mode_distribution": _mode_distribution(telemetry_rows),
        "created_run_mode_distribution": created_distribution,
        "deep_selection_rate": _selection_rate(created_distribution, DEEP_MODE),
        "median_tokens": _median_by_mode(telemetry_rows, lambda row: row.total_tokens),
        "median_latency_ms": _median_by_mode(telemetry_rows, lambda row: row.latency_ms),
        "audit_pass_rate": _audit_pass_rates(telemetry_rows),
        "failure_rate": _failure_rates(telemetry_rows),
        "failure_distribution": _failure_distribution(telemetry_rows),
        "audit_status_distribution": _audit_status_distribution(telemetry_rows),
    }


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    reference = _ensure_aware_utc(now or utcnow())
    stripped = value.strip()
    match = _SINCE_RE.fullmatch(stripped)
    if match is not None:
        count = int(match.group("count"))
        unit = match.group("unit")
        if unit == "d":
            return reference - timedelta(days=count)
        if unit == "h":
            return reference - timedelta(hours=count)
        if unit == "w":
            return reference - timedelta(weeks=count)
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "since must be a duration like 30d/12h/4w or an ISO datetime",
        ) from exc
    return _ensure_aware_utc(parsed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="30d", help="Duration like 30d/12h/4w or ISO datetime")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)
    since = parse_since(args.since)
    with SessionLocal() as session:
        report = build_report(session, since=since)
    if args.format == "markdown":
        print(render_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ADR-0003 Mode Telemetry Report",
        "",
        f"- Since: {report['since']}",
        f"- Telemetry runs: {report['telemetry_run_count']}",
        f"- Mode distribution: {json.dumps(report['mode_distribution'], sort_keys=True)}",
        f"- Median tokens: {json.dumps(report['median_tokens'], sort_keys=True)}",
        f"- Median latency ms: {json.dumps(report['median_latency_ms'], sort_keys=True)}",
        f"- Audit pass rate: {json.dumps(report['audit_pass_rate'], sort_keys=True)}",
        f"- Failure rate: {json.dumps(report['failure_rate'], sort_keys=True)}",
        f"- Failure distribution: {json.dumps(report['failure_distribution'], sort_keys=True)}",
    ]
    return "\n".join(lines) + "\n"


def _created_run_distribution(session: Session, *, since: datetime) -> dict[str, int]:
    rows = session.execute(
        select(Run.generation_mode, func.count())
        .where(Run.created_at >= since, Run.deleted_at.is_(None))
        .group_by(Run.generation_mode),
    ).all()
    counts = {mode: 0 for mode in MODES}
    for mode, count in rows:
        if mode in counts:
            counts[str(mode)] = int(count)
    return counts


def _mode_distribution(rows: Iterable[RunTelemetry]) -> dict[str, int]:
    counts = Counter(row.mode for row in rows)
    return {mode: counts.get(mode, 0) for mode in MODES}


def _selection_rate(distribution: dict[str, int], mode: str) -> float | None:
    total = sum(distribution.values())
    if total <= 0:
        return None
    return round(distribution.get(mode, 0) / total, 4)


def _median_value(values: Iterable[int | None]) -> float | int | None:
    cleaned = [value for value in values if value is not None]
    if not cleaned:
        return None
    return median(cleaned)


def _median_by_mode(
    rows: Iterable[RunTelemetry],
    getter: Callable[[RunTelemetry], int | None],
) -> dict[str, float | int | None]:
    grouped = _by_mode(rows)
    return {mode: _median_value(getter(row) for row in grouped[mode]) for mode in MODES}


def _audit_pass_rates(rows: Iterable[RunTelemetry]) -> dict[str, float | None]:
    grouped = _by_mode(rows)
    result: dict[str, float | None] = {}
    for mode in MODES:
        pass_count = sum(1 for row in grouped[mode] if row.audit_status == "pass")
        fail_count = sum(1 for row in grouped[mode] if row.audit_status == "fail")
        denominator = pass_count + fail_count
        result[mode] = None if denominator == 0 else round(pass_count / denominator, 4)
    return result


def _failure_rates(rows: Iterable[RunTelemetry]) -> dict[str, float | None]:
    grouped = _by_mode(rows)
    result: dict[str, float | None] = {}
    for mode in MODES:
        total = len(grouped[mode])
        failures = sum(1 for row in grouped[mode] if row.failure_code)
        result[mode] = None if total == 0 else round(failures / total, 4)
    return result


def _failure_distribution(rows: Iterable[RunTelemetry]) -> dict[str, dict[str, int]]:
    grouped = _by_mode(rows)
    result: dict[str, dict[str, int]] = {}
    for mode in MODES:
        counts = Counter(row.failure_code for row in grouped[mode] if row.failure_code)
        result[mode] = {str(key): value for key, value in sorted(counts.items())}
    return result


def _audit_status_distribution(rows: Iterable[RunTelemetry]) -> dict[str, dict[str, int]]:
    grouped = _by_mode(rows)
    result: dict[str, dict[str, int]] = {}
    for mode in MODES:
        counts = Counter(row.audit_status for row in grouped[mode])
        result[mode] = {str(key): value for key, value in sorted(counts.items())}
    return result


def _by_mode(rows: Iterable[RunTelemetry]) -> dict[str, list[RunTelemetry]]:
    grouped: dict[str, list[RunTelemetry]] = {mode: [] for mode in MODES}
    for row in rows:
        if row.mode in grouped:
            grouped[row.mode].append(row)
    return grouped


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
