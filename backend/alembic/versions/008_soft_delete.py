"""Soft-delete projects + cancellation intent on runs.

Adds:
- ``projects.deleted_at TIMESTAMPTZ NULL`` — soft-delete marker.
- ``runs.cancel_requested_at TIMESTAMPTZ NULL`` — cancellation intent
  set when an essay is soft-deleted; workers check this before
  writing further artifacts.
- ``ix_projects_user_deleted_at`` composite index on
  ``(user_id, deleted_at)`` — speeds up the hot-path "list active
  essays for user X" query. Single index name across dialects so
  alembic check stays clean. Postgres could use a partial
  ``WHERE deleted_at IS NULL`` for marginal extra speed; we keep the
  composite since our scale (< 1000 essays per user) doesn't justify
  the dialect-aware diffing.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "008_soft_delete"
down_revision: str | None = "007_project_language"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_projects_user_deleted_at",
        "projects",
        ["user_id", "deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_projects_user_deleted_at", table_name="projects")
    op.drop_column("runs", "cancel_requested_at")
    op.drop_column("projects", "deleted_at")
