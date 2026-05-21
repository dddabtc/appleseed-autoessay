"""Per-phase prompt override (codex-AGREEd #2 stage 2.B).

Stage 2.B introduces two tables:

- ``phase_prompt_drafts``: a single editable draft per
  ``(run_id, phase, prompt_key)``. The user types into a text area,
  hits "Save", and the row is upserted. ``prompt_key`` is fixed at
  ``"main"`` for 2.B but the schema is ready for multi-prompt phases
  (drafter has per-section calls, etc.) in 2.C/2.D.
- ``phase_version_prompts``: an immutable snapshot of the resolved
  prompt(s) used for a given ``phase_version``. Captured at begin
  time, never mutated. Lets the user later see "this version was
  produced with this exact prompt" via the phase history modal.

``phase_versions`` adds a ``prompt_hash`` column so future dedup
logic can treat (upstream, prompt) as the effective input identity.
A rerun with the same upstream artifacts but a different prompt is
NOT an identical-input rerun.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "012_phase_prompt_override"
down_revision: str | None = "011_phase_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "phase_prompt_drafts",
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("runs.id"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column(
            "prompt_key",
            sa.String(length=64),
            nullable=False,
            server_default="main",
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("run_id", "phase", "prompt_key", name="pk_phase_prompt_drafts"),
    )
    op.create_index(
        "ix_phase_prompt_drafts_run_phase",
        "phase_prompt_drafts",
        ["run_id", "phase"],
    )

    op.create_table(
        "phase_version_prompts",
        sa.Column(
            "phase_version_id",
            sa.String(length=64),
            sa.ForeignKey("phase_versions.id"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("prompt_key", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("template_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("phase_version_id", "prompt_key", name="pk_phase_version_prompts"),
        sa.CheckConstraint(
            "source IN ('default', 'override')", name="ck_phase_version_prompts_source"
        ),
    )
    op.create_index(
        "ix_phase_version_prompts_pv",
        "phase_version_prompts",
        ["phase_version_id"],
    )

    op.add_column(
        "phase_versions",
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("phase_versions", "prompt_hash")
    op.drop_index("ix_phase_version_prompts_pv", table_name="phase_version_prompts")
    op.drop_table("phase_version_prompts")
    op.drop_index("ix_phase_prompt_drafts_run_phase", table_name="phase_prompt_drafts")
    op.drop_table("phase_prompt_drafts")
