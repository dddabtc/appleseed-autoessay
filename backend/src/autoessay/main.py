import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field, validator
from sqlalchemy import Engine, delete, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import FileResponse, StreamingResponse

from autoessay.agents.critic import load_critic_payload, run_critic
from autoessay.agents.curator import (
    find_local_pdf_path,
    load_sources_payload,
    run_curator,
    store_uploaded_pdf,
)
from autoessay.agents.drafter import (
    load_draft_payload,
    load_drafts_payload,
    run_drafter,
)
from autoessay.agents.exporter import load_exports_payload, run_exports
from autoessay.agents.final_rewrite import (
    rewrite_summary_for_run,
    run_final_rewrite_then_critic,
)
from autoessay.agents.framework_lens import run_framework_lens
from autoessay.agents.ideator import (
    load_novelty_payload,
    regenerate_angle_cards_for_discussion,
    run_ideator,
    select_thesis_for_run,
)
from autoessay.agents.integrity import (
    latest_external_scan_decision,
    load_integrity_payload,
    run_integrity,
)
from autoessay.agents.proposal import (
    load_proposal_payload,
    run_proposal_draft,
    save_proposal_version,
)
from autoessay.agents.scout import run_scout
from autoessay.agents.stylist import (
    load_style_payload,
    load_style_score_payload,
    run_stylist,
)
from autoessay.agents.synthesizer import load_synthesis_payload, run_synthesizer
from autoessay.agents.tension_extraction import run_tension_extraction
from autoessay.auth.bootstrap import bootstrap_initial_admin
from autoessay.auth.middleware import AuthGateMiddleware, current_user, validate_auth_boot_settings
from autoessay.auth.routes import router as auth_router
from autoessay.config import get_settings
from autoessay.corpus import (
    CorpusUploadError,
    create_corpus_document,
    delete_document_files,
    get_user_document,
    list_user_documents,
    preview_document_text,
    run_corpus_ingest_job,
    run_corpus_style_profile_job,
    style_profile_path_for_user,
)
from autoessay.db import check_database, get_engine, get_session
from autoessay.domain_loader import DomainConfigError, LoadedDomain, load_domains
from autoessay.framework_lens import resolve_framework_lens_summary_ref
from autoessay.generation_modes import (
    DEEP_MODE,
    EXPRESS_MODE,
    GenerationMode,
    normalize_generation_mode,
)
from autoessay.kernel_suggest import (
    SCOPE_MAX_CHARS as KERNEL_SUGGEST_SCOPE_MAX_CHARS,
)
from autoessay.kernel_suggest import (
    KernelSuggestion,
    SuggestionLanguage,
    suggest_kernel,
)
from autoessay.models import (
    Author,
    Branch,
    Checkpoint,
    CorpusDocument,
    Domain,
    NoveltyDiscussion,
    PhasePromptDraft,
    PhaseVersion,
    PhaseVersionPrompt,
    Project,
    ProjectAuthor,
    Run,
    RunEvent,
    RunHead,
    RunState,
    SourceRecord,
    User,
    utcnow,
)
from autoessay.phase_force_approve import (
    compute_force_target,
)
from autoessay.phase_force_approve import (
    force_approve as _do_force_approve,
)
from autoessay.phase_lock import (
    claim_phase_lock,
    force_clear_phase_lock,
    get_active_phase_lock,
    new_lock_token,
    release_phase_lock,
)
from autoessay.phase_readiness import assert_phase_ready
from autoessay.research_kernel import (
    compute_kernel_hash,
    stale_marks_after_kernel_edit,
)
from autoessay.run_writer import create_run_directory
from autoessay.safety import (
    SafetyCheckResult,
    SafetyGateError,
    validate_user_input,
)
from autoessay.state_machine import RUN_STATES, InvalidTransition, append_event, transition
from autoessay.worker import (
    enqueue_corpus_ingest_job,
    enqueue_corpus_style_profile_job,
    enqueue_critic_job,
    enqueue_curator_job,
    enqueue_drafter_job,
    enqueue_exports_job,
    enqueue_express_job,
    enqueue_final_rewrite_job,
    enqueue_ideator_job,
    enqueue_integrity_job,
    enqueue_proposal_job,
    enqueue_scout_job,
    enqueue_stylist_job,
    enqueue_synthesizer_job,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    validate_auth_boot_settings()
    bootstrap_initial_admin()
    # PR-I2.a: spawn the zombie-phase reaper coroutine when
    # AUTOESSAY_ZOMBIE_REAPER_ENABLED=1; no-op otherwise. The lifespan
    # helper handles graceful task cancellation on shutdown.
    from autoessay.zombie_reaper import zombie_reaper_lifespan

    async with zombie_reaper_lifespan():
        yield


app = FastAPI(title="appleseed-autoessay", version="0.1.0", lifespan=lifespan)
app.add_middleware(AuthGateMiddleware)
app.include_router(auth_router)


def _claim_or_409(session: Session, run: Run, phase: str) -> str:
    """Atomically claim the phase-start lock for ``phase`` on ``run``.

    Stage 3.E follow-up P0 (codex AGREE-with-amendments): start_*
    endpoints must claim the lock in the same transaction as the
    state guard so two concurrent clicks (multi-tab or curl) cannot
    both pass and enqueue parallel agent runs.

    Returns the lock token. Raises 409 if the lock is held.
    """
    token = new_lock_token()
    if not claim_phase_lock(session, run, phase, token):
        held = run.active_phase_lock
        claimed = (
            run.active_phase_lock_claimed_at.isoformat()
            if run.active_phase_lock_claimed_at is not None
            else "unknown"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Another phase is already running for this run "
                f"(phase={held!r}, claimed_at={claimed!r}). "
                "Wait for it to finish before clicking again, or use "
                "the admin clear-phase-lock endpoint if it is stuck."
            ),
        )
    session.commit()
    return token


def _release_after_enqueue_failure(
    session: Session,
    run: Run,
    phase: str,
    token: str,
) -> None:
    """Codex amendment: when ``queue.enqueue`` raises (Redis hiccup,
    serialization error, etc.) we must release the lock or the run
    is permanently stuck behind a phantom job.
    """
    release_phase_lock(session, run, phase, token)
    session.commit()


SessionDependency = Annotated[Session, Depends(get_session)]
CurrentUserDependency = Annotated[User, Depends(current_user)]


def _run_generation_mode(run: Run) -> GenerationMode:
    return normalize_generation_mode(getattr(run, "generation_mode", DEEP_MODE) or DEEP_MODE)


def _assert_deep_generation_mode(run: Run, phase: str) -> None:
    if _run_generation_mode(run) != DEEP_MODE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{phase} is only available for mode=deep",
        )


SafetyContext = Literal[
    "project.title",
    "project.intro",
    "kernel.observed_puzzle",
    "kernel.tentative_question",
    "kernel.scope",
    "kernel.theory_preference",
    "kernel.method_preference",
    "kernel.research_kernel_other",
    "proposal.user_draft",
    "phase_prompt_override",
    "phase_edit",
    "novelty.user_message",
    "checkpoint.skip_reason",
    "checkpoint.text_edit",
    "source_upload.metadata",
    "author.display_name",
    "author.bio",
]

SAFETY_CONTEXT_PROJECT_TITLE: SafetyContext = "project.title"
SAFETY_CONTEXT_PROJECT_INTRO: SafetyContext = "project.intro"
SAFETY_CONTEXT_KERNEL_OBSERVED_PUZZLE: SafetyContext = "kernel.observed_puzzle"
SAFETY_CONTEXT_KERNEL_TENTATIVE_QUESTION: SafetyContext = "kernel.tentative_question"
SAFETY_CONTEXT_KERNEL_SCOPE: SafetyContext = "kernel.scope"
SAFETY_CONTEXT_KERNEL_THEORY_PREFERENCE: SafetyContext = "kernel.theory_preference"
SAFETY_CONTEXT_KERNEL_METHOD_PREFERENCE: SafetyContext = "kernel.method_preference"
SAFETY_CONTEXT_KERNEL_OTHER: SafetyContext = "kernel.research_kernel_other"
SAFETY_CONTEXT_PROPOSAL_USER_DRAFT: SafetyContext = "proposal.user_draft"
SAFETY_CONTEXT_PHASE_PROMPT_OVERRIDE: SafetyContext = "phase_prompt_override"
SAFETY_CONTEXT_PHASE_EDIT: SafetyContext = "phase_edit"
SAFETY_CONTEXT_NOVELTY_USER_MESSAGE: SafetyContext = "novelty.user_message"
SAFETY_CONTEXT_CHECKPOINT_SKIP_REASON: SafetyContext = "checkpoint.skip_reason"
SAFETY_CONTEXT_CHECKPOINT_TEXT_EDIT: SafetyContext = "checkpoint.text_edit"
SAFETY_CONTEXT_SOURCE_UPLOAD_METADATA: SafetyContext = "source_upload.metadata"
SAFETY_CONTEXT_AUTHOR_DISPLAY_NAME: SafetyContext = "author.display_name"
SAFETY_CONTEXT_AUTHOR_BIO: SafetyContext = "author.bio"


def _enforce_input_safety(
    text: str | None,
    *,
    context_hint: SafetyContext | str,
) -> SafetyCheckResult | None:
    """Run the LLM-backed safety gate on free-text user input.

    Returns the SafetyCheckResult on allow; raises HTTPException(400 / 422)
    on block / quarantine. Returns None when the gate is disabled or the
    input is empty.

    Production default is fail-closed when the LLM classifier is unavailable.
    ``AUTOESSAY_SAFETY_GATE_FAIL_OPEN=1`` restores the previous warning-log
    pass-through behavior for e2e/dev/canary or emergency degradation. Stub
    mode still short-circuits inside ``validate_user_input`` before any LLM
    call and is independent of this setting.
    """
    settings = get_settings()
    if not getattr(settings, "safety_gate_enabled", True):
        return None
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    try:
        result = validate_user_input(cleaned, context_hint=context_hint)
    except SafetyGateError as exc:
        if getattr(settings, "safety_gate_fail_open", False):
            logger = logging.getLogger("autoessay.safety")
            logger.warning(
                "safety_gate_unavailable: %s (context=%s, input_chars=%d)",
                str(exc)[:300],
                context_hint,
                len(cleaned),
            )
            return None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "safety_gate_unavailable",
                "user_facing_reason": (
                    "Safety check is temporarily unavailable. Please try again."
                ),
                "context_hint": context_hint,
            },
        ) from exc
    if result.verdict.verdict == "block":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "safety_gate_blocked",
                "verdict": result.verdict.verdict,
                "categories": result.verdict.categories,
                "evidence": result.verdict.evidence,
                "user_facing_reason": result.verdict.user_facing_reason,
                "context_hint": context_hint,
            },
        )
    if result.verdict.verdict == "quarantine":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "safety_gate_quarantined",
                "verdict": result.verdict.verdict,
                "categories": result.verdict.categories,
                "evidence": result.verdict.evidence,
                "user_facing_reason": result.verdict.user_facing_reason,
                "context_hint": context_hint,
                "quarantine_id": result.quarantine_id,
            },
        )
    return result


def _enforce_input_safety_batch(
    fields: dict[str, str],
    *,
    overall_context_hint: SafetyContext | str = "kernel.batch",
) -> None:
    """Run one safety check for a labeled batch of free-text fields."""
    cleaned_fields = {
        field_path: value.strip()
        for field_path, value in fields.items()
        if isinstance(value, str) and value.strip()
    }
    if not cleaned_fields:
        return
    joined = "\n\n".join(f"[{field_path}]\n{value}" for field_path, value in cleaned_fields.items())
    try:
        _enforce_input_safety(joined, context_hint=overall_context_hint)
    except HTTPException as exc:
        if isinstance(exc.detail, dict):
            detail = dict(exc.detail)
            field_paths = list(cleaned_fields)
            field_path = _safety_field_path_from_detail(detail, field_paths)
            detail.setdefault("field_paths", field_paths)
            if field_path is not None:
                detail.setdefault("field_path", field_path)
            exc.detail = detail
        raise


def _safety_field_path_from_detail(
    detail: dict[str, object],
    field_paths: list[str],
) -> str | None:
    if len(field_paths) == 1:
        return field_paths[0]
    try:
        haystack = json.dumps(detail, ensure_ascii=False)
    except TypeError:
        haystack = str(detail)
    for field_path in field_paths:
        if field_path in haystack:
            return field_path
    return None


def _add_safety_field(fields: dict[str, str], field_path: str, value: str) -> None:
    cleaned = value.strip()
    if not cleaned:
        return
    candidate = field_path
    suffix = 2
    while candidate in fields:
        candidate = f"{field_path}#{suffix}"
        suffix += 1
    fields[candidate] = cleaned


def _collect_string_safety_fields(
    value: object,
    *,
    field_path: str,
    fields: dict[str, str],
) -> None:
    if isinstance(value, str):
        _add_safety_field(fields, field_path, value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                _collect_string_safety_fields(
                    item,
                    field_path=f"{field_path}.{key}",
                    fields=fields,
                )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _collect_string_safety_fields(
                item,
                field_path=f"{field_path}[{index}]",
                fields=fields,
            )


_RESEARCH_KERNEL_SAFETY_CONTEXTS: dict[str, SafetyContext] = {
    "observed_puzzle": SAFETY_CONTEXT_KERNEL_OBSERVED_PUZZLE,
    "tentative_question": SAFETY_CONTEXT_KERNEL_TENTATIVE_QUESTION,
    "scope": SAFETY_CONTEXT_KERNEL_SCOPE,
    "theory_preference": SAFETY_CONTEXT_KERNEL_THEORY_PREFERENCE,
    "method_preference": SAFETY_CONTEXT_KERNEL_METHOD_PREFERENCE,
}


def _enforce_research_kernel_safety(kernel: dict[str, object]) -> None:
    fields: dict[str, str] = {}
    for key, value in kernel.items():
        if key == "kernel_schema_version":
            continue
        context = _RESEARCH_KERNEL_SAFETY_CONTEXTS.get(key)
        field_path = context if context is not None else f"{SAFETY_CONTEXT_KERNEL_OTHER}.{key}"
        _collect_string_safety_fields(value, field_path=field_path, fields=fields)
    _enforce_input_safety_batch(fields, overall_context_hint="kernel.batch")


def _enforce_proposal_json_safety(proposal_json: dict[str, object]) -> None:
    fields: dict[str, str] = {}
    _collect_string_safety_fields(
        proposal_json,
        field_path=SAFETY_CONTEXT_PROPOSAL_USER_DRAFT,
        fields=fields,
    )
    _enforce_input_safety_batch(
        fields,
        overall_context_hint=SAFETY_CONTEXT_PROPOSAL_USER_DRAFT,
    )


def _enforce_checkpoint_safety(request: "CheckpointDecisionRequest") -> None:
    _enforce_input_safety(
        _string_from_request(request, "skip_reason"),
        context_hint=SAFETY_CONTEXT_CHECKPOINT_SKIP_REASON,
    )
    edits = request.edits
    if edits is None:
        raw_edits = request.decision_payload.get("edits")
        edits = raw_edits if isinstance(raw_edits, dict) else None
    if not isinstance(edits, dict):
        return
    for value in edits.values():
        if isinstance(value, str):
            _enforce_input_safety(
                value,
                context_hint=SAFETY_CONTEXT_CHECKPOINT_TEXT_EDIT,
            )


DOMAIN_CACHE_TTL_SECONDS = 60.0


class HealthResponse(BaseModel):
    status: str


class VersionResponse(BaseModel):
    git_sha: str
    image_tag: str
    alembic_head: str


SUPPORTED_LANGUAGES = ("en", "zh", "ja")


class ProjectCreateRequest(BaseModel):
    title: str = Field("Untitled Project", min_length=1)
    domain_id: str = Field("financial_history", min_length=1)
    target_journal: str | None = None
    language: str = Field("en", min_length=2, max_length=8)

    @validator("language")
    def _language_must_be_supported(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"language must be one of {SUPPORTED_LANGUAGES}, got {value!r}",
            )
        return cleaned


class DomainSummary(BaseModel):
    id: str
    display_name: str
    version: str
    description: str
    target_journals: list[str]


DomainCacheEntry = tuple[float, Path, dict[str, LoadedDomain], list[DomainSummary]]
_DOMAIN_CACHE: DomainCacheEntry | None = None


class ProjectResponse(BaseModel):
    id: str
    user_id: str
    title: str
    domain_id: str
    domain_version: str
    target_journal: str | None
    language: str
    status: str
    deleted_at: str | None = None


class RunEventResponse(BaseModel):
    id: str
    run_id: str
    event_type: str
    payload: dict[str, object]
    created_at: str


class ActivePhaseLockResponse(BaseModel):
    phase: str
    job_id: str | None = None
    claimed_at: str | None = None


class ForceApproveResponse(BaseModel):
    """Stage 3.E follow-up: precomputed force-approve hint.

    Frontend reads this from RunResponse to decide whether to render
    the "Force approve and continue" button + which consequence
    string to show in the confirm modal. The mapping logic lives
    server-side (codex amendment) so frontend doesn't duplicate it.
    """

    applicable: bool
    target_state: str | None = None
    consequence: str | None = None
    blockers_to_resolve: int = 0


class ForceApproveRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=1000)


class RunResponse(BaseModel):
    id: str
    project_id: str
    project_title: str
    project_language: str
    state: str
    mode: str
    domain_id: str
    domain_version: str
    created_at: str
    updated_at: str
    last_event: RunEventResponse | None = None
    deleted_at: str | None = None
    project_deleted_at: str | None = None
    stale_from_phase: str | None = None
    # Stage 3.E follow-up P0: surface the phase-start lock so the
    # UI can render a "phase X is already running, claimed N min
    # ago" message and offer a manual clear if it's stuck.
    active_phase_lock: ActivePhaseLockResponse | None = None
    # Stage 3.E follow-up: precomputed force-approve hint for the
    # FailureResolutionBanner. None when the run is in a state
    # where force-approve doesn't apply.
    force_approve: ForceApproveResponse | None = None
    # PR-C0: research-kernel intake gate state. The hash is the
    # concurrency token consumed by PUT /api/runs/{id}/research_kernel
    # (codex round-3 amendment 1: hash alone is insufficient; full
    # paper_mode + kernel must also be exposed for reload-safe
    # editing).
    paper_mode: str = "case_analysis"
    research_kernel: dict[str, object] = Field(
        default_factory=lambda: {"kernel_schema_version": 1},
    )
    research_kernel_hash: str = ""
    proposal_version: int = 0
    # PR-366 (2026-05-13): per-run "数理增强模式" toggle. When true
    # the rewriter/critic round-0 holistic pass (stage A→B→C via
    # gpt-5.5) runs; default false keeps the cheap ~14 min path.
    # Frontend renders a checkbox on the new-run wizard and on the
    # workspace style subview; PATCH /api/runs/{id}/settings flips
    # it mid-run as long as the rewriter is not actively running.
    mathematical_mode: bool = False
    # PR-382: one-click full-auto pilot. Same wiring as
    # ``mathematical_mode`` — wizard checkbox + workspace toggle +
    # PATCH endpoint. When true the coordinator auto-advances every
    # ``USER_*_REVIEW`` gate; ``FAILED_*`` states still need user.
    auto_advance: bool = False


class GenerationModeOptionResponse(BaseModel):
    id: str
    label: str


class GenerationModesResponse(BaseModel):
    default_mode: str
    modes: list[GenerationModeOptionResponse]


class KernelSuggestRequest(BaseModel):
    title: str = Field(..., min_length=4, max_length=500)
    domain_id: str = Field(..., min_length=1, max_length=100)
    language: str = Field("zh", min_length=2, max_length=8)

    @validator("title")
    def _title_must_be_specific(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 4:
            raise ValueError("title must be at least 4 characters")
        return cleaned

    @validator("language")
    def _language_must_be_supported(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"language must be one of {SUPPORTED_LANGUAGES}, got {value!r}",
            )
        return cleaned


class KernelSuggestionPayload(BaseModel):
    observed_puzzle: str
    tentative_question: str
    scope: str = Field(..., max_length=KERNEL_SUGGEST_SCOPE_MAX_CHARS)
    method_preference: str
    theory_preference: str


class KernelSuggestResponse(BaseModel):
    suggestion: KernelSuggestionPayload
    model: str
    max_tokens: int


class ExpressTransparencyResponse(BaseModel):
    run_id: str
    mode: str
    state: str
    provider: str | None = None
    provider_model: str | None = None
    token_cap: int | None = None
    token_usage: dict[str, object] = Field(default_factory=dict)
    prompt_summary: dict[str, object] = Field(default_factory=dict)
    prompt_excerpt: str | None = None
    provenance: dict[str, object] = Field(default_factory=dict)
    audit_summary: dict[str, object] = Field(default_factory=dict)
    outline: list[dict[str, object]] = Field(default_factory=list)
    manuscript_preview: str | None = None
    failure: dict[str, object] | None = None


class TransitionRequest(BaseModel):
    to_state: str = Field(..., min_length=1)
    reason: str | None = None


def _requires_dedicated_failure_recovery(from_state: str, to_state: str) -> bool:
    return from_state.startswith("FAILED_") and to_state.startswith("USER_")


class CheckpointDecisionRequest(BaseModel):
    status: str | None = Field(None, min_length=1)
    decision_payload: dict[str, object] = Field(default_factory=dict)
    selected_angle_id: str | None = None
    edits: dict[str, object] | None = None
    approve: bool | None = None
    accept: bool | None = None
    scan_kinds: list[str] = Field(default_factory=list)
    skip_reason: str | None = None
    span_decisions: list[dict[str, object]] = Field(default_factory=list)
    next_revision_dimension: str | None = None
    export_formats: list[str] = Field(default_factory=list)


SOURCE_REVIEW_CHECKPOINT_SCOPES: dict[str, str] = {
    "USER_SEARCH_REVIEW": "search_review",
    "USER_DEEP_DIVE_REVIEW": "deep_dive_review",
}
SOURCE_REVIEW_SOURCE_ID_KEYS = (
    "source_ids",
    "approved_source_ids",
    "approved_sources",
    "approved",
    "deep_dive_source_ids",
    "selected_source_ids",
    "selection",
)


class CheckpointResponse(BaseModel):
    id: str
    run_id: str
    checkpoint_type: str
    status: str
    decision_payload: dict[str, object]
    created_at: str
    decided_at: str | None


class ProposalDraftRequest(BaseModel):
    user_draft: str | None = None


class ProposalSaveRequest(BaseModel):
    proposal_json: dict[str, object]
    # Optional concurrency token: client echoes back the version it
    # was viewing; server rejects (409) if the head moved underneath.
    # Optional for backwards compat; new clients should always send.
    # Codex AGREE 2026-05-01 amendment 6.
    base_version: int | None = None
    # ``"new"`` (default, current behavior) bumps proposal_version and
    # writes proposal_v<N+1>.json. ``"replace"`` overwrites the current
    # proposal_v<N>.json without bumping; only allowed when no pipeline
    # phase has produced output on the active branch (codex
    # AGREE-with-amendments 2026-05-01).
    mode: str | None = Field(default="new")

    @validator("mode")
    def _mode_must_be_known(cls, value: str | None) -> str:
        cleaned = (value or "new").strip().lower()
        if cleaned not in {"new", "replace"}:
            raise ValueError("mode must be one of: new, replace")
        return cleaned


class ProposalJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class ProposalResponse(BaseModel):
    run_id: str
    version: int
    proposal_json: dict[str, object]
    markdown: str
    path: str


class ScoutJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class CuratorJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class SynthesizerJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class IdeatorJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class DrafterJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class StylistJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class CriticJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class IntegrityJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class ExportsJobResponse(BaseModel):
    run_id: str
    job_id: str
    expected_state: str


class DiscoveryResponse(BaseModel):
    run_id: str
    skim_candidates: list[dict[str, object]]
    scout_report: str


class SourcesResponse(BaseModel):
    run_id: str
    shortlist: list[object]
    fulltext_manifest: dict[str, object]
    manual_upload_requests: list[dict[str, object]]
    curation_report: str
    skim_candidates: list[dict[str, object]]
    source_quality_counts: dict[str, int] = Field(default_factory=dict)


class SourceUploadResponse(BaseModel):
    run_id: str
    source_id: str
    manifest_entry: dict[str, object]
    shortlist_entry: dict[str, object]


class DualTrackPayload(BaseModel):
    """PR-C1.a dual-track artifact, surfaced by C1.b. Mirrors
    ``synthesis/synthesizer.json``. ``None`` for legacy runs that
    never produced the artifact (synthesizer ran before C1.a)."""

    schema_version: int = 1
    primary_track: list[dict[str, object]] = Field(default_factory=list)
    secondary_track: list[dict[str, object]] = Field(default_factory=list)
    theoretical_lens_track: list[dict[str, object]] = Field(default_factory=list)
    methodological_track: list[dict[str, object]] = Field(default_factory=list)
    tension_summary_ref: str | None = None
    framework_lens_summary_ref: str | None = None


class SynthesisResponse(BaseModel):
    run_id: str
    claims: list[dict[str, object]]
    source_notes: dict[str, object]
    synthesizer_report: str
    material_diagnostic: dict[str, object] | None = None
    material_diagnostic_md: str = ""
    # PR-C1.b: server-translated dual-track view. None when the
    # synthesizer.json artifact does not exist (pre-C1.a runs).
    dual_track: DualTrackPayload | None = None


class NoveltyResponse(BaseModel):
    run_id: str
    angle_cards: list[object]
    ideator_report: str
    selected_thesis: dict[str, object] | None
    detailed_outlines: list[dict[str, object]] = []
    detailed_outlines_md: str = ""


class DraftsResponse(BaseModel):
    run_id: str
    drafts: list[dict[str, object]]


class DraftResponse(BaseModel):
    run_id: str
    version: str
    metadata: dict[str, object]
    manuscript: str
    claim_map: list[dict[str, object]]
    citations_bib: str
    draft_rationale: str


class StyleResponse(BaseModel):
    run_id: str
    version: str
    paper_styled: str
    style_delta: str
    stop_slop_score: dict[str, object]
    n_gram_violations: list[object] | None = None


class CriticResponse(BaseModel):
    run_id: str
    critic_report: str
    claim_audit: list[dict[str, object]]
    revision_plan: str
    blocking_issues: dict[str, object]


class IntegrityResponse(BaseModel):
    run_id: str
    plagiarism_report: str
    ai_style_report: str
    integrity_summary: dict[str, object]


class ExportsResponse(BaseModel):
    run_id: str
    manifest: dict[str, object]
    files: list[dict[str, object]]


class CorpusDocumentResponse(BaseModel):
    id: str
    title: str
    document_type: str
    ingest_status: str
    original_size_bytes: int | None
    created_at: str


class CorpusUploadResponse(BaseModel):
    document: CorpusDocumentResponse
    task_id: str


class CorpusStyleProfileRebuildResponse(BaseModel):
    task_id: str


class CorpusPreviewResponse(BaseModel):
    id: str
    max_chars: int
    preview: str


class NoveltyDiscussionMessageResponse(BaseModel):
    id: str
    run_id: str
    role: str
    content: str
    generation_token: int
    created_at: str


class NoveltyDiscussRequest(BaseModel):
    user_message: str = Field(..., min_length=1)


class NoveltyDiscussResponse(BaseModel):
    run_id: str
    angle_cards: list[object]
    user_message: NoveltyDiscussionMessageResponse
    assistant_message: NoveltyDiscussionMessageResponse


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/api/health", response_model=HealthResponse)
def api_health() -> HealthResponse:
    return healthz()


@app.get("/readyz", response_model=HealthResponse)
def readyz() -> HealthResponse:
    try:
        check_database()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database is not ready",
        ) from exc
    return HealthResponse(status="ready")


@app.get("/version", response_model=VersionResponse)
def version() -> VersionResponse:
    settings = get_settings()
    return VersionResponse(
        git_sha=settings.git_sha,
        image_tag=settings.image_tag,
        alembic_head=_alembic_head(),
    )


@app.get("/api/domains", response_model=list[DomainSummary])
def list_domains(_user: CurrentUserDependency) -> list[DomainSummary]:
    """List all domain YAMLs the server has loaded."""
    return load_all_domain_summaries()


@app.get("/api/paper_modes")
def list_paper_modes(_user: CurrentUserDependency) -> dict[str, object]:
    """PR-C0: serve the paper-mode registry.

    Returns ``{registry_version, default_mode_id, modes: [...]}`` per
    ``paper_modes.serialize_for_api``. Modes have a ``status`` field
    (``available`` / ``developer_preview`` / ``coming_soon``) so the
    frontend wizard knows which to grey out vs offer.

    Cached at frontend init; backend re-evaluates only on process
    restart (registry is hardcoded Python).
    """
    from autoessay.paper_modes import serialize_for_api

    return serialize_for_api()


@app.get("/api/generation_modes", response_model=GenerationModesResponse)
def list_generation_modes(_user: CurrentUserDependency) -> GenerationModesResponse:
    settings = get_settings()
    return GenerationModesResponse(
        default_mode=settings.manuscript_default_mode,
        modes=[
            GenerationModeOptionResponse(id=EXPRESS_MODE, label="Express"),
            GenerationModeOptionResponse(id=DEEP_MODE, label="Deep"),
        ],
    )


@app.post("/api/runs/kernel_suggest", response_model=KernelSuggestResponse)
async def suggest_research_kernel(
    request: KernelSuggestRequest,
    _user: CurrentUserDependency,
) -> KernelSuggestResponse:
    """Suggest the five high-friction research-kernel fields.

    This endpoint is intentionally non-mutating: it does not create a
    project/run and does not write ``runs.research_kernel_json``. The
    NewRunPage remains the owner of when suggested text is applied to the
    editable form.
    """
    # ``_enforce_input_safety`` (and the kernel-safety variant below) run
    # the safety LLM via ``asyncio.run`` internally, which raises from
    # within FastAPI's running event loop. Hop to a threadpool so the
    # sync code path keeps working without ripping up the safety module.
    await run_in_threadpool(
        _enforce_input_safety,
        request.title,
        context_hint=SAFETY_CONTEXT_PROJECT_TITLE,
    )
    domain = _load_domain_for_request(request.domain_id)
    settings = get_settings()
    try:
        suggestion = await suggest_kernel(
            title=request.title,
            domain=domain,
            language=cast(SuggestionLanguage, request.language),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "kernel_suggest_failed",
                "user_facing_reason": ("Research-kernel suggestion failed. Please try again."),
            },
        ) from exc
    await run_in_threadpool(_enforce_research_kernel_safety, suggestion.as_kernel())
    return KernelSuggestResponse(
        suggestion=_kernel_suggestion_payload(suggestion),
        model=settings.kernel_suggest_model,
        max_tokens=min(int(settings.kernel_suggest_max_tokens), 3000),
    )


def _kernel_suggestion_payload(suggestion: KernelSuggestion) -> KernelSuggestionPayload:
    return KernelSuggestionPayload(
        observed_puzzle=suggestion.observed_puzzle,
        tentative_question=suggestion.tentative_question,
        scope=suggestion.scope,
        method_preference=suggestion.method_preference,
        theory_preference=suggestion.theory_preference,
    )


@app.get("/api/corpus", response_model=list[CorpusDocumentResponse])
def list_corpus_documents(
    session: SessionDependency,
    user: CurrentUserDependency,
) -> list[CorpusDocumentResponse]:
    return [
        _corpus_document_response(document) for document in list_user_documents(session, user.id)
    ]


