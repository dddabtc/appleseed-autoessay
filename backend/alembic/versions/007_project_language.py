from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "007_project_language"
down_revision: str | None = "006_proposal_stage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "language",
            sa.String(length=8),
            nullable=False,
            server_default="en",
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "language")
