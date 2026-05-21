from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    # oidc_subject uniqueness is declared as an explicit unique Index (not
    # column-level unique=True) to match the migration's `op.create_index(...,
    # unique=True)` form. This keeps `alembic check` clean on SQLite where
    # column-level unique generates a CONSTRAINT, but the migration created
    # a unique INDEX — those are not equivalent in SQLAlchemy's diff.
    __table_args__ = (
        Index("ix_users_email", "email"),
        Index("uq_users_oidc_subject", "oidc_subject", unique=True),
        Index("uq_users_username", "username", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # PR-B: ``username`` + ``password_hash`` are the native auth
    # primary key + credential. ``oidc_subject`` / ``oidc_issuer``
    # remain nullable so historical Casdoor rows survive the
    # migration as audit-only records (they cannot log in until
    # an admin assigns them a password).
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_issuer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    picture_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_expires_at", "expires_at"),)

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    config_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class Project(Base):
    __tablename__ = "projects"
    # Composite index speeds up the hot-path "list active essays for
    # user X" query. We use a non-partial composite for portability —
    # Postgres can do a partial index ``WHERE deleted_at IS NULL`` for
    # marginal extra speed but alembic check then needs dialect-aware
    # diffing. The composite is good enough at our scale (< 1000
    # essays per user).
    __table_args__ = (Index("ix_projects_user_deleted_at", "user_id", "deleted_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    domain_id: Mapped[str] = mapped_column(String(128), ForeignKey("domains.id"), nullable=False)
    domain_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_journal: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_project_deleted_at", "project_id", "deleted_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"), nullable=False)
    domain_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        server_default="",
    )
    run_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    proposal_content_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    proposal_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_branch_id: Mapped[str | None] = mapped_column(
        String(64),
        # use_alter breaks the runs <-> branches FK cycle so SQLAlchemy
        # can issue create_all/drop_all without an unresolvable order.
        ForeignKey("branches.id", use_alter=True, name="fk_runs_active_branch"),
        nullable=True,
    )
    # Stage 3.E follow-up P0 (codex AGREE-with-amendments): atomic
    # phase-start claim. ``active_phase_lock`` holds the phase name
    # currently dispatched ("drafter", "stylist", ...); a single-row
    # UPDATE WHERE active_phase_lock IS NULL is the claim. Release
    # is owner-checked against ``active_phase_lock_job_id`` so a
    # crashed/late worker can't clear a newer lock. ``claimed_at``
    # gives ops a way to spot zombie locks and a manual-clear
    # endpoint a way to display the wait time.
    active_phase_lock: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_phase_lock_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_phase_lock_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # PR-C0 research-kernel intake gate. ``paper_mode`` is a
    # validated string (NOT a DB enum, see paper_modes.py module
    # docstring) so adding modes in later PRs doesn't require a
    # schema migration. ``research_kernel_json`` is an opaque JSON
    # blob; the schema is versioned from inside via
    # ``kernel_schema_version`` so per-PR-C-step extensions don't
    # add columns.
    paper_mode: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="case_analysis",
        server_default="case_analysis",
    )
    research_kernel_json: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {"kernel_schema_version": 1},
        server_default='{"kernel_schema_version": 1}',
    )
    # PR-366 (2026-05-13) "数理增强模式" — opt-in execution flag that
    # gates round-0 stage B (gpt-5.5 holistic rewrite + LaTeX/表/【待填】
    # scaffolding) for BOTH polish_loop and critic_loop. Default false
    # keeps the production path at the cheap ~14 min run; flipping it
    # adds +20-30 min wall time and ~10x token cost. Per-run, set at
    # run creation via ``POST /api/projects/{pid}/runs`` body field or
    # changed mid-run via ``PATCH /api/runs/{id}/settings`` (refused
    # while final_rewrite / critic phase is actively running).
    mathematical_mode: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    # PR-382 (2026-05-13): one-click full-auto pilot. When ``true``,
    # the backend ``auto_advance`` coordinator advances every
    # ``USER_*_REVIEW`` gate to the next phase automatically. Default
    # ``false`` keeps manual-review behavior. Toggleable mid-run via
    # ``PATCH /api/runs/{id}/settings``. ``FAILED_*`` states still
    # require user intervention — coordinator pauses there.
    auto_advance: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    # ADR-0003: run-level manuscript generation architecture.
    # ``deep`` is the migration-safe database default for existing and
    # in-flight rows; new omitted API requests resolve through
    # MANUSCRIPT_DEFAULT_MODE before persisting this value.
    generation_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="deep",
        server_default="deep",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )


class Author(Base):
    """One row per author in the user's roster.

    ``is_self`` marks the lazy-bootstrapped author created the first
    time the user opens GET /api/authors — that author is the project
    owner themselves. Application code keeps at most one ``is_self``
    per user. Soft-delete via ``deleted_at`` retains existing
    ``project_author`` references so historical manuscripts still
    render the author correctly.
    """

    __tablename__ = "authors"
    __table_args__ = (Index("ix_authors_user_deleted_at", "user_id", "deleted_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    affiliation: Mapped[str | None] = mapped_column(String(500), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    orcid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_self: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PhaseVersion(Base):
    """One row per phase invocation that produced (or attempted to
    produce) artifacts.

    Stage 2.A keeps history linear via ``parent_pv_id``. Branches
    come in 2.C. ``input_snapshot_hash`` is the sha256 of the
    upstream phase_version ids concatenated in canonical order, used
    by future dedup logic to skip identical-input reruns.

    Status transitions (codex-AGREEd):
        running -> done -> superseded
        running -> failed | cancelled
    """

    __tablename__ = "phase_versions"
    __table_args__ = (
        UniqueConstraint("run_id", "phase", "version_no", name="uq_phase_versions_run_phase_no"),
        Index(
            "ix_phase_versions_run_phase_status",
            "run_id",
            "phase",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_pv_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("phase_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    artifacts_dir: Mapped[str] = mapped_column(String(500), nullable=False)
    input_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_on_branch_id: Mapped[str | None] = mapped_column(
        # use_alter breaks the phase_versions <-> branches cycle (the
        # branch FK to forked_from_pv_id closes the loop).
        String(64),
        ForeignKey("branches.id", use_alter=True, name="fk_phase_versions_branch"),
        nullable=True,
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ``source`` distinguishes how this version came to exist. Values
    # are conventional (validated at the API layer, not the DB), with
    # ``'agent'`` covering every version produced by an agent run
    # (the default and the value backfilled by migration 015) and
    # ``'user_edit'`` reserved for versions written by the upcoming
    # PUT /api/runs/{id}/<phase> user-edit endpoints (PR-A2). The
    # frontend uses this field to label user-edit versions distinctly
    # in the phase-history modal — codex amendment 6, issue 1,
    # 2026-05-01 design review.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="agent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PhaseArtifact(Base):
    """An immutable blob produced by a phase_version.

    ``logical_path`` is where existing readers look (e.g.
    ``synthesis/claims.jsonl``). ``blob_path`` is the per-version
    archive copy under ``runs/<run>/phases/<pv_id>/...``. Activation
    copies blobs back over logical paths so readers stay
    version-agnostic for now.
    """

    __tablename__ = "artifacts_v2"
    __table_args__ = (
        Index("ix_artifacts_v2_pv", "phase_version_id"),
        Index("ix_artifacts_v2_sha", "sha256"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    phase_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("phase_versions.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    logical_path: Mapped[str] = mapped_column(String(500), nullable=False)
    blob_path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunHead(Base):
    """Pointer to the active phase_version per (run, branch, phase)."""

    __tablename__ = "run_heads"

    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), primary_key=True)
    branch_id: Mapped[str] = mapped_column(String(64), ForeignKey("branches.id"), primary_key=True)
    phase: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("phase_versions.id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PhasePromptDraft(Base):
    """User's editable draft prompt override per (run, branch, phase, prompt_key).

    Stage 2.C keys this by branch so two branches can hold independent
    drafts. ``prompt_key`` stays as a future-proof slot for phases
    that gain multiple editable prompt surfaces (drafter per-section,
    etc.). ``content_hash`` doubles as a revision token for the
    optimistic concurrency check on rerun.
    """

    __tablename__ = "phase_prompt_drafts"
    __table_args__ = (Index("ix_phase_prompt_drafts_run_phase", "run_id", "phase"),)

    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), primary_key=True)
    branch_id: Mapped[str] = mapped_column(String(64), ForeignKey("branches.id"), primary_key=True)
    phase: Mapped[str] = mapped_column(String(64), primary_key=True)
    prompt_key: Mapped[str] = mapped_column(String(64), primary_key=True, default="main")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Branch(Base):
    """A named fork of a run's phase chain (codex-AGREEd #2 stage 2.C).

    Every run has at least one branch named ``main`` (created by the
    backfill in migration 013). A user creates additional branches by
    forking from a phase_version. Each branch carries its own
    ``run_heads``, ``phase_prompt_drafts``, and ``stale_from_phase``
    so two branches can diverge without stepping on each other.

    ``forked_from_pv_id`` and ``forked_phase`` document the fork
    point. ``parent_branch_id`` is the branch the fork was made on
    (NULL for ``main``). Soft delete via ``deleted_at`` lets a name
    be reused without breaking historical references.
    """

    __tablename__ = "branches"
    __table_args__ = (
        Index(
            "ix_branches_run_name_active",
            "run_id",
            "name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_branches_run", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_branch_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("branches.id"), nullable=True
    )
    forked_from_pv_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("phase_versions.id"), nullable=True
    )
    forked_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stale_from_phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PhaseVersionInput(Base):
    """Explicit upstream linkage for a phase_version.

    Codex round-1 review (#2 stage 2.C): without recording the exact
    upstream pv ids each version was produced from, a downstream pv
    on branch B could silently inherit upstream content from branch A
    via the global run_head pointer. This table fixes the model: at
    begin time, the rerun endpoint records the (run, branch)'s
    current upstream heads as the FK targets. From then on, this pv
    knows its lineage regardless of where run_heads later point.
    """

    __tablename__ = "phase_version_inputs"
    __table_args__ = (Index("ix_phase_version_inputs_upstream", "upstream_pv_id"),)

    phase_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("phase_versions.id"), primary_key=True
    )
    upstream_phase: Mapped[str] = mapped_column(String(64), primary_key=True)
    upstream_pv_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("phase_versions.id"), nullable=False
    )


class PhaseVersionPrompt(Base):
    """Immutable snapshot of the resolved prompt for one phase_version.

    Captured at begin time, never mutated. ``source`` is ``"default"``
    if no override was active, ``"override"`` if the user supplied
    one. Phase-history UI reads this to show "this version was
    produced with this exact prompt".
    """

    __tablename__ = "phase_version_prompts"
    __table_args__ = (
        Index("ix_phase_version_prompts_pv", "phase_version_id"),
        CheckConstraint(
            "source IN ('default', 'override')",
            name="ck_phase_version_prompts_source",
        ),
    )

    phase_version_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("phase_versions.id"), primary_key=True
    )
    prompt_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    template_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectAuthor(Base):
    """Ordered project ↔ author assignment.

    ``position`` is 0-based and contiguous (enforced by the API);
    DB-level CHECK rejects negatives even if the API is buggy.
    Per-project uniqueness on ``position`` prevents duplicate slots.
    The column name is ``position``, not ``order`` — ``order`` is a
    SQL reserved word and would force quoting in every query.
    """

    __tablename__ = "project_authors"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "author_id"),
        UniqueConstraint("project_id", "position", name="uq_project_authors_position"),
        CheckConstraint("position >= 0", name="ck_project_authors_position_nonneg"),
        Index("ix_project_authors_position", "project_id", "position"),
    )

    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"), nullable=False)
    author_id: Mapped[str] = mapped_column(String(64), ForeignKey("authors.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_id_created_at", "run_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunState(Base):
    __tablename__ = "run_states"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Checkpoint(Base):
    __tablename__ = "checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    checkpoint_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_payload: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="{}",
        server_default="{}",
    )
    decision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    creator: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceRecord(Base):
    __tablename__ = "source_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    authors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    venue: Mapped[str | None] = mapped_column(String(500), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    # PR-C1.a research_role: 4-tier classification per-run-context.
    # See `agents/research_role_classifier.py`. Values:
    #   primary_source / secondary_argument / theoretical_lens
    #   / methodological_reference. Default keeps legacy behaviour.
    research_role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="secondary_argument",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Corpus(Base):
    __tablename__ = "corpora"
    __table_args__ = (
        Index("ix_corpora_user_id", "user_id"),
        Index("ix_corpora_project_id", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("users.id"), nullable=True)
    # NULL → global corpus for this owner_user_id (current semantics).
    # Non-NULL → corpus uploaded under a specific project; included
    # automatically only by THAT project's effective list.
    # See ``corpus.corpora_for_project``. (PR-B1, codex amendment 1.)
    project_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("projects.id", name="fk_corpora_project_id"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectCorpusSelection(Base):
    """Records which GLOBAL corpora a specific project explicitly
    includes in its style-profile / dedup queries. Project-scoped
    corpora (``Corpus.project_id == this project``) are *always*
    included automatically and don't need a row here. Codex
    amendment 1 to issue 2 of the 2026-05-01 design review."""

    __tablename__ = "project_corpus_selections"

    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.id", name="fk_pcs_project_id"),
        primary_key=True,
    )
    corpus_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("corpora.id", name="fk_pcs_corpus_id"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )


class CorpusDocument(Base):
    __tablename__ = "corpus_documents"
    __table_args__ = (
        Index(
            "uq_corpus_documents_corpus_hash",
            "corpus_id",
            "document_hash",
            unique=True,
        ),
        Index("ix_corpus_documents_ingest_status", "ingest_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String(64), ForeignKey("corpora.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    document_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extracted_text_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    style_profile_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    privacy_level: Mapped[str] = mapped_column(String(64), nullable=False, default="private")
    ingest_status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    sensitivity: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NoveltyDiscussion(Base):
    __tablename__ = "novelty_discussions"
    __table_args__ = (Index("ix_novelty_discussions_run_created_at", "run_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    generation_token: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MemoryRef(Base):
    __tablename__ = "memory_refs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    corpus_document_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("corpus_documents.id"),
        nullable=False,
    )
    memory_id: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentInvocation(Base):
    __tablename__ = "agent_invocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderCall(Base):
    __tablename__ = "provider_calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    call_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunTelemetry(Base):
    __tablename__ = "run_telemetry"
    __table_args__ = (
        CheckConstraint("mode IN ('express', 'deep')", name="ck_run_telemetry_mode"),
        CheckConstraint(
            "total_tokens IS NULL OR total_tokens >= 0",
            name="ck_run_telemetry_total_tokens_nonnegative",
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name="ck_run_telemetry_latency_ms_nonnegative",
        ),
        CheckConstraint(
            "manuscript_chars IS NULL OR manuscript_chars >= 0",
            name="ck_run_telemetry_manuscript_chars_nonnegative",
        ),
        Index("ix_run_telemetry_mode_created_at", "mode", "created_at"),
        Index("ix_run_telemetry_finished_at", "finished_at"),
        Index("ix_run_telemetry_failure_code", "failure_code"),
    )

    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    audit_status: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    manuscript_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class RevisionPass(Base):
    __tablename__ = "revision_passes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"), nullable=False)
    dimension: Mapped[str] = mapped_column(String(64), nullable=False)
    report_path: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("runs.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