@app.post(
    "/api/corpus/upload",
    response_model=CorpusUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_corpus_document(
    session: SessionDependency,
    user: CurrentUserDependency,
    file: Annotated[UploadFile, File()],
) -> CorpusUploadResponse:
    payload = await file.read()
    try:
        document = create_corpus_document(
            session,
            user,
            filename=file.filename or "prior-paper",
            content_type=file.content_type,
            payload=payload,
        )
    except CorpusUploadError as exc:
        code = (
            status.HTTP_413_CONTENT_TOO_LARGE
            if "30 MB" in str(exc)
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    session.commit()
    session.refresh(document)
    settings = get_settings()
    if settings.sync_worker:
        run_corpus_ingest_job(document.id, session)
        task_id = "sync"
        session.refresh(document)
    else:
        task_id = enqueue_corpus_ingest_job(document.id)
    return CorpusUploadResponse(
        document=_corpus_document_response(document),
        task_id=task_id,
    )


@app.post(
    "/api/corpus/style-profile/rebuild",
    response_model=CorpusStyleProfileRebuildResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def rebuild_corpus_style_profile(
    session: SessionDependency,
    user: CurrentUserDependency,
) -> CorpusStyleProfileRebuildResponse:
    settings = get_settings()
    if settings.sync_worker:
        run_corpus_style_profile_job(user.id, session)
        return CorpusStyleProfileRebuildResponse(task_id="sync")
    return CorpusStyleProfileRebuildResponse(task_id=enqueue_corpus_style_profile_job(user.id))


@app.get("/api/corpus/style-profile")
def get_corpus_style_profile(user: CurrentUserDependency) -> dict[str, object]:
    path = style_profile_path_for_user(user.id)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="style profile not found",
        )
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="style profile is invalid",
        )
    return {key: value for key, value in decoded.items() if isinstance(key, str)}


@app.get("/api/corpus/{document_id}/preview", response_model=CorpusPreviewResponse)
def preview_corpus_document(
    document_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
    max_chars: int = 200,
) -> CorpusPreviewResponse:
    document = get_user_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    capped = min(max(max_chars, 0), 200)
    return CorpusPreviewResponse(
        id=document.id,
        max_chars=capped,
        preview=preview_document_text(document, capped),
    )


@app.delete("/api/corpus/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_corpus_document(
    document_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> None:
    document = get_user_document(session, user.id, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="document not found")
    delete_document_files(document)
    session.delete(document)
    session.commit()
    return None


@app.post(
    "/api/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    request: ProjectCreateRequest,
    http_request: Request,
    session: SessionDependency,
) -> Any:
    _enforce_input_safety(request.title, context_hint=SAFETY_CONTEXT_PROJECT_TITLE)
    _enforce_input_safety(request.target_journal, context_hint=SAFETY_CONTEXT_PROJECT_INTRO)
    domain = _load_domain_for_request(request.domain_id)
    request_user = getattr(http_request.state, "current_user", None)
    user_id = request_user.id if isinstance(request_user, User) else "single-user"
    user = session.get(User, user_id)
    if user is None:
        session.add(User(id=user_id, display_name="Single User"))
        session.flush()
    _lock_user_for_essay_limit(session, user_id)
    active_count = _count_active_essays(session, user_id)
    if active_count >= ACTIVE_ESSAY_LIMIT_PER_USER:
        return _essay_limit_response(active_count)
    session.merge(
        Domain(
            id=request.domain_id,
            display_name=str(domain.data["display_name"]),
            version=str(domain.data["version"]),
            config_path=str(domain.path),
            enabled=True,
        ),
    )
    session.flush()
    project = Project(
        id=f"proj_{uuid4().hex}",
        user_id=user_id,
        title=request.title,
        domain_id=request.domain_id,
        domain_version=str(domain.data["version"]),
        target_journal=request.target_journal,
        language=request.language,
        status="CREATED",
    )
    session.add(project)
    session.flush()
    # Auto-include every existing enabled global corpus of this user
    # in the new project's selection (PR-B1, codex amendment 1: the
    # selection model is explicit, but defaults to "include all
    # current globals" so users with prior corpora don't see their
    # style profile silently empty out for new projects).
    from autoessay.models import Corpus, ProjectCorpusSelection

    for corpus_id in session.scalars(
        select(Corpus.id).where(
            Corpus.owner_user_id == user_id,
            Corpus.project_id.is_(None),
            Corpus.enabled.is_(True),
        ),
    ):
        session.add(
            ProjectCorpusSelection(project_id=project.id, corpus_id=corpus_id),
        )
    session.commit()
    session.refresh(project)
    return _project_response(project)


@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    session: SessionDependency,
) -> ProjectResponse:
    project = session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return _project_response(project)


class ProjectPatchRequest(BaseModel):
    language: str | None = Field(None, min_length=2, max_length=8)
    target_journal: str | None = None
    title: str | None = Field(None, min_length=1, max_length=500)

    @validator("language")
    def _language_must_be_supported(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if cleaned not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"language must be one of {SUPPORTED_LANGUAGES}, got {value!r}",
            )
        return cleaned

    @validator("title")
    def _title_must_be_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title must not be empty")
        return cleaned


@app.patch("/api/projects/{project_id}", response_model=ProjectResponse)
def patch_project(
    project_id: str,
    request: ProjectPatchRequest,
    session: SessionDependency,
) -> ProjectResponse:
    """Update editable fields on a project after creation.

    Currently allows changing ``language`` (the paper output language
    used by Drafter / Stylist / Critic / Exporter for any subsequent
    runs) and ``target_journal``. Existing run artifacts are not
    rewritten — the new language only affects later phase runs.
    """
    project = session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    _assert_project_active(project)
    changed = False
    if request.language is not None and request.language != project.language:
        project.language = request.language
        changed = True
    if request.target_journal is not None and request.target_journal != project.target_journal:
        # Run safety check on the new target_journal value just like at create.
        _enforce_input_safety(request.target_journal, context_hint=SAFETY_CONTEXT_PROJECT_INTRO)
        project.target_journal = request.target_journal
        changed = True
    if request.title is not None and request.title != project.title:
        # Run safety check just like create_project does (line 773), so
        # title edits cannot smuggle URLs / control characters that the
        # creation path would have rejected. Title is metadata: codex
        # AGREE 2026-05-01 amendment 5 — no stale propagation. Mirrors
        # the existing language editor convention.
        _enforce_input_safety(request.title, context_hint=SAFETY_CONTEXT_PROJECT_TITLE)
        project.title = request.title
        changed = True
    if changed:
        session.add(project)
        session.commit()
        session.refresh(project)
    return _project_response(project)


class CreateRunRequest(BaseModel):
    # ADR-0003: generation architecture mode. ``None`` means use the
    # MANUSCRIPT_DEFAULT_MODE server flag and persist that concrete
    # value immediately; there is no runtime fallback between modes.
    mode: GenerationMode | None = None
    # PR-366: optional opt-in for round-0 holistic rewrite (gpt-5.5,
    # +20-30 min, ~10x token cost). Old clients omit this field and
    # land on the default cheap path.
    # PR-368 P2-3 (codex review): ``None`` (the default) means "inherit
    # from the latest non-deleted run on this project, fall back to
    # False if there is no prior run". Explicit ``True`` / ``False``
    # always wins. This stops users from being surprised that their
    # checkbox didn't carry over to a re-run on the same project.
    mathematical_mode: bool | None = None
    # PR-382: one-click full-auto. ``None`` inherits from prior run
    # (same shape as ``mathematical_mode``); explicit value wins.
    auto_advance: bool | None = None

    @validator("mode")
    def _mode_must_be_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_generation_mode(value)


@app.post(
    "/api/projects/{project_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_run(
    project_id: str,
    session: SessionDependency,
    request: CreateRunRequest | None = None,
) -> Any:
    project = session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    _assert_project_active(project)
    # Re-running a project that ended in a LIMIT_TERMINAL state (e.g.
    # EXPORTS_DONE → user wants to redo the paper) re-activates an
    # essay slot. Block if the user is already at the active-essay
    # limit. Projects that already have a non-terminal latest run, or
    # no run yet, are already active and don't trigger this gate.
    _lock_user_for_essay_limit(session, project.user_id)
    if not _project_currently_active(session, project):
        active_count = _count_active_essays(session, project.user_id)
        if active_count >= ACTIVE_ESSAY_LIMIT_PER_USER:
            return _essay_limit_response(active_count)

    run_id = f"run_{uuid4().hex}"
    settings = get_settings()
    generation_mode = (
        request.mode
        if request is not None and request.mode is not None
        else settings.manuscript_default_mode
    )
    if generation_mode == EXPRESS_MODE and request is not None and request.auto_advance is True:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="auto_advance is only available for mode=deep",
        )
    run_dir = create_run_directory(
        settings.data_dir / "runs",
        run_id,
        project.id,
        state="TOPIC_ENTERED",
        domain_id=project.domain_id,
    )
    # PR-368 P2-3: when the caller omits ``mathematical_mode`` (or
    # passes ``null``), inherit from the latest non-deleted run on this
    # project so a re-run keeps the user's checkbox choice. Explicit
    # True / False always wins.
    if request is not None and request.mathematical_mode is not None:
        mathematical_mode = bool(request.mathematical_mode)
    else:
        prior_mode = session.scalar(
            select(Run.mathematical_mode)
            .where(Run.project_id == project.id, Run.deleted_at.is_(None))
            .order_by(Run.created_at.desc())
            .limit(1),
        )
        mathematical_mode = bool(prior_mode) if prior_mode is not None else False
    # PR-382: same inherit-or-explicit semantics for auto_advance.
    # ADR-0003: express does not use the review-gate state machine, so
    # auto_advance must be false instead of enabled-but-ignored.
    if generation_mode == EXPRESS_MODE:
        auto_advance = False
    elif request is not None and request.auto_advance is not None:
        auto_advance = bool(request.auto_advance)
    else:
        prior_auto = session.scalar(
            select(Run.auto_advance)
            .where(Run.project_id == project.id, Run.deleted_at.is_(None))
            .order_by(Run.created_at.desc())
            .limit(1),
        )
        auto_advance = bool(prior_auto) if prior_auto is not None else False
    run = Run(
        id=run_id,
        project_id=project.id,
        domain_version=project.domain_version,
        run_dir=str(run_dir),
        state="TOPIC_ENTERED",
        baseline_hash="pending",
        mathematical_mode=mathematical_mode,
        auto_advance=auto_advance,
        generation_mode=generation_mode,
    )
    session.add(run)
    session.flush()
    # Every run gets a "main" branch created up-front (codex-AGREEd
    # #2 stage 2.C). Stage 2.A/2.B endpoints implicitly assume a
    # branch is in place; without this, `run_heads` and
    # `phase_prompt_drafts` rows would have no branch_id to point at.
    from autoessay.branches import ensure_main_branch

    ensure_main_branch(session, run)
    # PR-368 P2-1 (codex review): record the initial mathematical_mode
    # choice so post-mortems can see what the user opted into at
    # creation time (vs PATCHing it in later).
    # PR-382: also record auto_advance for the same audit reason.
    append_event(
        session,
        run,
        "run_created",
        {
            "run_id": run.id,
            "project_id": project.id,
            "state": run.state,
            "domain_version": run.domain_version,
            "mode": generation_mode,
            "mathematical_mode": mathematical_mode,
            "auto_advance": auto_advance,
        },
    )
    # TOPIC_ENTERED is a transient init state — the workspace UI has nothing
    # for it to do, so advance the run to DOMAIN_LOADED before returning.
    # That way the frontend lands on a state with a real next-step button.
    transition(run, "DOMAIN_LOADED", session, reason="run_created")
    session.commit()
    session.refresh(run)
    # PR-386: fire the auto-pilot coordinator at run-create time so
    # ``auto_advance=true`` actually kicks off the proposal phase
    # instead of stranding the run at DOMAIN_LOADED. PR-382 only
    # covered ``USER_*_REVIEW`` gates; the fresh-run kickoff was the
    # missing piece. ``maybe_advance`` is a no-op when the toggle is
    # off and never raises.
    if auto_advance:
        from autoessay.auto_advance import maybe_advance

        maybe_advance(session, run, source="run_created")
        session.refresh(run)
    return _run_response(session, run)


_TITLE_SEARCH_MAX_LEN = 200


def _title_like_clause(q: str | None) -> Any:
    """Return an SQLAlchemy WHERE fragment for case-insensitive substring
    search on ``Project.title``, or ``None`` if ``q`` is empty.

    LIKE wildcards typed by the user (``%``, ``_``, ``\\``) are escaped
    so they match literally — typing ``50%`` searches for the literal
    "50%" in titles, not "anything containing 50".
    """
    if q is None:
        return None
    cleaned = q.strip()
    if not cleaned:
        return None
    escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    from sqlalchemy import func

    return func.lower(Project.title).like(func.lower(pattern), escape="\\")


@app.get("/api/runs", response_model=list[RunResponse])
def list_runs(
    session: SessionDependency,
    user: CurrentUserDependency,
    q: Annotated[
        str | None,
        Query(max_length=_TITLE_SEARCH_MAX_LEN, description="essay title filter"),
    ] = None,
    include_deleted: bool = False,
) -> list[RunResponse]:
    """List runs the current user owns, newest first.

    ``?q=<term>`` filters by case-insensitive substring on the owning
    project's title; ``?include_deleted=1`` surfaces runs whose essay
    was soft-deleted.
    """
    stmt = select(Run).join(Project, Run.project_id == Project.id).where(Project.user_id == user.id)
    if not include_deleted:
        stmt = stmt.where(Project.deleted_at.is_(None), Run.deleted_at.is_(None))
    title_clause = _title_like_clause(q)
    if title_clause is not None:
        stmt = stmt.where(title_clause)
    rows = session.scalars(stmt.order_by(Run.updated_at.desc(), Run.created_at.desc())).all()
    return [_run_response(session, run) for run in rows]


@app.get("/api/projects", response_model=list[ProjectResponse])
def list_projects(
    session: SessionDependency,
    user: CurrentUserDependency,
    q: Annotated[
        str | None,
        Query(max_length=_TITLE_SEARCH_MAX_LEN, description="title substring filter"),
    ] = None,
    include_deleted: bool = False,
) -> list[ProjectResponse]:
    """List projects the current user owns, newest first.

    ``?q=<term>`` filters by case-insensitive substring on title;
    ``?include_deleted=1`` surfaces soft-deleted essays for the
    user to restore or inspect.
    """
    stmt = select(Project).where(Project.user_id == user.id)
    if not include_deleted:
        stmt = stmt.where(Project.deleted_at.is_(None))
    title_clause = _title_like_clause(q)
    if title_clause is not None:
        stmt = stmt.where(title_clause)
    rows = session.scalars(stmt.order_by(Project.created_at.desc())).all()
    return [_project_response(project) for project in rows]


@app.delete(
    "/api/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_project(
    project_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> Response:
    """Soft-delete an essay.

    Idempotent — re-deleting an already-deleted project is a 204
    no-op. The same transaction stamps ``cancel_requested_at`` on
    every unfinished run for the project so workers abort cleanly
    on their next checkpoint instead of writing more artifacts.
    Cancel intent is **not** cleared on restore (per design): the
    user must trigger the next phase manually after restoring,
    which creates a fresh run-state with no cancel intent in scope.
    """
    project = _get_project_or_404(session, project_id)
    if project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if project.deleted_at is not None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    now = utcnow()
    project.deleted_at = now
    # Stamp cancel intent on every run that is not yet in a terminal
    # state. Terminal states never produce more artifact writes, so
    # they don't need a cancel marker.
    terminal = (
        "EXPRESS_DONE",
        "EXPORTS_DONE",
        "FAILED_FATAL",
        "FAILED_VENDOR",
        "FAILED_POLICY",
    )
    runs = session.scalars(
        select(Run).where(
            Run.project_id == project.id,
            Run.cancel_requested_at.is_(None),
            Run.deleted_at.is_(None),
            ~Run.state.in_(terminal),
        ),
    ).all()
    for run in runs:
        run.cancel_requested_at = now
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/projects/{project_id}/restore",
    response_model=ProjectResponse,
)
def restore_project(
    project_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> Any:
    """Undo a soft-delete.

    Returns 409 if the project is not currently deleted (so the
    caller doesn't think a no-op succeeded by mistake). For runs
    whose state at delete time was a non-running USER_*_REVIEW
    waiting state, the lingering ``cancel_requested_at`` (set by
    ``delete_project``) is also cleared — without this, the next
    phase trigger after restore would see the residual cancel
    intent and silently transition the run to ``CANCELLED`` even
    though the user re-activated the project. Runs that were
    actually mid-flight (state in ``RUNNING_STATES``) keep their
    cancel intent; if the user truly wants to resume those, they
    have to explicitly clear-cancel via ``clear_run_cancel_intent``.

    Restoring re-activates the essay slot iff the project was active
    at the time of deletion (i.e. its latest run was non-terminal, or
    it had no run). If the active-essay limit is already reached, the
    restore is blocked with the standard ``essay_limit`` 409.
    """
    from autoessay.phase_rerun import RUNNING_STATES
    from autoessay.state_machine import append_event

    project = _get_project_or_404(session, project_id)
    if project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if project.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="project is not deleted")
    locked_run = session.scalar(
        select(Run)
        .where(
            Run.project_id == project.id,
            Run.deleted_at.is_(None),
            Run.active_phase_lock.is_not(None),
        )
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1),
    )
    if locked_run is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_restore_active_phase_lock_detail(locked_run, scope="project"),
        )
    _lock_user_for_essay_limit(session, project.user_id)
    # Determine whether restoring will consume a slot. Latest run state
    # decides:
    #   - latest in LIMIT_TERMINAL → restored project stays inactive; OK
    #   - no run / latest non-terminal → restored project becomes active;
    #     gate against the limit
    latest_state = session.scalar(
        select(Run.state)
        .where(Run.project_id == project.id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1),
    )
    will_become_active = latest_state is None or latest_state not in LIMIT_TERMINAL_STATES
    if will_become_active:
        active_count = _count_active_essays(session, project.user_id)
        if active_count >= ACTIVE_ESSAY_LIMIT_PER_USER:
            return _essay_limit_response(active_count)
    project.deleted_at = None
    # Clear residual cancel_requested_at on runs that were paused at
    # a USER_*_REVIEW waiting state when delete fired. Mid-flight
    # runs keep their cancel marker — those are recovered (if at all)
    # via the explicit ``/api/runs/{id}/clear-cancel-intent`` endpoint.
    runs_with_cancel = session.scalars(
        select(Run).where(
            Run.project_id == project.id,
            Run.cancel_requested_at.is_not(None),
            Run.deleted_at.is_(None),
        ),
    ).all()
    for r in runs_with_cancel:
        recovery_warning = _restore_recovery_warning_for_late_phase_done(
            session,
            r,
            r.cancel_requested_at,
        )
        if r.state not in RUNNING_STATES and r.state != "CANCELLED":
            r.cancel_requested_at = None
        if recovery_warning is not None:
            append_event(session, r, "run_restore_recovery_warning", recovery_warning)
    session.commit()
    session.refresh(project)
    return _project_response(project)


@app.post(
    "/api/runs/{run_id}/clear-cancel-intent",
    response_model=RunResponse,
)
def clear_run_cancel_intent(
    run_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> Any:
    """Recover a run that was transitioned to ``CANCELLED`` by a
    residual ``cancel_requested_at`` after a delete-then-restore
    cycle. Reverts the run to the state it was in immediately before
    the cancel transition fired and clears the cancel intent.

    Codex AGREE-WITH-AMENDMENTS (2026-05-07): the revert target is
    the ``from_state`` of the most recent ``state_transition`` whose
    ``to_state`` is ``CANCELLED`` — that's literally the state the
    cancel covered up. The same ``from_state`` is also the RUNNING
    guard subject: if the cancel hit a mid-flight phase
    (``from_state in RUNNING_STATES``), refuse — that was a real
    interrupt and recovery would resume a half-written artifact.

    Strict refusal cases (all 409):
    - state != CANCELLED
    - project deleted
    - cancel_requested_at IS NULL (no intent to clear)
    - no usable ``state_transition`` event with ``to_state=CANCELLED``
    - that transition's ``from_state`` is missing / unknown
    - that transition's ``from_state`` is in ``RUNNING_STATES``

    State machine note: ``ALLOWED_TRANSITIONS["CANCELLED"]`` is empty
    (CANCELLED is terminal in the normal flow). This endpoint mutates
    state directly via ``run.state = revert_state`` and records a
    ``run_uncancelled`` event manually, bypassing ``transition()``.
    No other transitions to CANCELLED-source states are added — the
    one-shot recovery here doesn't generalize CANCELLED into a hub.
    """
    from autoessay.phase_rerun import RUNNING_STATES
    from autoessay.state_machine import append_event

    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    if run.state != "CANCELLED":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is not cancelled",
        )
    project = _get_project_or_404(session, run.project_id)
    if project.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project is deleted; restore the project first",
        )
    if run.cancel_requested_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no cancel intent to clear",
        )
    # Find the most recent state_transition with to_state=CANCELLED.
    # Scan most-recent-first via id desc.
    rows = session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.event_type == "state_transition")
        .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
        .limit(50),
    ).all()
    cancel_from_state: str | None = None
    for ev in rows:
        try:
            payload = json.loads(ev.payload) if isinstance(ev.payload, str) else (ev.payload or {})
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("to_state") == "CANCELLED":
            from_state = payload.get("from_state")
            if isinstance(from_state, str) and from_state:
                cancel_from_state = from_state
                break
    if cancel_from_state is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no usable state_transition event found to determine revert target",
        )
    if cancel_from_state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"refusing to recover: cancel covered a running phase "
                f"({cancel_from_state}); artifacts may be half-written"
            ),
        )
    # Optional sanity: revert target should be a known pipeline state.
    from autoessay.state_machine import PIPELINE_STATES

    if cancel_from_state not in PIPELINE_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"revert target {cancel_from_state!r} is not a known pipeline state",
        )
    prior_cancel_at = run.cancel_requested_at
    run.state = cancel_from_state
    run.cancel_requested_at = None
    run.updated_at = utcnow()
    append_event(
        session,
        run,
        "run_uncancelled",
        {
            "from_state": "CANCELLED",
            "to_state": cancel_from_state,
            "cleared_cancel_requested_at": (
                prior_cancel_at.isoformat() if prior_cancel_at is not None else None
            ),
            "reason": "residual cancel cleared after delete-then-restore",
        },
    )
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


@app.get("/api/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, session: SessionDependency) -> RunResponse:
    run = _get_run_or_404(session, run_id)
    return _run_response(session, run)


class UpdateRunSettingsRequest(BaseModel):
    # PR-366: per-run execution-policy patch. Currently only ships the
    # "数理增强模式" toggle; future flags should land in the same payload
    # so the frontend has a single settings PATCH.
    mathematical_mode: bool | None = None
    # ADR-0003: generation mode is mutable only before generation
    # starts. Once a phase lock is held or the run leaves DOMAIN_LOADED,
    # mode becomes immutable.
    mode: GenerationMode | None = None
    # PR-382: one-click full-auto. Toggling on triggers a coordinator
    # call so the run advances NOW from whatever USER_*_REVIEW state
    # it's in.
    auto_advance: bool | None = None

    class Config:
        # PR-368 P2-2 (codex review): reject unknown keys so client
        # typos like ``{"mathmode": true}`` 422 instead of silently
        # no-opping. Pydantic v1 syntax (this codebase pins 1.10).
        extra = "forbid"

    @validator("mode")
    def _mode_must_be_known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_generation_mode(value)


_MATHEMATICAL_MODE_LOCKED_STATES: frozenset[str] = frozenset(
    {"REWRITE_RUNNING", "CRITIC_RUNNING"},
)
# PR-368 P1-1: PATCH must also refuse while the phase lock is held for
# final_rewrite / critic — start_critic claims the lock BEFORE the RQ
# worker transitions state, so there is a small window where state is
# still USER_REVISION_REVIEW but the rewriter is about to read
# ``run.mathematical_mode``. Flipping the flag in that window produces
# an inconsistent audit trail.
_MATHEMATICAL_MODE_LOCKED_PHASES: frozenset[str] = frozenset(
    {"final_rewrite", "critic"},
)


@app.patch("/api/runs/{run_id}/settings", response_model=RunResponse)
def update_run_settings(
    run_id: str,
    payload: UpdateRunSettingsRequest,
    session: SessionDependency,
) -> RunResponse:
    """PR-366: flip ``mathematical_mode`` (the "数理增强模式" toggle) on a
    run mid-flight.

    Refused while the rewriter or critic is actively running — the
    round-0 holistic pass is decided at the start of those phases, so
    flipping the flag while they're in flight would either be silently
    ignored or produce an inconsistent audit trail.

    PR-368 P1-1 (codex review): also refused while the active phase
    lock is held for ``final_rewrite`` or ``critic``. ``start_critic``
    claims the lock BEFORE the RQ worker transitions state, so without
    this guard there is a race window where state is still
    ``USER_REVISION_REVIEW`` but the rewriter is about to read
    ``run.mathematical_mode``. PR-368 P2-5: switched to
    ``_get_run_for_mutation_or_404`` so soft-deleted runs can't be
    mutated.
    """
    run = _get_run_for_mutation_or_404(session, run_id)
    if payload.mathematical_mode is None and payload.auto_advance is None and payload.mode is None:
        # Nothing to do — just return the current state. Pydantic still
        # validates the shape, and ``extra="forbid"`` means an
        # unrecognised key 422s.
        return _run_response(session, run)
    lock = get_active_phase_lock(run)
    # ---- generation mode flip ----
    if payload.mode is not None:
        new_mode = payload.mode
        prior_mode = _run_generation_mode(run)
        if prior_mode != new_mode:
            if run.state != "DOMAIN_LOADED" or lock is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "mode cannot be changed after generation begins "
                        f"(state={run.state}, active_phase_lock="
                        f"{lock.get('phase') if lock is not None else None})"
                    ),
                )
            if new_mode == EXPRESS_MODE and bool(getattr(run, "auto_advance", False)):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="mode=express cannot be combined with auto_advance=true",
                )
            run.generation_mode = new_mode
            run.updated_at = utcnow()
            append_event(
                session,
                run,
                "run_settings_updated",
                {
                    "field": "mode",
                    "from": prior_mode,
                    "to": new_mode,
                    "state": run.state,
                    "active_phase_lock": None,
                },
            )
    # ---- mathematical_mode flip ----
    if payload.mathematical_mode is not None:
        if run.state in _MATHEMATICAL_MODE_LOCKED_STATES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "cannot change mathematical_mode while rewriter or critic "
                    f"is running (state={run.state})"
                ),
            )
        if lock is not None and lock.get("phase") in _MATHEMATICAL_MODE_LOCKED_PHASES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "cannot change mathematical_mode while phase "
                    f"{lock.get('phase')} is enqueued or running "
                    f"(state={run.state}, lock_phase={lock.get('phase')})"
                ),
            )
        new_value = bool(payload.mathematical_mode)
        prior = bool(getattr(run, "mathematical_mode", False))
        if prior != new_value:
            run.mathematical_mode = new_value
            run.updated_at = utcnow()
            append_event(
                session,
                run,
                "run_settings_updated",
                {
                    "field": "mathematical_mode",
                    "from": prior,
                    "to": new_value,
                    "state": run.state,
                    "active_phase_lock": lock.get("phase") if lock is not None else None,
                },
            )
    # ---- PR-382: auto_advance flip ----
    triggered_advance = False
    if payload.auto_advance is not None:
        if _run_generation_mode(run) == EXPRESS_MODE and payload.auto_advance is True:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="auto_advance is only available for mode=deep",
            )
        new_auto = bool(payload.auto_advance)
        prior_auto = bool(getattr(run, "auto_advance", False))
        if prior_auto != new_auto:
            run.auto_advance = new_auto
            run.updated_at = utcnow()
            append_event(
                session,
                run,
                "run_settings_updated",
                {
                    "field": "auto_advance",
                    "from": prior_auto,
                    "to": new_auto,
                    "state": run.state,
                    "active_phase_lock": lock.get("phase") if lock is not None else None,
                },
            )
            # Flipping ON triggers the coordinator NOW so the run
            # advances from whatever USER_*_REVIEW state it's in.
            triggered_advance = new_auto
    session.commit()
    session.refresh(run)
    if triggered_advance:
        # Coordinator runs after the toggle commit so any failure on
        # advance doesn't roll back the toggle itself. The
        # coordinator never raises.
        from autoessay.auto_advance import maybe_advance

        maybe_advance(session, run, source="settings_toggle")
        session.refresh(run)
    return _run_response(session, run)


@app.delete(
    "/api/runs/{run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_run(
    run_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> Response:
    """Soft-delete a single run without deleting its owning project.

    The project-level delete route remains the only API that hides the
    entire essay and cascades cancel intent to every unfinished sibling
    run. This endpoint is intentionally run-scoped for the run-list card.
    """
    from autoessay.state_machine import append_event

    run = _get_user_run_or_404(session, run_id, user)
    project = session.get(Project, run.project_id)
    if project is not None and project.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project is deleted; restore it before deleting individual runs",
        )
    if run.deleted_at is not None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    now = utcnow()
    run.deleted_at = now
    if run.cancel_requested_at is None and run.state not in LIMIT_TERMINAL_STATES:
        run.cancel_requested_at = now
    run.updated_at = now
    append_event(
        session,
        run,
        "run_deleted",
        {
            "deleted_at": now.isoformat(),
            "scope": "run",
            "project_id": run.project_id,
        },
    )
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@contextlib.contextmanager
def _sqlite_fk_deferred(session: Session) -> Iterator[None]:
    """Defer SQLite FK checks until COMMIT for hard-delete cascades.

    ``PRAGMA foreign_keys=OFF`` is ignored once SQLAlchemy has opened a
    transaction, which hard-delete has already done by the time it loads
    the run/project. ``defer_foreign_keys`` is transaction-scoped and
    lets the cascade delete mutually-referencing rows first, while still
    validating the final database state at COMMIT. SQLite resets it on
    COMMIT/ROLLBACK, so do not turn it off inside this open transaction.
    """
    dialect = session.get_bind().dialect.name
    if dialect != "sqlite":
        yield
        return
    session.execute(text("PRAGMA defer_foreign_keys=ON"))
    yield


def _delete_phase_version_child_rows_for_run(session: Session, run_id: str) -> None:
    """Delete phase-version child rows that do not carry ``run_id``.

    The generic metadata loop below only sees tables with a direct
    ``run_id`` column. These rows are nevertheless owned by the run via
    ``phase_versions`` and must be removed before the run's phase
    versions disappear.
    """
    from autoessay.models import Base

    phase_versions = Base.metadata.tables["phase_versions"]
    phase_version_ids = select(phase_versions.c.id).where(phase_versions.c.run_id == run_id)
    for table_name, fk_columns in (
        ("phase_version_inputs", ("phase_version_id", "upstream_pv_id")),
        ("phase_version_prompts", ("phase_version_id",)),
        ("artifacts_v2", ("phase_version_id",)),
    ):
        table = Base.metadata.tables[table_name]
        predicates = [table.c[column].in_(phase_version_ids) for column in fk_columns]
        session.execute(table.delete().where(or_(*predicates)))


def _delete_project_corpus_child_rows_for_project(session: Session, project_id: str) -> None:
    """Delete project-scoped corpus descendants without ``project_id``."""
    from autoessay.models import Base

    corpora = Base.metadata.tables["corpora"]
    corpus_documents = Base.metadata.tables["corpus_documents"]
    memory_refs = Base.metadata.tables["memory_refs"]
    corpus_ids = select(corpora.c.id).where(corpora.c.project_id == project_id)
    document_ids = select(corpus_documents.c.id).where(
        corpus_documents.c.corpus_id.in_(corpus_ids),
    )
    session.execute(
        memory_refs.delete().where(memory_refs.c.corpus_document_id.in_(document_ids)),
    )
    session.execute(
        corpus_documents.delete().where(corpus_documents.c.corpus_id.in_(corpus_ids)),
    )


def _hard_delete_run_cascade(session: Session, run: Run) -> None:
    """PR-389/PR-390 helper: physically delete a run and every child
    row / on-disk artifact. The HTTP endpoint validates eligibility
    (``deleted_at IS NOT NULL`` + ``active_phase_lock IS NULL``)
    before calling this.

    Iterates ``Base.metadata.sorted_tables`` in reverse (children
    first) and issues ``DELETE FROM <table> WHERE run_id = :id`` for
    every child table. SQLite FK checks are deferred until COMMIT so
    cycles among ``runs``, ``branches``, and ``phase_versions`` can be
    removed in one transaction. Disk cleanup happens after the COMMIT
    so a disk failure can't roll back the DB cascade.
    """
    from autoessay.models import Base

    run_id = run.id
    run_dir = run.run_dir
    # Break the runs↔branches cycle so branches can be deleted first.
    run.active_branch_id = None
    session.flush()
    with _sqlite_fk_deferred(session):
        _delete_phase_version_child_rows_for_run(session, run_id)
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "runs":
                continue
            if "run_id" in table.columns:
                session.execute(table.delete().where(table.c.run_id == run_id))
        session.delete(run)
        session.flush()
    if run_dir:
        try:
            shutil.rmtree(run_dir, ignore_errors=True)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "run_dir cleanup failed for run=%s dir=%s: %s",
                run_id,
                run_dir,
                exc,
            )


def _hard_delete_project_cascade(session: Session, project: Project) -> None:
    """PR-389/PR-390 helper: physically delete every run owned by a
    project (using ``_hard_delete_run_cascade``), then delete project-
    scoped child rows that don't sit under a run, then the project row.

    Wraps the project-scoped deletes in ``_sqlite_fk_deferred`` for the
    same FK-cycle reason as the run-scoped helper."""
    from autoessay.models import Base

    project_id = project.id
    runs = session.scalars(select(Run).where(Run.project_id == project_id)).all()
    for run in runs:
        _hard_delete_run_cascade(session, run)
    with _sqlite_fk_deferred(session):
        _delete_project_corpus_child_rows_for_project(session, project_id)
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "projects":
                continue
            if "project_id" in table.columns and "run_id" not in table.columns:
                session.execute(
                    table.delete().where(table.c.project_id == project_id),
                )
        session.delete(project)
        session.flush()


@app.delete(
    "/api/runs/{run_id}/hard",
    status_code=status.HTTP_204_NO_CONTENT,
)
def hard_delete_run(
    run_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> Response:
    """PR-389: permanently delete a soft-deleted run and all its
    children. Eligibility:
    - Owner-only (404 otherwise).
    - Must already be soft-deleted (``deleted_at IS NOT NULL``);
      enforces a 2-step delete UX so users don't lose data via
      one-click misclick.
    - No ``active_phase_lock`` — a worker is mid-flight, can't drop
      its DB rows. 409 with hint to wait for the reaper / phase
      completion.
    """
    run = _get_user_run_or_404(session, run_id, user)
    if run.deleted_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run must be soft-deleted before permanent removal",
        )
    if run.active_phase_lock is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "run has an active phase lock; wait for the worker to "
                "finish (or reaper to clear a zombie lock) before "
                "permanent deletion"
            ),
        )
    _hard_delete_run_cascade(session, run)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.delete(
    "/api/projects/{project_id}/hard",
    status_code=status.HTTP_204_NO_CONTENT,
)
def hard_delete_project(
    project_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> Response:
    """PR-389: permanently delete a soft-deleted project and every
    run under it. Eligibility mirrors ``hard_delete_run`` at the
    project level: owner-only, ``deleted_at IS NOT NULL``, AND no
    child run currently holds ``active_phase_lock``."""
    project = _get_project_or_404(session, project_id)
    if project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if project.deleted_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project must be soft-deleted before permanent removal",
        )
    locked = session.scalar(
        select(Run.id).where(
            Run.project_id == project.id,
            Run.active_phase_lock.is_not(None),
        ),
    )
    if locked is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"run {locked} has an active phase lock; wait for the "
                "worker to finish before permanent deletion of the project"
            ),
        )
    _hard_delete_project_cascade(session, project)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/runs/{run_id}/restore",
    response_model=RunResponse,
)
def restore_run(
    run_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> Any:
    """Undo a run-scoped soft delete without restoring the whole essay."""
    from autoessay.phase_rerun import RUNNING_STATES
    from autoessay.state_machine import append_event

    run = _get_user_run_or_404(session, run_id, user)
    project = session.get(Project, run.project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    if project.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project is deleted; restore it before restoring individual runs",
        )
    if run.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run is not deleted")
    if run.active_phase_lock is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_restore_active_phase_lock_detail(run, scope="run"),
        )

    latest_visible_run = session.scalar(
        select(Run)
        .where(Run.project_id == project.id, Run.deleted_at.is_(None))
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1),
    )
    if latest_visible_run is None:
        latest_state_after_restore = run.state
    else:
        restored_run_becomes_latest = run.created_at > latest_visible_run.created_at or (
            run.created_at == latest_visible_run.created_at and run.id > latest_visible_run.id
        )
        latest_state_after_restore = (
            run.state if restored_run_becomes_latest else latest_visible_run.state
        )
    will_become_active = (
        not _project_currently_active(session, project)
        and latest_state_after_restore not in LIMIT_TERMINAL_STATES
    )
    if will_become_active:
        _lock_user_for_essay_limit(session, project.user_id)
        active_count = _count_active_essays(session, project.user_id)
        if active_count >= ACTIVE_ESSAY_LIMIT_PER_USER:
            return _essay_limit_response(active_count)

    prior_deleted_at = run.deleted_at
    prior_cancel_at = run.cancel_requested_at
    recovery_warning = _restore_recovery_warning_for_late_phase_done(
        session,
        run,
        prior_cancel_at,
    )
    now = utcnow()
    run.deleted_at = None
    if (
        run.cancel_requested_at is not None
        and run.state not in RUNNING_STATES
        and run.state != "CANCELLED"
    ):
        run.cancel_requested_at = None
    run.updated_at = now
    append_event(
        session,
        run,
        "run_restored",
        {
            "scope": "run",
            "project_id": run.project_id,
            "restored_at": now.isoformat(),
            "cleared_deleted_at": prior_deleted_at.isoformat(),
            "cleared_cancel_requested_at": (
                prior_cancel_at.isoformat()
                if prior_cancel_at is not None and run.cancel_requested_at is None
                else None
            ),
            "recovery_warning": recovery_warning,
        },
    )
    if recovery_warning is not None:
        append_event(session, run, "run_restore_recovery_warning", recovery_warning)
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


