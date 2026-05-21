from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "006_proposal_stage"
down_revision: str | None = "005_novelty_discussions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("proposal_content_path", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column(
            "proposal_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "proposal_version")
    op.drop_column("runs", "proposal_content_path")
