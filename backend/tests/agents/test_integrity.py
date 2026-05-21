import json
from pathlib import Path

from conftest import seed_approved_scan
from sqlalchemy import select

from autoessay.agents.integrity import run_integrity
from autoessay.config import get_settings
from autoessay.models import Run, RunEvent


def test_run_integrity_stub_writes_reports_summary_and_raw_vendor_payloads(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = seed_approved_scan(app_session, tmp_path, monkeypatch, "run_integrity_ok")
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "1")
    get_settings.cache_clear()

    with app_session() as session:
        summary = run_integrity(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    integrity_dir = run_dir / "integrity"
    summary_json = json.loads(
        (integrity_dir / "integrity_summary.json").read_text(encoding="utf-8")
    )

    assert run is not None
    assert run.state == "USER_INTEGRITY_REVIEW"
    assert summary["state"] == "USER_INTEGRITY_REVIEW"
    assert (integrity_dir / "plagiarism_report.md").exists()
    assert (integrity_dir / "ai_style_report.md").exists()
    assert summary_json["scans"]["plagiarism"]["vendor"] == "originality_ai"
    assert summary_json["scans"]["ai_style"]["span_count"] >= 1
    assert list((integrity_dir / "vendor_raw").glob("*.json"))
    assert events[-1].event_type == "phase_done"
