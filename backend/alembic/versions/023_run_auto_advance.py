"""Add ``runs.auto_advance`` per-run execution flag.

PR-382 (2026-05-13): one-click full-auto mode. When ``true``, the
backend ``auto_advance`` coordinator advances every ``USER_*_REVIEW``
gate to the next phase automatically — same semantics as the bash
drive scripts the team has been re-writing for every canary. Codex
AGREE-WITH-AMENDMENTS 2026-05-13: this is an execution policy, not
part of the research kernel, so it lives in its own column (mirrors
``mathematical_mode`` from PR-366).

Default ``false`` keeps the existing manual-review behavior for any
user who actively wants to read each phase's output. ``FAILED_*``
states still require user intervention — coordinator pauses there.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "023_run_auto_advance"
down_revision: str | None = "022_run_mathematical_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_advance",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("auto_advance")
