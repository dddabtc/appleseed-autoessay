import os
from pathlib import Path

import pytest
from conftest import seed_integrity_ready_run
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from autoessay.agents.integrity import run_integrity
from autoessay.clients.integrity import NormalizedScanResult
from autoessay.config import get_settings
from autoessay.models import ProviderCall

pytestmark = pytest.mark.skipif(
    os.getenv("AUTOESSAY_LIVE_INTEGRITY") != "1",
    reason="live Integrity invariant test is opt-in via AUTOESSAY_LIVE_INTEGRITY=1",
)


@pytest.mark.live
def test_live_integrity_legacy_and_harness_paths_satisfy_invariants(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _legacy_id, legacy_run_dir = seed_integrity_ready_run(
        app_session,
        tmp_path,
        "run_live_integrity_legacy",
    )
    _harness_id, harness_run_dir = seed_integrity_ready_run(
        app_session,
        tmp_path,
        "run_live_integrity_harness",
    )
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "0")

    with app_session() as session:
        get_settings.cache_clear()
        legacy_summary = run_integrity("run_live_integrity_legacy", session)
        get_settings.cache_clear()
        harness_summary = run_integrity("run_live_integrity_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_live_integrity_harness"),
            ),
        )

    assert legacy_summary["state"] == harness_summary["state"] == "USER_INTEGRITY_REVIEW"
    assert (legacy_run_dir / "integrity" / "integrity_summary.json").is_file()
    assert (harness_run_dir / "integrity" / "integrity_summary.json").is_file()
    assert any(call.status == "accepted" for call in provider_calls)
    for response_path in (harness_run_dir / "integrity" / "tool_responses").glob("*.json"):
        if ".attempt" not in response_path.name:
            NormalizedScanResult.parse_raw(response_path.read_text(encoding="utf-8"))
