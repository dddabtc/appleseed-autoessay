import json
from pathlib import Path

from conftest import seed_approved_scan
from sqlalchemy import select

from autoessay.agents import integrity as integrity_module
from autoessay.agents.integrity import run_integrity
from autoessay.clients.integrity import IntegrityClientError
from autoessay.config import get_settings
from autoessay.models import Run, RunEvent


def test_run_integrity_all_vendor_failure_enters_failed_vendor(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, _run_dir = seed_approved_scan(app_session, tmp_path, monkeypatch, "run_vendor_fail")
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "0")
    monkeypatch.setenv("ORIGINALITY_API_KEY", "test-key")
    monkeypatch.setenv("GPTZERO_API_KEY", "test-key")
    monkeypatch.setenv("COPYLEAKS_EMAIL", "test@example.com")
    monkeypatch.setenv("COPYLEAKS_API_KEY", "test-key")
    get_settings.cache_clear()

    async def failing_scan(_text: str, _kind: str):  # type: ignore[no-untyped-def]
        raise IntegrityClientError("vendor down")

    monkeypatch.setattr(integrity_module.originality, "scan", failing_scan)
    monkeypatch.setattr(integrity_module.gptzero, "scan", failing_scan)
    monkeypatch.setattr(integrity_module.copyleaks, "scan", failing_scan)

    with app_session() as session:
        summary = run_integrity(run_id, session)
        run = session.get(Run, run_id)
        event = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .where(RunEvent.event_type == "phase_failed")
            .order_by(RunEvent.created_at.desc())
            .limit(1),
        )

    assert run is not None
    assert run.state == "FAILED_VENDOR"
    assert summary["state"] == "FAILED_VENDOR"
    assert event is not None
    payload = json.loads(event.payload)
    assert "retry_later" in payload["resume_options"]
