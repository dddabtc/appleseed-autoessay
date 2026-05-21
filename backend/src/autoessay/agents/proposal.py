"""Proposal agent for user-reviewable opening research questions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, StrictStr, ValidationError, validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._language import language_directive
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.domain_loader import load_domain
from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    SchemaViolationError,
    hash_text,
    run_llm_step,
)
from autoessay.memory import MemoryClient, make_memory_pre_llm_hook
from autoessay.models import Project, Run
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

PROPOSAL_SCHEMA: dict[str, object] = {
    "research_question": "string",
    "significance": "string",
    "preliminary_approach": "string",
    "expected_contribution": "string",
    "scope": "string",
    "preliminary_keywords": ["string"],
}


class ProposalOutput(BaseModel):
    research_question: StrictStr
    significance: StrictStr
    preliminary_approach: StrictStr
    expected_contribution: StrictStr
    scope: StrictStr
    preliminary_keywords: list[StrictStr]

    @validator(
        "research_question",
        "significance",
        "preliminary_approach",
        "expected_contribution",
        "scope",
    )
    def _text_must_have_content(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("field must be non-empty")
        return cleaned

    @validator("preliminary_keywords")
    def _keywords_must_have_content(cls, value: list[str]) -> list[str]:
        cleaned = _normalize_keywords([str(keyword) for keyword in value])
        if not cleaned:
            raise ValueError("preliminary_keywords must contain at least one keyword")
        return cleaned

    class Config:
        extra = "ignore"


ProposalModel = ProposalOutput


def run_proposal_draft(
    run_id: str,
    db_session: Session | None = None,
    user_draft: str | None = None,
    hooks: HookRegistry | None = None,
    *,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run proposal draft. Stage 3.E follow-up P0: ``lock_token``
    triggers owner-checked phase-start lock release at exit."""
    from autoessay.phase_lock import phase_lock_release_on_exit

    with phase_lock_release_on_exit(run_id, "proposal", lock_token, session=db_session):
        if db_session is not None:
            return _run_proposal_draft_with_session(run_id, db_session, user_draft, hooks)
        with SessionLocal() as session:
            return _run_proposal_draft_with_session(run_id, session, user_draft, hooks)


def load_proposal_payload(run: Run) -> dict[str, object]:
    proposal_path = _latest_proposal_json_path(run)
    if proposal_path is None:
        raise FileNotFoundError("proposal not found")
    proposal_json = _load_json_mapping(proposal_path)
    if not proposal_json:
        raise FileNotFoundError("proposal not found")
    markdown_path = proposal_path.with_suffix(".md")
    markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
    version = _proposal_version_from_path(proposal_path)
    return {
        "run_id": run.id,
        "version": version,
        "proposal_json": proposal_json,
        "markdown": markdown,
        "path": _relative_run_path(run, proposal_path),
    }


