"""Soft-delete individual runs.

Adds ``runs.deleted_at`` so the run list can remove one run card without
soft-deleting the owning essay/project and every sibling run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "021_run_soft_delete"
down_revision: str | None = "020_native_password_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_runs_project_deleted_at", "runs", ["project_id", "deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_project_deleted_at", table_name="runs")
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("deleted_at")
