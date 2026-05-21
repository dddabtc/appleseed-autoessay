import json
from datetime import datetime, tzinfo
from pathlib import Path

import pytest
from conftest import seed_integrity_ready_run
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from autoessay.agents import integrity as integrity_module
from autoessay.agents.integrity import run_integrity
from autoessay.config import get_settings
from autoessay.models import ProviderCall


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> "FixedDateTime":
        return cls(2026, 1, 2, 3, 4, 5, tzinfo=tz)


def test_integrity_harness_stub_writes_audited_tool_artifacts(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _harness_id, harness_run_dir = seed_integrity_ready_run(
        app_session,
        tmp_path,
        "run_integrity_harness",
    )
    monkeypatch.setattr(integrity_module, "datetime", FixedDateTime)
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "1")

    with app_session() as session:
        get_settings.cache_clear()
        harness_summary = run_integrity("run_integrity_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_integrity_harness"),
            ),
        )

    assert harness_summary["state"] == "USER_INTEGRITY_REVIEW"
    assert (harness_run_dir / "integrity" / "plagiarism_report.md").is_file()
    assert (harness_run_dir / "integrity" / "ai_style_report.md").is_file()
    assert (harness_run_dir / "integrity" / "integrity_summary.json").is_file()
    assert len(provider_calls) == 2
    assert all(call.provider == "originality" for call in provider_calls)
    assert all(call.call_type == "tool" for call in provider_calls)
    assert all(call.status == "accepted" for call in provider_calls)
    tool_calls = _read_jsonl(harness_run_dir / "integrity" / "tool_calls.jsonl")
    assert len(tool_calls) == 2


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