def _restore_active_phase_lock_detail(run: Run, *, scope: str) -> str:
    claimed = (
        run.active_phase_lock_claimed_at.isoformat()
        if run.active_phase_lock_claimed_at is not None
        else "unknown"
    )
    return (
        f"{scope} restore is blocked: run {run.id} has active_phase_lock="
        f"{run.active_phase_lock!r} claimed_at={claimed!r}. Wait for the phase "
        "to finish, or use the admin clear-phase-lock endpoint if it is stuck."
    )


def _restore_recovery_warning_for_late_phase_done(
    session: Session,
    run: Run,
    cancel_requested_at: datetime | None,
) -> dict[str, object] | None:
    """Detect stale worker completion after a run delete/cancel intent.

    Deleting a run stamps ``cancel_requested_at``. A well-behaved
    worker should call ``assert_run_active`` before later writes and
    stop. If a historical worker still emitted ``phase_done`` after
    that timestamp, restoring the run should not hide the ambiguity:
    the UI needs an audit banner so the user reviews artifacts before
    continuing.
    """
    if cancel_requested_at is None:
        return None
    event = session.scalar(
        select(RunEvent)
        .where(
            RunEvent.run_id == run.id,
            RunEvent.event_type == "phase_done",
            RunEvent.created_at >= cancel_requested_at,
        )
        .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
        .limit(1),
    )
    if event is None:
        return None
    try:
        payload = json.loads(event.payload)
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    phase = payload.get("phase")
    return {
        "reason": "phase_done_after_cancel_intent",
        "phase": phase if isinstance(phase, str) and phase else None,
        "phase_done_event_id": event.id,
        "phase_done_at": event.created_at.isoformat(),
        "cancel_requested_at": cancel_requested_at.isoformat(),
        "guidance": (
            "This restored run recorded phase_done after a delete/cancel intent. "
            "Review phase artifacts and audit events before continuing."
        ),
    }


# ---------------------------------------------------------------------------
# Authors (codex-AGREEd #5)
# ---------------------------------------------------------------------------


class AuthorResponse(BaseModel):
    id: str
    display_name: str
    affiliation: str | None
    email: str | None
    orcid: str | None
    is_self: bool
    deleted_at: str | None = None


class AuthorCreateRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255)
    affiliation: str | None = Field(None, max_length=500)
    email: str | None = Field(None, max_length=255)
    orcid: str | None = Field(None, max_length=32)


class AuthorPatchRequest(BaseModel):
    display_name: str | None = Field(None, max_length=255)
    affiliation: str | None = Field(None, max_length=500)
    email: str | None = Field(None, max_length=255)
    orcid: str | None = Field(None, max_length=32)


class ProjectAuthorEntryRequest(BaseModel):
    author_id: str
    position: int = Field(..., ge=0)


class ProjectAuthorsRequest(BaseModel):
    authors: list[ProjectAuthorEntryRequest]


class ProjectAuthorEntryResponse(BaseModel):
    author_id: str
    position: int
    display_name: str
    affiliation: str | None
    email: str | None
    orcid: str | None
    deleted: bool


class ProjectAuthorsResponse(BaseModel):
    project_id: str
    authors: list[ProjectAuthorEntryResponse]


def _author_response(author: Author) -> AuthorResponse:
    return AuthorResponse(
        id=author.id,
        display_name=author.display_name,
        affiliation=author.affiliation,
        email=author.email,
        orcid=author.orcid,
        is_self=author.is_self,
        deleted_at=author.deleted_at.isoformat() if author.deleted_at else None,
    )


def _project_author_entry(pa: ProjectAuthor, author: Author) -> ProjectAuthorEntryResponse:
    return ProjectAuthorEntryResponse(
        author_id=pa.author_id,
        position=pa.position,
        display_name=author.display_name,
        affiliation=author.affiliation,
        email=author.email,
        orcid=author.orcid,
        deleted=author.deleted_at is not None,
    )


@app.get("/api/authors", response_model=list[AuthorResponse])
def list_authors_endpoint(
    session: SessionDependency,
    user: CurrentUserDependency,
    include_deleted: bool = False,
) -> list[AuthorResponse]:
    """List the user's author roster.

    Lazily creates a ``self``-author on first call so the user always
    has at least one author available. Soft-deleted authors are
    hidden by default.
    """
    from autoessay.authors import get_or_create_self_author, list_authors

    get_or_create_self_author(session, user)
    session.commit()
    rows = list_authors(session, user.id, include_deleted=include_deleted)
    return [_author_response(a) for a in rows]


@app.post(
    "/api/authors",
    response_model=AuthorResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_author(
    request: AuthorCreateRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> AuthorResponse:
    from autoessay.authors import (
        ROSTER_CAP_PER_USER,
        count_active_authors,
        get_or_create_self_author,
        normalize_display_name,
        normalize_optional_string,
        validate_email,
        validate_orcid,
    )

    # Ensure the user row + self-author exist before adding a roster
    # entry — the auth-bypass middleware can hand us a User stub
    # whose id has no matching ``users`` row yet, and Author has a FK.
    get_or_create_self_author(session, user)
    active = count_active_authors(session, user.id)
    if active >= ROSTER_CAP_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"author roster cap reached ({ROSTER_CAP_PER_USER})",
        )
    _enforce_input_safety(
        request.display_name,
        context_hint=SAFETY_CONTEXT_AUTHOR_DISPLAY_NAME,
    )
    _enforce_input_safety(request.affiliation, context_hint=SAFETY_CONTEXT_AUTHOR_BIO)
    author = Author(
        id=f"author_{uuid4().hex}",
        user_id=user.id,
        display_name=normalize_display_name(request.display_name),
        affiliation=normalize_optional_string(request.affiliation),
        email=validate_email(request.email),
        orcid=validate_orcid(request.orcid),
        is_self=False,
    )
    session.add(author)
    session.commit()
    session.refresh(author)
    return _author_response(author)


@app.patch("/api/authors/{author_id}", response_model=AuthorResponse)
def patch_author(
    author_id: str,
    request: AuthorPatchRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> AuthorResponse:
    from autoessay.authors import (
        normalize_display_name,
        normalize_optional_string,
        validate_email,
        validate_orcid,
    )

    author = session.scalar(
        select(Author).where(Author.id == author_id, Author.user_id == user.id),
    )
    if author is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="author not found")
    if request.display_name is not None:
        _enforce_input_safety(
            request.display_name,
            context_hint=SAFETY_CONTEXT_AUTHOR_DISPLAY_NAME,
        )
        author.display_name = normalize_display_name(request.display_name)
    if request.affiliation is not None:
        _enforce_input_safety(request.affiliation, context_hint=SAFETY_CONTEXT_AUTHOR_BIO)
        author.affiliation = normalize_optional_string(request.affiliation)
    if request.email is not None:
        author.email = validate_email(request.email)
    if request.orcid is not None:
        author.orcid = validate_orcid(request.orcid)
    session.commit()
    session.refresh(author)
    return _author_response(author)


@app.delete(
    "/api/authors/{author_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_author(
    author_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> Response:
    """Soft-delete an author. Idempotent. The self-author cannot be
    soft-deleted — try to edit it instead."""
    author = session.scalar(
        select(Author).where(Author.id == author_id, Author.user_id == user.id),
    )
    if author is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="author not found")
    if author.is_self:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot delete the self-author; edit it instead",
        )
    if author.deleted_at is not None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    author.deleted_at = utcnow()
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/api/projects/{project_id}/authors",
    response_model=ProjectAuthorsResponse,
)
def get_project_authors_endpoint(
    project_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> ProjectAuthorsResponse:
    from autoessay.authors import get_project_authors

    project = _get_project_or_404(session, project_id)
    if project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    rows = get_project_authors(session, project.id)
    return ProjectAuthorsResponse(
        project_id=project.id,
        authors=[_project_author_entry(pa, a) for pa, a in rows],
    )


@app.put(
    "/api/projects/{project_id}/authors",
    response_model=ProjectAuthorsResponse,
)
def put_project_authors(
    project_id: str,
    request: ProjectAuthorsRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> ProjectAuthorsResponse:
    from autoessay.authors import ProjectAuthorEntry, set_project_authors

    project = _get_project_or_404(session, project_id)
    if project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    _assert_project_active(project)
    entries = [
        ProjectAuthorEntry(author_id=e.author_id, position=e.position) for e in request.authors
    ]
    rows = set_project_authors(session, project, user, entries)
    session.commit()
    return ProjectAuthorsResponse(
        project_id=project.id,
        authors=[_project_author_entry(pa, a) for pa, a in rows],
    )


# ---------------------------------------------------------------------------
# Per-project corpus (PR-B1, codex AGREE-with-amendments). Issue 2 of
# the 2026-05-01 design review. Each project has an explicit
# selection of which user-global corpora to include, plus an
# optional project-scoped corpus for prior papers uploaded from
# inside the workspace.
# ---------------------------------------------------------------------------


class ProjectCorpusDocumentEntry(BaseModel):
    id: str
    title: str
    document_type: str
    ingest_status: str
    original_size_bytes: int | None
    created_at: str


class ProjectCorpusEntry(BaseModel):
    id: str
    name: str
    is_global: bool
    is_selected: bool
    document_count: int


class ProjectCorpusResponse(BaseModel):
    project_id: str
    project_corpus_id: str | None
    project_documents: list[ProjectCorpusDocumentEntry]
    global_corpora: list[ProjectCorpusEntry]


class ProjectCorpusSelectionRequest(BaseModel):
    global_corpus_ids: list[str]


class ProjectCorpusSelectionResponse(BaseModel):
    project_id: str
    selected_global_corpus_ids: list[str]


@app.get(
    "/api/projects/{project_id}/corpus",
    response_model=ProjectCorpusResponse,
)
def get_project_corpus(
    project_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> ProjectCorpusResponse:
    """List this project's effective corpus state — every project-
    scoped document plus every global corpus owned by the user
    (with ``is_selected`` reflecting the explicit selection in
    ``project_corpus_selections``)."""
    from autoessay.models import Corpus, CorpusDocument, ProjectCorpusSelection

    project = _get_project_or_404(session, project_id)
    if project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    project_corpus = session.scalar(
        select(Corpus)
        .where(
            Corpus.owner_user_id == project.user_id,
            Corpus.project_id == project.id,
            Corpus.enabled.is_(True),
        )
        .order_by(Corpus.created_at.asc())
        .limit(1),
    )
    project_documents: list[ProjectCorpusDocumentEntry] = []
    if project_corpus is not None:
        for doc in session.scalars(
            select(CorpusDocument)
            .where(CorpusDocument.corpus_id == project_corpus.id)
            .order_by(CorpusDocument.created_at.desc()),
        ):
            project_documents.append(
                ProjectCorpusDocumentEntry(
                    id=doc.id,
                    title=doc.title,
                    document_type=doc.document_type,
                    ingest_status=doc.ingest_status,
                    original_size_bytes=doc.original_size_bytes,
                    created_at=doc.created_at.isoformat(),
                ),
            )

    selected_ids = {
        row[0]
        for row in session.execute(
            select(ProjectCorpusSelection.corpus_id).where(
                ProjectCorpusSelection.project_id == project.id,
            ),
        )
    }
    global_corpora: list[ProjectCorpusEntry] = []
    for corpus in session.scalars(
        select(Corpus)
        .where(
            Corpus.owner_user_id == user.id,
            Corpus.project_id.is_(None),
            Corpus.enabled.is_(True),
        )
        .order_by(Corpus.created_at.asc()),
    ):
        doc_count = session.scalar(
            select(func.count(CorpusDocument.id)).where(
                CorpusDocument.corpus_id == corpus.id,
            ),
        )
        global_corpora.append(
            ProjectCorpusEntry(
                id=corpus.id,
                name=corpus.name,
                is_global=True,
                is_selected=corpus.id in selected_ids,
                document_count=int(doc_count or 0),
            ),
        )

    return ProjectCorpusResponse(
        project_id=project.id,
        project_corpus_id=project_corpus.id if project_corpus is not None else None,
        project_documents=project_documents,
        global_corpora=global_corpora,
    )


@app.put(
    "/api/projects/{project_id}/corpus/selection",
    response_model=ProjectCorpusSelectionResponse,
)
def put_project_corpus_selection(
    project_id: str,
    request: ProjectCorpusSelectionRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> ProjectCorpusSelectionResponse:
    """Replace this project's set of selected GLOBAL corpora.
    Project-scoped corpora are always included automatically, so
    this endpoint only accepts global corpus ids."""
    from autoessay.models import Corpus, ProjectCorpusSelection

    project = _get_project_or_404(session, project_id)
    if project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")

    requested_ids = list(dict.fromkeys(request.global_corpus_ids))
    if requested_ids:
        owned = {
            row[0]
            for row in session.execute(
                select(Corpus.id).where(
                    Corpus.id.in_(requested_ids),
                    Corpus.owner_user_id == user.id,
                    Corpus.project_id.is_(None),
                ),
            )
        }
        unknown = set(requested_ids) - owned
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown or non-global corpus ids: {sorted(unknown)}",
            )

    session.execute(
        delete(ProjectCorpusSelection).where(
            ProjectCorpusSelection.project_id == project.id,
        ),
    )
    for corpus_id in requested_ids:
        session.add(
            ProjectCorpusSelection(
                project_id=project.id,
                corpus_id=corpus_id,
            ),
        )
    session.commit()

    return ProjectCorpusSelectionResponse(
        project_id=project.id,
        selected_global_corpus_ids=requested_ids,
    )


class ProjectCorpusUploadResponse(BaseModel):
    document: ProjectCorpusDocumentEntry
    task_id: str


@app.post(
    "/api/projects/{project_id}/corpus/upload",
    response_model=ProjectCorpusUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_project_corpus_document(
    project_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
    file: Annotated[UploadFile, File()],
) -> ProjectCorpusUploadResponse:
    """Upload a prior-paper PDF/DOCX/MD/TXT into this project's
    project-scoped corpus. Mirrors ``upload_corpus_document`` but
    routes through ``create_project_corpus_document`` so the
    resulting Corpus has ``project_id == project.id``."""
    from autoessay.corpus import (
        CorpusUploadError,
        create_project_corpus_document,
        run_corpus_ingest_job,
    )
    from autoessay.worker import enqueue_corpus_ingest_job

    project = _get_project_or_404(session, project_id)
    if project.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    payload = await file.read()
    try:
        document = create_project_corpus_document(
            session,
            user,
            project,
            filename=file.filename or "prior-paper",
            content_type=file.content_type,
            payload=payload,
        )
    except CorpusUploadError as exc:
        code = (
            status.HTTP_413_CONTENT_TOO_LARGE
            if "30 MB" in str(exc)
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    session.commit()
    session.refresh(document)
    settings = get_settings()
    if settings.sync_worker:
        run_corpus_ingest_job(document.id, session)
        task_id = "sync"
        session.refresh(document)
    else:
        task_id = enqueue_corpus_ingest_job(document.id)

    return ProjectCorpusUploadResponse(
        document=ProjectCorpusDocumentEntry(
            id=document.id,
            title=document.title,
            document_type=document.document_type,
            ingest_status=document.ingest_status,
            original_size_bytes=document.original_size_bytes,
            created_at=document.created_at.isoformat(),
        ),
        task_id=task_id,
    )


@app.post("/api/runs/{run_id}/transitions", response_model=RunResponse)
def transition_run(
    run_id: str,
    request: TransitionRequest,
    session: SessionDependency,
) -> RunResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    if _requires_dedicated_failure_recovery(run.state, request.to_state):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "FAILED_* to USER_* recovery is phase-aware and must use "
                "the dedicated force-approve endpoint."
            ),
        )
    try:
        transition(run, request.to_state, session, reason=request.reason)
    except InvalidTransition as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


@app.post("/api/runs/{run_id}/clear-phase-lock", response_model=RunResponse)
def clear_phase_lock(run_id: str, session: SessionDependency) -> RunResponse:
    """Manual escape hatch for zombie phase locks (Stage 3.E P0).

    If a worker crashed mid-phase, the lock stays held forever. Codex
    AGREE: ship a manual clear path in the same PR as the lock itself
    so users / ops aren't permanently deadlocked. We deliberately
    skip the owner check here — that's the whole point.
    """
    run = _get_run_for_mutation_or_404(session, run_id)
    prior = force_clear_phase_lock(session, run)
    append_event(
        session,
        run,
        "phase_lock_force_cleared",
        {
            "prior_phase": prior.get("phase"),
            "prior_job_id": prior.get("job_id"),
            "prior_claimed_at": prior.get("claimed_at"),
        },
    )
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


@app.post("/api/runs/{run_id}/force-approve", response_model=RunResponse)
def force_approve_run(
    run_id: str,
    request: ForceApproveRequest,
    session: SessionDependency,
) -> RunResponse:
    """User-forced approval at a failure state (Stage 3.E follow-up,
    codex AGREE-with-amendments).

    Allows the user to override an exporter policy gate, accept a
    partial drafter/stylist output, or skip integrity. Emits a
    ``force_approve`` audit event with a hash of the pre-mutation
    ``blocking_issues.json`` so the trail is reconstructable.

    Body: ``{"reason": "free-text user reason, 5-1000 chars"}``.
    """
    run = _get_run_for_mutation_or_404(session, run_id)
    reason = request.reason.strip()
    if len(reason) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reason must be at least 5 non-whitespace characters",
        )
    _do_force_approve(run, session, reason)
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


class TestFailPhaseRequest(BaseModel):
    """PR-248 — body for the test-only fail-phase injector."""

    phase: str
    failure_state: Literal["FAILED_FIXABLE", "FAILED_POLICY"] = "FAILED_FIXABLE"


class TestLatePhaseDoneRequest(BaseModel):
    """Body for a test-only late phase_done injector."""

    phase: str


class TestStateLockRequest(BaseModel):
    """Body for a test-only state + active lock injector."""

    state: str
    active_phase_lock: str | None = None


class TestExpressCompleteRequest(BaseModel):
    """Body for a test-only express artifact injector."""

    manuscript: str | None = None
    audit_status: str = "pass"
    total_tokens: int = 30000


@app.post(
    "/api/test/runs/{run_id}/fail-phase",
    response_model=RunResponse,
)
def test_fail_phase(
    run_id: str,
    request: TestFailPhaseRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> RunResponse:
    """PR-248 — test-only injector that drops a run into a failed state
    for the named phase, so specs can exercise failure UI without
    needing a real agent crash.

    Gated on ``Settings.test_mode`` (env ``AUTOESSAY_TEST_MODE``).
    Returns 404 when the flag is off — looks identical to the endpoint
    not existing, so production traffic that probes the URL gets the
    same response as if the route weren't registered. ``test_mode``
    itself is hard-rejected in ``AUTOESSAY_ENV=production`` via the
    Settings root_validator (PR-248), so this endpoint is structurally
    impossible to reach in prod.

    Codex round-1 design verdict (PR-245 lifecycle spec design,
    Q3=D): a deterministic FAILED_FIXABLE injector is the right
    primitive for retry coverage. Env-flips on stub flags can't
    produce mid-suite failures reliably (the worker has already
    loaded the agent module); writing run.state directly via
    conftest doesn't exercise the same transition + event-emit
    code path the real failure runs through.

    Body: ``{"phase": "<phase-name>"}``. Validates against
    ``_PHASE_RUNNING_STATE``; rejects unknown phases with 404.

    On success the run is in the requested failed state with a
    ``phase_failed`` event whose payload includes
    ``failure_class="test_injected"``. The default ``FAILED_FIXABLE``
    path preserves the retry-leg behavior; ``FAILED_POLICY`` is used by
    landing-routing specs for exports/policy blockers.
    """
    settings = get_settings()
    if not settings.test_mode:
        # Pretend the route doesn't exist — same response as a
        # genuine 404. Don't leak the existence of the test endpoint
        # to a probing client even in non-production envs.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if request.phase not in _PHASE_RUNNING_STATE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "unknown_phase", "phase": request.phase},
        )

    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    # Allow injection from any non-terminal state — common cases:
    # 1. After walking to USER_*_REVIEW (target state for retry test).
    # 2. Mid-phase (state = *_RUNNING) — simulates worker crash.
    # The state machine permits FAILED_FIXABLE from every state, so
    # we don't need a phase-state-precondition gate here.
    try:
        transition(run, request.failure_state, session, reason=f"test_inject:{request.phase}")
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "test_inject_invalid_transition",
                "phase": request.phase,
                "current_state": run.state,
                "error": str(exc),
            },
        ) from exc
    append_event(
        session,
        run,
        "phase_failed",
        {
            "phase": request.phase,
            "failure_class": "test_injected",
            "failure_state": request.failure_state,
            "guidance": "test-only injection via /api/test/runs/{id}/fail-phase",
        },
    )
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


@app.post(
    "/api/test/runs/{run_id}/express-complete",
    response_model=RunResponse,
)
def test_express_complete(
    run_id: str,
    request: TestExpressCompleteRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> RunResponse:
    """Test-only injector for express workspace UI coverage.

    The real express runner shells out through the Codex CLI and should
    not be invoked from deterministic Playwright suites. This endpoint
    is gated by ``AUTOESSAY_TEST_MODE`` exactly like the existing test
    injectors and writes the same artifact paths that the read-only
    transparency endpoint consumes.
    """
    settings = get_settings()
    if not settings.test_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    if _run_generation_mode(run) != EXPRESS_MODE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run is not mode=express")
    if run.state not in {"DOMAIN_LOADED", "EXPRESS_FAILED", "EXPRESS_RUNNING", "EXPRESS_DONE"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"cannot inject express completion from state {run.state}",
        )

    manuscript = request.manuscript or (
        "# Express sample manuscript\n\n"
        "## Abstract\n"
        "This sample manuscript is written by the test injector for the express workspace.\n\n"
        "## Introduction\n"
        "Express mode produces a compact result view without deep phase previews.\n\n"
        "## Conclusion\n"
        "The result remains auditable through prompt, token, provider, and audit metadata.\n"
    )
    run_dir = Path(run.run_dir)
    express_dir = run_dir / "express"
    draft_dir = run_dir / "drafts" / "v001"
    exports_dir = run_dir / "exports"
    express_dir.mkdir(parents=True, exist_ok=True)
    draft_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)

    prompt = "TEST REDACTED EXPRESS PROMPT\n\nGenerate a complete academic manuscript."
    (express_dir / "ars_prompt.redacted.md").write_text(prompt, encoding="utf-8")
    (draft_dir / "manuscript.md").write_text(manuscript, encoding="utf-8")
    (draft_dir / "claim_map.jsonl").write_text("", encoding="utf-8")
    (draft_dir / "citations.bib").write_text("", encoding="utf-8")
    (exports_dir / "manuscript.md").write_text(manuscript, encoding="utf-8")
    (express_dir / "audit_critic.json").write_text(
        json.dumps(
            {
                "status": request.audit_status,
                "summary": "test audit summary",
                "citation_traceability": {"status": "soft"},
                "word_count": {"status": "ok"},
                "style_compliance": {"status": "ok"},
                "issues": [],
                "audit_only": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    token_usage = {
        "prompt_tokens": request.total_tokens // 2,
        "completion_tokens": request.total_tokens - request.total_tokens // 2,
        "total_tokens": request.total_tokens,
    }
    (express_dir / "provenance.json").write_text(
        json.dumps(
            {
                "schema_version": "express_provenance_v1",
                "mode": "express",
                "run_id": run.id,
                "provider": "test",
                "provider_model": "gpt-5.4",
                "token_cap": settings.express_token_cap,
                "token_usage": token_usage,
                "prompt_sha256": "test-prompt-sha",
                "audit_prompt_sha256": "test-audit-sha",
                "ars_skill_sha": "test-skill-sha",
                "completed_at": utcnow().isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    (exports_dir / "manifest.json").write_text(
        json.dumps(
            {
                "mode": "express",
                "files": {"markdown": "exports/manuscript.md"},
                "audit_only": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )

    if run.state in {"DOMAIN_LOADED", "EXPRESS_FAILED"}:
        transition(run, "EXPRESS_RUNNING", session, reason="test_express_complete")
    if run.state == "EXPRESS_RUNNING":
        transition(run, "EXPRESS_DONE", session, reason="test_express_complete")
    append_event(
        session,
        run,
        "express_generation_done",
        {"token_usage": token_usage, "test_injected": True},
    )
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


@app.post(
    "/api/test/runs/{run_id}/state-lock",
    response_model=RunResponse,
)
def test_set_state_lock(
    run_id: str,
    request: TestStateLockRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> RunResponse:
    """Test-only injector for state/lock mismatch UI coverage.

    This is gated exactly like ``/fail-phase`` and is structurally
    unavailable in production. It lets Playwright reproduce short
    handoff windows such as ``state=CRITIC_RUNNING`` with an older
    ``active_phase_lock=final_rewrite`` without relying on a real
    long-running worker race.
    """
    settings = get_settings()
    if not settings.test_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if request.state not in RUN_STATES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "unknown_state", "state": request.state},
        )
    if (
        request.active_phase_lock is not None
        and request.active_phase_lock not in _PHASE_RUNNING_STATE
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "unknown_phase", "phase": request.active_phase_lock},
        )

    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    run.state = request.state
    if request.active_phase_lock is None:
        run.active_phase_lock = None
        run.active_phase_lock_job_id = None
        run.active_phase_lock_claimed_at = None
    else:
        run.active_phase_lock = request.active_phase_lock
        run.active_phase_lock_job_id = new_lock_token()
        run.active_phase_lock_claimed_at = utcnow()
    append_event(
        session,
        run,
        "state_set",
        {
            "to": request.state,
            "active_phase_lock": request.active_phase_lock,
            "reason": "test_state_lock",
        },
    )
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


@app.post(
    "/api/test/runs/{run_id}/late-phase-done-after-cancel",
    response_model=RunResponse,
)
def test_late_phase_done_after_cancel(
    run_id: str,
    request: TestLatePhaseDoneRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> RunResponse:
    """Test-only injector for delete/restore consistency coverage.

    Production workers should never emit ``phase_done`` after
    ``cancel_requested_at``. This route creates that historical edge
    condition deterministically so Playwright can verify the restore
    warning path without racing a real long-running worker.
    """
    settings = get_settings()
    if not settings.test_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if request.phase not in _PHASE_RUNNING_STATE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "unknown_phase", "phase": request.phase},
        )
    run = _get_user_run_or_404(session, run_id, user)
    if run.cancel_requested_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run has no cancel intent",
        )
    append_event(
        session,
        run,
        "phase_done",
        {
            "phase": request.phase,
            "test_injected": True,
            "after_cancel_requested_at": run.cancel_requested_at.isoformat(),
        },
    )
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


@app.post(
    "/api/runs/{run_id}/phases/{phase}/recover",
    response_model=RunResponse,
)
def recover_stuck_phase(
    run_id: str,
    phase: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> RunResponse:
    """User-triggered escape hatch for stuck ``*_RUNNING`` runs (PR-I3).

    When a worker process is SIGKILLed mid-phase (OOM kill, container
    restart, RQ work-horse killed), the run state stays in
    ``*_RUNNING`` forever and ``FailureResolutionBanner`` never
    renders, so the user has no way to retry. The reaper background
    sweep eventually catches it, but only when its env flag is on
    and only every ``zombie_reaper_interval_seconds``. This endpoint
    is the synchronous, user-driven counterpart that ``StuckRunBanner``
    invokes when the run has gone idle past the same 15min threshold.

    Reuses PR-I1 ``_recover_zombie_running_phase`` verbatim — same
    compound gate (lock age + last-phase-event idle + no terminal
    event) — so the user trigger has the **identical** safety
    properties as the background sweep. The only difference is who
    pulls the lever.

    On success returns ``RunResponse`` reflecting the new
    ``FAILED_FIXABLE`` state; ``FailureResolutionBanner`` then takes
    over via the existing retry path.

    On gate-not-triggered returns 409 with a discriminator body so
    the front end can show an honest "the worker is still alive,
    refresh the page" message instead of pretending the recovery
    happened.
    """
    if phase not in _PHASE_RUNNING_STATE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "unknown_phase", "phase": phase},
        )
    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    expected_state = _PHASE_RUNNING_STATE[phase]
    pre_state = run.state
    _recover_zombie_running_phase(session, run, phase)
    if run.state != "FAILED_FIXABLE":
        # Gate did not fire. Three possibilities, all surfaced via
        # the same 409 so the UI can disambiguate from current_state:
        #   - run.state != expected_state (state drift, not zombie)
        #   - lock + event chain still active (worker alive)
        #   - terminal phase_done/failed already exists for this round
        # The recovery helper is silent (no return value) so we use
        # the pre/post state diff as the discriminator.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "recovery_gate_not_triggered",
                "phase": phase,
                "expected_state": expected_state,
                "current_state": pre_state,
            },
        )
    session.commit()
    session.refresh(run)
    return _run_response(session, run)


# PR-I5: phase → (sync runner, async enqueuer-or-None) dispatch
# table for the retry resolver. Each runner accepts
# ``(run_id, session, lock_token=token)``; each enqueuer accepts
# ``(run_id, lock_token=token)``. ``framework_lens`` and
# ``tension_extraction`` have no enqueuer (sync-only); their entry
# is ``(runner, None)`` and the kick-off helper always runs sync
# regardless of ``settings.sync_worker``. ``proposal`` is special-
# cased — its runner / enqueuer take an additional ``user_draft``
# argument; resolver passes ``user_draft=None`` (re-draft fresh).
def _retry_kickoff_proposal(run: Run, session: Session, token: str) -> dict[str, str]:
    settings = get_settings()
    if settings.sync_worker:
        run_proposal_draft(run.id, session, None, lock_token=token)
        return {"job_id": "sync"}
    try:
        job_id = enqueue_proposal_job(run.id, None, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "proposal", token)
        raise
    return {"job_id": job_id}


def _retry_kickoff_sync_only(
    runner: Callable[..., object],
) -> Callable[[Run, Session, str], dict[str, str]]:
    """Wrap a sync-only runner (framework_lens / tension_extraction)
    so the retry kick-off table has uniform shape. settings.sync_worker
    is irrelevant for these — they always run inline."""

    def _kick(run: Run, session: Session, token: str) -> dict[str, str]:
        runner(run.id, session, lock_token=token)
        return {"job_id": "sync"}

    return _kick


def _retry_kickoff_async_capable(
    runner: Callable[..., object],
    enqueuer: Callable[..., str],
    phase: str,
) -> Callable[[Run, Session, str], dict[str, str]]:
    """Wrap a runner+enqueuer pair (everything but proposal /
    framework_lens / tension_extraction) so the retry kick-off table
    has uniform shape."""

    def _kick(run: Run, session: Session, token: str) -> dict[str, str]:
        settings = get_settings()
        if settings.sync_worker:
            runner(run.id, session, lock_token=token)
            return {"job_id": "sync"}
        try:
            job_id = enqueuer(run.id, lock_token=token)
        except Exception:
            _release_after_enqueue_failure(session, run, phase, token)
            raise
        return {"job_id": job_id}

    return _kick


# Built lazily (after the runner/enqueuer imports above resolve) but
# in a module-level dict so callers don't pay per-call build cost.
_PHASE_RETRY_KICKOFF: dict[str, Callable[[Run, Session, str], dict[str, str]]] = {
    "proposal": _retry_kickoff_proposal,
    "scout": _retry_kickoff_async_capable(run_scout, enqueue_scout_job, "scout"),
    "curator": _retry_kickoff_async_capable(run_curator, enqueue_curator_job, "curator"),
    "synthesizer": _retry_kickoff_async_capable(
        run_synthesizer, enqueue_synthesizer_job, "synthesizer"
    ),
    "tension_extraction": _retry_kickoff_sync_only(run_tension_extraction),
    "framework_lens": _retry_kickoff_sync_only(run_framework_lens),
    "ideator": _retry_kickoff_async_capable(run_ideator, enqueue_ideator_job, "ideator"),
    "drafter": _retry_kickoff_async_capable(run_drafter, enqueue_drafter_job, "drafter"),
    "stylist": _retry_kickoff_async_capable(run_stylist, enqueue_stylist_job, "stylist"),
    "critic": _retry_kickoff_async_capable(run_critic, enqueue_critic_job, "critic"),
    "integrity": _retry_kickoff_async_capable(run_integrity, enqueue_integrity_job, "integrity"),
    "exports": _retry_kickoff_async_capable(run_exports, enqueue_exports_job, "exports"),
}


class RetryResponse(BaseModel):
    """PR-I5 retry resolver response. ``action`` is ``"start"`` or
    ``"rerun"`` so the front end knows which path the backend
    picked (purely informational — the front end doesn't have to
    branch on this)."""

    run_id: str
    phase: str
    action: str
    expected_state: str
    job_id: str | None = None


@app.post(
    "/api/runs/{run_id}/phases/{phase}/retry",
    response_model=RetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_failed_phase(
    run_id: str,
    phase: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> RetryResponse:
    """PR-I5: backend retry resolver.

    Replaces the front-end's PR-I4.a smart-retry static heuristic
    with a single endpoint that has full server-side context
    (``has_completed_output``, latest ``phase_failed`` event payload,
    lock state). Decision tree (codex 2-round consensus):

    1. ``state != FAILED_FIXABLE`` → 422 not_failed_fixable
    2. ``unknown phase`` (not in ``_PHASE_RUNNING_STATE``) → 404
    3. Latest ``phase_failed`` event's payload.phase != requested phase
       → 422 phase_mismatch (codex Q6 / Q-final-2: strict gate so
       users can't accidentally retry the wrong phase; upstream re-run
       belongs in the explicit ``rerun_phase`` UI)
    4. ``failure_class`` ∈ ``_PARTIAL_FAILURE_CLASSES`` → start path
       (worker died mid-flight; rewind via
       ``_recover_failed_fixable_for_phase`` and re-enqueue)
    5. ``has_completed_output(run, phase)`` → rerun path (artifact
       exists, agent should overwrite + re-evaluate)
    6. ``failure_class`` ∈ ``_GRACEFUL_FAILURE_CLASSES`` (and no
       output) → 422 guidance_required (re-running with same input
       would just hit the same fail; user should change inputs first)
    7. Fallback (unknown class, no output) → start path

    Distinct from ``/recover``: ``/recover`` is the
    ``*_RUNNING → FAILED_FIXABLE`` transition triggered by
    StuckRunBanner. ``/retry`` is the
    ``FAILED_FIXABLE → *_RUNNING`` transition triggered by the
    workspace failed-phase retry button. Two-step by design (codex
    Q-final-3): user sees what happened before deciding to re-run.
    """
    from autoessay.phase_rerun import has_completed_output

    if phase not in _PHASE_RUNNING_STATE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "unknown_phase", "phase": phase},
        )
    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    if run.state != "FAILED_FIXABLE":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "not_failed_fixable",
                "current_state": run.state,
            },
        )

    latest = _latest_phase_failed_payload(session, run)
    failure_class = latest.get("failure_class") if isinstance(latest, dict) else None
    latest_phase = latest.get("phase") if isinstance(latest, dict) else None

    # Codex Q6 / Q-final-2: strict phase mismatch guard. If the most
    # recent failure was for a different phase, refuse — pointing the
    # user at the wrong recovery path is more dangerous than nudging
    # them back to the failed phase. Upstream re-runs (e.g. user
    # wants to re-do synthesizer because lens was bad) belong in
    # the explicit ``rerun_phase`` UI, not here.
    if latest_phase is not None and latest_phase != phase:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "phase_mismatch",
                "requested_phase": phase,
                "actual_failed_phase": latest_phase,
            },
        )

    expected_running_state = _PHASE_RUNNING_STATE[phase]

    # Branch decision tree.
    is_partial = isinstance(failure_class, str) and failure_class in _PARTIAL_FAILURE_CLASSES
    has_output = has_completed_output(run, phase)
    is_graceful = isinstance(failure_class, str) and failure_class in _GRACEFUL_FAILURE_CLASSES

    # Proposal is not part of the generic phase_rerun version graph.
    # Retrying a failed proposal always means re-drafting it via the
    # proposal-specific runner, even if a previous proposal artifact exists.
    if phase == "proposal":
        return _retry_dispatch_start(run, phase, session, expected_running_state)
    if is_partial:
        return _retry_dispatch_start(run, phase, session, expected_running_state)
    if has_output:
        return _retry_dispatch_rerun(run, phase, session, expected_running_state)
    if is_graceful:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "guidance_required",
                "phase": phase,
                "failure_class": failure_class,
                "guidance": (latest or {}).get("guidance"),
            },
        )
    # Fallback: unknown failure_class + no output. Treat as
    # first-attempt failure (the original PR-I1 path) — rewind and
    # start. Safer than 422'ing on a class we just don't recognize.
    return _retry_dispatch_start(run, phase, session, expected_running_state)


