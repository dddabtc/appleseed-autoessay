"""Native username/password auth schema.

Adds ``username`` + ``password_hash`` columns to ``users``. This
migration intentionally does not create a default account. Deployments
must create the first administrator through explicit setup
configuration or an out-of-band management process.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "020_native_password_auth"
down_revision: str | None = "019_research_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("username", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("password_hash", sa.String(length=128), nullable=True))
    op.create_index("uq_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_username", table_name="users")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("password_hash")
        batch_op.drop_column("username")
