"""Phase rerun stale marker (codex-AGREEd #2 stage 1).

Adds ``runs.stale_from_phase TEXT NULL``: when an upstream phase has
been rerun, this column names the **earliest** completed downstream
phase whose artifacts are now older than the new upstream output.
The UI keys off it to:

- show a "this section may be out of date" banner on downstream tabs
- enforce monotonic refresh order (the user must rerun the stale
  phase next, not skip ahead)

Cleared once the user finishes refreshing all downstream phases.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "010_phase_rerun"
down_revision: str | None = "009_authors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("stale_from_phase", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "stale_from_phase")
