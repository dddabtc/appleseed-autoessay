from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from conftest import seed_project
from sqlalchemy.orm import Session

from autoessay.models import Run, RunTelemetry, utcnow
from autoessay.scripts.mode_telemetry_report import build_report, parse_since


def _seed_run(
    session: Session,
    tmp_path: Path,
    *,
    run_id: str,
    mode: str,
) -> Run:
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    run = Run(
        id=run_id,
        project_id="proj_test",
        domain_version="0.1.0",
        run_dir=str(run_dir),
        state="EXPORTS_DONE" if mode == "deep" else "EXPRESS_DONE",
        baseline_hash="test",
        generation_mode=mode,
        created_at=utcnow() - timedelta(minutes=10),
    )
    session.add(run)
    session.flush()
    return run


def test_parse_since_accepts_durations() -> None:
    now = utcnow()
    assert parse_since("30d", now=now) == now - timedelta(days=30)
    assert parse_since("12h", now=now) == now - timedelta(hours=12)
    assert parse_since("4w", now=now) == now - timedelta(weeks=4)


def test_build_report_summarizes_modes(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        seed_project(session)
        now = utcnow()
        express_ok = _seed_run(session, tmp_path, run_id="run_report_express_ok", mode="express")
        deep_ok = _seed_run(session, tmp_path, run_id="run_report_deep_ok", mode="deep")
        express_fail = _seed_run(
            session,
            tmp_path,
            run_id="run_report_express_fail",
            mode="express",
        )
        session.add_all(
            [
                RunTelemetry(
                    run_id=express_ok.id,
                    mode="express",
                    total_tokens=30000,
                    latency_ms=120000,
                    audit_status="pass",
                    manuscript_chars=42000,
                    created_at=now - timedelta(minutes=10),
                    finished_at=now - timedelta(minutes=8),
                ),
                RunTelemetry(
                    run_id=deep_ok.id,
                    mode="deep",
                    total_tokens=90000,
                    latency_ms=900000,
                    audit_status="pass",
                    manuscript_chars=44000,
                    created_at=now - timedelta(minutes=20),
                    finished_at=now - timedelta(minutes=5),
                ),
                RunTelemetry(
                    run_id=express_fail.id,
                    mode="express",
                    total_tokens=31000,
                    latency_ms=100000,
                    audit_status="fail",
                    manuscript_chars=None,
                    created_at=now - timedelta(minutes=15),
                    finished_at=now - timedelta(minutes=14),
                    failure_code="express_timeout",
                ),
            ],
        )
        session.commit()

        report = build_report(session, since=now - timedelta(days=30))

        assert report["telemetry_run_count"] == 3
        assert report["mode_distribution"] == {"express": 2, "deep": 1}
        assert report["created_run_mode_distribution"] == {"express": 2, "deep": 1}
        assert report["median_tokens"] == {"express": 30500.0, "deep": 90000}
        assert report["median_latency_ms"] == {"express": 110000.0, "deep": 900000}
        assert report["audit_pass_rate"] == {"express": 0.5, "deep": 1.0}
        assert report["failure_rate"] == {"express": 0.5, "deep": 0.0}
        assert report["failure_distribution"]["express"] == {"express_timeout": 1}
