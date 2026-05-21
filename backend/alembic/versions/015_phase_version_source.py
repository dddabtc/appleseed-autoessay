"""Add ``source`` column to phase_versions (PR-A1, codex AGREE-with-amendments).

Codex amendment 5/6 to issue 1 of the 2026-05-01 design review:

> Do not store literal ``created_by="user_edit"`` if ``created_by``
> currently means actor/user id. Prefer ``created_by=user.id`` plus
> ``source="user_edit"``/event payload, or add a separate origin
> field.
> Expose edit origin in ``PhaseVersionsResponse``; otherwise the
> frontend cannot reliably label user-edit versions.

This migration adds the separate origin field. Values ``'agent'`` for
versions produced by an agent run (the default for every existing
row), ``'user_edit'`` for versions written by the upcoming
``PUT /api/runs/{id}/<phase>`` user-edit endpoints (PR-A2).
``created_by`` keeps its existing semantics — actor identity (user
id or NULL for system-generated rows).

Backfill all existing rows with ``'agent'`` because every
phase_version persisted before this migration was created by an
agent run.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "015_phase_version_source"
down_revision: str | None = "014_phase_lock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("phase_versions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.String(length=32),
                nullable=False,
                server_default="agent",
            ),
        )
    # Drop the server_default after the backfill so future inserts
    # must explicitly state their origin (the model carries the
    # python-level default).
    with op.batch_alter_table("phase_versions") as batch_op:
        batch_op.alter_column("source", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("phase_versions") as batch_op:
        batch_op.drop_column("source")
