from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "005_novelty_discussions"
down_revision: str | None = "004_corpus_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "novelty_discussions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("generation_token", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_novelty_discussions_run_created_at",
        "novelty_discussions",
        ["run_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_novelty_discussions_run_created_at",
        table_name="novelty_discussions",
    )
    op.drop_table("novelty_discussions")