def _retry_dispatch_start(
    run: Run, phase: str, session: Session, expected_running_state: str
) -> RetryResponse:
    """Resolver dispatch: rewind state via PR-I3.b helper +
    claim lock + kick off agent (sync or async per worker config).
    The kick-off table is built once at module load."""
    _recover_failed_fixable_for_phase(session, run, phase)
    assert_phase_ready(run, phase, session)
    token = _claim_or_409(session, run, phase)
    kickoff = _PHASE_RETRY_KICKOFF.get(phase)
    if kickoff is None:
        # _PHASE_RUNNING_STATE check above should have caught this,
        # but be explicit so a registry drift between the two maps
        # (codex Q6 amendment / PR-J2 phase-registry parity) returns
        # 500 with a clear stack instead of silently no-op'ing.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "missing_kickoff", "phase": phase},
        )
    result = kickoff(run, session, token)
    return RetryResponse(
        run_id=run.id,
        phase=phase,
        action="start",
        expected_state=expected_running_state,
        job_id=result.get("job_id"),
    )


def _retry_dispatch_rerun(
    run: Run, phase: str, session: Session, expected_running_state: str
) -> RetryResponse:
    """Resolver dispatch: rerun the phase (overwrite artifacts).
    Mirrors the core of ``rerun_phase`` minus the HTTP plumbing —
    rewind state via ``resolve_rewind_state`` + run via ``_PHASE_RUNNERS``
    + update stale marker.

    P0 fix #4 (codex state-machine audit §1.2): the rerun branch
    of ``/retry`` was bypassing ``active_phase_lock`` claim/release
    even though the rerun runs the agent inline (sync_worker mode).
    Two concurrent rerun calls — or a rerun racing a ``start_*`` —
    both saw an empty lock and both kicked the agent. Wrap with
    ``_claim_or_409`` + ``phase_lock_release_on_exit`` so it has
    the same atomic claim semantics as ``_retry_dispatch_start``
    and the per-phase ``start_*`` endpoints.
    """
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_rerun import (
        PHASE_POST_RERUN_STATE,
        assert_can_rerun,
        resolve_rewind_state,
        rewind_for_rerun,
        update_stale_marker_after_success,
    )
    from autoessay.phase_version import maybe_run_with_versioning

    assert_can_rerun(run, phase, session=session)
    assert_phase_ready(run, phase, session)
    runner = _PHASE_RUNNERS.get(phase)
    if runner is None:
        # Same registry-drift defensive 500 as the start path.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "missing_runner", "phase": phase},
        )
    rewind_state = resolve_rewind_state(phase, run)
    pre_rerun_state = run.state
    if rewind_state is not None and run.state != rewind_state:
        rewind_for_rerun(run, phase, rewind_state, session, source="retry")
    # P0 fix #4: claim the active_phase_lock BEFORE invoking the
    # runner so concurrent retry/start calls hit ``409 another
    # phase is currently running`` instead of double-running.
    token = _claim_or_409(session, run, phase)
    with phase_lock_release_on_exit(run.id, phase, token, session=session):
        try:
            maybe_run_with_versioning(session, run, phase, lambda: runner(run.id, session))
            post_state = PHASE_POST_RERUN_STATE.get(phase)
            if post_state is not None:
                # Agent may have already landed at the right state;
                # don't double-transition.
                with contextlib.suppress(InvalidTransition):
                    transition(run, post_state, session, reason=f"retry_rerun:{phase}")
            update_stale_marker_after_success(session, run, phase)
            session.commit()
        except Exception:
            # On agent crash, restore the pre-rerun state so the run
            # doesn't appear stuck in the input state.
            if rewind_state is not None and run.state == rewind_state:
                run.state = pre_rerun_state
                session.flush()
                session.commit()
            raise
    return RetryResponse(
        run_id=run.id,
        phase=phase,
        action="rerun",
        expected_state=expected_running_state,
        job_id="sync",
    )


@app.get("/api/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    bind: Annotated[Engine, Depends(get_engine)],
    close_after_event: bool = False,
) -> StreamingResponse:
    """SSE event stream for a run.

    Deliberately does NOT take a SessionDependency. SSE connections are
    long-lived and would each pin a slot in the SQLAlchemy connection
    pool for their entire lifetime, exhausting the pool with even a
    handful of open browser tabs. Instead we open short-lived sessions
    bound to the injected engine.
    """
    with Session(bind) as bootstrap:
        run = _get_run_or_404(bootstrap, run_id)
        run_id_value = run.id

    async def generate() -> AsyncIterator[str]:
        seen: set[str] = set()
        next_keepalive = time.monotonic() + 30.0
        while True:
            emitted = False
            for event in _load_events(bind, run_id_value):
                if event.id in seen:
                    continue
                seen.add(event.id)
                emitted = True
                yield _sse_run_event(event)
                if close_after_event:
                    return
            if not emitted and time.monotonic() >= next_keepalive:
                next_keepalive = time.monotonic() + 30.0
                yield ": keepalive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# PR-I3.b: failure_class values that signal a worker died mid-flight
# (zombie recovery from PR-I1 / PR-I2.a reaper, or PR-I2.b common
# failure boundary catching an exception in run_with_versioning).
# Both leave the phase artifacts in a partial state — even when the
# rerun-stale completion glob (e.g. claims.jsonl for synthesizer)
# was satisfied earlier in the agent before the worker died.
# `_recover_failed_fixable_for_phase` treats these as first-attempt
# style rewinds so the user's "重试该步骤" click can re-run the
# phase end-to-end and overwrite the half-written artifacts.
# Graceful failures (failed_fixable / fixable_* / failed_policy /
# failed_vendor) deliberately stay out — those phases finished a
# clean attempt that just didn't pass policy, and the user should
# call rerun_phase or fix the input rather than blow away artifacts.
_PARTIAL_FAILURE_CLASSES: frozenset[str] = frozenset(
    {"zombie_recovered", "phase_runtime_error"},
)


# PR-I5: failure_class values that signal an agent ran to completion
# but its output failed a policy / vendor / first-pass check. The
# phase wrote whatever it normally writes; the run state moved to
# FAILED_FIXABLE because the agent itself decided "the user should
# fix something and try again". Distinguishing these from partial
# failures matters for the retry resolver: graceful + has_completed_
# output means rerun (overwrite + re-evaluate); graceful + no output
# means 422 guidance_required (re-running with the same input would
# just hit the same fail). codex PR-I5 round-2 Q1.
_GRACEFUL_FAILURE_CLASSES: frozenset[str] = frozenset(
    {"failed_fixable", "failed_vendor", "failed_policy"},
)


def _latest_phase_failed_payload(
    session: Session,
    run: Run,
) -> dict[str, object] | None:
    """Return the parsed payload of the run's most recent
    ``phase_failed`` event, or None if there is none / the payload
    isn't valid JSON. Single-row LIMIT 1 query — the existing
    ``ix_run_events_run_id_created_at`` index serves it (codex
    PR-I3.b amendment#3: don't add a hard LIMIT inside the helper,
    it would mask correctness; if the event volume per run grows
    materially, add a ``(run_id, event_type, created_at)`` composite
    index later).
    """
    ev = (
        session.query(RunEvent)
        .filter(RunEvent.run_id == run.id, RunEvent.event_type == "phase_failed")
        .order_by(RunEvent.created_at.desc())
        .first()
    )
    if ev is None:
        return None
    try:
        payload = json.loads(ev.payload or "{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _recover_failed_fixable_for_phase(
    session: Session,
    run: Run,
    phase: str,
) -> None:
    """Allow ``start_<phase>`` to recover from ``FAILED_FIXABLE`` when
    ``phase`` is the failed phase that has not produced output yet.

    First-attempt phase failures (e.g. proposal LLM 503'd, scout
    couldn't reach an upstream literature source) leave the run at
    ``FAILED_FIXABLE`` with no completed artifacts. The
    ``FailureResolutionBanner`` "重试该步骤" button now calls
    ``start_<phase>`` for these — but every ``start_<phase>`` state
    guard rejects ``FAILED_FIXABLE``. This helper rewinds the run
    state to the phase's input state (``DOMAIN_LOADED`` for
    proposal, the corresponding ``USER_*_REVIEW`` for everything
    else) and force-clears any leftover phase-lock from the
    failed attempt, so the existing guard then accepts the
    request unchanged.

    PR-I3.b: the "phase already produced output" early return used
    to make zombie-recovered SIGKILL victims (e.g. synthesizer dying
    inside ``run_material_diagnostic`` after ``claims.jsonl`` was
    already written) un-retryable through the banner — start_<phase>
    would 409 because the rewind was skipped. We now distinguish
    "clean completion that hit a policy block" (skip rewind, user
    should call rerun_phase) from "worker died mid-phase" (rewind
    + start so the user can re-run end-to-end). The discriminator
    is the latest ``phase_failed`` event's ``failure_class`` —
    members of ``_PARTIAL_FAILURE_CLASSES`` mean the phase did NOT
    cleanly complete even if a sentinel completion glob was hit.

    No-op (so the existing 409 reaches the caller) when:
    - run state is not ``FAILED_FIXABLE``;
    - ``phase`` already has completed output AND the latest
      ``phase_failed`` event is not for this phase OR is a graceful
      failure class (non-partial — a clean policy / vendor failure);
    - the failed phase is not this one (run is at FAILED_FIXABLE
      because of an unrelated phase, e.g. user navigated to
      start_drafter while a previous scout failure persists).
    """
    from autoessay.phase_rerun import PHASE_INPUT_STATES, has_completed_output

    if run.state != "FAILED_FIXABLE":
        return
    if phase == "proposal":
        proposal_path = Path(run.run_dir) / "proposal" / "proposal.md"
        if proposal_path.is_file() and proposal_path.stat().st_size > 0:
            # Proposal already succeeded once; FAILED_FIXABLE is for
            # a later phase. Let the existing guard 409 — the user
            # should retry the actual failed phase, not proposal.
            return
        rewind = "DOMAIN_LOADED"
    else:
        if has_completed_output(run, phase):
            # PR-I3.b: check whether the most recent phase_failed
            # event is THIS phase + a partial-failure class
            # (worker died mid-flight). If so, fall through to
            # rewind regardless of the satisfied completion glob.
            # codex amendment#2: must be the latest phase_failed
            # globally (not "latest matching phase") — looking at
            # the latest matching event would mis-rewind a phase
            # that had a stale historical zombie but has since
            # cleanly completed.
            payload = _latest_phase_failed_payload(session, run)
            if payload is None:
                return
            if payload.get("phase") != phase:
                return
            failure_class = payload.get("failure_class")
            if not isinstance(failure_class, str) or failure_class not in _PARTIAL_FAILURE_CLASSES:
                return
        rewind = PHASE_INPUT_STATES.get(phase, "")
        if not rewind:
            return
    # Force-clear before mutating state, because force_clear
    # internally calls ``session.refresh(run)`` which would re-load
    # from the DB and clobber any unflushed in-memory state change.
    if run.active_phase_lock is not None:
        force_clear_phase_lock(session, run)
    run.state = rewind
    session.flush()


# PR-I1: phase → RUNNING-state mapping for zombie detection. Mirrors
# the order in ``state_machine.ALLOWED_TRANSITIONS`` and the
# ``RUNNING_STATES`` frozenset in ``phase_rerun``. Used by
# ``_recover_zombie_running_phase`` to decide whether a state-vs-phase
# pair is a candidate for zombie recovery.
_PHASE_RUNNING_STATE: dict[str, str] = {
    "express": "EXPRESS_RUNNING",
    "proposal": "PROPOSAL_DRAFTING",
    "scout": "SCOUT_RUNNING",
    "curator": "CURATOR_RUNNING",
    "synthesizer": "SYNTHESIZER_RUNNING",
    # PR-C3.a: tension_extraction phase between synthesizer and lens.
    "tension_extraction": "TENSION_EXTRACTION_RUNNING",
    "framework_lens": "FRAMEWORK_LENS_RUNNING",
    "ideator": "IDEATOR_RUNNING",
    "drafter": "DRAFTER_RUNNING",
    "stylist": "STYLIST_RUNNING",
    "final_rewrite": "REWRITE_RUNNING",
    "critic": "CRITIC_RUNNING",
    "integrity": "INTEGRITY_RUNNING",
    "exports": "EXPORTS_RUNNING",
}

# How long a phase event chain may sit idle before we treat the run
# as a zombie. Drafter writes section_progress every section (~30s on
# real-LLM), so 15 minutes of silence is the operational signal that
# the worker died mid-phase. Other phases finish in <5min on real LLM,
# so 10 minutes is conservative there too. The numbers can be tuned
# from a single env var without code changes.
_ZOMBIE_PHASE_IDLE_SECONDS_DEFAULT = 15 * 60


def _recover_zombie_running_phase(
    session: Session,
    run: Run,
    phase: str,
) -> None:
    """Detect and recover from a stuck ``*_RUNNING + worker dead``
    zombie before ``start_<phase>`` rejects the request.

    PR-I1: covers the gap between PR #88 (atomic phase lock) and
    PR #108 (FAILED_FIXABLE recovery). Worker death — OOM kill,
    container restart, RQ job dropped — leaves the run in
    ``DRAFTER_RUNNING + active_phase_lock=NULL`` (when the auto-start
    path skipped the lock claim, fixed in this PR) or
    ``DRAFTER_RUNNING + lock held by a dead worker process``.
    Either case has no banner, no retry, no recovery — so users see
    a permanently stuck run.

    Trigger conditions (all must hold):
      1. ``run.state`` is in ``RUNNING_STATES`` and matches ``phase``
         (e.g. ``DRAFTER_RUNNING`` ↔ ``drafter``)
      2. Either no active phase lock OR the lock is older than the
         idle threshold (``AUTOESSAY_ZOMBIE_PHASE_IDLE_SECONDS`` or
         15 min default)
      3. The most recent ``run_events`` row for this run+phase is
         older than the idle threshold
      4. There is no ``phase_done`` or ``phase_failed`` event for
         the same phase since ``phase_started`` (i.e. the phase
         really did get stuck mid-flight, not finish silently)

    Recovery actions (all in one transaction):
      - mark stale ``phase_versions.status='running'`` rows as
        ``failed`` so the next ``maybe_run_with_versioning`` does not
        skip the wrapper (codex C2c amendment)
      - force-clear the phase lock if any
      - append a ``phase_failed`` event with ``failure_class=
        zombie_recovered`` so the audit trail records the recovery
      - transition the run to ``FAILED_FIXABLE`` so the existing
        ``FailureResolutionBanner`` retry path takes over (which
        then routes back through ``_recover_failed_fixable_for_phase``
        on the next ``start_<phase>`` call)

    No-op (so the existing 409 reaches the caller) when:
      - run state is not in ``RUNNING_STATES`` for this ``phase``
      - the phase event chain is still active (worker is alive)
      - a terminal event (``phase_done`` / ``phase_failed``) exists
        — recovery should not undo a finished phase
    """
    expected_state = _PHASE_RUNNING_STATE.get(phase)
    if expected_state is None or run.state != expected_state:
        return

    # PR-real-paper-fix (codex round-1 Q3 amendment): idle threshold
    # must be PHASE-AWARE — drafter legitimately runs 45 min, so a
    # global 15-min reaper would constantly false-positive. Read the
    # phase's RQ ``job_timeout`` from ``worker.phase_job_timeout_seconds``
    # and add a 10-min grace. Env override (legacy
    # ``AUTOESSAY_ZOMBIE_PHASE_IDLE_SECONDS``) still wins so prod can
    # disable the phase-aware behavior in a hurry.
    env_override = os.environ.get("AUTOESSAY_ZOMBIE_PHASE_IDLE_SECONDS")
    if env_override:
        try:
            idle_seconds = int(env_override)
        except ValueError:
            idle_seconds = _ZOMBIE_PHASE_IDLE_SECONDS_DEFAULT
    else:
        try:
            from autoessay.worker import phase_job_timeout_seconds

            idle_seconds = phase_job_timeout_seconds(phase) + 10 * 60
        except Exception:  # noqa: BLE001 — fall back to legacy fixed default
            idle_seconds = _ZOMBIE_PHASE_IDLE_SECONDS_DEFAULT
    cutoff = utcnow() - timedelta(seconds=idle_seconds)

    # Lock liveness: if a token is held but the claim is older than
    # the idle threshold, the worker that claimed it is dead. If
    # there is no lock at all (the auto-start zombie this PR fixes),
    # we still need to inspect the event chain before recovering.
    # PR-I2.a: SQLite returns naive datetimes even on tz-aware columns,
    # so normalize ``claimed_at`` to the cutoff tzinfo before compare
    # (same pattern the ``last_phase_event_at`` branch below uses).
    lock_dead = False
    if run.active_phase_lock is not None:
        claimed_at = run.active_phase_lock_claimed_at
        if claimed_at is None:
            lock_dead = True
        else:
            claimed_compare = (
                claimed_at
                if claimed_at.tzinfo is not None
                else claimed_at.replace(tzinfo=cutoff.tzinfo)
            )
            if claimed_compare < cutoff:
                lock_dead = True

    # Most recent event for this run + phase. We treat any event
    # whose ``payload.phase`` matches as relevant — covers
    # ``phase_started``, ``section_progress``, ``source_progress``,
    # ``phase_done``, ``phase_failed``.
    last_event = (
        session.query(RunEvent)
        .filter(RunEvent.run_id == run.id)
        .order_by(RunEvent.created_at.desc())
        .all()
    )
    # Walk events DESC (newest first) only as far back as the most
    # recent ``phase_started`` for THIS phase; events older than that
    # belong to a previous phase_version round and must not be counted
    # as terminal events for the current zombie. Bug surfaced on prod
    # run_6c0640: drafter v1 phase_done from 2026-05-02 was being
    # treated as terminal for drafter v2 zombie that started
    # 2026-05-03, blocking recovery.
    last_phase_event_at: datetime | None = None
    has_terminal_event = False
    for ev in last_event:
        try:
            payload_phase = json.loads(ev.payload or "{}").get("phase")
        except json.JSONDecodeError:
            payload_phase = None
        if payload_phase != phase:
            continue
        if last_phase_event_at is None:
            last_phase_event_at = ev.created_at
        if ev.event_type in {"phase_done", "phase_failed"}:
            # In DESC walk, encountering done/failed BEFORE we see a
            # phase_started means this terminal event happened AFTER
            # the latest phase_started — i.e. the current round
            # actually finished. State drift bug, not zombie; bail.
            has_terminal_event = True
            break
        if ev.event_type == "phase_started":
            # Reached the latest phase_started for this phase. Older
            # events (DESC walk continuing further) belong to previous
            # rounds and are irrelevant to the current zombie.
            break

    if has_terminal_event:
        # Phase already finished or failed in the latest round; the
        # *_RUNNING state is a different bug (caller should rewind via
        # state_transition).
        return
    if last_phase_event_at is None:
        # No event at all for this phase yet. With no lock that's
        # a fresh-zombie (auto-start failed before any work logged).
        # With a lock, we wait for the lock to age out.
        recoverable = True if run.active_phase_lock is None else lock_dead
    else:
        # SQLite returns naive datetimes; ``cutoff`` is tz-aware. Make
        # the comparison naive-vs-naive when the stored side has no
        # tzinfo, otherwise both sides should already be UTC.
        last_evt_compare = (
            last_phase_event_at
            if last_phase_event_at.tzinfo is not None
            else last_phase_event_at.replace(tzinfo=cutoff.tzinfo)
        )
        recoverable = last_evt_compare < cutoff and (run.active_phase_lock is None or lock_dead)

    if not recoverable:
        return

    # Mark stale running phase_versions for this phase (any branch)
    # as failed. Without this, ``maybe_run_with_versioning`` on the
    # next start_<phase> would skip the wrapper because a running
    # row already exists, and the new attempt would silently land
    # without a phase_versions row.
    stale_pvs = session.scalars(
        select(PhaseVersion).where(
            PhaseVersion.run_id == run.id,
            PhaseVersion.phase == phase,
            PhaseVersion.status == "running",
        ),
    ).all()
    for pv in stale_pvs:
        pv.status = "failed"
        pv.completed_at = utcnow()

    if run.active_phase_lock is not None:
        force_clear_phase_lock(session, run)

    guidance = (
        f"{phase} 阶段似乎中断了 — worker 进程在写入文件之前已退出。点击下方"
        f"「重试该步骤」重新启动该阶段。The {phase} phase appears to have "
        f"been interrupted — worker exited mid-flight. Click 'Retry phase' "
        f"to restart."
    )
    # Order matters: ``transition`` writes a ``state_transition`` event
    # whose top-level payload does NOT carry ``phase``, so the
    # FailureResolutionBanner (which reads ``lastEvent.payload.phase``)
    # would see null and disable the retry button. By appending
    # ``phase_failed`` AFTER the transition we make it the latest
    # event, so the banner picks up the phase correctly. (Bug surfaced
    # on prod run_6c0640: zombie recovery succeeded but the retry
    # button rendered disabled because lastEvent was the transition
    # event.)
    try:
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason=f"zombie_recovered:{phase}",
            payload={"phase": phase, "guidance": guidance},
        )
    except InvalidTransition:
        # If the run is already in a terminal state for some reason,
        # don't escalate; the caller's start_<phase> guard will 409
        # naturally and the audit trail above explains why.
        return
    append_event(
        session,
        run,
        "phase_failed",
        {
            "phase": phase,
            "failure_class": "zombie_recovered",
            "guidance": guidance,
            "stale_pv_count": len(stale_pvs),
        },
    )
    session.flush()


def _start_express_generation(run: Run, session: Session) -> ProposalJobResponse:
    _recover_zombie_running_phase(session, run, "express")
    if run.state not in {"DOMAIN_LOADED", "EXPRESS_FAILED"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Express can only start from DOMAIN_LOADED or EXPRESS_FAILED",
        )
    previous_state = run.state
    token = _claim_or_409(session, run, "express")
    transition(
        run,
        "EXPRESS_RUNNING",
        session,
        reason="express_generation_enqueued",
        payload={"runner": "express", "queue": "rq"},
    )
    append_event(
        session,
        run,
        "express_generation_enqueued",
        {"runner": "express", "queue": "rq"},
    )
    session.commit()
    try:
        job_id = enqueue_express_job(run.id, lock_token=token)
    except Exception:
        release_phase_lock(session, run, "express", token)
        run.state = previous_state
        append_event(
            session,
            run,
            "express_enqueue_failed",
            {"runner": "express", "restored_state": previous_state},
        )
        session.commit()
        raise
    return ProposalJobResponse(run_id=run.id, job_id=job_id, expected_state="EXPRESS_RUNNING")


@app.post(
    "/api/runs/{run_id}/proposal",
    response_model=ProposalJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_proposal(
    run_id: str,
    request: ProposalDraftRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> ProposalJobResponse:
    run = _get_user_run_or_404(session, run_id, user)
    if _run_generation_mode(run) == EXPRESS_MODE:
        return _start_express_generation(run, session)
    _assert_deep_generation_mode(run, "proposal")
    _recover_zombie_running_phase(session, run, "proposal")
    _recover_failed_fixable_for_phase(session, run, "proposal")
    if run.state not in {"DOMAIN_LOADED", "USER_PROPOSAL_REVIEW"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposal can only start from DOMAIN_LOADED or USER_PROPOSAL_REVIEW",
        )
    _enforce_input_safety(
        request.user_draft,
        context_hint=SAFETY_CONTEXT_PROPOSAL_USER_DRAFT,
    )
    token = _claim_or_409(session, run, "proposal")
    settings = get_settings()
    if settings.sync_worker:
        run_proposal_draft(run.id, session, request.user_draft, lock_token=token)
        return ProposalJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="PROPOSAL_DRAFTING",
        )
    try:
        job_id = enqueue_proposal_job(run.id, request.user_draft, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "proposal", token)
        raise
    return ProposalJobResponse(run_id=run.id, job_id=job_id, expected_state="PROPOSAL_DRAFTING")


@app.get("/api/runs/{run_id}/proposal", response_model=ProposalResponse)
def get_proposal(
    run_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> ProposalResponse:
    run = _get_user_run_or_404(session, run_id, user)
    try:
        payload = load_proposal_payload(run)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="proposal not found",
        ) from exc
    return _proposal_response(payload)


@app.put("/api/runs/{run_id}/proposal", response_model=ProposalResponse)
def save_proposal(
    run_id: str,
    request: ProposalSaveRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> ProposalResponse:
    """Save (or re-save) the proposal.

    Initially this endpoint hard-required ``USER_PROPOSAL_REVIEW``. The
    user reported (2026-05-01) that after accepting the proposal and
    moving on to scout, the proposal subview becomes permanently
    read-only — losing the "every phase content editable" property
    that PR-A2 promised. We now accept edits whenever the run has a
    proposal at all and is not currently running an agent. When the
    edit happens past USER_PROPOSAL_REVIEW we mark the active
    branch's earliest-completed-downstream phase as stale (per codex
    amendment 3 on 2026-05-01) so the existing stale banner can
    prompt the user to rerun the affected phase chain.
    """
    from autoessay.branches import ensure_main_branch, set_branch_stale
    from autoessay.phase_rerun import (
        PHASES,
        RUNNING_STATES,
        has_completed_output,
    )

    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    if run.cancel_requested_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this run is cancelled",
        )
    # Initial flow: USER_PROPOSAL_REVIEW always allowed (and is the
    # only state where saving without a prior proposal makes sense).
    # Post-accept editing requires a prior proposal AND must wait for
    # any in-flight agent to finish.
    is_initial_state = run.state == "USER_PROPOSAL_REVIEW"
    if not is_initial_state:
        if int(run.proposal_version or 0) < 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Proposal edits require an existing proposal",
            )
        if run.state in RUNNING_STATES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"another phase is currently running ({run.state}); "
                    "wait for it to finish before editing the proposal"
                ),
            )

    # Concurrency token (codex amendment 6): if the client echoed back
    # a base_version, reject if the head moved underneath. We accept
    # ``None`` for backwards compatibility with the legacy client; new
    # clients always send. Compare against the prior ``proposal_version``
    # because by definition every save bumps that, so equality at the
    # time the client loaded the page is the right invariant.
    if request.base_version is not None:
        current_version = int(run.proposal_version or 0)
        if request.base_version != current_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "another save committed since you opened the editor "
                    f"(base_version={request.base_version}, current="
                    f"{current_version}); reload to merge"
                ),
            )

    # Pre-compute branch + earliest-completed once: we use the answer
    # both to validate ``mode=replace`` and to mark stale on a
    # successful ``mode=new`` save. Codex amendment 5: dual-check —
    # branch-aware RunHead, with a legacy-file-glob fallback for
    # vanilla first runs.
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    earliest_completed: str | None = None
    for candidate in PHASES:
        if has_completed_output(
            run,
            candidate,
            session=session,
            branch_id=branch_id,
        ) or has_completed_output(run, candidate):
            earliest_completed = candidate
            break

    mode = (request.mode or "new").lower()
    if mode == "replace" and earliest_completed is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "cannot replace the proposal because phase "
                f"{earliest_completed!r} has already produced output; "
                "save with mode='new' instead"
            ),
        )

    if mode == "replace":
        creator = "user_replace" if is_initial_state else "user_post_accept_replace"
    else:
        creator = "user" if is_initial_state else "user_post_accept"

    _enforce_proposal_json_safety(request.proposal_json)

    try:
        payload = save_proposal_version(
            run,
            session,
            request.proposal_json,
            creator=creator,
            replace=mode == "replace",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not is_initial_state and mode == "new" and earliest_completed is not None:
        set_branch_stale(session, run, earliest_completed, branch_id=branch_id)
    session.commit()
    return _proposal_response(payload)


class ResearchKernelEditRequest(BaseModel):
    paper_mode: str = Field(..., min_length=1)
    kernel: dict[str, object] = Field(default_factory=dict)
    base_proposal_version: int = Field(..., ge=0)
    base_kernel_hash: str = Field(..., min_length=1)
    accept_developer_preview: bool = False


class ResearchKernelEditResponse(BaseModel):
    paper_mode: str
    kernel: dict[str, object]
    proposal_version: int
    research_kernel_hash: str
    stale_from_phase: str | None


@app.put(
    "/api/runs/{run_id}/research_kernel",
    response_model=ResearchKernelEditResponse,
)
def edit_research_kernel(
    run_id: str,
    request: ResearchKernelEditRequest,
    session: SessionDependency,
) -> ResearchKernelEditResponse:
    """PR-C0.b1: edit the research kernel + paper_mode.

    Three-branch dispatch (codex round-1 amendment 1):
    - pre-proposal (proposal_version == 0): DB only, no file I/O.
    - no downstream completed: replace research_kernel_v{N}.json
      in place via save_proposal_version(replace=True).
    - downstream completed: bump proposal_version, clone proposal
      artifact, write new kernel snapshot, mark stale on every
      non-deleted branch with completed downstream.

    Concurrency: rejects on (proposal_version, kernel_hash)
    mismatch. Acquires the ``research_kernel_edit`` short-lived
    edit lock to close the TOCTOU between state check and DB
    commit.
    """
    from datetime import datetime, timezone

    from autoessay.branches import set_branch_stale
    from autoessay.paper_modes import (
        ModeNotAvailableError,
        assert_mode_creatable,
    )
    from autoessay.research_kernel import (
        write_kernel_snapshot,
    )

    run = _get_run_for_mutation_or_404(session, run_id)

    # State guard: reject if a phase is currently running OR if
    # active_phase_lock is held (codex round-2 amendment 5: TOCTOU).
    from autoessay.phase_rerun import RUNNING_STATES

    if run.cancel_requested_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this run is cancelled",
        )
    if run.state in RUNNING_STATES or run.active_phase_lock is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "another phase is currently running; wait for it to "
                "finish before editing the research kernel"
            ),
        )

    # Concurrency tokens FIRST (codex round-3 answer 1: stale-token
    # checks run before mode-change-guard so true races still
    # return 409 vs 400 for structurally-invalid requests).
    if int(run.proposal_version or 0) != request.base_proposal_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"base_proposal_version stale: server has "
                f"{int(run.proposal_version or 0)}, request has "
                f"{request.base_proposal_version}"
            ),
        )
    current_hash = compute_kernel_hash(
        run.paper_mode or "case_analysis",
        dict(run.research_kernel_json or {"kernel_schema_version": 1}),
    )
    if current_hash != request.base_kernel_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="base_kernel_hash stale; reload before retrying",
        )

    # Mode validation. ack-on-transition-only semantics
    # (codex round-2 amendment 2 + round-3 amendment 2): preserving
    # the current mode does NOT require re-acking developer_preview.
    # Endpoint-local helper; assert_mode_creatable stays a pure
    # registry/run-creation validator.
    current_paper_mode = run.paper_mode or "case_analysis"
    is_mode_change = request.paper_mode != current_paper_mode
    try:
        assert_mode_creatable(
            request.paper_mode,
            accept_developer_preview=(request.accept_developer_preview or not is_mode_change),
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown paper_mode: {request.paper_mode!r}",
        ) from exc
    except ModeNotAvailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # Mode-change guard: paper_mode immutable once a proposal exists
    # (codex round-2 amendment 1 + round-3 amendment 1). Prevents
    # curl/SDK callers from flipping modes after downstream work has
    # started. To switch modes, user must create a new run.
    if int(run.proposal_version or 0) >= 1 and is_mode_change:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "paper_mode cannot be changed after the proposal "
                "exists; create a new run with the desired mode"
            ),
        )

    # No-op if neither paper_mode nor kernel changed.
    new_kernel = dict(request.kernel)
    if "kernel_schema_version" not in new_kernel:
        existing_version_raw = (run.research_kernel_json or {}).get(
            "kernel_schema_version",
            1,
        )
        if isinstance(existing_version_raw, int):
            new_kernel["kernel_schema_version"] = existing_version_raw or 1
        elif isinstance(existing_version_raw, str):
            try:
                new_kernel["kernel_schema_version"] = int(existing_version_raw) or 1
            except ValueError:
                new_kernel["kernel_schema_version"] = 1
        else:
            new_kernel["kernel_schema_version"] = 1
    _enforce_research_kernel_safety(new_kernel)
    if new_kernel == dict(run.research_kernel_json or {}) and request.paper_mode == (
        run.paper_mode or "case_analysis"
    ):
        return ResearchKernelEditResponse(
            paper_mode=run.paper_mode or "case_analysis",
            kernel=dict(run.research_kernel_json or {"kernel_schema_version": 1}),
            proposal_version=int(run.proposal_version or 0),
            research_kernel_hash=current_hash,
            stale_from_phase=_branch_stale_from_phase(session, run),
        )

    # Acquire short-lived edit lock (codex round-2 amendment 2).
    edit_token = new_lock_token()
    if not claim_phase_lock(session, run, "research_kernel_edit", edit_token):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another research-kernel edit is in progress",
        )
    session.commit()
    try:
        # Codex round-3 amendment 3: assign new state to run BEFORE
        # save_proposal_version so the snapshot captures the edited
        # kernel.
        run.paper_mode = request.paper_mode
        run.research_kernel_json = new_kernel

        proposal_version = int(run.proposal_version or 0)
        stale_marks = stale_marks_after_kernel_edit(session, run.id)
        downstream_completed = bool(stale_marks)

        if proposal_version == 0:
            # Proposal-less path: keep DB-only/no-snapshot semantics,
            # but if the pipeline already ran from a kernel-only flow,
            # source discovery and everything downstream now depend on
            # the previous kernel.
            for branch_id, _earliest_phase in stale_marks:
                set_branch_stale(session, run, "scout", branch_id=branch_id)
        elif not downstream_completed:
            # No downstream completed: overwrite snapshot at current
            # version, no proposal bump. Routes through
            # save_proposal_version(replace=True) so the proposal
            # file rewrite + kernel snapshot stay in sync.
            current_proposal = _read_current_proposal_json(run)
            if current_proposal is not None:
                save_proposal_version(
                    run,
                    session,
                    current_proposal,
                    creator="research_kernel_edit",
                    replace=True,
                )
            else:
                # No proposal file on disk despite version >= 1: just
                # write the snapshot directly (legacy backfilled run).
                write_kernel_snapshot(
                    run_dir=Path(run.run_dir),
                    proposal_version=proposal_version,
                    paper_mode=request.paper_mode,
                    kernel=new_kernel,
                    timestamp_utc=datetime.now(timezone.utc).isoformat(),
                )
        else:
            # Downstream completed: bump version, clone proposal,
            # mark stale on every non-deleted branch with
            # completed work.
            current_proposal = _read_current_proposal_json(run)
            if current_proposal is not None:
                save_proposal_version(
                    run,
                    session,
                    current_proposal,
                    creator="research_kernel_edit",
                    replace=False,
                )
            else:
                # Legacy run with no proposal file: bump and write
                # snapshot only.
                run.proposal_version = proposal_version + 1
                write_kernel_snapshot(
                    run_dir=Path(run.run_dir),
                    proposal_version=proposal_version + 1,
                    paper_mode=request.paper_mode,
                    kernel=new_kernel,
                    timestamp_utc=datetime.now(timezone.utc).isoformat(),
                )
            for branch_id, earliest_phase in stale_marks:
                set_branch_stale(session, run, earliest_phase, branch_id=branch_id)

        new_hash = compute_kernel_hash(request.paper_mode, new_kernel)
        session.commit()
        session.refresh(run)
        return ResearchKernelEditResponse(
            paper_mode=run.paper_mode or "case_analysis",
            kernel=dict(run.research_kernel_json or {"kernel_schema_version": 1}),
            proposal_version=int(run.proposal_version or 0),
            research_kernel_hash=new_hash,
            stale_from_phase=_branch_stale_from_phase(session, run),
        )
    finally:
        release_phase_lock(session, run, "research_kernel_edit", edit_token)
        session.commit()


