from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "002_runs_sse_state"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("domain_version", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_run_events_run_id_created_at",
        "run_events",
        ["run_id", "created_at"],
    )
    op.add_column(
        "checkpoints",
        sa.Column("decision_payload", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("checkpoints", "decision_payload")
    op.drop_index("ix_run_events_run_id_created_at", table_name="run_events")
    op.drop_table("run_events")
    op.drop_column("runs", "domain_version")
