"""Add persisted run generation mode.

ADR-0003 P1 introduces a run-level architecture selector:
``deep`` keeps the existing state machine, while ``express`` routes
to the independent express runner. Existing rows backfill to deep.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "024_run_generation_mode"
down_revision: str | None = "023_run_auto_advance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "generation_mode",
                sa.String(length=16),
                nullable=False,
                server_default="deep",
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("generation_mode")