def save_proposal_version(
    run: Run,
    session: Session,
    proposal_json: Mapping[str, object],
    *,
    creator: str,
    replace: bool = False,
) -> dict[str, object]:
    """Persist a proposal JSON + markdown pair under
    ``run.run_dir/proposal/``.

    ``replace=False`` (default) creates ``proposal_v<N+1>.json`` and
    bumps ``run.proposal_version``. ``replace=True`` overwrites
    ``proposal_v<N>.json`` in place — used by the user-edit endpoint
    when no downstream phase has completed yet (codex AGREE
    2026-05-01). The caller is responsible for validating that
    ``replace=True`` is appropriate (i.e. no completed pipeline
    phase). When ``replace=True`` and the run has no prior proposal
    (``proposal_version`` is 0/None), falls back to ``replace=False``
    semantics — there's nothing to overwrite, so the first save is
    always a new v001.
    """
    proposal = _normalize_proposal(proposal_json)
    if proposal is None:
        raise ValueError("proposal_json must match the proposal schema")

    prior_version = int(run.proposal_version or 0)
    version = prior_version if replace and prior_version >= 1 else prior_version + 1
    proposal_dir = Path(run.run_dir) / "proposal"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    json_path = proposal_dir / f"proposal_v{version:03d}.json"
    markdown_path = proposal_dir / f"proposal_v{version:03d}.md"
    markdown = _proposal_markdown(proposal)
    # Capture the previous on-disk content hash for audit (codex
    # amendment 2 — include before/after hashes so a replace event
    # is reconstructible). Best-effort: missing file → empty hash.
    prior_hash = _hash_file(json_path) if replace and json_path.exists() else ""
    _write_json(json_path, proposal)
    _write_text(markdown_path, markdown)
    new_hash = _hash_file(json_path)
    if not replace or prior_version == 0:
        run.proposal_version = version
    run.proposal_content_path = _relative_run_path(run, json_path)

    # PR-C0.b1: kernel snapshot rides alongside the proposal artifact.
    # Same proposal_version → same NNN. Caller (research_kernel
    # endpoint OR apply_phase_user_edit OR agent run) is expected
    # to have already assigned run.paper_mode + run.research_kernel_json
    # to the desired post-edit state before invoking save_proposal_version
    # (codex round-3 amendment 3). For agent runs and existing user
    # edits, that state is unchanged so the snapshot just persists
    # the current kernel.
    from datetime import datetime, timezone

    from autoessay.research_kernel import write_kernel_snapshot

    write_kernel_snapshot(
        run_dir=Path(run.run_dir),
        proposal_version=version,
        paper_mode=str(run.paper_mode or "case_analysis"),
        kernel=dict(run.research_kernel_json or {"kernel_schema_version": 1}),
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
    )
    append_event(
        session,
        run,
        "proposal_saved",
        {
            "version": version,
            "path": run.proposal_content_path,
            "creator": creator,
            "mode": "replace" if replace and prior_version >= 1 else "new",
            "prior_sha256": prior_hash,
            "new_sha256": new_hash,
        },
    )
    return {
        "run_id": run.id,
        "version": version,
        "proposal_json": proposal,
        "markdown": markdown,
        "path": run.proposal_content_path,
    }


def _run_proposal_draft_with_session(
    run_id: str,
    session: Session,
    user_draft: str | None,
    hooks: HookRegistry | None,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    assert_run_active(run, session)
    if run.state not in {"DOMAIN_LOADED", "USER_PROPOSAL_REVIEW"}:
        raise InvalidTransition(
            f"Proposal requires DOMAIN_LOADED or USER_PROPOSAL_REVIEW, got {run.state}",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    transition(run, "PROPOSAL_DRAFTING", session, reason="Proposal drafting started")
    append_event(
        session,
        run,
        "phase_started",
        {
            "phase": "proposal",
            "run_id": run.id,
            "has_user_draft": bool(user_draft and user_draft.strip()),
        },
    )
    session.commit()
    session.refresh(run)

    domain = load_domain(_domain_path(project.domain_id))
    settings = get_settings()
    proposal: dict[str, object] | None
    if settings.proposal_stub:
        proposal = _stub_proposal(project.title, domain.data, user_draft)
    else:
        try:
            proposal = _proposal_via_harness(
                run=run,
                project=project,
                session=session,
                domain_data=domain.data,
                user_draft=user_draft,
                hooks=hooks or HookRegistry(),
            )
        except SchemaViolationError:
            proposal = None
        except Exception:  # noqa: BLE001 - phase records fixable failure.
            proposal = None
    if proposal is None:
        guidance = "Proposal draft did not parse as strict JSON after one retry."
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="Proposal needs prompt-fixable output",
            payload={"guidance": guidance},
        )
        append_event(
            session,
            run,
            "phase_failed",
            {
                "phase": "proposal",
                "failure_class": "failed_fixable",
                "guidance": guidance,
            },
        )
        session.commit()
        return {"run_id": run.id, "state": run.state, "guidance": guidance}

    saved = save_proposal_version(run, session, proposal, creator="proposal_agent")
    transition(
        run,
        "USER_PROPOSAL_REVIEW",
        session,
        reason="Proposal draft ready for user review",
        payload={"version": saved["version"], "path": saved["path"]},
    )
    append_event(
        session,
        run,
        "phase_done",
        {
            "phase": "proposal",
            "version": saved["version"],
            "path": saved["path"],
        },
    )
    session.commit()
    return {"run_id": run.id, "state": run.state, **saved}


def _proposal_via_harness(
    *,
    run: Run,
    project: Project,
    session: Session,
    domain_data: Mapping[str, Any],
    user_draft: str | None,
    hooks: HookRegistry,
) -> dict[str, object] | None:
    prompt = _proposal_prompt(
        topic=project.title,
        domain_data=domain_data,
        user_draft=user_draft,
        suffix="",
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a research-proposal advisor. Return one strict JSON object. "
                    "Do not propose novelty; literature review and Ideator come later. "
                    + language_directive(project.language)
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.2,
        max_tokens=1800,
        response_format={"type": "json_object"},
        request_id="proposal_draft",
        prompt_template_id="proposal.draft.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="proposal",
        step_id="proposal.draft",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "has_user_draft": bool(user_draft and user_draft.strip()),
            "memory_query": f"phase=proposal topic={project.title} domain={project.domain_id}",
        },
    )
    _register_proposal_memory_hook(hooks)
    audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Proposal")
    response = asyncio.run(
        run_llm_step(
            request=request,
            hooks=hooks,
            context=context,
            output_schema=ProposalOutput,
            audit=audit,
            max_corrective_retries=2,
            llm_optional=False,
        ),
    )
    parsed = response.parsed
    if isinstance(parsed, ProposalOutput):
        return _normalize_proposal(parsed.dict())
    if isinstance(parsed, Mapping):
        return _normalize_proposal(parsed)
    return None


