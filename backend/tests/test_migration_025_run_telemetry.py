from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _load_migration_module():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "025_run_telemetry.py"
    spec = importlib.util.spec_from_file_location("migration_025", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(conn) -> dict[str, dict[str, object]]:  # type: ignore[no-untyped-def]
    rows = conn.execute(text("PRAGMA table_info(run_telemetry)")).mappings().all()
    return {str(row["name"]): dict(row) for row in rows}


def _indexes(conn) -> set[str]:  # type: ignore[no-untyped-def]
    return {
        str(row["name"])
        for row in conn.execute(text("PRAGMA index_list(run_telemetry)")).mappings()
    }


def test_025_adds_run_telemetry_table_and_rolls_back() -> None:
    engine = create_engine("sqlite://", future=True)
    module = _load_migration_module()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id VARCHAR(64) PRIMARY KEY)"))
        conn.execute(text("INSERT INTO runs (id) VALUES ('run_a')"))

        context = MigrationContext.configure(conn)
        module.op = Operations(context)
        module.upgrade()

        columns = _columns(conn)
        assert set(columns) == {
            "run_id",
            "mode",
            "total_tokens",
            "latency_ms",
            "audit_status",
            "manuscript_chars",
            "created_at",
            "finished_at",
            "failure_code",
        }
        assert columns["run_id"]["pk"] == 1
        assert columns["audit_status"]["notnull"] == 1
        assert columns["audit_status"]["dflt_value"] == "'unknown'"
        assert {
            "ix_run_telemetry_mode_created_at",
            "ix_run_telemetry_finished_at",
            "ix_run_telemetry_failure_code",
        }.issubset(_indexes(conn))

        conn.execute(
            text(
                "INSERT INTO run_telemetry "
                "(run_id, mode, total_tokens, latency_ms, audit_status, manuscript_chars, "
                "created_at, finished_at, failure_code) "
                "VALUES ('run_a', 'express', 30000, 120000, 'pass', 42000, "
                "'2026-05-01T00:00:00Z', '2026-05-01T00:02:00Z', NULL)"
            ),
        )
        rows = conn.execute(text("SELECT run_id, mode, total_tokens FROM run_telemetry")).all()
        assert rows == [("run_a", "express", 30000)]

        module.downgrade()

        table_names = {
            str(row[0])
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"),
            )
        }
        assert "run_telemetry" not in table_names
        assert "runs" in table_names
