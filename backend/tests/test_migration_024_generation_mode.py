from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions" / "024_run_generation_mode.py"
    )
    spec = importlib.util.spec_from_file_location("migration_024", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(conn) -> dict[str, dict[str, object]]:  # type: ignore[no-untyped-def]
    rows = conn.execute(text("PRAGMA table_info(runs)")).mappings().all()
    return {str(row["name"]): dict(row) for row in rows}


def test_024_adds_generation_mode_with_deep_backfill_and_rolls_back() -> None:
    engine = create_engine("sqlite://", future=True)
    module = _load_migration_module()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE runs (id VARCHAR(64) PRIMARY KEY)"))
        conn.execute(text("INSERT INTO runs (id) VALUES ('run_a'), ('run_b')"))

        context = MigrationContext.configure(conn)
        module.op = Operations(context)
        module.upgrade()

        columns = _columns(conn)
        assert "generation_mode" in columns
        assert columns["generation_mode"]["notnull"] == 1
        assert columns["generation_mode"]["dflt_value"] == "'deep'"
        rows = conn.execute(
            text("SELECT id, generation_mode FROM runs ORDER BY id"),
        ).all()
        assert rows == [("run_a", "deep"), ("run_b", "deep")]

        module.downgrade()

        assert "generation_mode" not in _columns(conn)
        remaining = conn.execute(text("SELECT id FROM runs ORDER BY id")).all()
        assert remaining == [("run_a",), ("run_b",)]