def _read_current_proposal_json(run: Run) -> dict[str, object] | None:
    """Read the current proposal_v{N}.json contents for cloning
    on kernel edit. Returns ``None`` if the file is missing
    (legacy backfilled run with no on-disk proposal yet)."""
    if not run.proposal_content_path:
        return None
    json_path = Path(run.run_dir) / run.proposal_content_path
    if not json_path.exists():
        return None
    try:
        import json as _json

        parsed = _json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return dict(parsed)


@app.post(
    "/api/runs/{run_id}/scout",
    response_model=ScoutJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_scout(run_id: str, session: SessionDependency) -> ScoutJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "scout")
    _recover_zombie_running_phase(session, run, "scout")
    _recover_failed_fixable_for_phase(session, run, "scout")
    if run.state not in {"DOMAIN_LOADED", "USER_PROPOSAL_REVIEW"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scout can only start from DOMAIN_LOADED or USER_PROPOSAL_REVIEW",
        )
    token = _claim_or_409(session, run, "scout")
    settings = get_settings()
    if settings.sync_worker:
        run_scout(run.id, session, lock_token=token)
        return ScoutJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="SCOUT_RUNNING",
        )
    try:
        job_id = enqueue_scout_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "scout", token)
        raise
    return ScoutJobResponse(run_id=run.id, job_id=job_id, expected_state="SCOUT_RUNNING")


@app.post(
    "/api/runs/{run_id}/curator",
    response_model=CuratorJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_curator(run_id: str, session: SessionDependency) -> CuratorJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "curator")
    _recover_zombie_running_phase(session, run, "curator")
    _recover_failed_fixable_for_phase(session, run, "curator")
    if run.state != "USER_SEARCH_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Curator can only start from USER_SEARCH_REVIEW",
        )
    assert_phase_ready(run, "curator", session)
    _require_source_review_checkpoint(
        run,
        session,
        checkpoint_type="USER_SEARCH_REVIEW",
        upstream_phase="scout",
        consumer_phase="curator",
    )
    token = _claim_or_409(session, run, "curator")
    settings = get_settings()
    if settings.sync_worker:
        run_curator(run.id, session, lock_token=token)
        return CuratorJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="CURATOR_RUNNING",
        )
    try:
        job_id = enqueue_curator_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "curator", token)
        raise
    return CuratorJobResponse(run_id=run.id, job_id=job_id, expected_state="CURATOR_RUNNING")


@app.post(
    "/api/runs/{run_id}/synthesizer",
    response_model=SynthesizerJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_synthesizer(run_id: str, session: SessionDependency) -> SynthesizerJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "synthesizer")
    _recover_zombie_running_phase(session, run, "synthesizer")
    _recover_failed_fixable_for_phase(session, run, "synthesizer")
    if run.state != "USER_DEEP_DIVE_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Synthesizer can only start from USER_DEEP_DIVE_REVIEW",
        )
    assert_phase_ready(run, "synthesizer", session)
    _require_source_review_checkpoint(
        run,
        session,
        checkpoint_type="USER_DEEP_DIVE_REVIEW",
        upstream_phase="curator",
        consumer_phase="synthesizer",
    )
    token = _claim_or_409(session, run, "synthesizer")
    settings = get_settings()
    if settings.sync_worker:
        run_synthesizer(run.id, session, lock_token=token)
        return SynthesizerJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="SYNTHESIZER_RUNNING",
        )
    try:
        job_id = enqueue_synthesizer_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "synthesizer", token)
        raise
    return SynthesizerJobResponse(
        run_id=run.id,
        job_id=job_id,
        expected_state="SYNTHESIZER_RUNNING",
    )


@app.post(
    "/api/runs/{run_id}/ideator",
    response_model=IdeatorJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_ideator(run_id: str, session: SessionDependency) -> IdeatorJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "ideator")
    _recover_zombie_running_phase(session, run, "ideator")
    _recover_failed_fixable_for_phase(session, run, "ideator")
    # PR-C2.b: ideator accepts USER_FIELD_REVIEW (lens-skipped path)
    # AND USER_LENS_REVIEW (post-lens path). Use the shared validity
    # set so phase-history runnable_now + this endpoint stay in sync.
    from autoessay.phase_rerun import IDEATOR_VALID_INPUT_STATES

    if run.state not in IDEATOR_VALID_INPUT_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ideator can only start from one of "
                f"{sorted(IDEATOR_VALID_INPUT_STATES)}, got {run.state}"
            ),
        )
    # Codex round-4 #1 (2026-05-03): theory_article paper_mode
    # MUST traverse the framework_lens phase. The lens runner itself
    # FAILs_FIXABLE on theory_article + zero lens inputs, but a user
    # can bypass that path entirely by clicking "start ideator"
    # directly from USER_FIELD_REVIEW. Reject the skip so theory
    # papers are forced through the lens checkpoint (USER_LENS_REVIEW
    # is the post-lens state — accept it).
    if run.paper_mode == "theory_article" and run.state == "USER_FIELD_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "theory_article paper_mode requires running framework_lens "
                "before ideator; please start the framework_lens phase "
                "first, then ideator from USER_LENS_REVIEW"
            ),
        )
    token = _claim_or_409(session, run, "ideator")
    settings = get_settings()
    if settings.sync_worker:
        run_ideator(run.id, session, lock_token=token)
        return IdeatorJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="IDEATOR_RUNNING",
        )
    try:
        job_id = enqueue_ideator_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "ideator", token)
        raise
    return IdeatorJobResponse(run_id=run.id, job_id=job_id, expected_state="IDEATOR_RUNNING")


@app.post(
    "/api/runs/{run_id}/tension_extraction",
    response_model=IdeatorJobResponse,  # same shape: run_id / job_id / expected_state
    status_code=status.HTTP_202_ACCEPTED,
)
def start_tension_extraction(run_id: str, session: SessionDependency) -> IdeatorJobResponse:
    """PR-C3.b: explicit user trigger for the optional tension_extraction
    phase. Gated by ``Settings.tension_taxonomy_enabled``; when False
    the endpoint returns 409 so the frontend hides the action.
    Caller can advance from ``USER_FIELD_REVIEW`` (initial path) or
    ``USER_TENSION_REVIEW`` (rerun)."""
    from autoessay.agents.tension_extraction import (
        run_tension_extraction,
        should_run_tension_extraction,
    )

    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "tension_extraction")
    _recover_zombie_running_phase(session, run, "tension_extraction")
    _recover_failed_fixable_for_phase(session, run, "tension_extraction")
    valid_inputs = {"USER_FIELD_REVIEW", "USER_TENSION_REVIEW"}
    if run.state not in valid_inputs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"TensionExtraction can only start from {sorted(valid_inputs)}, got {run.state}"
            ),
        )

    # Operational gate (codex round-2 amendment 6).
    synth_path = Path(run.run_dir) / "synthesis" / "synthesizer.json"
    synth_payload: dict[str, object] | None = None
    if synth_path.exists():
        try:
            synth_payload = json.loads(synth_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            synth_payload = None
    paper_mode = run.paper_mode or "case_analysis"
    if not should_run_tension_extraction(
        paper_mode=paper_mode,
        synthesizer_payload=synth_payload,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "TensionExtraction phase is disabled "
                "(AUTOESSAY_TENSION_TAXONOMY_ENABLED=0 or no synthesizer claims)."
            ),
        )

    token = _claim_or_409(session, run, "tension_extraction")
    run_tension_extraction(run.id, session, lock_token=token)
    return IdeatorJobResponse(
        run_id=run.id,
        job_id="sync",
        expected_state="TENSION_EXTRACTION_RUNNING",
    )


class TensionPoleResponse(BaseModel):
    label: str
    claim_refs: list[dict[str, str]]


class TensionEntryResponse(BaseModel):
    tension_id: str
    class_id: str
    discipline_subtype: str | None = None
    summary: str
    poles: list[TensionPoleResponse]
    boundary_fields: dict[str, str]
    research_role_align: str | None = None


class TensionExtractionResponse(BaseModel):
    run_id: str
    artifact_present: bool
    schema_version: int | None
    paper_mode: str | None = None
    extracted_at: str | None = None
    tensions: list[TensionEntryResponse]


@app.get(
    "/api/runs/{run_id}/tension_extraction",
    response_model=TensionExtractionResponse,
)
def get_tension_extraction(run_id: str, session: SessionDependency) -> TensionExtractionResponse:
    """PR-C3.b: expose the tension artifact so the frontend
    TensionSubview can render. Reads
    ``synthesis/tension_extraction.json`` directly. Returns
    ``artifact_present=False`` when the phase hasn't run / was
    skipped / artifact is malformed."""
    run = _get_run_or_404(session, run_id)
    artifact_path = Path(run.run_dir) / "synthesis" / "tension_extraction.json"
    empty = TensionExtractionResponse(
        run_id=run.id,
        artifact_present=False,
        schema_version=None,
        tensions=[],
    )
    if not artifact_path.exists():
        return empty
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty
    if not isinstance(payload, dict):
        return empty
    tensions_payload = payload.get("tensions", [])
    tensions: list[TensionEntryResponse] = []
    if isinstance(tensions_payload, list):
        for entry in tensions_payload:
            if not isinstance(entry, dict):
                continue
            poles_payload = entry.get("poles", [])
            poles: list[TensionPoleResponse] = []
            if isinstance(poles_payload, list):
                for pole in poles_payload:
                    if not isinstance(pole, dict):
                        continue
                    refs = pole.get("claim_refs", [])
                    coerced_refs: list[dict[str, str]] = []
                    if isinstance(refs, list):
                        for ref in refs:
                            if isinstance(ref, dict):
                                coerced_refs.append(
                                    {
                                        "track": str(ref.get("track", "")),
                                        "source_id": str(ref.get("source_id", "")),
                                        "claim_id": str(ref.get("claim_id", "")),
                                    },
                                )
                    poles.append(
                        TensionPoleResponse(
                            label=str(pole.get("label", "")),
                            claim_refs=coerced_refs,
                        ),
                    )
            tensions.append(
                TensionEntryResponse(
                    tension_id=str(entry.get("tension_id", "")),
                    class_id=str(entry.get("class_id", "")),
                    discipline_subtype=entry.get("discipline_subtype"),
                    summary=str(entry.get("summary", "")),
                    poles=poles,
                    boundary_fields=entry.get("boundary_fields") or {},
                    research_role_align=entry.get("research_role_align"),
                ),
            )
    return TensionExtractionResponse(
        run_id=run.id,
        artifact_present=True,
        schema_version=payload.get("schema_version"),
        paper_mode=payload.get("paper_mode"),
        extracted_at=payload.get("extracted_at"),
        tensions=tensions,
    )


@app.post(
    "/api/runs/{run_id}/framework_lens",
    response_model=IdeatorJobResponse,  # same shape: run_id / job_id / expected_state
    status_code=status.HTTP_202_ACCEPTED,
)
def start_framework_lens(run_id: str, session: SessionDependency) -> IdeatorJobResponse:
    """PR-C2.b: explicit user trigger for the optional framework_lens
    phase. Caller is the workspace status panel button shown when
    the run is at ``USER_FIELD_REVIEW`` AND
    ``framework_lens.should_run_framework_lens`` returns True.

    The lens phase is skippable for non-theory papers without lens
    inputs (caller may directly POST ``/ideator`` instead). For
    ``theory_article`` runs without lens inputs, the agent itself
    transitions to FAILED_FIXABLE inside ``run_framework_lens``
    rather than silently producing an empty USER_LENS_REVIEW.
    """
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "framework_lens")
    _recover_zombie_running_phase(session, run, "framework_lens")
    _recover_failed_fixable_for_phase(session, run, "framework_lens")
    if run.state != "USER_FIELD_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="FrameworkLens can only start from USER_FIELD_REVIEW",
        )
    token = _claim_or_409(session, run, "framework_lens")
    settings = get_settings()
    if settings.sync_worker:
        run_framework_lens(run.id, session, lock_token=token)
        return IdeatorJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="FRAMEWORK_LENS_RUNNING",
        )
    # No async-worker path yet; framework_lens is fast (deterministic
    # stub). Sync execution covers both prod and stub. If the phase
    # gains an LLM-driven path later, plug a queue here.
    run_framework_lens(run.id, session, lock_token=token)
    return IdeatorJobResponse(
        run_id=run.id,
        job_id="sync",
        expected_state="FRAMEWORK_LENS_RUNNING",
    )


class FrameworkLensSignalResponse(BaseModel):
    lens_name: str
    key_concepts: list[str]
    source_id: str
    applicability_to_kernel: str


class SynthesizerInputRefResponse(BaseModel):
    synthesizer_pv_id: str | None = None
    synthesizer_artifact_hash: str | None = None


class FrameworkLensResponse(BaseModel):
    run_id: str
    artifact_present: bool
    schema_version: int | None
    synthesizer_input_ref: SynthesizerInputRefResponse | None = None
    signals: list[FrameworkLensSignalResponse]


@app.get(
    "/api/runs/{run_id}/framework_lens",
    response_model=FrameworkLensResponse,
)
def get_framework_lens(run_id: str, session: SessionDependency) -> FrameworkLensResponse:
    """PR-C2.b follow-up (Tier 4): expose the lens artifact so the
    frontend Lens tab can render signals + applicability text. Reads
    ``synthesis/framework_lens.json`` directly; returns
    ``artifact_present=False`` for runs that haven't run lens yet
    (or skipped it via the direct-ideator path)."""
    run = _get_run_or_404(session, run_id)
    artifact_path = Path(run.run_dir) / "synthesis" / "framework_lens.json"
    if not artifact_path.exists():
        return FrameworkLensResponse(
            run_id=run.id,
            artifact_present=False,
            schema_version=None,
            synthesizer_input_ref=None,
            signals=[],
        )
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return FrameworkLensResponse(
            run_id=run.id,
            artifact_present=False,
            schema_version=None,
            synthesizer_input_ref=None,
            signals=[],
        )
    if not isinstance(payload, dict):
        return FrameworkLensResponse(
            run_id=run.id,
            artifact_present=False,
            schema_version=None,
            synthesizer_input_ref=None,
            signals=[],
        )
    raw_signals = payload.get("signals", [])
    signals: list[FrameworkLensSignalResponse] = []
    if isinstance(raw_signals, list):
        for sig in raw_signals:
            if not isinstance(sig, dict):
                continue
            key_concepts_raw = sig.get("key_concepts", [])
            if not isinstance(key_concepts_raw, list):
                key_concepts_raw = []
            signals.append(
                FrameworkLensSignalResponse(
                    lens_name=str(sig.get("lens_name") or ""),
                    key_concepts=[str(k) for k in key_concepts_raw if isinstance(k, str)],
                    source_id=str(sig.get("source_id") or ""),
                    applicability_to_kernel=str(sig.get("applicability_to_kernel") or ""),
                ),
            )
    schema_v_raw = payload.get("schema_version")
    schema_v = schema_v_raw if isinstance(schema_v_raw, int) else None
    raw_ref = payload.get("synthesizer_input_ref")
    input_ref = (
        SynthesizerInputRefResponse(
            synthesizer_pv_id=(
                str(raw_ref["synthesizer_pv_id"])
                if isinstance(raw_ref.get("synthesizer_pv_id"), str)
                else None
            ),
            synthesizer_artifact_hash=(
                str(raw_ref["synthesizer_artifact_hash"])
                if isinstance(raw_ref.get("synthesizer_artifact_hash"), str)
                else None
            ),
        )
        if isinstance(raw_ref, dict)
        else None
    )
    return FrameworkLensResponse(
        run_id=run.id,
        artifact_present=True,
        schema_version=schema_v,
        synthesizer_input_ref=input_ref,
        signals=signals,
    )


@app.post(
    "/api/runs/{run_id}/drafter",
    response_model=DrafterJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_drafter(run_id: str, session: SessionDependency) -> DrafterJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "drafter")
    _recover_zombie_running_phase(session, run, "drafter")
    _recover_failed_fixable_for_phase(session, run, "drafter")
    if run.state not in {"USER_NOVELTY_REVIEW", "DRAFTER_RUNNING"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Drafter can only start from USER_NOVELTY_REVIEW or DRAFTER_RUNNING",
        )
    # Shared readiness check — same registry that ``rerun_phase`` uses,
    # so start_* and rerun reject identical mis-clicks identically.
    assert_phase_ready(run, "drafter", session)
    token = _claim_or_409(session, run, "drafter")
    settings = get_settings()
    if settings.sync_worker:
        run_drafter(run.id, session, lock_token=token)
        return DrafterJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="DRAFTER_RUNNING",
        )
    try:
        job_id = enqueue_drafter_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "drafter", token)
        raise
    return DrafterJobResponse(run_id=run.id, job_id=job_id, expected_state="DRAFTER_RUNNING")


@app.post(
    "/api/runs/{run_id}/stylist",
    response_model=StylistJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_stylist(run_id: str, session: SessionDependency) -> StylistJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "stylist")
    _recover_zombie_running_phase(session, run, "stylist")
    _recover_failed_fixable_for_phase(session, run, "stylist")
    if run.state not in {"DRAFTER_RUNNING", "USER_REVISION_REVIEW"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Stylist can only start from DRAFTER_RUNNING or USER_REVISION_REVIEW",
        )
    assert_phase_ready(run, "stylist", session)
    token = _claim_or_409(session, run, "stylist")
    settings = get_settings()
    if settings.sync_worker:
        run_stylist(run.id, session, lock_token=token)
        return StylistJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="STYLIST_RUNNING",
        )
    try:
        job_id = enqueue_stylist_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "stylist", token)
        raise
    return StylistJobResponse(run_id=run.id, job_id=job_id, expected_state="STYLIST_RUNNING")


@app.post(
    "/api/runs/{run_id}/critic",
    response_model=CriticJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_critic(run_id: str, session: SessionDependency) -> CriticJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "critic")
    _recover_zombie_running_phase(session, run, "final_rewrite")
    _recover_zombie_running_phase(session, run, "critic")
    _recover_failed_fixable_for_phase(session, run, "critic")
    if run.state != "USER_REVISION_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Critic can only start from USER_REVISION_REVIEW",
        )
    assert_phase_ready(run, "critic", session)
    settings = get_settings()
    if settings.final_rewrite_enabled:
        token = _claim_or_409(session, run, "final_rewrite")
        if settings.sync_worker:
            run_final_rewrite_then_critic(run.id, session, lock_token=token)
            return CriticJobResponse(
                run_id=run.id,
                job_id="sync",
                expected_state="REWRITE_RUNNING",
            )
        try:
            job_id = enqueue_final_rewrite_job(run.id, lock_token=token)
        except Exception:
            _release_after_enqueue_failure(session, run, "final_rewrite", token)
            raise
        return CriticJobResponse(run_id=run.id, job_id=job_id, expected_state="REWRITE_RUNNING")
    token = _claim_or_409(session, run, "critic")
    if settings.sync_worker:
        run_critic(run.id, session, lock_token=token)
        return CriticJobResponse(run_id=run.id, job_id="sync", expected_state="CRITIC_RUNNING")
    try:
        job_id = enqueue_critic_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "critic", token)
        raise
    return CriticJobResponse(run_id=run.id, job_id=job_id, expected_state="CRITIC_RUNNING")


@app.post(
    "/api/runs/{run_id}/integrity",
    response_model=IntegrityJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_integrity(run_id: str, session: SessionDependency) -> IntegrityJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "integrity")
    _recover_zombie_running_phase(session, run, "integrity")
    _recover_failed_fixable_for_phase(session, run, "integrity")
    if run.state not in {"USER_EXTERNAL_SCAN_APPROVAL", "FAILED_VENDOR"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Integrity can only start from USER_EXTERNAL_SCAN_APPROVAL or FAILED_VENDOR",
        )
    decision = latest_external_scan_decision(session, run)
    if decision is None or decision.get("approve") is not True:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Integrity requires an approved external scan checkpoint",
        )
    token = _claim_or_409(session, run, "integrity")
    settings = get_settings()
    if settings.sync_worker:
        run_integrity(run.id, session, lock_token=token)
        return IntegrityJobResponse(
            run_id=run.id,
            job_id="sync",
            expected_state="INTEGRITY_RUNNING",
        )
    try:
        job_id = enqueue_integrity_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "integrity", token)
        raise
    return IntegrityJobResponse(run_id=run.id, job_id=job_id, expected_state="INTEGRITY_RUNNING")


@app.post(
    "/api/runs/{run_id}/export",
    response_model=ExportsJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_exports(run_id: str, session: SessionDependency) -> ExportsJobResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _assert_deep_generation_mode(run, "exports")
    _recover_zombie_running_phase(session, run, "exports")
    _recover_failed_fixable_for_phase(session, run, "exports")
    if run.state != "USER_FINAL_ACCEPTANCE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Exports can only start from USER_FINAL_ACCEPTANCE",
        )
    assert_phase_ready(run, "exports", session)
    token = _claim_or_409(session, run, "exports")
    settings = get_settings()
    if settings.sync_worker:
        run_exports(run.id, session, lock_token=token)
        return ExportsJobResponse(run_id=run.id, job_id="sync", expected_state="EXPORTS_RUNNING")
    try:
        job_id = enqueue_exports_job(run.id, lock_token=token)
    except Exception:
        _release_after_enqueue_failure(session, run, "exports", token)
        raise
    return ExportsJobResponse(run_id=run.id, job_id=job_id, expected_state="EXPORTS_RUNNING")


# ---------------------------------------------------------------------------
# Phase rerun (codex-AGREEd #2 stage 1)
# ---------------------------------------------------------------------------


class RerunResponse(BaseModel):
    run_id: str
    phase: str
    state: str
    stale_from_phase: str | None


_PHASE_RUNNERS: dict[str, Any] = {
    "scout": run_scout,
    "curator": run_curator,
    "synthesizer": run_synthesizer,
    # PR-C2.a: optional lens phase between synthesizer and ideator.
    # Skip semantics evaluated by callers via
    # ``framework_lens.should_run_framework_lens``.
    "framework_lens": run_framework_lens,
    "ideator": run_ideator,
    "drafter": run_drafter,
    "stylist": run_stylist,
    "critic": run_critic,
    "integrity": run_integrity,
    "exports": run_exports,
}


class RerunRequest(BaseModel):
    """Optional body for the rerun endpoint.

    ``draft_hash``: when set, the rerun endpoint compares it against
    the current ``phase_prompt_drafts`` row's ``content_hash`` for
    the requested ``prompt_key`` and rejects with 409 if they
    disagree (codex round-1 round-trip safety: another tab may have
    edited the draft after the user hit "Save and rerun").

    ``prompt_key``: which prompt surface the ``draft_hash`` refers
    to. Defaults to ``"main"`` so existing single-key clients keep
    working (Stage 3.A.2 made this explicit because phases can now
    expose multiple keys, e.g. drafter has 9).
    """

    prompt_key: str = "main"
    draft_hash: str | None = None


def _branch_stale_from_phase(session: Session, run: Run) -> str | None:
    """Look up ``stale_from_phase`` from the run's active branch.

    Stage 2.C moved this column from ``runs`` to ``branches``; this
    helper centralizes the active-branch lookup so all the response-
    building call sites stay readable.
    """
    from autoessay.branches import ensure_main_branch, get_branch_stale

    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    return get_branch_stale(session, run)


def _resolve_phase_prompts(
    session: Session, run_id: str, phase: str, branch_id: str
) -> tuple[list[Any], dict[str, str]]:
    """Build the ``ResolvedPrompt`` list for the rerun, plus the
    runner-side override mapping. Per-branch (codex-AGREEd #2 stage 2.C).
    """
    from autoessay.phase_version import ResolvedPrompt
    from autoessay.prompts import (
        get_phase_prompt_spec,
        hash_content,
        supported_keys_for_phase,
    )

    resolved: list[ResolvedPrompt] = []
    override_mapping: dict[str, str] = {}
    for key in supported_keys_for_phase(phase):
        spec = get_phase_prompt_spec(phase, key)
        if spec is None:
            continue
        draft = session.scalar(
            select(PhasePromptDraft)
            .where(PhasePromptDraft.run_id == run_id)
            .where(PhasePromptDraft.branch_id == branch_id)
            .where(PhasePromptDraft.phase == phase)
            .where(PhasePromptDraft.prompt_key == key),
        )
        if draft is not None and draft.content:
            resolved.append(
                ResolvedPrompt(
                    prompt_key=key,
                    source="override",
                    content=draft.content,
                    content_hash=draft.content_hash,
                    template_id=spec.template_id,
                )
            )
            override_mapping[key] = draft.content
        else:
            resolved.append(
                ResolvedPrompt(
                    prompt_key=key,
                    source="default",
                    content=spec.default_content,
                    content_hash=hash_content(spec.default_content),
                    template_id=spec.template_id,
                )
            )
    return resolved, override_mapping


@app.post(
    "/api/runs/{run_id}/phases/{phase}/rerun",
    response_model=RerunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def rerun_phase(
    run_id: str,
    phase: str,
    session: SessionDependency,
    body: Annotated[RerunRequest | None, Body()] = None,
) -> RerunResponse:
    """Re-run a single phase that already produced output.

    Stage 1: artifacts overwrite in place — no version history. The
    ``stale_from_phase`` marker is recomputed on success so the UI
    can guide the user through the downstream refresh chain. See
    :mod:`autoessay.phase_rerun` for the invariants codex AGREEd to.
    """
    from autoessay.phase_rerun import (
        PHASE_POST_RERUN_STATE,
        assert_can_rerun,
        resolve_rewind_state,
        rewind_for_rerun,
        update_stale_marker_after_success,
    )

    run = _get_run_for_mutation_or_404(session, run_id)
    if run.state == "FAILED_POLICY":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "FAILED_POLICY must be resolved through the dedicated "
                "force-approve endpoint; direct phase rerun is disabled"
            ),
        )
    assert_can_rerun(run, phase, session=session)
    # Codex AGREE (system-wide audit): rerun must enforce the same
    # phase-readiness preconditions as ``start_*``. Without this the
    # new ``FailedRetryBanner`` (which calls rerun) could bypass
    # guards. ``activate_phase_version`` does NOT need this — it only
    # flips the head pointer, no agent re-runs.
    assert_phase_ready(run, phase, session)
    runner = _PHASE_RUNNERS.get(phase)
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown phase: {phase}",
        )
    # Rewind run.state to the canonical input state for this phase. A
    # completed phase has progressed past its own input state, so the
    # agent's `if run.state != EXPECTED: raise InvalidTransition` check
    # would fail without this. The rewind bypasses ALLOWED_TRANSITIONS
    # — rerun is by design a back-edge in the state graph.
    pre_rerun_state = run.state
    rewind_state = resolve_rewind_state(phase, run)
    if rewind_state is not None and run.state != rewind_state:
        rewind_for_rerun(run, phase, rewind_state, session, source="rerun_phase")
    from autoessay.phase_version import run_with_versioning

    project = session.scalar(select(Project).where(Project.id == run.project_id))
    user_id = project.user_id if project is not None else None
    from autoessay.branches import ensure_main_branch
    from autoessay.state_machine import RunCancelled

    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    assert branch_id is not None
    resolved_prompts, override_mapping = _resolve_phase_prompts(session, run.id, phase, branch_id)
    # Optimistic concurrency check (codex round-2 review): the body
    # distinguishes "key absent" (no check) from "key present, value
    # null" (caller expects NO override to be active). Without this
    # split, a save-and-rerun that just deleted the override could
    # be raced by a concurrent tab writing a new override and the
    # rerun would silently consume that new override.
    # ``__fields_set__`` (Pydantic v1) / ``model_fields_set`` (v2) is
    # the only way to distinguish "key omitted" from "key present
    # with value null"; we are on v1 (see imports for ``validator``).
    if body is not None and "draft_hash" in body.__fields_set__:
        from autoessay.prompts import (
            get_phase_prompt_spec as _get_spec,
        )
        from autoessay.prompts import (
            supported_keys_for_phase as _supported_keys,
        )

        # Prompt-key-aware concurrency check (Stage 3.A.2): only
        # compare the override hash for the SPECIFIC key the client
        # is acting on. Without this, a phase with multiple keys
        # (drafter has 9 after Stage 3.A.2) would compare the wrong
        # override and either false-409 or silently consume a stale
        # draft.
        #
        # Backward-compat fallback: when the caller did NOT pass
        # ``prompt_key`` explicitly (i.e. it is the default "main")
        # AND ``(phase, "main")`` is unsupported but the phase has
        # other registered keys (e.g. curator's only key is
        # "ranking"), use the first supported key as the effective
        # check key. This keeps the legacy single-key
        # ``{"draft_hash": "..."}`` request shape working for phases
        # that never expose a ``main`` surface.
        effective_key = body.prompt_key
        spec = _get_spec(phase, effective_key)
        if spec is None and "prompt_key" not in body.__fields_set__:
            keys = _supported_keys(phase)
            if keys:
                effective_key = keys[0]
                spec = _get_spec(phase, effective_key)
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "prompt_surface_not_supported",
                    "message": (
                        f"phase {phase!r} has no editable prompt surface named {body.prompt_key!r}"
                    ),
                },
            )
        active_override_hash: str | None = None
        for resolved in resolved_prompts:
            if resolved.prompt_key == effective_key and resolved.source == "override":
                active_override_hash = resolved.content_hash
                break
        if active_override_hash != body.draft_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "prompt_draft_changed",
                    "message": (
                        "the prompt draft changed since you opened the editor; "
                        "reload it before triggering rerun"
                    ),
                },
            )
    runner_kwargs = {"prompt_overrides": override_mapping} if override_mapping else {}
    # Stage 3.E follow-up P0 (codex AGREE): rerun must claim the
    # phase-start lock so a click during an in-flight rerun (or a
    # concurrent start_*) is rejected. We do NOT thread the token
    # through to the runner here (existing test fakes inject
    # _PHASE_RUNNERS that don't accept lock_token kwargs); instead
    # we release in this endpoint's finally below. Real agent
    # call sites still get token threading via the start_*
    # endpoints — codex's owner-check semantics matter most for
    # the long-running async path, not the synchronous rerun.
    rerun_lock_token = _claim_or_409(session, run, phase)
    try:
        try:
            pv = run_with_versioning(
                session,
                run,
                phase,
                lambda: runner(run.id, session, **runner_kwargs),
                created_by=user_id,
                prompts=resolved_prompts,
                branch_id=branch_id,
            )
        except RunCancelled:
            # Cancellation transitions the run to CANCELLED on purpose;
            # do NOT restore pre_rerun_state. The file rollback already
            # ran inside run_with_versioning.
            raise
        except Exception:
            # File rollback already happened in run_with_versioning. Now
            # restore the run state to what it was before the rewind, so
            # state and files agree.
            run.state = pre_rerun_state
            session.commit()
            raise
    finally:
        release_phase_lock(session, run, phase, rerun_lock_token)
        session.commit()
    session.refresh(run)
    if pv.status == "done":
        # Force a quiescent post-rerun state for phases that normally
        # chain (drafter ends at DRAFTER_RUNNING expecting stylist to
        # follow). Otherwise the next stale-banner click would 409 as
        # "phase currently running".
        post_state = PHASE_POST_RERUN_STATE.get(phase)
        if post_state is not None and run.state != post_state:
            run.state = post_state
            session.flush()
        # Only advance stale_from_phase on a successful version. A
        # graceful failure (FAILED_FIXABLE etc.) rolled back to the
        # prior active head, so downstream phases are no more stale
        # than they already were.
        update_stale_marker_after_success(session, run, phase)
    session.commit()
    session.refresh(run)
    return RerunResponse(
        run_id=run.id,
        phase=phase,
        state=run.state,
        stale_from_phase=_branch_stale_from_phase(session, run),
    )


class PhaseVersionEntry(BaseModel):
    id: str
    version_no: int
    status: str
    parent_pv_id: str | None
    is_active: bool
    input_snapshot_hash: str | None
    created_at: str
    completed_at: str | None
    artifact_count: int
    # Origin of this version. ``'agent'`` for the normal agent-run
    # output (the default for every version produced before PR-A1
    # landed), ``'user_edit'`` for PUT-based inline edits introduced
    # by PR-A2. Frontend uses this to label user-edit versions in
    # the phase-history modal.
    source: str = "agent"


