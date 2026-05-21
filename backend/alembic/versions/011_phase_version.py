"""Persistent phase version history (codex-AGREEd #2 stage 2.A).

Stage 2.A retains every successful phase invocation as an immutable
``phase_version`` row pointing to archived artifact blobs. The user
can switch back to any prior version. Activation copies that
version's blobs into the legacy paths the existing readers use, so
no agent refactor is needed in this PR.

Tables:
- ``phase_version``: one row per (run, phase, version_no). Linear
  history in 2.A — branches come in 2.C. Status transitions:
    running -> done -> superseded
    running -> failed | cancelled
  ``parent_pv_id`` chains versions; ``input_snapshot_hash`` lets
  future dedup logic skip identical-input reruns.
- ``artifact``: one row per blob produced by a phase_version. Holds
  the legacy ``logical_path`` (where readers look) and the immutable
  ``blob_path`` (under ``runs/<run>/phases/<pv_id>/...``).
- ``run_head``: per-(run, phase) pointer to the active version. Set
  on successful run; can be flipped manually via the activate API.

Stage 1's ``runs.stale_from_phase`` column stays as-is — it remains
the user-facing "what to refresh next" cue.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "011_phase_version"
down_revision: str | None = "010_phase_rerun"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "phase_versions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("runs.id"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "parent_pv_id",
            sa.String(length=64),
            sa.ForeignKey("phase_versions.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("artifacts_dir", sa.String(length=500), nullable=False),
        sa.Column("input_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.UniqueConstraint("run_id", "phase", "version_no", name="uq_phase_versions_run_phase_no"),
    )
    op.create_index(
        "ix_phase_versions_run_phase_status",
        "phase_versions",
        ["run_id", "phase", "status"],
    )

    op.create_table(
        "artifacts_v2",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "phase_version_id",
            sa.String(length=64),
            sa.ForeignKey("phase_versions.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=True),
        sa.Column("logical_path", sa.String(length=500), nullable=False),
        sa.Column("blob_path", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_artifacts_v2_pv", "artifacts_v2", ["phase_version_id"])
    op.create_index("ix_artifacts_v2_sha", "artifacts_v2", ["sha256"])

    op.create_table(
        "run_heads",
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("runs.id"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column(
            "version_id",
            sa.String(length=64),
            sa.ForeignKey("phase_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("run_id", "phase", name="pk_run_heads"),
    )


def downgrade() -> None:
    op.drop_table("run_heads")
    op.drop_index("ix_artifacts_v2_sha", table_name="artifacts_v2")
    op.drop_index("ix_artifacts_v2_pv", table_name="artifacts_v2")
    op.drop_table("artifacts_v2")
    op.drop_index("ix_phase_versions_run_phase_status", table_name="phase_versions")
    op.drop_table("phase_versions")
