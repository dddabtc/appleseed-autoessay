import json
from pathlib import Path

import pytest
from conftest import seed_integrity_ready_run
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from autoessay.agents import integrity as integrity_module
from autoessay.agents.integrity import run_integrity
from autoessay.clients.integrity import IntegrityClientError, NormalizedScanResult
from autoessay.config import get_settings
from autoessay.models import ProviderCall, Run, RunEvent


def test_integrity_harness_all_vendor_failure_enters_failed_vendor(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _run_dir = seed_integrity_ready_run(app_session, tmp_path, "run_integrity_vendor_fail")
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "0")
    monkeypatch.setenv("ORIGINALITY_API_KEY", "test-key")
    monkeypatch.setenv("GPTZERO_API_KEY", "test-key")
    monkeypatch.setenv("COPYLEAKS_EMAIL", "test@example.com")
    monkeypatch.setenv("COPYLEAKS_API_KEY", "test-key")
    get_settings.cache_clear()

    async def failing_scan(_text: str, _kind: str) -> NormalizedScanResult:
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
        provider_calls = list(
            session.scalars(select(ProviderCall).where(ProviderCall.run_id == run_id)),
        )

    assert run is not None
    assert run.state == "FAILED_VENDOR"
    assert summary["state"] == "FAILED_VENDOR"
    assert summary["resume_options"] == ["retry_later", "skip_with_note"]
    assert event is not None
    payload = json.loads(event.payload)
    assert payload["resume_options"] == ["retry_later", "skip_with_note"]
    assert {call.provider for call in provider_calls} == {"originality", "gptzero", "copyleaks"}
    assert all(call.call_type == "tool" for call in provider_calls)
    assert all(call.status == "failed_vendor" for call in provider_calls)