class PhaseVersionsResponse(BaseModel):
    run_id: str
    phase: str
    active_version_id: str | None
    versions: list[PhaseVersionEntry]
    #: True iff the phase produced output at least once on the active
    #: branch, regardless of whether it was a versioned rerun (which
    #: leaves a row in ``versions[]``) or the initial vanilla run
    #: (which does NOT create a phase_version per Stage 2.A design —
    #: see ``phase_version.py`` for the rationale). Stage 3.E surfaces
    #: this so the UI can show "Rerun phase" / "Edit prompt and rerun"
    #: even before the first rerun has happened.
    has_completed_output: bool


class PhaseUserEditRequest(BaseModel):
    #: The version_id the caller was viewing when they clicked Save.
    #: When provided, the endpoint 409s if the active head moved
    #: between load and save (codex amendment 3, optimistic
    #: concurrency). May be null on the very first user edit when
    #: the run has no phase_version row yet (vanilla first run).
    base_version_id: str | None = None
    #: Map of legacy-relative artifact path → new content (UTF-8
    #: text). Only paths in the phase's editable registry are
    #: accepted; see ``phase_user_edit.editable_paths_for_phase``.
    files: dict[str, str]
    #: ``"new"`` (default, current behavior) creates a new pv tagged
    #: ``source='user_edit'`` and bumps the branch head.
    #: ``"replace"`` overwrites the current head's archive in place
    #: (codex AGREE 2026-05-01) — only allowed when no downstream
    #: phase has produced output AND the head pv is exclusive to the
    #: active branch.
    mode: str | None = Field(default="new")

    @validator("mode")
    def _mode_must_be_known(cls, value: str | None) -> str:
        cleaned = (value or "new").strip().lower()
        if cleaned not in {"new", "replace"}:
            raise ValueError("mode must be one of: new, replace")
        return cleaned


class PhaseUserEditResponse(BaseModel):
    phase_version_id: str
    version_no: int
    branch_id: str
    source: str
    stale_from_phase: str | None
    mode: str | None = None


class PhaseEditableEntry(BaseModel):
    path: str
    kind: str
    required_with: str | None
    current_content: str


class PhaseEditableResponse(BaseModel):
    phase: str
    base_version_id: str | None
    entries: list[PhaseEditableEntry]
    #: Whether replace mode is offered to the caller. ``True`` only
    #: when no downstream phase has produced output on the active
    #: branch AND (when a head pv exists) it is branch-exclusive.
    #: Codex AGREE 2026-05-01: the UI uses this to gate the radio
    #: between "replace current version" and "publish new version".
    replace_eligible: bool = False


@app.get(
    "/api/runs/{run_id}/phases/{phase}/editable",
    response_model=PhaseEditableResponse,
)
def list_editable_artifacts(
    run_id: str,
    phase: str,
    session: SessionDependency,
) -> PhaseEditableResponse:
    """Surface every artifact the user may submit through the
    matching ``PUT /api/runs/{run_id}/phases/{phase}/edit`` endpoint
    along with the file's current on-disk contents and the active
    head's version id (so the UI can echo it back as
    ``base_version_id`` for optimistic concurrency).

    Empty ``entries`` list means the phase has not produced
    artifacts on this branch yet (or the phase is not
    user-editable, e.g. ``proposal`` which has its own save
    endpoint, or ``exports`` which is the terminal output).
    """
    from autoessay.branches import ensure_main_branch
    from autoessay.phase_rerun import first_completed_downstream
    from autoessay.phase_user_edit import editable_paths_for_phase
    from autoessay.phase_version import get_run_head, is_pv_branch_exclusive

    run = _get_run_or_404(session, run_id)
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    head = get_run_head(session, run.id, phase, branch_id=branch_id)
    registry = editable_paths_for_phase(phase, run)
    run_dir = Path(run.run_dir)
    entries: list[PhaseEditableEntry] = []
    for path, kind, required_with in registry:
        target = run_dir / path
        try:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
        except (OSError, UnicodeDecodeError):
            current = ""
        entries.append(
            PhaseEditableEntry(
                path=path,
                kind=kind,
                required_with=required_with,
                current_content=current,
            ),
        )
    # Replace-mode eligibility. UI shows the replace toggle only
    # when ALL of:
    #
    #   (a) no downstream phase has produced output (codex AGREE
    #       2026-05-01); and
    #   (b) the head pv is branch-exclusive (no other branch
    #       references it); and
    #   (c) no agent is currently running on this run (2026-05-03
    #       follow-up: the PUT endpoint already 409s on
    #       ``run.state in RUNNING_STATES`` via apply_phase_user_edit,
    #       but until now the GET response advertised
    #       replace_eligible=True even mid-flight, so the UI
    #       offered a toggle the save would reject. Mirror the
    #       PUT's gate on the GET so the affordance matches the
    #       authority).
    from autoessay.phase_rerun import RUNNING_STATES as _RUNNING_STATES

    downstream_completed = first_completed_downstream(
        run,
        phase,
        session=session,
        branch_id=branch_id,
    ) or first_completed_downstream(run, phase)
    # ``branch_id`` is non-None here because ``ensure_main_branch``
    # ran above. mypy can't see that (Run.active_branch_id is
    # ``str | None`` on the model), so narrow explicitly.
    assert branch_id is not None
    replace_eligible = (
        downstream_completed is None
        and (head is None or is_pv_branch_exclusive(session, head, branch_id))
        and run.state not in _RUNNING_STATES
        and run.active_phase_lock is None
    )
    return PhaseEditableResponse(
        phase=phase,
        base_version_id=head,
        entries=entries,
        replace_eligible=replace_eligible,
    )


@app.put(
    "/api/runs/{run_id}/phases/{phase}/edit",
    response_model=PhaseUserEditResponse,
)
def edit_phase_artifacts(
    run_id: str,
    phase: str,
    request: PhaseUserEditRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> PhaseUserEditResponse:
    """User-edit the artifacts produced by ``phase``. Writes a new
    ``phase_version`` row tagged ``source='user_edit'`` and updates
    the branch head; downstream phases on the active branch get
    marked stale (codex amendments 1/2/3/4/5/6/7 from the
    2026-05-01 design review)."""
    from autoessay.phase_user_edit import (
        PhaseUserEditError,
        apply_phase_user_edit,
    )

    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    _enforce_input_safety_batch(
        {
            f"{SAFETY_CONTEXT_PHASE_EDIT}:{phase}:{path}": content
            for path, content in request.files.items()
        },
        overall_context_hint=f"{SAFETY_CONTEXT_PHASE_EDIT}:{phase}",
    )
    try:
        result = apply_phase_user_edit(
            session=session,
            run=run,
            phase=phase,
            base_version_id=request.base_version_id,
            files=request.files,
            user_id=getattr(user, "id", None),
            mode=(request.mode or "new"),
        )
    except PhaseUserEditError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    raw_version_no = result["version_no"]
    assert isinstance(raw_version_no, int)
    raw_stale = result.get("stale_from_phase")
    raw_mode = result.get("mode")
    return PhaseUserEditResponse(
        phase_version_id=str(result["phase_version_id"]),
        version_no=raw_version_no,
        branch_id=str(result["branch_id"]),
        source=str(result["source"]),
        stale_from_phase=str(raw_stale) if raw_stale else None,
        mode=str(raw_mode) if raw_mode else None,
    )


@app.get("/api/runs/{run_id}/phase-history")
def get_phase_history(
    run_id: str,
    session: SessionDependency,
) -> Any:
    """Per-phase modal payload (PR-A4.2, codex AGREE-with-amendments
    2026-05-02). For each pipeline phase on the run's active branch,
    returns:

    - ``state_flags`` (head_missing / prompt_dirty / lineage_dirty)
    - ``head_pv_id`` and ``head_version_no``
    - ``upstream_summary`` (per upstream phase: current head + whether
      this phase's head pv's recorded lineage matches it)
    - ``versions`` (every pv created on this branch, newest first,
      with full lineage and downstream-dependents info for the
      delete-button gate per rule 4)

    UI consumes this as a single GET; no per-phase round trips.
    """
    from autoessay.branches import ensure_main_branch
    from autoessay.phase_history import compute_phase_history, serialize_response

    run = _get_run_or_404(session, run_id)
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    assert branch_id is not None
    payload = compute_phase_history(session, run, branch_id)
    return serialize_response(payload)


@app.get(
    "/api/runs/{run_id}/phases/{phase}/versions",
    response_model=PhaseVersionsResponse,
)
def list_phase_versions(
    run_id: str,
    phase: str,
    session: SessionDependency,
) -> PhaseVersionsResponse:
    """All versions of ``phase`` for ``run_id`` on the run's active
    branch, newest first (codex round-2 #2 stage 2.C: history must be
    branch-scoped, otherwise UI shows versions unreachable from the
    current view)."""
    from autoessay.branches import ensure_main_branch
    from autoessay.models import PhaseArtifact
    from autoessay.phase_rerun import has_completed_output
    from autoessay.phase_version import get_run_head, list_versions

    run = _get_run_or_404(session, run_id)
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    rows = list_versions(session, run.id, phase, branch_id=branch_id)
    head = get_run_head(session, run.id, phase, branch_id=branch_id)
    # Use the file-glob path (no session/branch_id args) to match
    # `rerun_phase`'s `assert_can_rerun` eligibility check exactly:
    # vanilla first runs do not create phase_versions or RunHead but
    # they do write the legacy sentinel, so they ARE rerunnable. The
    # branch-aware RunHead path would return False for them and
    # break the "first rerun" UI surface. Branch isolation is still
    # safe because `materialize_branch_legacy_paths` (Stage 2.C)
    # rewrites the legacy files on each branch switch.
    completed = has_completed_output(run, phase)
    entries: list[PhaseVersionEntry] = []
    for pv, is_active in rows:
        count = (
            session.scalar(
                select(func.count(PhaseArtifact.id)).where(PhaseArtifact.phase_version_id == pv.id)
            )
            or 0
        )
        entries.append(
            PhaseVersionEntry(
                id=pv.id,
                version_no=pv.version_no,
                status=pv.status,
                parent_pv_id=pv.parent_pv_id,
                is_active=is_active,
                input_snapshot_hash=pv.input_snapshot_hash,
                created_at=pv.created_at.isoformat(),
                completed_at=(pv.completed_at.isoformat() if pv.completed_at else None),
                artifact_count=int(count),
                source=pv.source,
            )
        )
    return PhaseVersionsResponse(
        run_id=run.id,
        phase=phase,
        active_version_id=head,
        versions=entries,
        has_completed_output=completed,
    )


@app.delete(
    "/api/runs/{run_id}/phases/{phase}/versions/{pv_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_phase_version_endpoint(
    run_id: str,
    phase: str,
    pv_id: str,
    session: SessionDependency,
) -> Response:
    """Delete a phase_version + its child rows + on-disk archive
    (PR-A4.3, codex AGREE-with-amendments 2026-05-02).

    Reverse-dependency rule (rule 4): rejects with 409 when the
    pv is still referenced by any RunHead, lineage, parent_pv_id,
    or fork-point (including soft-deleted branches). The error
    detail names the dependent so the user can resolve it
    (e.g. "delete drafter v2 first") before retrying.

    No 4xx authentication / ownership check beyond the standard
    run lookup — same as the existing activate endpoint. Future
    PRs may add a per-user gate.
    """
    from autoessay.phase_rerun import RUNNING_STATES
    from autoessay.phase_version import delete_phase_version

    run = _get_run_for_mutation_or_404(session, run_id)
    # Round-1 audit #15: phase-version delete had no running-state
    # authority guard. The UI disables the button while running, but
    # direct API calls could still mutate the version graph and
    # archive bytes during active agent work. Same pattern as
    # activate / prompt / branch mutations.
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before deleting a version"
            ),
        )
    try:
        delete_phase_version(session, run, phase, pv_id)
    except ValueError as exc:
        msg = str(exc)
        # Distinguish 404 ("not found for this run/phase") from 409
        # (still referenced).
        if "not found" in msg.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=msg,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=msg,
        ) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/runs/{run_id}/phases/{phase}/versions/{pv_id}/activate",
    response_model=RerunResponse,
)
def activate_phase_version(
    run_id: str,
    phase: str,
    pv_id: str,
    session: SessionDependency,
) -> RerunResponse:
    """Switch ``run_head`` to a prior version and refresh legacy paths.

    Returns the same shape as ``rerun_phase`` so the frontend can
    treat them uniformly. ``stale_from_phase`` is recomputed via the
    existing first-completed-downstream logic.
    """
    from autoessay.branches import ensure_main_branch
    from autoessay.models import PhaseVersion as PV
    from autoessay.phase_rerun import (
        update_stale_marker_after_success,
    )
    from autoessay.phase_version import activate_version

    run = _get_run_for_mutation_or_404(session, run_id)
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    assert branch_id is not None
    # Codex round-2 #2 stage 2.C: visibility check. The target pv must
    # be reachable from the active branch (created on it OR currently
    # its head). Without this, any 'done' pv across all branches could
    # be activated onto the active branch, which is the cherry-pick
    # codex round-1 explicitly ruled out.
    pv = session.get(PV, pv_id)
    if pv is None or pv.run_id != run.id or pv.phase != phase:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="phase_version not found for this run/phase",
        )
    # Visibility = parent-chain reachability from the branch (codex
    # round-4 #2 stage 2.C). The previous "current head OR created on
    # branch" rule dropped the fork base after the first divergent
    # rerun, even though it remained an ancestor.
    from autoessay.phase_version import reachable_pv_ids_for_branch

    if pv.id not in reachable_pv_ids_for_branch(session, run.id, phase, branch_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "pv_not_visible",
                "message": (
                    "this version is not reachable from the active branch; switch branches first"
                ),
            },
        )
    # Codex 2026-05-02 review (PR-A4.3 amendment): the rerun-
    # endpoint's ``assert_can_rerun`` is too broad for activate
    # because PR-A4.3's cascade can DELETE downstream RunHeads,
    # and assert_can_rerun requires a current head to consider
    # the phase rerunnable. After a cascade, the branch could be
    # locked from re-activating an earlier upstream that would
    # actually restore those heads. Use a narrower guard:
    # quiescent run + target pv done + reachability (already
    # enforced above).
    from autoessay.phase_rerun import RUNNING_STATES

    if run.cancel_requested_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this run is cancelled",
        )
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before activating a different version"
            ),
        )
    try:
        activate_version(session, run, phase, pv_id, branch_id=branch_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    update_stale_marker_after_success(session, run, phase, branch_id=branch_id)
    session.commit()
    session.refresh(run)
    return RerunResponse(
        run_id=run.id,
        phase=phase,
        state=run.state,
        stale_from_phase=_branch_stale_from_phase(session, run),
    )


@app.post(
    "/api/runs/{run_id}/phases/{phase}/versions/activate-lineage-match",
    response_model=RerunResponse,
)
def activate_lineage_match(
    run_id: str,
    phase: str,
    session: SessionDependency,
) -> RerunResponse:
    """Find and activate the reachable pv whose lineage matches
    the current upstream vector for ``phase``. PR-A4.4 codex
    amendment 2 (2026-05-02): the modal needs a one-click
    "snap to upstream" action when ``lineage_dirty=true`` —
    instead of having the frontend re-implement
    ``_lineage_matches`` / ``reachable_pv_ids_for_branch``, the
    backend exposes this server-decided op.

    Behavior:
    1. Build the current upstream-vector dict for ``phase``.
    2. Scan every reachable pv for ``phase`` (status=done) and
       pick the one whose recorded lineage equals the current
       vector.
    3. If found: activate it (with full cascade + materialize
       per :func:`activate_version`).
    4. If not found: 404 — the modal then knows to show "rerun
       to generate a matching version" instead.
    """
    from autoessay.branches import ensure_main_branch
    from autoessay.phase_rerun import (
        PHASES,
        RUNNING_STATES,
        update_stale_marker_after_success,
    )
    from autoessay.phase_version import (
        _lineage_dict,
        _lineage_matches,
        activate_version,
        reachable_pv_ids_for_branch,
    )

    run = _get_run_for_mutation_or_404(session, run_id)
    if run.cancel_requested_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this run is cancelled",
        )
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before activating a different version"
            ),
        )
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    assert branch_id is not None

    if phase not in PHASES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown phase: {phase}",
        )

    # Build current upstream vector.
    phase_idx = PHASES.index(phase)
    current_upstream: dict[str, str] = {}
    for upstream_phase in PHASES[:phase_idx]:
        from autoessay.models import RunHead as _RunHead

        up_head = session.scalar(
            select(_RunHead.version_id)
            .where(_RunHead.run_id == run.id)
            .where(_RunHead.branch_id == branch_id)
            .where(_RunHead.phase == upstream_phase),
        )
        if up_head is not None:
            current_upstream[upstream_phase] = up_head

    reachable = reachable_pv_ids_for_branch(session, run.id, phase, branch_id)
    candidate: str | None = None
    if reachable:
        from autoessay.models import PhaseVersion as _PV

        for pv in session.scalars(
            select(_PV)
            .where(_PV.id.in_(reachable))
            .where(_PV.status == "done")
            .order_by(_PV.version_no.desc()),
        ).all():
            if _lineage_matches(_lineage_dict(session, pv.id), current_upstream):
                candidate = pv.id
                break
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "no historical version matches the current upstream vector "
                "for this phase; rerun to generate a new version"
            ),
        )

    try:
        activate_version(session, run, phase, candidate, branch_id=branch_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    update_stale_marker_after_success(session, run, phase, branch_id=branch_id)
    session.commit()
    session.refresh(run)
    return RerunResponse(
        run_id=run.id,
        phase=phase,
        state=run.state,
        stale_from_phase=_branch_stale_from_phase(session, run),
    )


@app.delete(
    "/api/runs/{run_id}/phases/{phase}/prompts/drafts",
    status_code=status.HTTP_204_NO_CONTENT,
)
def cancel_phase_prompt_drafts(
    run_id: str,
    phase: str,
    session: SessionDependency,
) -> Response:
    """Phase-wide cancel of all prompt drafts on the active
    branch (PR-A4.4 codex amendment 3 2026-05-02).

    Existing ``PUT /prompt`` with ``content=null`` only clears
    one prompt key, which is insufficient for drafter / stylist
    multi-key phases. This endpoint deletes every
    ``PhasePromptDraft`` row for ``(run, active_branch, phase)``,
    idempotently — re-issuing on an empty draft set is a no-op.

    Used by the modal's ``[取消修改]`` button when the phase is
    in the ``prompt_dirty`` state. Reverts the phase to its
    last-generated prompt state.
    """
    from autoessay.branches import ensure_main_branch
    from autoessay.models import PhasePromptDraft as _Draft
    from autoessay.phase_rerun import RUNNING_STATES

    run = _get_run_for_mutation_or_404(session, run_id)
    # Round-1 audit #13: prompt mutation must not race a running phase.
    # Reject during RUNNING_STATES so the running agent's rendered
    # prompt can't change beneath it (which would silently corrupt the
    # version-ledger record of "what prompt produced this output").
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before cancelling prompt drafts"
            ),
        )
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    from sqlalchemy import delete as _delete

    session.execute(
        _delete(_Draft)
        .where(_Draft.run_id == run.id)
        .where(_Draft.branch_id == branch_id)
        .where(_Draft.phase == phase),
    )
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class PhasePromptResponse(BaseModel):
    run_id: str
    phase: str
    prompt_key: str
    label: str
    template_id: str | None
    default_content: str
    override_content: str | None
    draft_hash: str | None
    supported: bool
    #: All prompt_keys this phase supports overriding, sorted
    #: alphabetically (Stage 3.A.4). Lets the frontend render a
    #: dropdown without a separate metadata round-trip.
    supported_keys: list[str]


class PhasePromptUpdateRequest(BaseModel):
    """Body for ``PUT /api/runs/{run}/phases/{phase}/prompt``.

    A null or empty string ``content`` deletes the override (revert to
    default). Length-capped at 50KB so the editor cannot create a
    monster row that breaks rendering or LLM context budgets.
    """

    content: str | None = None
    prompt_key: str = "main"


_PROMPT_OVERRIDE_MAX_BYTES = 50_000


@app.get(
    "/api/runs/{run_id}/phases/{phase}/prompt",
    response_model=PhasePromptResponse,
)
def get_phase_prompt(
    run_id: str,
    phase: str,
    session: SessionDependency,
    prompt_key: Annotated[str | None, Query()] = None,
) -> PhasePromptResponse:
    """Return the registered default prompt and the current draft
    override for ``(run, phase, prompt_key)``.

    Stage 3.A.4 discovery fallback: when the caller did NOT pass
    ``prompt_key`` AND ``(phase, "main")`` is unsupported, fall back
    to the first alphabetically-sorted supported key (e.g. curator's
    only key is ``ranking``). When the caller IS explicit (sends a
    ``prompt_key`` query param), no fallback — strict 404 if that
    key is not registered.
    """
    from autoessay.branches import ensure_main_branch
    from autoessay.prompts import get_phase_prompt_spec, supported_keys_for_phase

    run = _get_run_or_404(session, run_id)
    # ``prompt_key=""`` (FastAPI parses ``?prompt_key=`` as an empty
    # string) is an explicit-but-invalid request; preserve strict
    # 404 semantics rather than silently falling back to "main".
    if prompt_key is None:
        explicit = False
        effective_key = "main"
    else:
        explicit = True
        effective_key = prompt_key
    spec = get_phase_prompt_spec(phase, effective_key)
    if spec is None and not explicit:
        keys = supported_keys_for_phase(phase)
        if keys:
            effective_key = keys[0]
            spec = get_phase_prompt_spec(phase, effective_key)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "prompt_surface_not_supported",
                "message": (
                    f"phase {phase!r} has no editable prompt surface named {effective_key!r} yet"
                ),
            },
        )
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    draft = session.scalar(
        select(PhasePromptDraft)
        .where(PhasePromptDraft.run_id == run.id)
        .where(PhasePromptDraft.branch_id == branch_id)
        .where(PhasePromptDraft.phase == phase)
        .where(PhasePromptDraft.prompt_key == effective_key),
    )
    return PhasePromptResponse(
        run_id=run.id,
        phase=phase,
        prompt_key=effective_key,
        label=spec.label,
        template_id=spec.template_id,
        default_content=spec.default_content,
        override_content=draft.content if draft is not None else None,
        draft_hash=draft.content_hash if draft is not None else None,
        supported=spec.supported,
        supported_keys=supported_keys_for_phase(phase),
    )


@app.put(
    "/api/runs/{run_id}/phases/{phase}/prompt",
    response_model=PhasePromptResponse,
)
def upsert_phase_prompt(
    run_id: str,
    phase: str,
    session: SessionDependency,
    body: PhasePromptUpdateRequest,
) -> PhasePromptResponse:
    """Save or delete the user's draft prompt override for this
    ``(run, phase, prompt_key)``. ``content=null`` (or empty after
    strip) deletes the row and reverts to the registered default."""
    from autoessay.branches import ensure_main_branch
    from autoessay.phase_rerun import RUNNING_STATES
    from autoessay.prompts import (
        get_phase_prompt_spec,
        hash_content,
        supported_keys_for_phase,
    )

    run = _get_run_for_mutation_or_404(session, run_id)
    # Round-1 audit #13: same guard as cancel — reject prompt edits
    # while another phase is running so the agent's rendered prompt
    # doesn't change mid-flight.
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before editing prompts"
            ),
        )
    spec = get_phase_prompt_spec(phase, body.prompt_key)
    if spec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "prompt_surface_not_supported",
                "message": (
                    f"phase {phase!r} has no editable prompt surface named {body.prompt_key!r} yet"
                ),
            },
        )
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branch_id = run.active_branch_id
    assert branch_id is not None
    raw = body.content
    cleaned = raw.strip() if isinstance(raw, str) else None
    existing = session.scalar(
        select(PhasePromptDraft)
        .where(PhasePromptDraft.run_id == run.id)
        .where(PhasePromptDraft.branch_id == branch_id)
        .where(PhasePromptDraft.phase == phase)
        .where(PhasePromptDraft.prompt_key == body.prompt_key),
    )
    if not cleaned:
        if existing is not None:
            session.delete(existing)
            session.commit()
        return PhasePromptResponse(
            run_id=run.id,
            phase=phase,
            prompt_key=body.prompt_key,
            label=spec.label,
            template_id=spec.template_id,
            default_content=spec.default_content,
            override_content=None,
            draft_hash=None,
            supported=spec.supported,
            supported_keys=supported_keys_for_phase(phase),
        )
    if len(cleaned.encode("utf-8")) > _PROMPT_OVERRIDE_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "prompt_too_large",
                "message": (
                    f"prompt override exceeds {_PROMPT_OVERRIDE_MAX_BYTES} bytes; "
                    "trim the text and try again"
                ),
            },
        )
    _enforce_input_safety(
        cleaned,
        context_hint=f"{SAFETY_CONTEXT_PHASE_PROMPT_OVERRIDE}:{phase}:{body.prompt_key}",
    )
    content_hash = hash_content(cleaned)
    if existing is None:
        session.add(
            PhasePromptDraft(
                run_id=run.id,
                branch_id=branch_id,
                phase=phase,
                prompt_key=body.prompt_key,
                content=cleaned,
                content_hash=content_hash,
            )
        )
    else:
        existing.content = cleaned
        existing.content_hash = content_hash
        existing.updated_at = utcnow()
    session.commit()
    return PhasePromptResponse(
        run_id=run.id,
        phase=phase,
        prompt_key=body.prompt_key,
        label=spec.label,
        template_id=spec.template_id,
        default_content=spec.default_content,
        override_content=cleaned,
        draft_hash=content_hash,
        supported=spec.supported,
        supported_keys=supported_keys_for_phase(phase),
    )


class PhaseVersionPromptEntry(BaseModel):
    prompt_key: str
    source: str
    content: str
    content_hash: str
    template_id: str | None


@app.get(
    "/api/runs/{run_id}/phases/{phase}/versions/{pv_id}/prompts",
    response_model=list[PhaseVersionPromptEntry],
)
def list_phase_version_prompts(
    run_id: str,
    phase: str,
    pv_id: str,
    session: SessionDependency,
) -> list[PhaseVersionPromptEntry]:
    """Snapshot of every prompt surface used to produce ``pv_id``.

    Empty list ONLY for versions created before this phase had any
    registered prompt surface (legacy 2.A data). Nonexistent or
    cross-run pv ids return 404 instead of an ambiguous empty list.
    """
    from autoessay.models import PhaseVersion

    run = _get_run_or_404(session, run_id)
    pv = session.get(PhaseVersion, pv_id)
    if pv is None or pv.run_id != run.id or pv.phase != phase:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="phase_version not found for this run/phase",
        )
    rows = session.scalars(
        select(PhaseVersionPrompt)
        .where(PhaseVersionPrompt.phase_version_id == pv_id)
        .where(PhaseVersionPrompt.phase == phase)
        .order_by(PhaseVersionPrompt.prompt_key),
    ).all()
    return [
        PhaseVersionPromptEntry(
            prompt_key=r.prompt_key,
            source=r.source,
            content=r.content,
            content_hash=r.content_hash,
            template_id=r.template_id,
        )
        for r in rows
    ]


class FileDiffEntry(BaseModel):
    logical_path: str
    file_status: str
    diff_type: str
    body: dict[str, Any]
    match_basis: str | None = None


class DiffVersionInfo(BaseModel):
    id: str
    version_no: int
    status: str
    prompt_hash: str | None
    input_snapshot_hash: str | None
    created_on_branch_id: str | None
    created_at: str | None


class DiffContext(BaseModel):
    same_upstream_inputs: bool
    prompt_hash_changed: bool


class DiffSummary(BaseModel):
    files_added: int
    files_removed: int
    files_changed: int
    files_unchanged: int


class DiffResponseModel(BaseModel):
    run_id: str
    phase: str
    from_version: DiffVersionInfo
    to_version: DiffVersionInfo
    context: DiffContext
    summary: DiffSummary
    files: list[FileDiffEntry]


@app.get(
    "/api/runs/{run_id}/phases/{phase}/versions/{pv_id}/diff",
    response_model=DiffResponseModel,
)
def diff_phase_versions(
    run_id: str,
    phase: str,
    pv_id: str,
    session: SessionDependency,
    against: Annotated[str | None, Query()] = None,
) -> DiffResponseModel:
    """Per-file diff between two pvs of the same (run, phase).

    ``pv_id`` is the "to" side; ``against`` is the "from" side. If
    ``against`` is omitted, defaults to ``pv_id``'s ``parent_pv_id``;
    409 if there is no parent. Both versions must have
    ``status='done'`` (codex-AGREEd #2 stage 2.D).
    """
    from autoessay.models import PhaseVersion as PV
    from autoessay.phase_diff import diff_versions

    run = _get_run_or_404(session, run_id)
    to_pv = session.get(PV, pv_id)
    if to_pv is None or to_pv.run_id != run.id or to_pv.phase != phase:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="phase_version not found for this run/phase",
        )
    from_id = against or to_pv.parent_pv_id
    if from_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "no_default_against",
                "message": (
                    "this version has no parent; pass `against=<pv_id>` to "
                    "choose what to compare against"
                ),
            },
        )
    from_pv = session.get(PV, from_id)
    if from_pv is None or from_pv.run_id != run.id or from_pv.phase != phase:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="against pv not found for this run/phase",
        )
    try:
        result = diff_versions(session, run, phase, from_pv, to_pv)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return DiffResponseModel(
        run_id=result.run_id,
        phase=result.phase,
        from_version=DiffVersionInfo(**result.from_version),
        to_version=DiffVersionInfo(**result.to_version),
        context=DiffContext(**result.context),
        summary=DiffSummary(**result.summary),
        files=[
            FileDiffEntry(
                logical_path=f.logical_path,
                file_status=f.file_status,
                diff_type=f.diff_type,
                body=f.body,
                match_basis=f.match_basis,
            )
            for f in result.files
        ],
    )


class BranchEntry(BaseModel):
    id: str
    run_id: str
    name: str
    parent_branch_id: str | None
    forked_from_pv_id: str | None
    forked_phase: str | None
    stale_from_phase: str | None
    is_active: bool
    created_at: str
    deleted_at: str | None


class BranchListResponse(BaseModel):
    run_id: str
    active_branch_id: str | None
    branches: list[BranchEntry]


class CreateBranchRequest(BaseModel):
    """Body for ``POST /api/runs/{run}/branches``.

    ``base_pv_id`` identifies the phase_version to fork from. ``base_branch_id``
    is the branch the user is forking on (defaults to the run's active branch).
    The new branch inherits ``base_branch``'s heads for every phase
    upstream of ``base_pv_id``'s phase, and starts empty downstream.
    """

    name: str
    base_pv_id: str
    base_branch_id: str | None = None


def _branch_entry(run: Run, branch: Branch) -> BranchEntry:
    return BranchEntry(
        id=branch.id,
        run_id=branch.run_id,
        name=branch.name,
        parent_branch_id=branch.parent_branch_id,
        forked_from_pv_id=branch.forked_from_pv_id,
        forked_phase=branch.forked_phase,
        stale_from_phase=branch.stale_from_phase,
        is_active=branch.id == run.active_branch_id,
        created_at=branch.created_at.isoformat(),
        deleted_at=branch.deleted_at.isoformat() if branch.deleted_at else None,
    )


@app.get(
    "/api/runs/{run_id}/branches",
    response_model=BranchListResponse,
)
def list_run_branches(run_id: str, session: SessionDependency) -> BranchListResponse:
    """List every non-deleted branch on the run, oldest first."""
    from autoessay.branches import ensure_main_branch, list_active_branches

    run = _get_run_or_404(session, run_id)
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    branches = list_active_branches(session, run)
    return BranchListResponse(
        run_id=run.id,
        active_branch_id=run.active_branch_id,
        branches=[_branch_entry(run, b) for b in branches],
    )


@app.post(
    "/api/runs/{run_id}/branches",
    response_model=BranchEntry,
    status_code=status.HTTP_201_CREATED,
)
def create_run_branch(
    run_id: str,
    body: CreateBranchRequest,
    session: SessionDependency,
) -> BranchEntry:
    """Fork a new branch from ``base_pv_id``.

    Codex round-1 visibility check: the base pv must belong to the
    same (run, base_branch). The new branch starts empty and inherits
    ``base_branch``'s heads for every phase strictly upstream of
    ``base_pv_id.phase`` (copied here so the branch's run_heads are
    self-contained — no implicit "look at parent branch" lookups
    elsewhere).
    """
    from autoessay.branches import (
        create_branch,
        ensure_main_branch,
        get_branch,
    )
    from autoessay.models import PhaseVersion as PV
    from autoessay.phase_rerun import RUNNING_STATES

    run = _get_run_for_mutation_or_404(session, run_id)
    # Round-1 audit #19: branch creation mutates the branch graph.
    # Doing it mid-flight could install fork relationships against
    # a phase version that the running agent is still writing.
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before creating a branch"
            ),
        )
    if run.active_branch_id is None:
        ensure_main_branch(session, run)
    cleaned_name = body.name.strip()
    if not cleaned_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "branch_name_blank", "message": "branch name required"},
        )
    if len(cleaned_name) > 120:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "branch_name_too_long",
                "message": "branch name must be <= 120 chars",
            },
        )
    try:
        base_branch = get_branch(session, run, body.base_branch_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    base_pv = session.get(PV, body.base_pv_id)
    if base_pv is None or base_pv.run_id != run.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="base_pv_id not found for this run",
        )
    # base_pv must be a successful run — codex round-4 #2 stage 2.C:
    # forking from a failed/cancelled/running pv would install a
    # non-restorable head on the new branch.
    if base_pv.status != "done":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "base_pv_not_done",
                "message": (
                    f"cannot fork from a version with status={base_pv.status!r}; "
                    "only 'done' versions are forkable"
                ),
            },
        )
    # Visibility = parent-chain reachability from base_branch (codex
    # round-4 #2 stage 2.C). The fork base may be an ancestor of
    # base_branch's head rather than the head itself.
    from autoessay.phase_version import reachable_pv_ids_for_branch

    if base_pv.id not in reachable_pv_ids_for_branch(
        session, run.id, base_pv.phase, base_branch.id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "base_pv_not_visible",
                "message": (
                    "base_pv is not reachable from base_branch — choose a "
                    "version that was created on or activated by that branch"
                ),
            },
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    user_id = project.user_id if project is not None else None
    try:
        branch = create_branch(
            session,
            run,
            name=cleaned_name,
            base_branch=base_branch,
            forked_from_pv_id=body.base_pv_id,
            forked_phase=base_pv.phase,
            created_by=user_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "branch_name_in_use", "message": str(exc)},
        ) from exc
    # Copy base_pv's recorded UPSTREAM lineage onto the new branch
    # (codex round-2 #2 stage 2.C). Using ``phase_version_inputs``
    # rather than the base branch's CURRENT run_heads matters when
    # upstream changed AFTER base_pv was produced. Plus: set the
    # forked phase's head to base_pv itself (codex round-3 #2 stage
    # 2.C — without this, the rerun endpoint rejects "no output for
    # this phase" because branch's run_heads has no entry for it).
    # Downstream heads are intentionally not copied; the fork's whole
    # purpose is to diverge from base_pv onward.
    from autoessay.models import PhaseVersionInput

    upstream_rows = session.scalars(
        select(PhaseVersionInput).where(PhaseVersionInput.phase_version_id == base_pv.id),
    ).all()
    for row in upstream_rows:
        session.add(
            RunHead(
                run_id=run.id,
                branch_id=branch.id,
                phase=row.upstream_phase,
                version_id=row.upstream_pv_id,
            )
        )
    session.add(
        RunHead(
            run_id=run.id,
            branch_id=branch.id,
            phase=base_pv.phase,
            version_id=base_pv.id,
        )
    )
    session.commit()
    return _branch_entry(run, branch)


