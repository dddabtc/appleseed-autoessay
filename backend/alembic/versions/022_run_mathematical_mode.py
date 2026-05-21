"""Add ``runs.mathematical_mode`` execution flag.

PR-366 (2026-05-13): expose round-0 stage B (gpt-5.5 holistic rewrite)
as a per-run UI checkbox ("数理增强模式"). Default ``false`` so the
production path stays at the cheap, ~14 min run; users opt in when
they want gpt-5.5 + LaTeX/表/【待填】 scaffolding (+20-30 min,
~10x token cost). Codex AGREE-WITH-AMENDMENTS 2026-05-12 PR-366 said
this is an execution policy, not part of the research kernel, so it
lives in its own column rather than being smuggled into
``research_kernel_json``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "022_run_mathematical_mode"
down_revision: str | None = "021_run_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "mathematical_mode",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("mathematical_mode")
