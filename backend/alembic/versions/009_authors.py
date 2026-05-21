"""Author roster + per-project author assignment.

Adds:

- ``authors`` table — one row per author the user has on file. The
  ``is_self`` flag marks the auto-bootstrapped author for the user
  themselves (lazily created on first GET /api/authors). Soft-delete
  via ``deleted_at`` so removing an author preserves ``project_author``
  rows on existing manuscripts.
- ``project_author`` join table — orders authors per project. The
  column is ``position`` (not ``order``) because ``order`` is a SQL
  reserved word and would force quoting in every query.

A composite uniqueness on ``(project_id, position)`` plus the
``(project_id, author_id)`` primary key prevents duplicate or
overlapping positions inside a single project. ``CHECK(position >= 0)``
is enforced at DB level — the API also enforces 0..N-1 contiguity but
we want the DB to refuse negative positions even if the API is wrong.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "009_authors"
down_revision: str | None = "008_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "authors",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("affiliation", sa.String(length=500), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("orcid", sa.String(length=32), nullable=True),
        sa.Column(
            "is_self",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_authors_user_deleted_at", "authors", ["user_id", "deleted_at"])

    op.create_table(
        "project_authors",
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            sa.String(length=64),
            sa.ForeignKey("authors.id"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "author_id"),
        sa.UniqueConstraint("project_id", "position", name="uq_project_authors_position"),
        sa.CheckConstraint("position >= 0", name="ck_project_authors_position_nonneg"),
    )
    op.create_index(
        "ix_project_authors_position",
        "project_authors",
        ["project_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_authors_position", table_name="project_authors")
    op.drop_table("project_authors")
    op.drop_index("ix_authors_user_deleted_at", table_name="authors")
    op.drop_table("authors")