class SwitchBranchRequest(BaseModel):
    branch_id: str


@app.post(
    "/api/runs/{run_id}/branches/active",
    response_model=BranchListResponse,
)
def switch_active_branch(
    run_id: str,
    body: SwitchBranchRequest,
    session: SessionDependency,
) -> BranchListResponse:
    """Switch the run's active branch — what the workspace is scoped to.

    Codex round-2 #2 stage 2.C: switching also materializes the
    selected branch's heads onto the legacy paths. Without this, the
    bundle endpoints (which read ``run_dir``) would keep showing the
    previous branch's last-restored files, and reruns would record A's
    run_heads while consuming B's bytes.
    """
    from autoessay.branches import (
        get_branch,
        list_active_branches,
        materialize_branch_legacy_paths,
    )
    from autoessay.phase_rerun import RUNNING_STATES

    run = _get_run_for_mutation_or_404(session, run_id)
    # Round-1 audit #18: switching active branch materializes
    # heads onto run_dir paths. Doing it while an agent is mid-write
    # would race the agent's file output against the materialization.
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before switching branches"
            ),
        )
    try:
        branch = get_branch(session, run, body.branch_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if branch.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "branch_deleted", "message": "branch is soft-deleted"},
        )
    run.active_branch_id = branch.id
    materialize_branch_legacy_paths(session, run, branch)
    session.commit()
    branches = list_active_branches(session, run)
    return BranchListResponse(
        run_id=run.id,
        active_branch_id=run.active_branch_id,
        branches=[_branch_entry(run, b) for b in branches],
    )


@app.delete(
    "/api/runs/{run_id}/branches/{branch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_run_branch(
    run_id: str,
    branch_id: str,
    session: SessionDependency,
) -> Response:
    """Soft-delete a branch (refuses ``main``). Active branch falls
    back to ``main``."""
    from autoessay.branches import get_branch, soft_delete_branch
    from autoessay.phase_rerun import RUNNING_STATES

    run = _get_run_for_mutation_or_404(session, run_id)
    # Round-1 audit #19: deleting the active branch falls back to
    # main + remateriliazes — same race window as a switch. Reject
    # mid-flight.
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before deleting a branch"
            ),
        )
    try:
        branch = get_branch(session, run, branch_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    try:
        soft_delete_branch(session, run, branch)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "main_branch_protected", "message": str(exc)},
        ) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/runs/{run_id}/discovery", response_model=DiscoveryResponse)
def get_discovery(run_id: str, session: SessionDependency) -> DiscoveryResponse:
    run = _get_run_or_404(session, run_id)
    discovery_dir = Path(run.run_dir) / "discovery"
    return DiscoveryResponse(
        run_id=run.id,
        skim_candidates=_load_jsonl_objects(discovery_dir / "skim_candidates.jsonl"),
        scout_report=_read_optional_text(discovery_dir / "scout_report.md"),
    )


@app.get("/api/runs/{run_id}/sources", response_model=SourcesResponse)
def get_sources(run_id: str, session: SessionDependency) -> SourcesResponse:
    run = _get_run_or_404(session, run_id)
    payload = load_sources_payload(run)
    return SourcesResponse(
        run_id=str(payload["run_id"]),
        shortlist=cast(list[object], payload["shortlist"]),
        fulltext_manifest=cast(dict[str, object], payload["fulltext_manifest"]),
        manual_upload_requests=cast(
            list[dict[str, object]],
            payload["manual_upload_requests"],
        ),
        curation_report=str(payload["curation_report"]),
        skim_candidates=cast(list[dict[str, object]], payload["skim_candidates"]),
        source_quality_counts=cast(dict[str, int], payload.get("source_quality_counts") or {}),
    )


@app.get("/api/runs/{run_id}/synthesis", response_model=SynthesisResponse)
def get_synthesis(run_id: str, session: SessionDependency) -> SynthesisResponse:
    run = _get_run_or_404(session, run_id)
    payload = load_synthesis_payload(run)
    diagnostic = payload.get("material_diagnostic")
    dual_track_dict = payload.get("dual_track")
    dual_track_payload: DualTrackPayload | None = None
    if isinstance(dual_track_dict, dict):
        dual_track_payload = DualTrackPayload(
            schema_version=int(dual_track_dict.get("schema_version") or 1),
            primary_track=cast(list[dict[str, object]], dual_track_dict.get("primary_track") or []),
            secondary_track=cast(
                list[dict[str, object]], dual_track_dict.get("secondary_track") or []
            ),
            theoretical_lens_track=cast(
                list[dict[str, object]],
                dual_track_dict.get("theoretical_lens_track") or [],
            ),
            methodological_track=cast(
                list[dict[str, object]],
                dual_track_dict.get("methodological_track") or [],
            ),
            tension_summary_ref=(
                str(dual_track_dict["tension_summary_ref"])
                if dual_track_dict.get("tension_summary_ref")
                else None
            ),
            framework_lens_summary_ref=resolve_framework_lens_summary_ref(
                Path(run.run_dir),
                synthesizer_payload=dual_track_dict,
            ),
        )
    return SynthesisResponse(
        run_id=str(payload["run_id"]),
        claims=cast(list[dict[str, object]], payload["claims"]),
        source_notes=cast(dict[str, object], payload["source_notes"]),
        synthesizer_report=str(payload["synthesizer_report"]),
        material_diagnostic=(
            cast(dict[str, object], diagnostic) if isinstance(diagnostic, dict) else None
        ),
        material_diagnostic_md=str(payload.get("material_diagnostic_md") or ""),
        dual_track=dual_track_payload,
    )


def _safe_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


class ResearchRoleUpdateRequest(BaseModel):
    research_role: str


class ResearchRoleUpdateResponse(BaseModel):
    source_id: str
    research_role: str
    synthesis_marked_stale: bool


@app.put(
    # ``{source_id:path}`` is required: scout/curator produce DOI-shaped
    # source_ids like ``crossref:10.1108/ijbm-02-2025-0095``. With the
    # default ``{source_id}`` converter, Starlette decodes ``%2F`` to
    # ``/`` BEFORE route matching, which prevents the URL from ever
    # reaching this handler (a hard 404 with ``{"detail":"Not Found"}``).
    # That broke every real-LLM lens promotion in prod since PR-C1.b
    # (#144) shipped — surfaced by the PR-C2c real-paper acceptance run
    # 2026-05-03.
    "/api/runs/{run_id}/sources/{source_id:path}/research_role",
    response_model=ResearchRoleUpdateResponse,
)
def update_research_role(
    run_id: str,
    source_id: str,
    body: ResearchRoleUpdateRequest,
    session: SessionDependency,
) -> ResearchRoleUpdateResponse:
    """PR-C1.b: override the research_role of a single source.

    Codex round-1 amendment: changing role AFTER the synthesizer
    has produced ``synthesis/synthesizer.json`` would silently
    desynchronize the dual-track view from the live shortlist.
    The endpoint marks the synthesis phase stale on the active
    branch so the user re-runs synthesizer with the corrected
    partition. (No mutation of synthesizer.json itself — that
    file lives under phase versioning.)
    """
    from autoessay.agents.research_role_classifier import is_valid_role
    from autoessay.branches import mark_branch_stale_at_or_earlier
    from autoessay.phase_rerun import RUNNING_STATES

    if not is_valid_role(body.research_role):
        raise HTTPException(status_code=400, detail=f"invalid role: {body.research_role}")

    run = _get_run_or_404(session, run_id)
    # Round-1 audit #22: research_role update writes shortlist.json
    # and (if synthesizer output exists) marks synthesis stale.
    # Mid-flight mutation could race the running agent's read.
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before changing research_role"
            ),
        )
    run_dir = Path(run.run_dir)
    shortlist_path = run_dir / "sources" / "shortlist.json"
    if not shortlist_path.exists():
        raise HTTPException(status_code=404, detail="shortlist not found for this run")

    try:
        shortlist = json.loads(shortlist_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"shortlist corrupted: {exc}") from exc
    if not isinstance(shortlist, list):
        raise HTTPException(status_code=500, detail="shortlist is not a JSON array")

    target_index: int | None = None
    for idx, entry in enumerate(shortlist):
        if isinstance(entry, dict) and entry.get("source_id") == source_id:
            target_index = idx
            break
    if target_index is None:
        raise HTTPException(status_code=404, detail=f"source not found: {source_id}")

    shortlist[target_index]["research_role"] = body.research_role
    shortlist_path.write_text(
        json.dumps(shortlist, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Mirror to SourceRecord row if one exists. The C1 pipeline
    # does not yet write SourceRecord rows in production, but the
    # column was added in alembic 019 for future use.
    record = session.scalar(
        select(SourceRecord)
        .where(SourceRecord.run_id == run_id)
        .where(SourceRecord.id == source_id),
    )
    if record is not None:
        record.research_role = body.research_role

    # Stale-propagation: if synthesizer.json exists the new
    # partition no longer matches what the dual-track view shows.
    # Codex DISAGREE #3 (2026-05-03): use earliest-of so we don't
    # overwrite a pre-existing earlier marker (e.g. curator).
    synthesis_marked_stale = False
    if (run_dir / "synthesis" / "synthesizer.json").exists():
        actual_marker = mark_branch_stale_at_or_earlier(session, run, "synthesizer")
        synthesis_marked_stale = True
        append_event(
            session,
            run,
            "research_role_changed",
            {
                "source_id": source_id,
                "research_role": body.research_role,
                "synthesis_marked_stale": True,
                "stale_from_phase": actual_marker,
            },
        )

    session.commit()
    return ResearchRoleUpdateResponse(
        source_id=source_id,
        research_role=body.research_role,
        synthesis_marked_stale=synthesis_marked_stale,
    )


class EvidenceLedgerEntry(BaseModel):
    source_id: str
    claim_id: str
    claim_text: str
    citation_target: str
    confidence: float
    extra: dict[str, object] = Field(default_factory=dict)
    # Effective override after folding (latest per (source_id,
    # claim_id) considering both per-claim and source-wide rows).
    # ``None`` when the user has never set an override; otherwise
    # the latest action — note ``cite_normally`` is an EXPLICIT
    # cancellation (not the same as ``None``).
    override_action: str | None = None
    override_recorded_at: str | None = None
    override_user: str | None = None


class EvidenceLedgerResponse(BaseModel):
    run_id: str
    artifact_present: bool
    entries: list[EvidenceLedgerEntry]


@app.get(
    "/api/runs/{run_id}/evidence_ledger",
    response_model=EvidenceLedgerResponse,
)
def get_evidence_ledger(
    run_id: str,
    session: SessionDependency,
) -> EvidenceLedgerResponse:
    """PR-C1.b: server-folded ledger view.

    Returns one entry per ``kind=claim`` row, augmented with the
    effective override (folded across per-claim and source-wide
    overrides by ``recorded_at``). The frontend renders this
    table directly.

    ``artifact_present`` distinguishes "synthesis ran but no
    primary evidence" (artifact_present=True, entries=[]) from
    "synthesis never ran" (artifact_present=False).
    """
    from autoessay.evidence_ledger import (
        fold_overrides,
        ledger_path_for_run,
        read_rows,
    )

    run = _get_run_or_404(session, run_id)
    run_dir = Path(run.run_dir)
    artifact_present = ledger_path_for_run(run_dir).exists()
    rows = read_rows(run_dir)
    folded = fold_overrides(rows)

    entries: list[EvidenceLedgerEntry] = []
    for row in rows:
        if row.get("kind") != "claim":
            continue
        sid = str(row.get("source_id") or "")
        cid = str(row.get("claim_id") or "")
        # Effective override: per-claim wins if more recent than
        # source-wide; source-wide applies otherwise.
        per_claim = folded.get((sid, cid))
        source_wide = folded.get((sid, None))
        chosen: dict[str, object] | None = None
        if per_claim and source_wide:
            pc_at = str(per_claim.get("recorded_at") or "")
            sw_at = str(source_wide.get("recorded_at") or "")
            chosen = per_claim if pc_at >= sw_at else source_wide
        else:
            chosen = per_claim or source_wide
        action = str(chosen.get("action")) if chosen and chosen.get("action") else None
        recorded_at = (
            str(chosen.get("recorded_at")) if chosen and chosen.get("recorded_at") else None
        )
        user = str(chosen.get("user")) if chosen and chosen.get("user") else None
        entries.append(
            EvidenceLedgerEntry(
                source_id=sid,
                claim_id=cid,
                claim_text=str(row.get("claim_text") or ""),
                citation_target=str(row.get("citation_target") or ""),
                confidence=_safe_float(row.get("confidence")),
                extra=cast(dict[str, object], row.get("extra") or {}),
                override_action=action,
                override_recorded_at=recorded_at,
                override_user=user,
            ),
        )

    return EvidenceLedgerResponse(
        run_id=run_id,
        artifact_present=artifact_present,
        entries=entries,
    )


_LEDGER_OVERRIDE_ACTIONS = {"attribute_to_user", "cite_normally"}


class EvidenceLedgerOverrideRequest(BaseModel):
    source_id: str
    claim_id: str | None = None
    action: str
    user: str = "user"


class EvidenceLedgerOverrideResponse(BaseModel):
    appended: bool
    source_id: str
    claim_id: str | None
    action: str
    recorded_at: str


@app.post(
    "/api/runs/{run_id}/evidence_ledger/overrides",
    response_model=EvidenceLedgerOverrideResponse,
)
def append_evidence_ledger_override(
    run_id: str,
    body: EvidenceLedgerOverrideRequest,
    session: SessionDependency,
) -> EvidenceLedgerOverrideResponse:
    """PR-C1.b: append a user-attribution override.

    Append-only — the writer never mutates earlier rows. The
    reader folds via ``fold_overrides`` to determine the
    effective state. ``cite_normally`` is an explicit
    cancellation override (resets a prior ``attribute_to_user``)
    and is recorded as its own row.
    """
    from autoessay.branches import mark_branch_stale_at_or_earlier
    from autoessay.evidence_ledger import (
        append_rows,
        ensure_synthesis_dir,
        override_row,
    )
    from autoessay.phase_rerun import RUNNING_STATES

    if body.action not in _LEDGER_OVERRIDE_ACTIONS:
        valid = sorted(_LEDGER_OVERRIDE_ACTIONS)
        raise HTTPException(
            status_code=400,
            detail=f"invalid action: {body.action}; expected one of {valid}",
        )
    run = _get_run_or_404(session, run_id)
    # Round-1 audit #23: evidence-ledger overrides feed downstream
    # synthesizer reasoning and (when applied) mark synthesis stale.
    # Mutating the ledger mid-flight would race the synthesizer's
    # snapshot read.
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before overriding ledger rows"
            ),
        )
    run_dir = Path(run.run_dir)
    ensure_synthesis_dir(run_dir)

    row = override_row(
        source_id=body.source_id,
        claim_id=body.claim_id,
        action=body.action,
        user=body.user or "user",
    )
    written = append_rows(run_dir, [row])
    append_event(
        session,
        run,
        "evidence_ledger_override",
        {
            "source_id": body.source_id,
            "claim_id": body.claim_id,
            "action": body.action,
        },
    )
    # Round-1 audit #23 (stale-propagation): if synthesizer.json
    # already exists, its dual-track partition was computed against
    # the pre-override ledger fold. Mark synthesis stale so the user
    # re-runs against the new fold. Codex DISAGREE #3 (2026-05-03):
    # use earliest-of so a pre-existing earlier marker (e.g.
    # curator) isn't overwritten.
    if (run_dir / "synthesis" / "synthesizer.json").exists():
        actual_marker = mark_branch_stale_at_or_earlier(session, run, "synthesizer")
        append_event(
            session,
            run,
            "phase_stale_propagated",
            {
                "phase": "synthesizer",
                "trigger": "evidence_ledger_override",
                "source_id": body.source_id,
                "claim_id": body.claim_id,
                "action": body.action,
                "stale_from_phase": actual_marker,
            },
        )
    session.commit()
    return EvidenceLedgerOverrideResponse(
        appended=written > 0,
        source_id=body.source_id,
        claim_id=body.claim_id,
        action=body.action,
        recorded_at=str(row["recorded_at"]),
    )


@app.get("/api/runs/{run_id}/novelty", response_model=NoveltyResponse)
def get_novelty(run_id: str, session: SessionDependency) -> NoveltyResponse:
    run = _get_run_or_404(session, run_id)
    payload = load_novelty_payload(run)
    selected_thesis = payload.get("selected_thesis")
    raw_outlines = payload.get("detailed_outlines")
    detailed_outlines: list[dict[str, object]] = []
    if isinstance(raw_outlines, list):
        for entry in raw_outlines:
            if isinstance(entry, dict):
                detailed_outlines.append(cast(dict[str, object], entry))
    return NoveltyResponse(
        run_id=str(payload["run_id"]),
        angle_cards=cast(list[object], payload["angle_cards"]),
        ideator_report=str(payload["ideator_report"]),
        selected_thesis=(
            cast(dict[str, object], selected_thesis) if isinstance(selected_thesis, dict) else None
        ),
        detailed_outlines=detailed_outlines,
        detailed_outlines_md=str(payload.get("detailed_outlines_md") or ""),
    )


@app.get(
    "/api/runs/{run_id}/novelty/discussion",
    response_model=list[NoveltyDiscussionMessageResponse],
)
def get_novelty_discussion(
    run_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> list[NoveltyDiscussionMessageResponse]:
    run = _get_user_run_or_404(session, run_id, user)
    messages = list(
        session.scalars(
            select(NoveltyDiscussion)
            .where(NoveltyDiscussion.run_id == run.id)
            .order_by(NoveltyDiscussion.created_at.asc(), NoveltyDiscussion.id.asc()),
        ),
    )
    return [_novelty_discussion_response(message) for message in messages]


@app.post(
    "/api/runs/{run_id}/novelty/discuss",
    response_model=NoveltyDiscussResponse,
    status_code=status.HTTP_201_CREATED,
)
def discuss_novelty(
    run_id: str,
    request: NoveltyDiscussRequest,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> NoveltyDiscussResponse:
    run = _get_user_run_for_mutation_or_404(session, run_id, user)
    # Round-1 audit #24: novelty discussion mutates angle cards via
    # regenerate_angle_cards_for_discussion. Without a state guard,
    # a discussion request mid-flight (e.g. while drafter is running)
    # could rewrite novelty artifacts under another agent's feet.
    # Restrict to USER_NOVELTY_REVIEW (the only state where discussing
    # angles is meaningful) and reject any RUNNING_STATES outright.
    from autoessay.phase_rerun import RUNNING_STATES

    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before discussing angles"
            ),
        )
    if run.state != "USER_NOVELTY_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "novelty discussion is only available at "
                f"USER_NOVELTY_REVIEW (current state: {run.state})"
            ),
        )
    _enforce_input_safety(
        request.user_message,
        context_hint=SAFETY_CONTEXT_NOVELTY_USER_MESSAGE,
    )
    existing_messages = list(
        session.scalars(
            select(NoveltyDiscussion)
            .where(NoveltyDiscussion.run_id == run.id)
            .order_by(NoveltyDiscussion.created_at.asc(), NoveltyDiscussion.id.asc()),
        ),
    )
    provisional_history = [
        _novelty_discussion_mapping(message) for message in existing_messages
    ] + [{"role": "user", "content": request.user_message, "generation_token": 0}]
    try:
        angle_cards, generation_token = regenerate_angle_cards_for_discussion(
            run,
            session,
            provisional_history,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    user_message = NoveltyDiscussion(
        id=f"discussion_{uuid4().hex}",
        run_id=run.id,
        role="user",
        content=request.user_message,
        generation_token=generation_token,
    )
    assistant_message = NoveltyDiscussion(
        id=f"discussion_{uuid4().hex}",
        run_id=run.id,
        role="assistant",
        content=_novelty_assistant_summary(request.user_message, generation_token),
        generation_token=generation_token,
    )
    session.add(user_message)
    session.add(assistant_message)
    session.commit()
    session.refresh(user_message)
    session.refresh(assistant_message)
    return NoveltyDiscussResponse(
        run_id=run.id,
        angle_cards=cast(list[object], angle_cards),
        user_message=_novelty_discussion_response(user_message),
        assistant_message=_novelty_discussion_response(assistant_message),
    )


@app.get("/api/runs/{run_id}/drafts", response_model=DraftsResponse)
def get_drafts(run_id: str, session: SessionDependency) -> DraftsResponse:
    run = _get_run_or_404(session, run_id)
    payload = load_drafts_payload(run)
    return DraftsResponse(
        run_id=str(payload["run_id"]),
        drafts=cast(list[dict[str, object]], payload["drafts"]),
    )


@app.get("/api/runs/{run_id}/drafts/{version}", response_model=DraftResponse)
def get_draft(run_id: str, version: str, session: SessionDependency) -> DraftResponse:
    run = _get_run_or_404(session, run_id)
    try:
        payload = load_draft_payload(run, version)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="draft not found",
        ) from exc
    return DraftResponse(
        run_id=str(payload["run_id"]),
        version=str(payload["version"]),
        metadata=cast(dict[str, object], payload["metadata"]),
        manuscript=str(payload["manuscript"]),
        claim_map=cast(list[dict[str, object]], payload["claim_map"]),
        citations_bib=str(payload["citations_bib"]),
        draft_rationale=str(payload["draft_rationale"]),
    )


@app.get("/api/runs/{run_id}/style", response_model=StyleResponse)
def get_style(run_id: str, session: SessionDependency) -> StyleResponse:
    run = _get_run_or_404(session, run_id)
    try:
        payload = load_style_payload(run)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="style artifacts not found",
        ) from exc
    return StyleResponse(
        run_id=str(payload["run_id"]),
        version=str(payload["version"]),
        paper_styled=str(payload["paper_styled"]),
        style_delta=str(payload["style_delta"]),
        stop_slop_score=cast(dict[str, object], payload["stop_slop_score"]),
        n_gram_violations=cast(list[object] | None, payload["n_gram_violations"]),
    )


@app.get("/api/runs/{run_id}/style/score")
def get_style_score(run_id: str, session: SessionDependency) -> dict[str, object]:
    run = _get_run_or_404(session, run_id)
    try:
        return load_style_score_payload(run)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="style score not found",
        ) from exc


@app.get("/api/runs/{run_id}/critic", response_model=CriticResponse)
def get_critic(run_id: str, session: SessionDependency) -> CriticResponse:
    run = _get_run_or_404(session, run_id)
    payload = load_critic_payload(run)
    return CriticResponse(
        run_id=str(payload["run_id"]),
        critic_report=str(payload["critic_report"]),
        claim_audit=cast(list[dict[str, object]], payload["claim_audit"]),
        revision_plan=str(payload["revision_plan"]),
        blocking_issues=cast(dict[str, object], payload["blocking_issues"]),
    )


@app.get("/api/runs/{run_id}/integrity", response_model=IntegrityResponse)
def get_integrity(run_id: str, session: SessionDependency) -> IntegrityResponse:
    run = _get_run_or_404(session, run_id)
    payload = load_integrity_payload(run)
    return IntegrityResponse(
        run_id=str(payload["run_id"]),
        plagiarism_report=str(payload["plagiarism_report"]),
        ai_style_report=str(payload["ai_style_report"]),
        integrity_summary=cast(dict[str, object], payload["integrity_summary"]),
    )


@app.get("/api/runs/{run_id}/exports", response_model=ExportsResponse)
def get_exports(run_id: str, session: SessionDependency) -> ExportsResponse:
    from autoessay.export_filename import download_filename_for_export

    run = _get_run_or_404(session, run_id)
    payload = load_exports_payload(run)
    project = session.get(Project, run.project_id)
    project_title = project.title if project else ""
    # PR-371 (codex AGREE-WITH-AMENDMENTS amendment 4): annotate every
    # listed file with the slug-derived ``download_filename`` so the
    # frontend can show it without duplicating slug logic. The on-disk
    # ``filename`` (``manuscript.docx`` etc.) stays as the URL.
    files = cast(list[dict[str, object]], payload["files"])
    enriched: list[dict[str, object]] = []
    for entry in files:
        annotated = dict(entry)
        disk_name = str(entry.get("filename") or "")
        if disk_name:
            annotated["download_filename"] = download_filename_for_export(
                disk_filename=disk_name,
                project_title=project_title,
                run_id=run.id,
            )
        enriched.append(annotated)
    return ExportsResponse(
        run_id=str(payload["run_id"]),
        manifest=cast(dict[str, object], payload["manifest"]),
        files=enriched,
    )


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _read_express_optional_text(path: Path, *, limit: int | None = None) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        text_value = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if limit is not None and len(text_value) > limit:
        return text_value[:limit].rstrip() + "\n..."
    return text_value


def _extract_markdown_outline(markdown: str | None) -> list[dict[str, object]]:
    if not markdown:
        return []
    outline: list[dict[str, object]] = []
    for line_no, line in enumerate(markdown.splitlines(), start=1):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        outline.append(
            {
                "level": len(match.group(1)),
                "title": match.group(2).strip(),
                "line": line_no,
            },
        )
    return outline[:32]


def _express_audit_summary(payload: dict[str, object]) -> dict[str, object]:
    if not payload:
        return {}
    issues = payload.get("issues")
    issue_count = len(issues) if isinstance(issues, list) else 0
    return {
        "status": payload.get("status"),
        "summary": payload.get("summary"),
        "citation_traceability": payload.get("citation_traceability"),
        "word_count": payload.get("word_count"),
        "style_compliance": payload.get("style_compliance"),
        "issue_count": issue_count,
        "audit_only": payload.get("audit_only", True),
    }


@app.get("/api/runs/{run_id}/express_transparency", response_model=ExpressTransparencyResponse)
def get_express_transparency(
    run_id: str,
    session: SessionDependency,
    user: CurrentUserDependency,
) -> ExpressTransparencyResponse:
    run = _get_user_run_or_404(session, run_id, user)
    if _run_generation_mode(run) != EXPRESS_MODE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="express artifacts not found",
        )

    run_dir = Path(run.run_dir)
    express_dir = run_dir / "express"
    provenance = _read_json_object(express_dir / "provenance.json")
    audit_payload = _read_json_object(express_dir / "audit_critic.json")
    failure = _read_json_object(express_dir / "failure.json") or None
    manuscript = (
        _read_express_optional_text(run_dir / "drafts" / "v001" / "manuscript.md")
        or _read_express_optional_text(run_dir / "exports" / "manuscript.md")
        or _read_express_optional_text(express_dir / "ars_manuscript_raw.md")
    )
    prompt_summary = {
        "prompt_path": "express/ars_prompt.redacted.md",
        "prompt_sha256": provenance.get("prompt_sha256"),
        "audit_prompt_sha256": provenance.get("audit_prompt_sha256"),
        "ars_skill_sha": provenance.get("ars_skill_sha"),
        "ars_skill_file_sha256": provenance.get("ars_skill_file_sha256"),
        "completed_at": provenance.get("completed_at"),
    }
    token_usage = provenance.get("token_usage")
    return ExpressTransparencyResponse(
        run_id=run.id,
        mode=EXPRESS_MODE,
        state=run.state,
        provider=cast(str | None, provenance.get("provider")),
        provider_model=cast(str | None, provenance.get("provider_model")),
        token_cap=cast(int | None, provenance.get("token_cap")),
        token_usage=dict(token_usage) if isinstance(token_usage, dict) else {},
        prompt_summary={k: v for k, v in prompt_summary.items() if v is not None},
        prompt_excerpt=_read_express_optional_text(
            express_dir / "ars_prompt.redacted.md",
            limit=1200,
        ),
        provenance=provenance,
        audit_summary=_express_audit_summary(audit_payload),
        outline=_extract_markdown_outline(manuscript),
        manuscript_preview=manuscript,
        failure=failure,
    )


@app.get("/api/runs/{run_id}/exports/{filename}")
def get_export_file(run_id: str, filename: str, session: SessionDependency) -> FileResponse:
    from autoessay.export_filename import (
        download_filename_for_export,
        encode_content_disposition,
    )

    run = _get_run_or_404(session, run_id)
    if Path(filename).name != filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid filename")
    exports_dir = Path(run.run_dir) / "exports"
    path = exports_dir / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="export not found")
    # PR-371: serve the file with a Content-Disposition derived from
    # the project title so ``curl -OJ`` / browsers save it under a
    # title-friendly name. Disk path + URL are unchanged.
    project = session.get(Project, run.project_id)
    download_name = download_filename_for_export(
        disk_filename=filename,
        project_title=project.title if project else None,
        run_id=run.id,
    )
    # PR-378: ``Cache-Control: no-store`` defeats the Cloudflare 4h
    # cache that was serving stale broken docx files to users who
    # downloaded BEFORE the docx fix shipped. Field-discovered
    # 2026-05-13 on ``run_e11d7e52`` — disk had the new file
    # (3 ``<w:tbl>`` + 表 1/2/3 caption), but ``cf-cache-status:
    # HIT`` plus ``cache-control: max-age=14400`` meant the user's
    # browser + every Cloudflare edge node kept feeding the old
    # tofu-pipe-paragraph version. Exports are run-specific +
    # session-gated, so they should never be cached at the CDN.
    return FileResponse(
        path,
        headers={
            "Content-Disposition": encode_content_disposition(download_name),
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ``{source_id:path}`` mirrors the research_role endpoint above so
# DOI-shaped source_ids (``crossref:10.x/y``) reach the handler instead
# of being rejected as ``Not Found`` at route-match time.
@app.get("/api/runs/{run_id}/sources/{source_id:path}/pdf")
def get_source_pdf(
    run_id: str,
    source_id: str,
    session: SessionDependency,
) -> FileResponse:
    run = _get_run_or_404(session, run_id)
    try:
        pdf_path = find_local_pdf_path(run, source_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not found") from exc
    return FileResponse(pdf_path, media_type="application/pdf")


@app.post(
    "/api/runs/{run_id}/sources/upload",
    response_model=SourceUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_source_pdf(
    run_id: str,
    session: SessionDependency,
    source_id: Annotated[str, Form()],
    title: Annotated[str, Form()],
    pdf: Annotated[UploadFile, File()],
    authors: Annotated[str | None, Form()] = None,
    year: Annotated[int | None, Form()] = None,
    doi: Annotated[str | None, Form()] = None,
    url: Annotated[str | None, Form()] = None,
) -> SourceUploadResponse:
    from autoessay.branches import mark_branch_stale_at_or_earlier
    from autoessay.phase_rerun import RUNNING_STATES

    run = _get_run_for_mutation_or_404(session, run_id)
    # Round-1 audit #21: source upload mutates the source corpus and
    # downstream agents (curator/synthesizer) read it. A mid-flight
    # upload would race the running agent's snapshot read.
    if run.state in RUNNING_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"another phase is currently running ({run.state}); "
                "wait for it to finish before uploading sources"
            ),
        )
    await asyncio.to_thread(
        _enforce_input_safety_batch,
        {
            "source_upload.metadata.title": title,
            "source_upload.metadata.authors": authors or "",
            "source_upload.metadata.doi": doi or "",
            "source_upload.metadata.url": url or "",
        },
        overall_context_hint=SAFETY_CONTEXT_SOURCE_UPLOAD_METADATA,
    )
    pdf_bytes = await pdf.read()
    try:
        result = store_uploaded_pdf(
            run=run,
            requested_source_id=source_id,
            title=title,
            authors=_parse_authors(authors),
            year=year,
            doi=doi,
            url=url,
            pdf_bytes=pdf_bytes,
            max_size_mb=get_settings().max_upload_mb,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    append_event(
        session,
        run,
        "source_uploaded",
        {
            "source_id": result["source_id"],
            "local_only": True,
            "privacy_boundary": "stored under run data; not sent to integrity vendors",
        },
    )
    # Round-1 audit #21 (stale-propagation): if downstream synthesizer
    # has already produced output, the new source isn't reflected in
    # synthesizer.json. Mark synthesis stale so the user re-runs.
    # Codex DISAGREE secondary (2026-05-03): also fire for legacy
    # runs that have synthesis/claims.jsonl but no dual-track json
    # — those still feed the downstream pipeline.
    # Codex DISAGREE #3: use earliest-of so we don't overwrite an
    # earlier marker (curator).
    run_dir = Path(run.run_dir)
    synthesis_dir = run_dir / "synthesis"
    if (synthesis_dir / "synthesizer.json").exists() or (synthesis_dir / "claims.jsonl").exists():
        actual_marker = mark_branch_stale_at_or_earlier(session, run, "synthesizer")
        append_event(
            session,
            run,
            "phase_stale_propagated",
            {
                "phase": "synthesizer",
                "trigger": "source_upload",
                "source_id": result["source_id"],
                "stale_from_phase": actual_marker,
            },
        )
    session.commit()
    return SourceUploadResponse(
        run_id=run.id,
        source_id=str(result["source_id"]),
        manifest_entry=cast(dict[str, object], result["manifest_entry"]),
        shortlist_entry=cast(dict[str, object], result["shortlist_entry"]),
    )


@app.post(
    "/api/runs/{run_id}/checkpoints/{checkpoint_type}",
    response_model=CheckpointResponse,
    status_code=status.HTTP_201_CREATED,
)
def record_checkpoint(
    run_id: str,
    checkpoint_type: str,
    request: CheckpointDecisionRequest,
    session: SessionDependency,
) -> CheckpointResponse:
    run = _get_run_for_mutation_or_404(session, run_id)
    _enforce_checkpoint_safety(request)
    if checkpoint_type == "USER_PROPOSAL_REVIEW":
        return _record_proposal_checkpoint(run, request, session)
    if checkpoint_type == "USER_NOVELTY_REVIEW":
        return _record_novelty_checkpoint(run, request, session)
    if checkpoint_type == "USER_EXTERNAL_SCAN_APPROVAL":
        return _record_external_scan_checkpoint(run, request, session)
    if checkpoint_type == "USER_INTEGRITY_REVIEW":
        return _record_integrity_review_checkpoint(run, request, session)
    if checkpoint_type == "USER_FINAL_ACCEPTANCE":
        return _record_final_acceptance_checkpoint(run, request, session)
    if checkpoint_type in SOURCE_REVIEW_CHECKPOINT_SCOPES:
        return _record_source_review_checkpoint(run, checkpoint_type, request, session)
    checkpoint_status = request.status
    if checkpoint_status not in {"PENDING", "ACCEPTED", "REJECTED"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid checkpoint status",
        )
    decided_at = None if checkpoint_status == "PENDING" else utcnow()
    checkpoint = Checkpoint(
        id=f"checkpoint_{uuid4().hex}",
        run_id=run.id,
        checkpoint_type=checkpoint_type,
        status=checkpoint_status,
        decision_payload=json.dumps(request.decision_payload, sort_keys=True),
        decided_at=decided_at,
    )
    session.add(checkpoint)
    append_event(
        session,
        run,
        "checkpoint_recorded",
        {
            "checkpoint_type": checkpoint_type,
            "status": request.status,
            "decision_payload": request.decision_payload,
        },
    )
    session.commit()
    session.refresh(checkpoint)
    return _checkpoint_response(checkpoint)


def _record_source_review_checkpoint(
    run: Run,
    checkpoint_type: str,
    request: CheckpointDecisionRequest,
    session: Session,
) -> CheckpointResponse:
    if run.state != checkpoint_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{checkpoint_type} checkpoint requires run state {checkpoint_type}",
        )
    checkpoint_status = request.status
    if checkpoint_status not in {"PENDING", "ACCEPTED", "REJECTED"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid checkpoint status",
        )
    expected_scope = SOURCE_REVIEW_CHECKPOINT_SCOPES[checkpoint_type]
    scope = request.decision_payload.get("review_scope")
    if scope is not None and scope != expected_scope:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{checkpoint_type} checkpoint requires review_scope={expected_scope}",
        )
    if checkpoint_status == "ACCEPTED":
        source_ids = _source_review_source_ids(request.decision_payload)
        if source_ids is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{checkpoint_type} checkpoint requires source_ids list",
            )
    decided_at = None if checkpoint_status == "PENDING" else utcnow()
    checkpoint = Checkpoint(
        id=f"checkpoint_{uuid4().hex}",
        run_id=run.id,
        checkpoint_type=checkpoint_type,
        status=checkpoint_status,
        decision_payload=json.dumps(request.decision_payload, sort_keys=True),
        decided_at=decided_at,
    )
    session.add(checkpoint)
    append_event(
        session,
        run,
        "checkpoint_recorded",
        {
            "checkpoint_type": checkpoint_type,
            "status": checkpoint_status,
            "decision_payload": request.decision_payload,
        },
    )
    session.commit()
    session.refresh(checkpoint)
    return _checkpoint_response(checkpoint)


