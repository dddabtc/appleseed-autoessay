from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "003_oidc_auth"
down_revision: str | None = "002_runs_sse_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("oidc_subject", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("oidc_issuer", sa.String(length=500), nullable=True))
    op.add_column("users", sa.Column("picture_url", sa.String(length=1000), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_email", "users", ["email"], unique=False)
    op.create_index("uq_users_oidc_subject", "users", ["oidc_subject"], unique=True)
    op.create_table(
        "auth_sessions",
        sa.Column("session_id", sa.String(length=128), primary_key=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("csrf_token", sa.String(length=128), nullable=False),
    )
    op.create_index(
        "ix_auth_sessions_expires_at",
        "auth_sessions",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("uq_users_oidc_subject", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "picture_url")
    op.drop_column("users", "oidc_issuer")
    op.drop_column("users", "oidc_subject")
