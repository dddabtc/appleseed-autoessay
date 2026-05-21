"""Phase branches / forks (codex-AGREEd #2 stage 2.C).

Stage 2.C makes branches a first-class concept. A run can have many
named branches; each branch has its own ``run_heads`` per phase, its
own prompt-override drafts, and its own ``stale_from_phase`` marker.
Forking from any past phase_version creates a new branch that
inherits its parent's heads for upstream phases (still pointing at
the parent's pv ids) and starts empty for the forked phase + every
downstream.

Non-negotiables codex enforced (round-1 review):

1. ``run_heads`` and ``phase_prompt_drafts`` keyed by branch.
2. New ``phase_version_inputs`` table records the EXACT upstream pv
   ids each phase_version was produced from. Without this, a
   downstream pv on branch B could silently link back to branch A's
   upstream via the run-level ``run_head``. Fixes the
   cross-branch-leak that the input_snapshot_hash alone cannot.
3. ``stale_from_phase`` is per-branch, not per-run. Two branches
   can have independent stale states.
4. Branch ``deleted_at`` for soft delete. Partial unique index on
   ``(run_id, name) WHERE deleted_at IS NULL`` so a deleted name can
   be reused.
5. ``phase_versions`` records ``created_on_branch_id`` so the version
   history UI can scope its listing to one branch.

Scope shrink: ``Run.state`` (the FastAPI state-machine state) stays
on ``runs`` for now. This means at most one branch can be running at
a time; concurrent cross-branch reruns are out of scope for 2.C.
``Run.stale_from_phase`` is dropped — every run gets a "main" branch
and its stale_from_phase migrates to ``branches.stale_from_phase``
on that branch.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "013_phase_branches"
down_revision: str | None = "012_phase_prompt_override"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "branches",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("runs.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "parent_branch_id",
            sa.String(length=64),
            sa.ForeignKey("branches.id"),
            nullable=True,
        ),
        sa.Column(
            "forked_from_pv_id",
            sa.String(length=64),
            sa.ForeignKey("phase_versions.id"),
            nullable=True,
        ),
        sa.Column("forked_phase", sa.String(length=64), nullable=True),
        sa.Column("stale_from_phase", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial unique index: a soft-deleted branch's name can be reused.
    op.create_index(
        "ix_branches_run_name_active",
        "branches",
        ["run_id", "name"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_branches_run", "branches", ["run_id"])

    # Backfill: one "main" branch per existing run; that branch
    # inherits the run's existing stale_from_phase.
    op.execute(
        sa.text(
            """
            INSERT INTO branches (id, run_id, name, parent_branch_id,
                                  forked_from_pv_id, forked_phase,
                                  stale_from_phase, created_by, created_at,
                                  deleted_at)
            SELECT 'br_main_' || id, id, 'main', NULL, NULL, NULL,
                   stale_from_phase, NULL, COALESCE(created_at, CURRENT_TIMESTAMP),
                   NULL
            FROM runs
            """
        )
    )

    # runs gains active_branch_id, drops stale_from_phase. We leave
    # the column nullable so existing rows can be backfilled before
    # NOT NULL is enforced — but since we just backfilled the main
    # branches, set it now too.
    with op.batch_alter_table("runs") as batch:
        batch.add_column(
            sa.Column(
                "active_branch_id",
                sa.String(length=64),
                sa.ForeignKey("branches.id", name="fk_runs_active_branch"),
                nullable=True,
            )
        )
    op.execute(
        sa.text(
            """
            UPDATE runs
            SET active_branch_id = 'br_main_' || id
            """
        )
    )
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("stale_from_phase")

    # run_heads: change PK from (run_id, phase) to (run_id, branch_id, phase).
    with op.batch_alter_table("run_heads") as batch:
        batch.add_column(
            sa.Column(
                "branch_id",
                sa.String(length=64),
                sa.ForeignKey("branches.id", name="fk_run_heads_branch"),
                nullable=True,
            )
        )
    op.execute(
        sa.text(
            """
            UPDATE run_heads
            SET branch_id = 'br_main_' || run_id
            """
        )
    )
    with op.batch_alter_table("run_heads") as batch:
        batch.alter_column("branch_id", nullable=False)
        batch.drop_constraint("pk_run_heads", type_="primary")
        batch.create_primary_key("pk_run_heads", ["run_id", "branch_id", "phase"])

    # phase_prompt_drafts: same migration.
    with op.batch_alter_table("phase_prompt_drafts") as batch:
        batch.add_column(
            sa.Column(
                "branch_id",
                sa.String(length=64),
                sa.ForeignKey("branches.id", name="fk_phase_prompt_drafts_branch"),
                nullable=True,
            )
        )
    op.execute(
        sa.text(
            """
            UPDATE phase_prompt_drafts
            SET branch_id = 'br_main_' || run_id
            """
        )
    )
    with op.batch_alter_table("phase_prompt_drafts") as batch:
        batch.alter_column("branch_id", nullable=False)
        batch.drop_constraint("pk_phase_prompt_drafts", type_="primary")
        batch.create_primary_key(
            "pk_phase_prompt_drafts",
            ["run_id", "branch_id", "phase", "prompt_key"],
        )

    # phase_versions: track which branch produced each version.
    with op.batch_alter_table("phase_versions") as batch:
        batch.add_column(
            sa.Column(
                "created_on_branch_id",
                sa.String(length=64),
                sa.ForeignKey("branches.id", name="fk_phase_versions_created_on_branch"),
                nullable=True,
            )
        )
    op.execute(
        sa.text(
            """
            UPDATE phase_versions
            SET created_on_branch_id = 'br_main_' || run_id
            """
        )
    )

    # Stage 2.C drops the global "superseded" status — activeness is
    # determined per-branch via run_heads. Legacy rows from 2.A/2.B
    # are flipped back to "done" so activation works (codex round-2
    # review #2 stage 2.C).
    op.execute(
        sa.text(
            """
            UPDATE phase_versions
            SET status = 'done'
            WHERE status = 'superseded'
            """
        )
    )

    # Explicit upstream linkage per pv. Without this, a downstream
    # pv on branch B could silently inherit branch A's upstream via
    # the global run_head pointer.
    op.create_table(
        "phase_version_inputs",
        sa.Column(
            "phase_version_id",
            sa.String(length=64),
            sa.ForeignKey("phase_versions.id"),
            nullable=False,
        ),
        sa.Column("upstream_phase", sa.String(length=64), nullable=False),
        sa.Column(
            "upstream_pv_id",
            sa.String(length=64),
            sa.ForeignKey("phase_versions.id"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "phase_version_id", "upstream_phase", name="pk_phase_version_inputs"
        ),
    )
    op.create_index(
        "ix_phase_version_inputs_upstream",
        "phase_version_inputs",
        ["upstream_pv_id"],
    )

    # Backfill phase_version_inputs for pre-2.C versions (codex
    # rounds 3-4 #2 stage 2.C). We can't reconstruct historical
    # upstream heads exactly, so we approximate: for each existing
    # phase_version P, link to the most-recent earlier-phase 'done'
    # phase_version with created_at <= P's. Earlier means strictly
    # earlier in pipeline rank — codex round-4 noted that without a
    # rank filter, a scout pv would get downstream phases (curator,
    # synthesizer, ...) recorded as inputs, and forking from it would
    # copy downstream heads onto the new branch.
    pipeline_phases = (
        "proposal",
        "scout",
        "curator",
        "synthesizer",
        "ideator",
        "drafter",
        "stylist",
        "critic",
        "integrity",
        "exports",
    )
    for downstream_idx, downstream_phase in enumerate(pipeline_phases):
        for upstream_phase in pipeline_phases[:downstream_idx]:
            op.execute(
                sa.text(
                    f"""
                    INSERT INTO phase_version_inputs
                        (phase_version_id, upstream_phase, upstream_pv_id)
                    SELECT pv.id, '{upstream_phase}',
                      (SELECT u.id FROM phase_versions u
                       WHERE u.run_id = pv.run_id
                         AND u.phase = '{upstream_phase}'
                         AND u.status = 'done'
                         AND u.created_at <= pv.created_at
                       ORDER BY u.created_at DESC
                       LIMIT 1)
                    FROM phase_versions pv
                    WHERE pv.phase = '{downstream_phase}'
                      AND NOT EXISTS (
                        SELECT 1 FROM phase_version_inputs i
                        WHERE i.phase_version_id = pv.id
                          AND i.upstream_phase = '{upstream_phase}'
                      )
                      AND (SELECT u.id FROM phase_versions u
                           WHERE u.run_id = pv.run_id
                             AND u.phase = '{upstream_phase}'
                             AND u.status = 'done'
                             AND u.created_at <= pv.created_at
                           LIMIT 1) IS NOT NULL
                    """
                )
            )


def downgrade() -> None:
    op.drop_index("ix_phase_version_inputs_upstream", table_name="phase_version_inputs")
    op.drop_table("phase_version_inputs")

    with op.batch_alter_table("phase_versions") as batch:
        batch.drop_column("created_on_branch_id")

    with op.batch_alter_table("phase_prompt_drafts") as batch:
        batch.drop_constraint("pk_phase_prompt_drafts", type_="primary")
        batch.create_primary_key("pk_phase_prompt_drafts", ["run_id", "phase", "prompt_key"])
        batch.drop_column("branch_id")

    with op.batch_alter_table("run_heads") as batch:
        batch.drop_constraint("pk_run_heads", type_="primary")
        batch.create_primary_key("pk_run_heads", ["run_id", "phase"])
        batch.drop_column("branch_id")

    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("stale_from_phase", sa.String(length=64), nullable=True))
        batch.drop_column("active_branch_id")

    op.drop_index("ix_branches_run", table_name="branches")
    op.drop_index("ix_branches_run_name_active", table_name="branches")
    op.drop_table("branches")
