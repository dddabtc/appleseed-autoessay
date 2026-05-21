from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004_corpus_library"
down_revision: str | None = "003_oidc_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("corpora") as batch_op:
        batch_op.add_column(
            sa.Column(
                "user_id",
                sa.String(length=64),
                sa.ForeignKey("users.id", name="fk_corpora_user_id_users"),
                nullable=True,
            ),
        )
    op.create_index("ix_corpora_user_id", "corpora", ["user_id"], unique=False)
    op.add_column(
        "corpus_documents",
        sa.Column("document_hash", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "corpus_documents",
        sa.Column("original_size_bytes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "corpus_documents",
        sa.Column("extracted_text_path", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "corpus_documents",
        sa.Column("style_profile_path", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "corpus_documents",
        sa.Column(
            "privacy_level",
            sa.String(length=64),
            nullable=False,
            server_default="private",
        ),
    )
    op.add_column(
        "corpus_documents",
        sa.Column(
            "ingest_status",
            sa.String(length=64),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_index(
        "uq_corpus_documents_corpus_hash",
        "corpus_documents",
        ["corpus_id", "document_hash"],
        unique=True,
    )
    op.create_index(
        "ix_corpus_documents_ingest_status",
        "corpus_documents",
        ["ingest_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_corpus_documents_ingest_status", table_name="corpus_documents")
    op.drop_index("uq_corpus_documents_corpus_hash", table_name="corpus_documents")
    op.drop_column("corpus_documents", "ingest_status")
    op.drop_column("corpus_documents", "privacy_level")
    op.drop_column("corpus_documents", "style_profile_path")
    op.drop_column("corpus_documents", "extracted_text_path")
    op.drop_column("corpus_documents", "original_size_bytes")
    op.drop_column("corpus_documents", "document_hash")
    op.drop_index("ix_corpora_user_id", table_name="corpora")
    with op.batch_alter_table("corpora") as batch_op:
        batch_op.drop_column("user_id")
