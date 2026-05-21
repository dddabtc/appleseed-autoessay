"""Add ``research_role`` column to ``source_records`` (PR-C1.a).

Tags every retrieved source with one of 4 tiers:

- ``primary_source``     — evidentiary item (archive, fieldwork
                            transcript, manuscript, statute,
                            contemporary witness)
- ``secondary_argument`` — published scholarship arguing a
                            position about the topic (DEFAULT)
- ``theoretical_lens``   — framework-level work used as a
                            conceptual lens (Bourdieu, Skinner,
                            social network theory…)
- ``methodological_reference`` — work cited only for a method

Backfill: every existing row gets ``secondary_argument`` since
that matches the dominant prior behaviour (the C0 pipeline did
not differentiate). PR-C1.a's classifier agent will overwrite
on subsequent runs (per-run-context classification — Bourdieu
might be theoretical_lens for one run and primary_source for
another).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "019_research_role"
down_revision: str | None = "018_research_kernel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source_records") as batch_op:
        batch_op.add_column(
            sa.Column(
                "research_role",
                sa.String(length=32),
                nullable=False,
                server_default="secondary_argument",
            ),
        )

    with op.batch_alter_table("source_records") as batch_op:
        batch_op.alter_column(
            "research_role",
            existing_type=sa.String(length=32),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("source_records") as batch_op:
        batch_op.drop_column("research_role")
