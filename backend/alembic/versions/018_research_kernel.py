"""Research-kernel intake gate columns on runs (PR-C0 foundation).

Adds two columns to the ``runs`` table:

- ``paper_mode``: validated string (NOT a DB enum, so adding modes
  in later C-series PRs doesn't require schema migration). Default
  ``'case_analysis'`` — the only mode marked ``available`` at PR-C0
  ship time. Existing rows backfill with ``'empirical'`` because
  the project so far has been an "empirical-by-default" pipeline,
  but the registry will mark that as ``developer_preview`` until
  PR-C1 lands the evidence ledger.
- ``research_kernel_json``: JSON blob carrying the user's intake
  data (observed_puzzle, tentative_question, scope, method
  preference, theory preference, primary_materials_status,
  confidence_permissions). Single blob rather than denormalized
  columns because the kernel will become mode-specific across
  C1-C5; columns would create false stability. Schema-versioned
  from inside via ``kernel_schema_version: 1``. Existing rows
  backfill with a ``{"legacy_backfill": true,
  "kernel_schema_version": 1}`` placeholder per codex round-5
  amendment 5.

No artifact-file writes during this migration (codex round-5 amendment
5): kernel snapshot files (`proposal/research_kernel_v{NNN}.json`)
will be written lazily on next proposal save / explicit
post-deploy maintenance, NOT inside alembic.

No stale marker is set on existing runs — backfill is bookkeeping
only, not a real kernel change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "018_research_kernel"
down_revision: str | None = "017_backfill_pv_first_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEGACY_KERNEL_BACKFILL = json.dumps(
    {
        "kernel_schema_version": 1,
        "legacy_backfill": True,
    },
)


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "paper_mode",
                sa.String(length=64),
                nullable=False,
                server_default="empirical",
            ),
        )
        batch_op.add_column(
            sa.Column(
                "research_kernel_json",
                sa.JSON(),
                nullable=False,
                server_default=_LEGACY_KERNEL_BACKFILL,
            ),
        )

    # Drop server_defaults after the backfill so future inserts must
    # explicitly state mode + kernel (the model carries the
    # Python-level defaults).
    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column(
            "paper_mode",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "research_kernel_json",
            existing_type=sa.JSON(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("research_kernel_json")
        batch_op.drop_column("paper_mode")