def _register_proposal_memory_hook(hooks: HookRegistry) -> None:
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


def _proposal_prompt(
    *,
    topic: str,
    domain_data: Mapping[str, Any],
    user_draft: str | None,
    suffix: str,
) -> str:
    user_draft_text = user_draft.strip() if user_draft and user_draft.strip() else "None"
    return (
        "You are a research-proposal advisor. "
        f"Topic: {topic}. "
        f"Domain: {json.dumps(_domain_summary(domain_data), sort_keys=True)}. "
        f"User's draft notes: {user_draft_text}. "
        "Produce a roughly 600 word structured proposal in the schema. "
        "Be specific to the supplied domain. Do NOT propose novelty; that comes after "
        "literature review. Do propose what to investigate and how the literature search "
        "should be grounded. "
        f"Return strict JSON matching this schema: {json.dumps(PROPOSAL_SCHEMA, sort_keys=True)}"
        f"{suffix}"
    )


def _parse_proposal_response(value: str) -> dict[str, object] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return _normalize_proposal(decoded)


def _normalize_proposal(payload: Mapping[str, object]) -> dict[str, object] | None:
    try:
        parsed = ProposalOutput.parse_obj(payload)
    except ValidationError:
        return None
    values: dict[str, object] = {
        "research_question": parsed.research_question.strip(),
        "significance": parsed.significance.strip(),
        "preliminary_approach": parsed.preliminary_approach.strip(),
        "expected_contribution": parsed.expected_contribution.strip(),
        "scope": parsed.scope.strip(),
        "preliminary_keywords": _normalize_keywords(
            [str(keyword) for keyword in parsed.preliminary_keywords],
        ),
    }
    if any(not str(values[field]) for field in values if field != "preliminary_keywords"):
        return None
    if not values["preliminary_keywords"]:
        return None
    return values


