"""Atomic phase-start claim columns on runs (Stage 3.E follow-up P0).

Codex AGREE-with-amendments (system-wide audit P0):
> Many start_* endpoints only check state then enqueue. Two clicks
> (multi-tab or curl bypass) can both pass the state check and
> enqueue parallel agent runs into the same run_dir. Need a
> single-transaction phase-start claim with owner token and
> timestamp so reruns/branch-switches/double-clicks can't race.

We add a run-level lock (per codex: pipeline is sequential, no
legitimate same-run concurrent phases). Three columns:

- ``active_phase_lock``: phase name currently held, or NULL
- ``active_phase_lock_job_id``: owner token; release must match
- ``active_phase_lock_claimed_at``: timestamp for ops visibility
  (a janitor / manual-clear endpoint can detect zombie locks)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "014_phase_lock"
down_revision: str | None = "013_phase_branches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column("active_phase_lock", sa.String(length=64), nullable=True),
        )
        batch_op.add_column(
            sa.Column("active_phase_lock_job_id", sa.String(length=64), nullable=True),
        )
        batch_op.add_column(
            sa.Column("active_phase_lock_claimed_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("active_phase_lock_claimed_at")
        batch_op.drop_column("active_phase_lock_job_id")
        batch_op.drop_column("active_phase_lock")
