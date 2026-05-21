"""Per-project corpus + selection model (PR-B1, codex AGREE-with-amendments).

Codex amendments to issue 2 of the 2026-05-01 design review:

> 1. "Can select from global" needs an explicit selection model,
>    not only automatic global+project union. Add project-level
>    inclusion/selection. Preserve current behavior by defaulting
>    existing projects to include the global corpus.
> 2. Scope query should always enforce owner.

Schema:

- Add ``corpora.project_id`` (nullable). NULL = global to its
  owner_user (current semantics); set = exclusively this project's
  corpus (uploaded under the workspace's Corpus sub-tab).
- New ``project_corpus_selections`` join table (project_id,
  corpus_id) records which global corpora a specific project
  explicitly includes. Project-scoped corpora are *always*
  included for their owning project — they don't need a row here.

Backfill: insert (project, corpus) rows for every existing
project + every enabled global corpus that the project's owner
also owns. This preserves today's behavior, where every project
of a user implicitly draws from the user's global corpus, while
making the inclusion explicit going forward.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "016_project_corpus"
down_revision: str | None = "015_phase_version_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("corpora") as batch_op:
        batch_op.add_column(
            sa.Column("project_id", sa.String(length=64), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_corpora_project_id",
            "projects",
            ["project_id"],
            ["id"],
        )
        batch_op.create_index("ix_corpora_project_id", ["project_id"])

    op.create_table(
        "project_corpus_selections",
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("corpus_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("project_id", "corpus_id"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_pcs_project_id",
        ),
        sa.ForeignKeyConstraint(
            ["corpus_id"],
            ["corpora.id"],
            name="fk_pcs_corpus_id",
        ),
    )

    # Backfill: every active project of a user gets a selection row
    # for every enabled GLOBAL corpus owned by the same user. Use a
    # raw SQL INSERT … SELECT so we don't depend on the model layer
    # at migration time.
    op.execute(
        sa.text(
            """
            INSERT INTO project_corpus_selections (project_id, corpus_id)
            SELECT p.id, c.id
              FROM projects p
              JOIN corpora c ON c.owner_user_id = p.user_id
             WHERE p.deleted_at IS NULL
               AND c.enabled = 1
               AND c.project_id IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM project_corpus_selections s
                    WHERE s.project_id = p.id AND s.corpus_id = c.id
               )
            """,
        ),
    )


def downgrade() -> None:
    op.drop_table("project_corpus_selections")
    with op.batch_alter_table("corpora") as batch_op:
        batch_op.drop_index("ix_corpora_project_id")
        batch_op.drop_constraint("fk_corpora_project_id", type_="foreignkey")
        batch_op.drop_column("project_id")