def _require_source_review_checkpoint(
    run: Run,
    session: Session,
    *,
    checkpoint_type: str,
    upstream_phase: str,
    consumer_phase: str,
) -> Checkpoint:
    checkpoint = _latest_source_review_checkpoint_after_phase(
        session,
        run,
        checkpoint_type=checkpoint_type,
        upstream_phase=upstream_phase,
    )
    if checkpoint is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{consumer_phase} requires an accepted {checkpoint_type} "
                "source review checkpoint for the current upstream output"
            ),
        )
    payload = _checkpoint_payload_dict(checkpoint)
    if checkpoint.status != "ACCEPTED" or _source_review_source_ids(payload) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{consumer_phase} requires an accepted {checkpoint_type} "
                "source review checkpoint with source_ids"
            ),
        )
    expected_scope = SOURCE_REVIEW_CHECKPOINT_SCOPES[checkpoint_type]
    scope = payload.get("review_scope")
    if scope is not None and scope != expected_scope:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{checkpoint_type} checkpoint review_scope is stale or invalid",
        )
    return checkpoint


def _latest_source_review_checkpoint_after_phase(
    session: Session,
    run: Run,
    *,
    checkpoint_type: str,
    upstream_phase: str,
) -> Checkpoint | None:
    phase_done_at = _latest_phase_done_at(session, run.id, upstream_phase)
    query = (
        select(Checkpoint)
        .where(Checkpoint.run_id == run.id)
        .where(Checkpoint.checkpoint_type == checkpoint_type)
    )
    if phase_done_at is not None:
        query = query.where(Checkpoint.created_at >= phase_done_at)
    return session.scalar(
        query.order_by(Checkpoint.created_at.desc(), Checkpoint.id.desc()).limit(1)
    )


def _latest_phase_done_at(session: Session, run_id: str, phase: str) -> datetime | None:
    events = session.scalars(
        select(RunEvent)
        .where(RunEvent.run_id == run_id)
        .where(RunEvent.event_type == "phase_done")
        .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
    )
    for event in events:
        payload = _event_payload_dict(event)
        if payload.get("phase") == phase:
            return event.created_at
    return None


def _checkpoint_payload_dict(checkpoint: Checkpoint) -> dict[str, object]:
    try:
        payload = json.loads(checkpoint.decision_payload)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _event_payload_dict(event: RunEvent) -> dict[str, object]:
    try:
        payload = json.loads(event.payload)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _source_review_source_ids(payload: dict[str, object]) -> list[str] | None:
    for key in SOURCE_REVIEW_SOURCE_ID_KEYS:
        raw = payload.get(key)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, str) and item]
    return None


def _record_proposal_checkpoint(
    run: Run,
    request: CheckpointDecisionRequest,
    session: Session,
) -> CheckpointResponse:
    if run.state != "USER_PROPOSAL_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Proposal checkpoint requires USER_PROPOSAL_REVIEW",
        )
    accept = _bool_from_request(request, "accept")
    if accept is True:
        decision_payload: dict[str, object] = {"accept": True}
        checkpoint = _add_checkpoint(
            run,
            "USER_PROPOSAL_REVIEW",
            "ACCEPTED",
            decision_payload,
            session,
        )
        append_event(
            session,
            run,
            "checkpoint_recorded",
            {
                "checkpoint_type": "USER_PROPOSAL_REVIEW",
                "status": "ACCEPTED",
                "decision_payload": decision_payload,
            },
        )
        # PR-I1: claim phase lock before transitioning so the scout
        # run has the same lock-release-on-exit guarantee as
        # ``start_scout``. Without this, proposal accept produced
        # ``SCOUT_RUNNING + active_phase_lock=NULL`` zombies on worker
        # death (mirror of the run_6c0640... drafter incident).
        token = _claim_or_409(session, run, "scout")
        try:
            transition(
                run,
                "SCOUT_RUNNING",
                session,
                reason="Proposal accepted; Scout started",
            )
        except InvalidTransition as exc:
            _release_after_enqueue_failure(session, run, "scout", token)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        session.commit()
        session.refresh(checkpoint)
        settings = get_settings()
        if settings.sync_worker:
            run_scout(run.id, session, lock_token=token)
        else:
            try:
                enqueue_scout_job(run.id, lock_token=token)
            except Exception:
                _release_after_enqueue_failure(session, run, "scout", token)
                raise
        return _checkpoint_response(checkpoint)
    if accept is False:
        decision_payload = {"accept": False}
        checkpoint = _add_checkpoint(
            run,
            "USER_PROPOSAL_REVIEW",
            "REJECTED",
            decision_payload,
            session,
        )
        append_event(
            session,
            run,
            "checkpoint_recorded",
            {
                "checkpoint_type": "USER_PROPOSAL_REVIEW",
                "status": "REJECTED",
                "decision_payload": decision_payload,
            },
        )
        session.commit()
        session.refresh(checkpoint)
        return _checkpoint_response(checkpoint)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="accept must be true or false",
    )


def _record_external_scan_checkpoint(
    run: Run,
    request: CheckpointDecisionRequest,
    session: Session,
) -> CheckpointResponse:
    if run.state not in {"USER_EXTERNAL_SCAN_APPROVAL", "FAILED_VENDOR"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="External scan checkpoint requires USER_EXTERNAL_SCAN_APPROVAL or FAILED_VENDOR",
        )
    approve = _bool_from_request(request, "approve")
    skip_reason = _string_from_request(request, "skip_reason")
    if approve is True:
        scan_kinds = _scan_kinds_from_request(request)
        if not scan_kinds:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="scan_kinds must include plagiarism, ai_style, or both",
            )
        checkpoint = _add_checkpoint(
            run,
            "USER_EXTERNAL_SCAN_APPROVAL",
            "ACCEPTED",
            {"approve": True, "scan_kinds": scan_kinds},
            session,
        )
        append_event(
            session,
            run,
            "checkpoint_recorded",
            {
                "checkpoint_type": "USER_EXTERNAL_SCAN_APPROVAL",
                "status": "ACCEPTED",
                "decision_payload": {"approve": True, "scan_kinds": scan_kinds},
            },
        )
        session.commit()
        session.refresh(checkpoint)
        return _checkpoint_response(checkpoint)
    if approve is False and skip_reason:
        rewrite_summary = _rewrite_summary_for_run(run)
        decision_payload = {"approve": False, "skip_reason": skip_reason}
        if rewrite_summary is not None:
            decision_payload["rewrite_summary"] = rewrite_summary
        checkpoint = _add_checkpoint(
            run,
            "USER_EXTERNAL_SCAN_APPROVAL",
            "ACCEPTED",
            decision_payload,
            session,
        )
        append_event(
            session,
            run,
            "checkpoint_recorded",
            {
                "checkpoint_type": "USER_EXTERNAL_SCAN_APPROVAL",
                "status": "ACCEPTED",
                "decision_payload": decision_payload,
            },
        )
        try:
            transition(
                run,
                "USER_FINAL_ACCEPTANCE",
                session,
                reason="External scan skipped with note",
                payload={
                    "skip_reason": skip_reason,
                    **({"rewrite_summary": rewrite_summary} if rewrite_summary is not None else {}),
                },
            )
        except InvalidTransition as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        session.commit()
        session.refresh(checkpoint)
        return _checkpoint_response(checkpoint)
    if approve is False:
        checkpoint = _add_checkpoint(
            run,
            "USER_EXTERNAL_SCAN_APPROVAL",
            "REJECTED",
            {"approve": False},
            session,
        )
        append_event(
            session,
            run,
            "checkpoint_recorded",
            {
                "checkpoint_type": "USER_EXTERNAL_SCAN_APPROVAL",
                "status": "REJECTED",
                "decision_payload": {"approve": False},
            },
        )
        session.commit()
        session.refresh(checkpoint)
        return _checkpoint_response(checkpoint)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="approve must be true, or false with skip_reason to skip",
    )


def _record_integrity_review_checkpoint(
    run: Run,
    request: CheckpointDecisionRequest,
    session: Session,
) -> CheckpointResponse:
    if run.state != "USER_INTEGRITY_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Integrity review checkpoint requires USER_INTEGRITY_REVIEW",
        )
    accept = _bool_from_request(request, "accept")
    span_decisions = _span_decisions_from_request(request)
    next_dimension = _string_from_request(request, "next_revision_dimension")
    decision_payload: dict[str, object] = {
        "accept": accept,
        "span_decisions": span_decisions,
        "next_revision_dimension": next_dimension,
    }
    if accept is True:
        rewrite_summary = _rewrite_summary_for_run(run)
        if rewrite_summary is not None:
            decision_payload["rewrite_summary"] = rewrite_summary
        checkpoint = _add_checkpoint(
            run,
            "USER_INTEGRITY_REVIEW",
            "ACCEPTED",
            decision_payload,
            session,
        )
        append_event(
            session,
            run,
            "checkpoint_recorded",
            {
                "checkpoint_type": "USER_INTEGRITY_REVIEW",
                "status": "ACCEPTED",
                "decision_payload": decision_payload,
            },
        )
        try:
            transition(
                run,
                "USER_FINAL_ACCEPTANCE",
                session,
                reason="Integrity findings accepted",
                payload=(
                    {"rewrite_summary": rewrite_summary} if rewrite_summary is not None else None
                ),
            )
        except InvalidTransition as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        session.commit()
        session.refresh(checkpoint)
        return _checkpoint_response(checkpoint)
    if accept is False and (next_dimension or _has_revise_decision(span_decisions)):
        checkpoint = _add_checkpoint(
            run,
            "USER_INTEGRITY_REVIEW",
            "REJECTED",
            decision_payload,
            session,
        )
        append_event(
            session,
            run,
            "checkpoint_recorded",
            {
                "checkpoint_type": "USER_INTEGRITY_REVIEW",
                "status": "REJECTED",
                "decision_payload": decision_payload,
            },
        )
        # Round-1 audit #10: integrity request-revision used to
        # transition to DRAFTER_RUNNING without claiming the phase
        # lock or enqueueing a drafter job — leaving the UI stuck
        # showing "drafter running" with no worker driving it.
        # Mirror the start_drafter path: claim the lock and enqueue
        # (or run sync) so a worker actually drives the revision.
        token = _claim_or_409(session, run, "drafter")
        try:
            transition(
                run,
                "DRAFTER_RUNNING",
                session,
                reason="Final revision requested from integrity review",
                payload={"final_revision": True, "dimension": next_dimension or "prose"},
            )
        except InvalidTransition as exc:
            _release_after_enqueue_failure(session, run, "drafter", token)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        session.commit()
        session.refresh(checkpoint)
        settings = get_settings()
        if settings.sync_worker:
            run_drafter(run.id, session, lock_token=token)
        else:
            try:
                enqueue_drafter_job(run.id, lock_token=token)
            except Exception:
                _release_after_enqueue_failure(session, run, "drafter", token)
                raise
        return _checkpoint_response(checkpoint)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="accept=true or a revise decision is required",
    )


def _record_final_acceptance_checkpoint(
    run: Run,
    request: CheckpointDecisionRequest,
    session: Session,
) -> CheckpointResponse:
    if run.state != "USER_FINAL_ACCEPTANCE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Final acceptance checkpoint requires USER_FINAL_ACCEPTANCE",
        )
    accept = _bool_from_request(request, "accept")
    if accept is not True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="accept=true is required for final acceptance",
        )
    export_formats = _export_formats_from_request(request)
    rewrite_summary = _rewrite_summary_for_run(run)
    decision_payload = {"accept": True, "export_formats": export_formats}
    if rewrite_summary is not None:
        decision_payload["rewrite_summary"] = rewrite_summary
    checkpoint = _add_checkpoint(
        run,
        "USER_FINAL_ACCEPTANCE",
        "ACCEPTED",
        decision_payload,
        session,
    )
    append_event(
        session,
        run,
        "checkpoint_recorded",
        {
            "checkpoint_type": "USER_FINAL_ACCEPTANCE",
            "status": "ACCEPTED",
            "decision_payload": decision_payload,
        },
    )
    session.commit()
    session.refresh(checkpoint)
    return _checkpoint_response(checkpoint)


def _rewrite_summary_for_run(run: Run) -> dict[str, object] | None:
    return rewrite_summary_for_run(run)


def _add_checkpoint(
    run: Run,
    checkpoint_type: str,
    checkpoint_status: str,
    decision_payload: dict[str, object],
    session: Session,
) -> Checkpoint:
    checkpoint = Checkpoint(
        id=f"checkpoint_{uuid4().hex}",
        run_id=run.id,
        checkpoint_type=checkpoint_type,
        status=checkpoint_status,
        decision_payload=json.dumps(decision_payload, sort_keys=True),
        decided_at=utcnow(),
    )
    session.add(checkpoint)
    return checkpoint


def _record_novelty_checkpoint(
    run: Run,
    request: CheckpointDecisionRequest,
    session: Session,
) -> CheckpointResponse:
    if run.state != "USER_NOVELTY_REVIEW":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Novelty checkpoint requires USER_NOVELTY_REVIEW",
        )
    selected_angle_id = _selected_angle_id(request)
    if selected_angle_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="selected_angle_id is required",
        )
    edits = _checkpoint_edits(request)
    try:
        selected_thesis = select_thesis_for_run(run, selected_angle_id, edits)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    decision_payload = {
        "selected_angle_id": selected_angle_id,
        "edits": edits,
        "selected_thesis": selected_thesis,
    }
    checkpoint = Checkpoint(
        id=f"checkpoint_{uuid4().hex}",
        run_id=run.id,
        checkpoint_type="USER_NOVELTY_REVIEW",
        status="ACCEPTED",
        decision_payload=json.dumps(decision_payload, sort_keys=True),
        decided_at=utcnow(),
    )
    session.add(checkpoint)
    append_event(
        session,
        run,
        "checkpoint_recorded",
        {
            "checkpoint_type": "USER_NOVELTY_REVIEW",
            "status": "ACCEPTED",
            "decision_payload": decision_payload,
        },
    )
    # PR-I1: claim phase lock before transitioning so the drafter run
    # has the same lock-release-on-exit guarantee as ``start_drafter``.
    # Without this the auto-start path produced ``DRAFTER_RUNNING +
    # active_phase_lock=NULL`` zombies when worker died before the
    # agent finished (run_6c0640... 2026-05-03).
    token = _claim_or_409(session, run, "drafter")
    try:
        transition(
            run,
            "DRAFTER_RUNNING",
            session,
            reason="Novelty angle selected",
            payload={"selected_angle_id": selected_angle_id},
        )
    except InvalidTransition as exc:
        _release_after_enqueue_failure(session, run, "drafter", token)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _upsert_selected_thesis_state(session, run, selected_thesis)
    session.commit()
    session.refresh(checkpoint)

    settings = get_settings()
    if settings.sync_worker:
        run_drafter(run.id, session, lock_token=token)
    else:
        try:
            enqueue_drafter_job(run.id, lock_token=token)
        except Exception:
            _release_after_enqueue_failure(session, run, "drafter", token)
            raise
    return _checkpoint_response(checkpoint)


def _selected_angle_id(request: CheckpointDecisionRequest) -> str | None:
    if request.selected_angle_id:
        return request.selected_angle_id
    raw_value = request.decision_payload.get("selected_angle_id")
    if isinstance(raw_value, str) and raw_value:
        return raw_value
    return None


def _checkpoint_edits(request: CheckpointDecisionRequest) -> dict[str, object]:
    raw_edits: object = request.edits
    if raw_edits is None:
        raw_edits = request.decision_payload.get("edits")
    if not isinstance(raw_edits, dict):
        return {}
    return {key: value for key, value in raw_edits.items() if isinstance(key, str)}


def _bool_from_request(request: CheckpointDecisionRequest, key: str) -> bool | None:
    if key == "approve" and request.approve is not None:
        return request.approve
    if key == "accept" and request.accept is not None:
        return request.accept
    value = request.decision_payload.get(key)
    return value if isinstance(value, bool) else None


def _string_from_request(request: CheckpointDecisionRequest, key: str) -> str | None:
    if key == "skip_reason" and request.skip_reason:
        return request.skip_reason
    if key == "next_revision_dimension" and request.next_revision_dimension:
        return request.next_revision_dimension
    value = request.decision_payload.get(key)
    return value if isinstance(value, str) and value else None


def _scan_kinds_from_request(request: CheckpointDecisionRequest) -> list[str]:
    raw_kinds: object = request.scan_kinds or request.decision_payload.get("scan_kinds")
    if not isinstance(raw_kinds, list):
        return []
    allowed = {"plagiarism", "ai_style"}
    kinds: list[str] = []
    for item in raw_kinds:
        if isinstance(item, str) and item in allowed and item not in kinds:
            kinds.append(item)
    return kinds


def _span_decisions_from_request(request: CheckpointDecisionRequest) -> list[dict[str, object]]:
    raw_decisions: object = request.span_decisions or request.decision_payload.get("span_decisions")
    if not isinstance(raw_decisions, list):
        return []
    decisions: list[dict[str, object]] = []
    allowed = {"accept", "revise", "ignore"}
    for item in raw_decisions:
        if not isinstance(item, dict):
            continue
        span_id = item.get("span_id")
        decision = item.get("decision")
        if isinstance(span_id, str) and isinstance(decision, str) and decision in allowed:
            decisions.append({"span_id": span_id, "decision": decision})
    return decisions


def _has_revise_decision(span_decisions: list[dict[str, object]]) -> bool:
    return any(decision.get("decision") == "revise" for decision in span_decisions)


def _export_formats_from_request(request: CheckpointDecisionRequest) -> list[str]:
    raw_formats: object = request.export_formats or request.decision_payload.get("export_formats")
    # PR-370 (2026-05-13): "latex" was missing from the allow-list,
    # which silently stripped manuscript.tex from every export.
    # PR-364 shipped the .tex writer in ``agents.exporter`` and added
    # "latex" to ``DEFAULT_EXPORT_FORMATS`` there, but this API filter
    # — the binding gate at USER_FINAL_ACCEPTANCE — never accepted it.
    # The 2026-05-13 数理增强模式 canary exposed the gap when latex
    # silently dropped out of ``{"html","markdown","docx","latex"}``.
    allowed = {"markdown", "docx", "html", "bibtex", "csl_json", "latex"}
    default_formats = ["markdown", "docx", "html", "bibtex", "csl_json", "latex"]
    if not isinstance(raw_formats, list):
        return default_formats
    formats = [item for item in raw_formats if isinstance(item, str) and item in allowed]
    return formats or default_formats


def _upsert_selected_thesis_state(
    session: Session,
    run: Run,
    selected_thesis: dict[str, object],
) -> None:
    state_id = f"state_{run.id}"
    payload = {"selected_thesis": selected_thesis}
    run_state = session.get(RunState, state_id)
    if run_state is None:
        session.add(RunState(id=state_id, run_id=run.id, state=run.state, payload=payload))
        return
    run_state.state = run.state
    run_state.payload = payload
    run_state.updated_at = utcnow()


def _project_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        user_id=project.user_id,
        title=project.title,
        domain_id=project.domain_id,
        domain_version=project.domain_version,
        target_journal=project.target_journal,
        language=project.language or "en",
        status=project.status,
        deleted_at=project.deleted_at.isoformat() if project.deleted_at else None,
    )


def _get_project_or_404(session: Session, project_id: str) -> Project:
    project = session.scalar(select(Project).where(Project.id == project_id))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


def _assert_project_active(project: Project) -> None:
    """Reject mutations on soft-deleted projects.

    Restore is the one mutation that ignores this check; everything
    else (start phase, accept checkpoint, upload source, …) must
    refuse to touch a deleted essay so a late worker write or a
    stale frontend tab cannot resurrect a project the user already
    threw away.
    """
    if project.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project is deleted; restore it before continuing",
        )


# ---------------------------------------------------------------------------
# 3-essay limit (codex-AGREEd)
# ---------------------------------------------------------------------------

ACTIVE_ESSAY_LIMIT_PER_USER = 3

# A project that reaches one of these states is treated as "finished /
# closed" for the purposes of the active-essay limit. The user can have
# any number of projects piled up in these states; they don't consume a
# slot. NOTE: ``FAILED_FIXABLE`` and ``FAILED_NEEDS_USER`` are
# intentionally NOT here — those are absorbing states the user has to
# come back and resolve, so they keep consuming a slot. If the user
# wants to free the slot they must explicitly delete the project.
LIMIT_TERMINAL_STATES = frozenset(
    {
        "EXPORTS_DONE",
        "EXPRESS_DONE",
        "CANCELLED",
        "FAILED_VENDOR",
        "FAILED_POLICY",
    }
)


def _latest_run_state_subq() -> Any:
    """Subquery for the latest non-deleted run per project.

    Latest is determined by ``Run.created_at`` descending with ``Run.id``
    as tiebreaker, so two runs created in the same millisecond get a
    deterministic ordering.
    """
    rn = (
        func.row_number()
        .over(
            partition_by=Run.project_id,
            order_by=(Run.created_at.desc(), Run.id.desc()),
        )
        .label("rn")
    )
    inner = select(Run.project_id, Run.state, rn).where(Run.deleted_at.is_(None)).subquery()
    return (
        select(inner.c.project_id, inner.c.state.label("latest_state"))
        .where(inner.c.rn == 1)
        .subquery()
    )


def _project_has_any_run_subq() -> Any:
    """Subquery containing every project id that has ever had a run."""

    return select(Run.project_id).distinct().subquery()


def _count_active_essays(session: Session, user_id: str) -> int:
    """Count of essays that consume a slot for ``user_id``.

    Active = ``Project.deleted_at IS NULL`` AND either no run has ever
    existed yet, or the latest non-deleted run is not in
    :data:`LIMIT_TERMINAL_STATES`. If all runs have been soft-deleted,
    the project no longer consumes a run-workflow slot.
    """
    latest = _latest_run_state_subq()
    any_run = _project_has_any_run_subq()
    stmt = (
        select(func.count(Project.id))
        .select_from(Project)
        .outerjoin(latest, latest.c.project_id == Project.id)
        .outerjoin(any_run, any_run.c.project_id == Project.id)
        .where(Project.user_id == user_id)
        .where(Project.deleted_at.is_(None))
        .where(
            (any_run.c.project_id.is_(None))
            | (
                latest.c.latest_state.is_not(None)
                & (~latest.c.latest_state.in_(LIMIT_TERMINAL_STATES))
            )
        )
    )
    return int(session.scalar(stmt) or 0)


def _project_currently_active(session: Session, project: Project) -> bool:
    """``True`` if this single project is consuming a slot right now."""
    if project.deleted_at is not None:
        return False
    has_any_run = (
        session.scalar(select(Run.id).where(Run.project_id == project.id).limit(1)) is not None
    )
    latest = session.scalar(
        select(Run.state)
        .where(Run.project_id == project.id, Run.deleted_at.is_(None))
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1),
    )
    if latest is None:
        return not has_any_run
    return latest not in LIMIT_TERMINAL_STATES


def _lock_user_for_essay_limit(session: Session, user_id: str) -> None:
    """Take a row-level lock on the user before counting + activating.

    Without this, two concurrent ``create_project`` calls can each see
    ``active_count == 2`` and both pass the gate, leaving the user with
    4 active essays. ``with_for_update`` is a no-op on SQLite (used in
    dev/tests) — concurrent test cases would still race in theory but
    pytest runs them serially. On Postgres this is a real lock.
    """
    session.execute(
        select(User.id).where(User.id == user_id).with_for_update(),
    )


def _essay_limit_response(active_count: int) -> Response:
    """Standard 409 body the frontend keys off ``code``."""
    import json as _json

    body = {
        "detail": (
            f"You already have {active_count} active essays. "
            f"Finish or delete one to start a new essay."
        ),
        "limit": ACTIVE_ESSAY_LIMIT_PER_USER,
        "active_count": active_count,
        "code": "essay_limit",
    }
    return Response(
        content=_json.dumps(body),
        status_code=status.HTTP_409_CONFLICT,
        media_type="application/json",
    )


def _corpus_document_response(document: CorpusDocument) -> CorpusDocumentResponse:
    return CorpusDocumentResponse(
        id=document.id,
        title=document.title,
        document_type=document.document_type,
        ingest_status=document.ingest_status,
        original_size_bytes=document.original_size_bytes,
        created_at=document.created_at.isoformat(),
    )


def _get_run_or_404(session: Session, run_id: str) -> Run:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


def _get_run_for_mutation_or_404(session: Session, run_id: str) -> Run:
    """Same as :func:`_get_run_or_404` plus a soft-delete guard.

    Mutating endpoints (start phase, accept checkpoint, upload source,
    …) must refuse to touch a run whose owning project has been
    soft-deleted. Read-only endpoints keep using the bare
    ``_get_run_or_404`` so a deleted essay can still be inspected
    in read-only mode.
    """
    run = _get_run_or_404(session, run_id)
    if run.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is deleted",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is not None and project.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project is deleted; restore it before continuing",
        )
    return run


def _get_user_run_or_404(session: Session, run_id: str, user: User) -> Run:
    run = session.scalar(
        select(Run)
        .join(Project, Run.project_id == Project.id)
        .where(Run.id == run_id, Project.user_id == user.id),
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return run


def _get_user_run_for_mutation_or_404(session: Session, run_id: str, user: User) -> Run:
    """Owner-scoped mutation guard. Same shape as
    :func:`_get_user_run_or_404` plus a 409 if the project has been
    soft-deleted.
    """
    run = _get_user_run_or_404(session, run_id, user)
    if run.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is deleted",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is not None and project.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="project is deleted; restore it before continuing",
        )
    return run


def _run_response(session: Session, run: Run) -> RunResponse:
    last_event = session.scalar(
        select(RunEvent)
        .where(RunEvent.run_id == run.id)
        .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
        .limit(1),
    )
    project = session.get(Project, run.project_id)
    project_title = project.title if project else ""
    project_language = (project.language if project else None) or "en"
    domain_id = project.domain_id if project else ""
    project_deleted_at = (
        project.deleted_at.isoformat() if project and project.deleted_at is not None else None
    )
    lock = get_active_phase_lock(run)
    return RunResponse(
        id=run.id,
        project_id=run.project_id,
        project_title=project_title,
        project_language=project_language,
        state=run.state,
        mode=_run_generation_mode(run),
        domain_id=domain_id,
        domain_version=run.domain_version,
        created_at=run.created_at.isoformat(),
        updated_at=run.updated_at.isoformat(),
        last_event=_run_event_response(last_event) if last_event is not None else None,
        deleted_at=run.deleted_at.isoformat() if run.deleted_at is not None else None,
        project_deleted_at=project_deleted_at,
        stale_from_phase=_branch_stale_from_phase(session, run),
        active_phase_lock=(
            ActivePhaseLockResponse(
                phase=lock["phase"] or "",
                job_id=lock.get("job_id"),
                claimed_at=lock.get("claimed_at"),
            )
            if lock is not None
            else None
        ),
        force_approve=_force_approve_response(session, run),
        paper_mode=run.paper_mode or "case_analysis",
        research_kernel=dict(run.research_kernel_json or {"kernel_schema_version": 1}),
        research_kernel_hash=compute_kernel_hash(
            run.paper_mode or "case_analysis",
            dict(run.research_kernel_json or {"kernel_schema_version": 1}),
        ),
        proposal_version=int(run.proposal_version or 0),
        mathematical_mode=bool(getattr(run, "mathematical_mode", False)),
        auto_advance=bool(getattr(run, "auto_advance", False)),
    )


def _force_approve_response(session: Session, run: Run) -> ForceApproveResponse | None:
    """Compute the per-state force-approve hint. Returns None
    outside failure states so frontend can simply check ``run.force_approve``."""
    info = compute_force_target(run, session)
    if not info.applicable and info.consequence is None:
        return None
    return ForceApproveResponse(
        applicable=info.applicable,
        target_state=info.target_state,
        consequence=info.consequence,
        blockers_to_resolve=info.blockers_to_resolve,
    )


def _run_event_response(event: RunEvent) -> RunEventResponse:
    return RunEventResponse(
        id=event.id,
        run_id=event.run_id,
        event_type=event.event_type,
        payload=_json_object(event.payload),
        created_at=event.created_at.isoformat(),
    )


def _checkpoint_response(checkpoint: Checkpoint) -> CheckpointResponse:
    return CheckpointResponse(
        id=checkpoint.id,
        run_id=checkpoint.run_id,
        checkpoint_type=checkpoint.checkpoint_type,
        status=checkpoint.status,
        decision_payload=_json_object(checkpoint.decision_payload),
        created_at=checkpoint.created_at.isoformat(),
        decided_at=checkpoint.decided_at.isoformat() if checkpoint.decided_at is not None else None,
    )


def _proposal_response(payload: dict[str, object]) -> ProposalResponse:
    proposal_json = payload.get("proposal_json")
    if not isinstance(proposal_json, dict):
        proposal_json = {}
    raw_version = payload.get("version")
    version = raw_version if isinstance(raw_version, int) else 0
    return ProposalResponse(
        run_id=str(payload["run_id"]),
        version=version,
        proposal_json={key: value for key, value in proposal_json.items() if isinstance(key, str)},
        markdown=str(payload["markdown"]),
        path=str(payload["path"]),
    )


def _novelty_discussion_response(
    message: NoveltyDiscussion,
) -> NoveltyDiscussionMessageResponse:
    return NoveltyDiscussionMessageResponse(
        id=message.id,
        run_id=message.run_id,
        role=message.role,
        content=message.content,
        generation_token=message.generation_token,
        created_at=message.created_at.isoformat(),
    )


def _novelty_discussion_mapping(message: NoveltyDiscussion) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "generation_token": message.generation_token,
    }


def _novelty_assistant_summary(user_message: str, generation_token: int) -> str:
    summary = user_message.strip().replace("\n", " ")
    if len(summary) > 160:
        summary = summary[:157] + "..."
    return f"Regenerated angle cards as v{generation_token:03d} using your feedback: {summary}"


def _json_object(value: str) -> dict[str, object]:
    decoded = json.loads(value)
    if isinstance(decoded, dict):
        return decoded
    return {"value": decoded}


def _load_events(bind: Engine, run_id: str) -> list[RunEvent]:
    with Session(bind=bind, future=True) as session:
        return list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc(), RunEvent.id.asc()),
            ),
        )


def _load_jsonl_objects(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            decoded = json.loads(stripped)
            if isinstance(decoded, dict):
                records.append(decoded)
    return records


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _parse_authors(value: str | None) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in re.split(r"[;,]", value) if item.strip()]


def _sse_run_event(event: RunEvent) -> str:
    data = _run_event_response(event).json()
    return f"id: {event.id}\nevent: run_event\ndata: {data}\n\n"


def load_all_domain_summaries() -> list[DomainSummary]:
    _domains, summaries = _load_domain_cache()
    return [summary.copy(deep=True) for summary in summaries]


def _load_domain_cache() -> tuple[dict[str, LoadedDomain], list[DomainSummary]]:
    global _DOMAIN_CACHE
    domain_dir = _domain_config_dir()
    cache_key = domain_dir.resolve()
    now = time.monotonic()
    if _DOMAIN_CACHE is not None:
        cached_at, cached_key, domains, summaries = _DOMAIN_CACHE
        if cached_key == cache_key and now - cached_at < DOMAIN_CACHE_TTL_SECONDS:
            return domains, summaries

    domains = load_domains(domain_dir)
    summaries = [_domain_summary(domain) for domain in domains.values()]
    _DOMAIN_CACHE = (now, cache_key, domains, summaries)
    return domains, summaries


def _domain_config_dir() -> Path:
    settings = get_settings()
    if settings.domain_dir.exists():
        return settings.domain_dir
    # Common Docker fallback when the API runs from /app/backend.
    fallback = Path(__file__).resolve().parents[3] / "domains"
    if fallback.exists():
        return fallback
    return settings.domain_dir


def _domain_summary(domain: LoadedDomain) -> DomainSummary:
    data = domain.data
    return DomainSummary(
        id=str(data["id"]),
        display_name=str(data["display_name"]),
        version=str(data.get("version", "")),
        description=_optional_string(data.get("description")),
        target_journals=_target_journal_names(data),
    )


def _optional_string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _target_journal_names(data: dict[str, object]) -> list[str]:
    journals = data.get("journals")
    if not isinstance(journals, dict):
        return []
    targets = journals.get("targets")
    if not isinstance(targets, list):
        return []
    names: list[str] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        name = target.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _load_domain_for_request(domain_id: str) -> LoadedDomain:
    try:
        domains, _summaries = _load_domain_cache()
    except (DomainConfigError, FileNotFoundError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid domain config",
        ) from exc
    domain = domains.get(domain_id)
    if domain is None:
        available = ", ".join(sorted(domains)) if domains else "none"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown domain_id: {domain_id}. Available domains: {available}",
        )
    return domain


def _alembic_head() -> str:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    return head or "none"
