"""Add per-run generation-mode telemetry.

ADR-0003 P3 records production-mode evidence in a dedicated table.
The Alembic environment wraps upgrade and downgrade in one transaction;
the downgrade below is the rollback SQL for removing the framework.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "025_run_telemetry"
down_revision: str | None = "024_run_generation_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_telemetry",
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "audit_status",
            sa.String(length=64),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("manuscript_chars", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_code", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint("mode IN ('express', 'deep')", name="ck_run_telemetry_mode"),
        sa.CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_run_telemetry_total_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_run_telemetry_latency_ms_nonnegative",
        ),
        sa.CheckConstraint(
            "manuscript_chars IS NULL OR manuscript_chars >= 0",
            name="ck_run_telemetry_manuscript_chars_nonnegative",
        ),
    )
    op.create_index(
        "ix_run_telemetry_mode_created_at",
        "run_telemetry",
        ["mode", "created_at"],
    )
    op.create_index("ix_run_telemetry_finished_at", "run_telemetry", ["finished_at"])
    op.create_index("ix_run_telemetry_failure_code", "run_telemetry", ["failure_code"])


def downgrade() -> None:
    op.drop_index("ix_run_telemetry_failure_code", table_name="run_telemetry")
    op.drop_index("ix_run_telemetry_finished_at", table_name="run_telemetry")
    op.drop_index("ix_run_telemetry_mode_created_at", table_name="run_telemetry")
    op.drop_table("run_telemetry")
