"""Backfill v001 phase_versions rows for vanilla first runs (PR-A4.1).

Codex 2026-05-02 amendment 6 to the per-phase version-model reset:

> Blanket "write v001 rows for vanilla first-runs" is unsafe. If a
> phase already has version_no=1, you cannot insert a baseline v001
> without renumbering every FK and lineage row. If a rerun or direct
> edit already overwrote legacy files, the original vanilla bytes
> are gone. Safe scope: runs with no phase_versions at all, or
> phases proven untouched and unambiguous.

This migration follows the safer scope:

- Per (run, phase): only insert a v001 row if NO row already exists
  for that (run, phase) AND the phase's owned legacy artifacts are
  present on disk per ``PHASE_COMPLETION_GLOBS``.
- Source = ``'agent'`` because vanilla first runs were always agent
  output.
- ``input_snapshot_hash`` and ``prompt_hash`` are left NULL — we
  cannot reconstruct what hashes the agent saw at the time. Future
  pvs created via ``run_with_versioning`` will populate them; this
  is acceptable for retrospective backfill.
- ``created_at`` is set to ``run.updated_at`` as a best-effort
  timestamp; a precise per-phase completion time is not recorded
  in any pre-existing event we can rely on across the dataset.
- ``parent_pv_id`` = NULL (these are the chain roots).
- ``phase_version_inputs`` rows are inserted to record lineage
  against the same-run earlier-phase v001 rows we just inserted.
- ``run_heads`` rows on the run's ``main`` branch (creating
  ``main`` if missing) point at the inserted v001s.

The migration is idempotent: re-running it does nothing because the
"row already exists" guard short-circuits every phase.

Operational impact: the existing UI (and PR-A4.2's modal redesign)
can rely on every phase that has produced output also having a
``phase_versions`` row. Downstream PRs (delete, cascade activate)
become well-defined.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

# Match autoessay.phase_rerun.PHASES (cannot import — alembic
# scripts run with a minimal context). Keep in sync if the
# canonical list changes.
PIPELINE_PHASES: tuple[str, ...] = (
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


# Sentinel files matching ``PHASE_COMPLETION_GLOBS``. Phases without
# a sentinel file present on disk are NOT backfilled (we cannot
# claim they completed).
PHASE_SENTINEL_GLOBS: dict[str, tuple[str, ...]] = {
    "scout": ("discovery/scout_report.md",),
    "curator": ("sources/shortlist.json",),
    "synthesizer": ("synthesis/claims.jsonl",),
    "ideator": ("novelty/angle_cards.json",),
    "drafter": ("drafts/*/manuscript.md",),
    "stylist": ("drafts/*/style/*",),
    "critic": ("reviews/*",),
    "integrity": ("integrity/integrity_summary.json",),
    "exports": ("exports/manifest.json",),
}


revision: str = "017_backfill_pv_first_runs"
down_revision: str | None = "016_project_corpus"
branch_labels: str | None = None
depends_on: str | None = None


def _phase_has_completed_output(run_dir: Path, phase: str) -> bool:
    patterns = PHASE_SENTINEL_GLOBS.get(phase, ())
    for pattern in patterns:
        for match in run_dir.glob(pattern):
            if match.is_file() and match.stat().st_size > 0:
                return True
    return False


def _ensure_main_branch(connection, run_id: str, run_updated_at: str) -> str:
    """Return the run's main branch id, creating it if missing.

    Mirrors ``autoessay.branches.ensure_main_branch`` but uses raw
    SQL because alembic context is not the live SQLAlchemy session.
    """
    main_id = f"br_main_{run_id}"
    existing = connection.execute(
        sa.text("SELECT id FROM branches WHERE id = :id"),
        {"id": main_id},
    ).first()
    if existing is None:
        connection.execute(
            sa.text(
                """
                INSERT INTO branches (id, run_id, name, created_at)
                VALUES (:id, :run_id, 'main', :created_at)
                """,
            ),
            {"id": main_id, "run_id": run_id, "created_at": run_updated_at},
        )
        connection.execute(
            sa.text(
                "UPDATE runs SET active_branch_id = :bid "
                "WHERE id = :rid AND active_branch_id IS NULL",
            ),
            {"bid": main_id, "rid": run_id},
        )
    return main_id


def upgrade() -> None:
    connection = op.get_bind()

    runs = connection.execute(
        sa.text("SELECT id, run_dir, updated_at FROM runs"),
    ).all()

    for row in runs:
        run_id = row[0]
        run_dir_str = row[1]
        run_updated_at = row[2]
        if not run_dir_str:
            continue
        run_dir = Path(run_dir_str)
        if not run_dir.exists():
            continue

        # Snapshot existing pv rows for this run so we can short-
        # circuit phases that already have history.
        existing = {
            r[0]
            for r in connection.execute(
                sa.text(
                    "SELECT phase FROM phase_versions WHERE run_id = :rid",
                ),
                {"rid": run_id},
            ).all()
        }

        branch_id: str | None = None  # lazily resolved
        # Track inserted v001 ids per phase for upstream lineage rows.
        inserted_pv_ids: dict[str, str] = {}

        for phase in PIPELINE_PHASES:
            if phase in existing:
                # Run already has at least one pv row for this phase.
                # Codex amendment: do not renumber; skip.
                continue
            if not _phase_has_completed_output(run_dir, phase):
                continue

            if branch_id is None:
                branch_id = _ensure_main_branch(connection, run_id, run_updated_at)

            pv_id = f"pv_{uuid4().hex}"
            artifacts_dir = f"phases/{pv_id}"
            connection.execute(
                sa.text(
                    """
                    INSERT INTO phase_versions (
                        id, run_id, phase, version_no, parent_pv_id,
                        status, artifacts_dir, input_snapshot_hash,
                        prompt_hash, created_on_branch_id, created_by,
                        source, created_at, completed_at
                    ) VALUES (
                        :id, :run_id, :phase, 1, NULL,
                        'done', :artifacts_dir, NULL,
                        NULL, :branch_id, NULL,
                        'agent', :created_at, :completed_at
                    )
                    """,
                ),
                {
                    "id": pv_id,
                    "run_id": run_id,
                    "phase": phase,
                    "artifacts_dir": artifacts_dir,
                    "branch_id": branch_id,
                    "created_at": run_updated_at,
                    "completed_at": run_updated_at,
                },
            )

            # Lineage rows: every earlier pipeline phase we just
            # inserted is an upstream of this phase.
            for upstream_phase, upstream_pv_id in inserted_pv_ids.items():
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO phase_version_inputs (
                            phase_version_id, upstream_phase, upstream_pv_id
                        ) VALUES (:pv, :up_phase, :up_pv)
                        """,
                    ),
                    {
                        "pv": pv_id,
                        "up_phase": upstream_phase,
                        "up_pv": upstream_pv_id,
                    },
                )

            # RunHead points at the new pv on the main branch.
            existing_head = connection.execute(
                sa.text(
                    "SELECT version_id FROM run_heads "
                    "WHERE run_id = :rid AND branch_id = :bid AND phase = :p",
                ),
                {"rid": run_id, "bid": branch_id, "p": phase},
            ).first()
            if existing_head is None:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO run_heads (run_id, branch_id, phase, version_id, updated_at)
                        VALUES (:rid, :bid, :p, :vid, :ts)
                        """,
                    ),
                    {
                        "rid": run_id,
                        "bid": branch_id,
                        "p": phase,
                        "vid": pv_id,
                        "ts": run_updated_at,
                    },
                )

            inserted_pv_ids[phase] = pv_id


def downgrade() -> None:
    """Best-effort downgrade: drop pv rows we definitely created.

    A round-trip migration is impossible to make perfectly safe
    because we cannot distinguish backfilled v001 rows from rows
    written by the new ``run_with_versioning``-wrapped start_*
    code path that lands in the same PR. The downgrade only
    removes rows whose ``input_snapshot_hash IS NULL AND prompt_hash
    IS NULL AND version_no = 1 AND source = 'agent'`` —
    backfilled-shaped rows. Newly-created rows from the runtime
    path will have non-NULL hashes.
    """
    connection = op.get_bind()
    # Order matters because of FKs.
    connection.execute(
        sa.text(
            """
            DELETE FROM run_heads
            WHERE version_id IN (
                SELECT id FROM phase_versions
                WHERE version_no = 1
                  AND source = 'agent'
                  AND input_snapshot_hash IS NULL
                  AND prompt_hash IS NULL
            )
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM phase_version_inputs
            WHERE phase_version_id IN (
                SELECT id FROM phase_versions
                WHERE version_no = 1
                  AND source = 'agent'
                  AND input_snapshot_hash IS NULL
                  AND prompt_hash IS NULL
            )
            """,
        ),
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM phase_versions
            WHERE version_no = 1
              AND source = 'agent'
              AND input_snapshot_hash IS NULL
              AND prompt_hash IS NULL
            """,
        ),
    )
