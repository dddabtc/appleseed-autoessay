from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from conftest import seed_project
from sqlalchemy.orm import Session

from autoessay.models import ProviderCall, Run, RunTelemetry, utcnow
from autoessay.state_machine import transition
from autoessay.telemetry import record_run_telemetry


def _seed_run(
    session: Session,
    tmp_path: Path,
    *,
    run_id: str,
    mode: str,
    state: str,
) -> Run:
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    run = Run(
        id=run_id,
        project_id="proj_test",
        domain_version="0.1.0",
        run_dir=str(run_dir),
        state=state,
        baseline_hash="test",
        generation_mode=mode,
        created_at=utcnow() - timedelta(minutes=5),
    )
    session.add(run)
    session.flush()
    return run


def test_record_run_telemetry_reads_express_artifacts(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        seed_project(session)
        run = _seed_run(
            session,
            tmp_path,
            run_id="run_tel_express",
            mode="express",
            state="EXPRESS_DONE",
        )
        express_dir = Path(run.run_dir) / "express"
        express_dir.mkdir(parents=True)
        (express_dir / "provenance.json").write_text(
            json.dumps({"token_usage": {"total_tokens": 5500}}),
            encoding="utf-8",
        )
        (express_dir / "audit_critic.json").write_text(
            json.dumps({"status": "completed", "issues": []}),
            encoding="utf-8",
        )
        manuscript = "Abstract\n\nBody\n\nReferences\n"
        (express_dir / "ars_manuscript_raw.md").write_text(manuscript, encoding="utf-8")

        record_run_telemetry(session, run)
        session.commit()

        row = session.get(RunTelemetry, run.id)
        assert row is not None
        assert row.mode == "express"
        assert row.total_tokens == 5500
        assert row.audit_status == "pass"
        assert row.manuscript_chars == len(manuscript)
        assert row.failure_code is None


def test_deep_export_transition_records_telemetry(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        seed_project(session)
        run = _seed_run(
            session,
            tmp_path,
            run_id="run_tel_deep_done",
            mode="deep",
            state="EXPORTS_RUNNING",
        )
        exports_dir = Path(run.run_dir) / "exports"
        exports_dir.mkdir()
        manuscript = "final manuscript"
        (exports_dir / "manuscript.md").write_text(manuscript, encoding="utf-8")
        session.add(
            ProviderCall(
                id="pc_deep_a",
                run_id=run.id,
                provider="fake",
                call_type="llm",
                status="accepted",
                units=100,
            ),
        )
        session.add(
            ProviderCall(
                id="pc_deep_b",
                run_id=run.id,
                provider="fake",
                call_type="llm",
                status="accepted",
                units=101,
            ),
        )

        transition(run, "EXPORTS_DONE", session, reason="Exports completed")
        session.commit()

        row = session.get(RunTelemetry, run.id)
        assert row is not None
        assert row.mode == "deep"
        assert row.total_tokens == 201
        assert row.audit_status == "pass"
        assert row.manuscript_chars == len(manuscript)


def test_deep_failure_transition_records_failure_code(app_session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with app_session() as session:
        seed_project(session)
        run = _seed_run(
            session,
            tmp_path,
            run_id="run_tel_deep_failed",
            mode="deep",
            state="EXPORTS_RUNNING",
        )

        transition(
            run,
            "FAILED_POLICY",
            session,
            reason="Exporter citation gate blocked",
            payload={"failure_class": "failed_policy"},
        )
        session.commit()

        row = session.get(RunTelemetry, run.id)
        assert row is not None
        assert row.audit_status == "fail"
        assert row.failure_code == "failed_policy"