def _normalize_keywords(keywords: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        cleaned = " ".join(keyword.split())
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            normalized.append(cleaned)
    return normalized[:12]


def _stub_proposal(
    topic: str,
    domain_data: Mapping[str, Any],
    user_draft: str | None,
) -> dict[str, object]:
    domain_name = str(domain_data.get("display_name") or domain_data.get("id") or "the domain")
    draft_focus = _draft_focus(user_draft)
    focus_clause = f" with attention to {draft_focus}" if draft_focus else ""
    default_terms = _default_terms(domain_data)
    keywords = _normalize_keywords([*topic.split(), *default_terms, draft_focus])
    return {
        "research_question": (
            f"How should {topic} be investigated in {domain_name}{focus_clause}, "
            "and which literature streams define the initial evidence base?"
        ),
        "significance": (
            f"The project matters because {topic} can connect domain-specific debates "
            f"in {domain_name} to concrete evidence about institutions, markets, and "
            "historical interpretation."
        ),
        "preliminary_approach": (
            "Begin with a structured literature search across the configured sources, "
            "separate consensus claims from unresolved debates, and use the reviewed "
            "sources to refine the research question before any novelty angle is chosen."
        ),
        "expected_contribution": (
            "The expected contribution at this stage is a focused field map and a clearer "
            "starting question that can later be refined into source-bound angle cards."
        ),
        "scope": (
            f"Initial scope centers on {topic}{focus_clause}; it excludes final novelty "
            "claims, manuscript argumentation, and unsupported causal claims until the "
            "literature review has been completed."
        ),
        "preliminary_keywords": keywords or [topic, domain_name],
    }


def _proposal_markdown(proposal: Mapping[str, object]) -> str:
    keywords = proposal.get("preliminary_keywords")
    keyword_text = ", ".join(_string_list(keywords))
    lines = [
        "# Initial Proposal",
        "",
        "## Research Question",
        "",
        str(proposal["research_question"]),
        "",
        "## Significance",
        "",
        str(proposal["significance"]),
        "",
        "## Preliminary Approach",
        "",
        str(proposal["preliminary_approach"]),
        "",
        "## Expected Contribution",
        "",
        str(proposal["expected_contribution"]),
        "",
        "## Scope",
        "",
        str(proposal["scope"]),
        "",
        "## Preliminary Keywords",
        "",
        keyword_text or "none",
    ]
    return "\n".join(lines) + "\n"


def _latest_proposal_json_path(run: Run) -> Path | None:
    if run.proposal_content_path:
        path = _resolve_run_path(Path(run.run_dir), run.proposal_content_path)
        if path.exists():
            return path
    proposal_dir = Path(run.run_dir) / "proposal"
    candidates = sorted(proposal_dir.glob("proposal_v*.json"))
    return candidates[-1] if candidates else None


def _proposal_version_from_path(path: Path) -> int:
    match = re.search(r"_v(\d{3})\.json$", path.name)
    return int(match.group(1)) if match else 0


def _relative_run_path(run: Run, path: Path) -> str:
    run_dir = Path(run.run_dir).resolve()
    return str(path.resolve().relative_to(run_dir))


def _resolve_run_path(run_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = run_dir / path
    resolved = path.resolve()
    run_root = run_dir.resolve()
    if not resolved.is_relative_to(run_root):
        raise FileNotFoundError(raw_path)
    return resolved


def _domain_summary(domain_data: Mapping[str, Any]) -> dict[str, object]:
    return {
        "id": domain_data.get("id"),
        "display_name": domain_data.get("display_name"),
        "description": domain_data.get("description"),
        "search": domain_data.get("search", {}),
        "terms": domain_data.get("terms", {}),
        "journals": domain_data.get("journals", {}),
        "evidence": domain_data.get("evidence", {}),
    }


def _default_terms(domain_data: Mapping[str, Any]) -> list[str]:
    search = domain_data.get("search", {})
    if not isinstance(search, dict):
        return []
    terms = search.get("default_query_terms", [])
    if not isinstance(terms, list):
        return []
    return [term for term in terms if isinstance(term, str)]


def _draft_focus(user_draft: str | None) -> str:
    if not user_draft:
        return ""
    words = re.findall(r"\b[\w'-]{4,}\b", user_draft.casefold())
    return " ".join(words[:8])


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _load_json_mapping(path: Path) -> dict[str, object]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: value for key, value in decoded.items() if isinstance(key, str)}


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _hash_file(path: Path) -> str:
    """sha256 of file contents — used as a before/after marker on
    proposal save events so a replace mode save remains audit-
    reconstructible. Empty string for missing files; the caller
    treats absence as no-prior-content."""
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _domain_path(domain_id: str) -> Path:
    settings = get_settings()
    path = settings.domain_dir / f"{domain_id}.yaml"
    if path.exists():
        return path
    return Path(__file__).resolve().parents[4] / "domains" / f"{domain_id}.yaml"
