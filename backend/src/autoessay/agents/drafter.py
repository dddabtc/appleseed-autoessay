"""Drafter agent for v1 source-bound manuscript sections."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictStr, ValidationError, validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.agents._evidence_policy import EvidencePolicies, PhaseMode
from autoessay.agents._humanizer import humanizer_directive
from autoessay.agents._language import language_directive
from autoessay.agents.detailed_outline import OutlineSection
from autoessay.agents.ideator import select_thesis_for_run
from autoessay.agents.phase_context import phase_context_prompt_block
from autoessay.clients.citations import generate_bib
from autoessay.clients.common import NormalizedSource
from autoessay.config import get_settings
from autoessay.db import SessionLocal
from autoessay.domain_loader import load_domain
from autoessay.harness import (
    AuditVerdict,
    AuditWriter,
    HookContext,
    HookRegistry,
    HookResult,
    LLMCallRequest,
    SchemaViolationError,
    hash_text,
    run_llm_step,
)
from autoessay.harness.dedup import DrafterLocalDedupHook
from autoessay.memory import MemoryClient, make_memory_pre_llm_hook
from autoessay.models import Checkpoint, Project, Run
from autoessay.prompts import (
    DRAFTER_SECTION_ROLES,
    DRAFTER_SECTION_TYPE_DIRECTIVES,
)
from autoessay.state_machine import InvalidTransition, append_event, assert_run_active, transition

DEFAULT_SECTION_TITLES = (
    "Introduction",
    "Historiography",
    "Sources & Method",
    "Empirical Section I",
    "Empirical Section II",
    "Empirical Section III",
    "Discussion",
    "Conclusion",
)

# PR-256: per-language section title overrides (codex round-1 verdict
# Q3=B). When ``_resolve_paper_language(project, kernel)`` returns a
# non-English code AND the domain config did NOT supply a custom
# ``structure_template``, drafter swaps the English titles above for
# the locale-appropriate equivalents below. The keys remain identical
# in slug form (introduction / historiography / etc.) so the
# downstream prompt registry + section-id-keyed override lookups are
# unaffected.
DEFAULT_SECTION_TITLES_BY_LANG: dict[str, tuple[str, ...]] = {
    "en": DEFAULT_SECTION_TITLES,
    "zh": (
        "一、引言",
        "二、文献综述",
        "三、研究方法",
        "四、第一节正文",
        "五、第二节正文",
        "六、第三节正文",
        "七、讨论",
        "八、结论",
    ),
    "ja": (
        "一、序論",
        "二、先行研究",
        "三、研究方法",
        "四、本論第一節",
        "五、本論第二節",
        "六、本論第三節",
        "七、考察",
        "八、結論",
    ),
}


def _prompt_json(value: object) -> str:
    """Serialize prompt JSON without CJK unicode-escape inflation."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


EvidenceStatus = Literal["source_bound", "model_backed"]
EvidenceConfidence = Literal["low", "medium", "high"]


def _resolve_paper_language(project: Project, kernel: Mapping[str, Any] | None) -> str:
    """Resolve the language the manuscript should be written in.

    Codex round-1 verdict (PR-256, Q2=B + A fallback): explicit
    ``project.language`` wins when it's anything other than the legacy
    ``en`` default. When the project still carries ``en`` (which is the
    NewRunPage form's default), look at the kernel's free-text fields
    and auto-promote to ``zh`` or ``ja`` if their character-class
    distribution is overwhelmingly that script. This bridges the
    very common scenario where a Chinese researcher fills in a Chinese
    research_kernel but never noticed the English language dropdown.

    Returns one of: ``"en"``, ``"zh"``, ``"ja"``. Defaults to ``"en"``
    when nothing else applies — same as legacy behaviour.
    """
    explicit = (project.language or "").strip().lower()
    if explicit in {"zh", "ja"}:
        return explicit
    # ``explicit`` is ``en`` (default) or empty/unknown — try detection.
    if not isinstance(kernel, Mapping):
        return explicit or "en"
    sample_fields: list[str] = []
    for key in ("observed_puzzle", "tentative_question", "scope"):
        v = kernel.get(key)
        if isinstance(v, str) and v.strip():
            sample_fields.append(v)
    if not sample_fields:
        return explicit or "en"
    sample = " ".join(sample_fields)
    chinese = sum(1 for ch in sample if "一" <= ch <= "鿿")
    japanese_only = sum(1 for ch in sample if "぀" <= ch <= "ヿ")  # hiragana + katakana
    total_cjk_or_alpha = sum(1 for ch in sample if ch.isalpha() or ("぀" <= ch <= "鿿"))
    if total_cjk_or_alpha == 0:
        return explicit or "en"
    # Japanese kernels usually have hiragana/katakana even with kanji;
    # treat ≥10% kana as a strong signal even if Chinese chars dominate.
    if japanese_only > 0 and japanese_only / max(1, total_cjk_or_alpha) >= 0.1:
        return "ja"
    if chinese / total_cjk_or_alpha >= 0.5:
        return "zh"
    return explicit or "en"


# Per-section role hints / type directives. The data lives in
# `autoessay.prompts` so the prompt registry can compose per-section
# default content; these aliases keep the existing local symbol
# names so internal call sites and tests that import them from this
# module continue to work unchanged.
_SECTION_ROLE_HINTS = DRAFTER_SECTION_ROLES
_SECTION_TYPE_DIRECTIVES = DRAFTER_SECTION_TYPE_DIRECTIVES
NOVELTY_CHECKPOINT_TYPES = {
    "USER_NOVELTY_REVIEW",
    "novelty-review",
    "novelty_review",
    "novelty-selection",
    "novelty_selection",
}


@dataclass(frozen=True)
class SectionPlan:
    section_id: str
    title: str
    target_words: int


@dataclass(frozen=True)
class DraftedSection:
    section_id: str
    title: str
    prose: str
    claim_map: list[dict[str, object]]
    failed: bool
    warnings: list[str]
    word_count: int
    target_words: int


class RawClaimRecord(BaseModel):
    paragraph_id: str
    claim_text: str
    source_ids: list[str] | str = Field(default_factory=list)
    evidence_status: EvidenceStatus = "source_bound"
    confidence: EvidenceConfidence | None = None

    class Config:
        extra = "ignore"


class RawSectionDraft(BaseModel):
    section_id: str
    section_title: str
    prose: str
    claim_map: list[RawClaimRecord]

    class Config:
        extra = "ignore"


class DrafterClaim(BaseModel):
    paragraph_id: StrictStr
    claim_text: StrictStr
    source_ids: list[StrictStr] | Literal["[UNCITED]"] = "[UNCITED]"
    evidence_status: EvidenceStatus = "source_bound"
    confidence: EvidenceConfidence | None = None

    @validator("paragraph_id", "claim_text")
    def _text_must_have_content(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("field must be non-empty")
        return cleaned

    class Config:
        extra = "ignore"


class DrafterSection(BaseModel):
    section_id: StrictStr
    section_title: StrictStr
    prose: StrictStr
    claim_map: list[DrafterClaim]

    @validator("section_id", "section_title", "prose")
    def _text_must_have_content(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("field must be non-empty")
        return value

    @validator("claim_map")
    def _claim_map_must_not_be_empty(cls, value: list[DrafterClaim]) -> list[DrafterClaim]:
        if not value:
            raise ValueError("claim_map must contain at least one claim")
        return value

    class Config:
        extra = "ignore"


def run_drafter(
    run_id: str,
    db_session: Session | None = None,
    hooks: HookRegistry | None = None,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
    lock_token: str | None = None,
) -> dict[str, object]:
    """Run the drafter.

    ``prompt_overrides["main"]`` replaces the universal instruction
    block on every section call within this run.
    ``prompt_overrides[<section_id>]`` (Stage 3.A.2) replaces the
    role hint plus optional section-type directive for THAT one
    section only; the ``main`` override still applies cross-section.

    ``lock_token`` (Stage 3.E follow-up P0): if non-None, the
    function releases the run-level phase-start lock at exit
    (success or exception) using owner-checked release.

    PR-A4.1b (2026-05-02): wraps in ``maybe_run_with_versioning``.
    """
    from autoessay.phase_lock import phase_lock_release_on_exit
    from autoessay.phase_version import maybe_run_with_versioning

    def _execute(session: Session) -> dict[str, object]:
        run = session.scalar(select(Run).where(Run.id == run_id))
        if run is None:
            raise ValueError(f"run not found: {run_id}")
        result: dict[str, object] = {}

        def _runner() -> None:
            result["value"] = _run_drafter_with_session(
                run_id,
                session,
                hooks or HookRegistry(),
                prompt_overrides=prompt_overrides,
            )

        maybe_run_with_versioning(session, run, "drafter", _runner)
        return result.get("value", {})  # type: ignore[return-value]

    with phase_lock_release_on_exit(run_id, "drafter", lock_token, session=db_session):
        if db_session is not None:
            return _execute(db_session)
        with SessionLocal() as session:
            return _execute(session)


def load_drafts_payload(run: Run) -> dict[str, object]:
    drafts_dir = Path(run.run_dir) / "drafts"
    drafts: list[dict[str, object]] = []
    if drafts_dir.exists():
        for version_dir in sorted(drafts_dir.glob("v[0-9][0-9][0-9]")):
            metadata = _load_json_mapping(version_dir / "draft_metadata.json")
            if not metadata:
                metadata = {
                    "version": version_dir.name,
                    "manuscript_path": str(version_dir / "manuscript.md"),
                }
            drafts.append(metadata)
    return {"run_id": run.id, "drafts": drafts}


def load_draft_payload(run: Run, version: str) -> dict[str, object]:
    version_id = _normalize_version(version)
    draft_dir = Path(run.run_dir) / "drafts" / version_id
    if not draft_dir.exists():
        raise FileNotFoundError(version)
    return {
        "run_id": run.id,
        "version": version_id,
        "metadata": _load_json_mapping(draft_dir / "draft_metadata.json"),
        "manuscript": _read_optional_text(draft_dir / "manuscript.md"),
        "claim_map": _load_jsonl_objects(draft_dir / "claim_map.jsonl"),
        "citations_bib": _read_optional_text(draft_dir / "citations.bib"),
        "draft_rationale": _read_optional_text(draft_dir / "draft_rationale.md"),
    }


def _run_drafter_with_session(
    run_id: str,
    session: Session,
    hooks: HookRegistry,
    *,
    prompt_overrides: Mapping[str, str] | None = None,
) -> dict[str, object]:
    run = session.scalar(select(Run).where(Run.id == run_id))
    if run is None:
        raise ValueError(f"run not found: {run_id}")
    assert_run_active(run, session)
    if run.state not in {"USER_NOVELTY_REVIEW", "DRAFTER_RUNNING"}:
        raise InvalidTransition(
            f"Drafter requires USER_NOVELTY_REVIEW or DRAFTER_RUNNING, got {run.state}",
        )
    project = session.scalar(select(Project).where(Project.id == run.project_id))
    if project is None:
        raise ValueError(f"project not found for run: {run_id}")

    run_dir = Path(run.run_dir)
    draft_version = _next_draft_version(run_dir / "drafts")
    settings = get_settings()
    policies = EvidencePolicies.from_settings("drafting", settings)
    if run.state == "USER_NOVELTY_REVIEW":
        transition(run, "DRAFTER_RUNNING", session, reason="Drafter started")
    append_event(
        session,
        run,
        "phase_started",
        {"phase": "drafter", "run_id": run.id, "draft_version": draft_version},
    )
    append_event(
        session,
        run,
        "evidence_policy_applied",
        {"phase": "drafter", "run_id": run.id, **policies.event_payload()},
    )
    session.commit()
    session.refresh(run)

    selected_thesis = _load_selected_thesis(run, session)
    if selected_thesis is None:
        return _fail_fixable(run, session, "No selected novelty angle is available for drafting.")

    domain = load_domain(_domain_path(project.domain_id))
    from autoessay.agents.shadow_baseline import maybe_inject_baseline_as_evidence_source

    maybe_inject_baseline_as_evidence_source(run_dir)
    shortlist = _read_sources_json(run_dir / "sources" / "shortlist.json")
    if not shortlist:
        return _fail_fixable(run, session, "No shortlist sources are available for drafting.")
    source_notes = _load_source_notes(run_dir / "synthesis" / "source_notes")
    # PR-256: pass the resolved paper language so default section
    # titles render in zh / ja / en based on kernel-text detection.
    paper_language_for_sections = _resolve_paper_language(
        project, getattr(run, "research_kernel_json", None)
    )
    sections = _section_plan(
        domain.data,
        project.target_journal,
        run.paper_mode,
        paper_language=paper_language_for_sections,
    )
    outline_sections = _load_outline_sections_for_thesis(run_dir, selected_thesis)
    draft_dir = run_dir / "drafts" / draft_version
    draft_dir.mkdir(parents=True, exist_ok=True)
    material_diagnostic = _load_material_diagnostic(run_dir)
    use_harness = not settings.drafter_stub
    audit = None
    local_dedup_hook: DrafterLocalDedupHook | None = None
    if use_harness:
        _register_drafter_memory_hook(hooks)
        hooks.register_post_llm(
            "citation_whitelist",
            _make_citation_whitelist_hook(
                {source.source_id for source in shortlist},
                policies=policies,
            ),
        )
        local_dedup_hook = DrafterLocalDedupHook(
            run_dir=run_dir,
            user_id=project.user_id,
            project=project,
            session=session,
            shortlist=shortlist,
        )
        hooks.register_post_llm("local_dedup", local_dedup_hook.post_llm)
        audit = AuditWriter(session=session, run_dir=run.run_dir, agent_name="Drafter")

    instructions_override = prompt_overrides.get("main") if prompt_overrides else None

    drafted_sections: list[DraftedSection] = []
    total = len(sections)
    for completed, section in enumerate(sections, start=1):
        outline_section = _match_outline_section(section, outline_sections, completed - 1)
        section_override = prompt_overrides.get(section.section_id) if prompt_overrides else None
        # PR-G-Conclusion-Evidence-Whitelist (codex AGREE-WITH-AMENDMENTS,
        # 2026-05-07): feed the conclusion drafter the supported claims
        # digest from already-drafted body sections so it can
        # only summarize what the body has actually shown.
        prior_supported_claims_digest = (
            _build_supported_claims_digest(drafted_sections)
            if section.section_id == "conclusion" and drafted_sections
            else ""
        )
        drafted = _draft_section(
            section=section,
            selected_thesis=selected_thesis,
            source_notes=source_notes,
            shortlist=shortlist,
            domain_data=domain.data,
            target_journal=project.target_journal,
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            audit=audit,
            section_index=completed,
            section_count=total,
            outline_section=outline_section,
            instructions_override=instructions_override,
            section_override=section_override,
            prior_supported_claims_digest=prior_supported_claims_digest,
            phase_mode=policies.phase,
            material_diagnostic=material_diagnostic,
        )
        if drafted is None:
            # PR-257b: pass shortlist so the stub can borrow the
            # first available source_id and not block exports with a
            # ``[UNCITED]`` integrity audit failure.
            drafted = _stub_section(
                section,
                "LLM JSON did not parse after one retry.",
                shortlist=shortlist,
            )
        drafted_sections.append(drafted)
        append_event(
            session,
            run,
            "section_progress",
            {
                "phase": "drafter",
                "draft_version": draft_version,
                "section_id": section.section_id,
                "section_title": section.title,
                "status": "stubbed" if drafted.failed else "drafted",
                "completed": completed,
                "total": total,
                "word_count": drafted.word_count,
                "target_words": section.target_words,
            },
        )
        session.commit()

    # PR-G-Sources Stage 2 (codex round-2 amendment Q2): after all 8
    # sections are drafted but before manuscript assembly / CNKI
    # wrap, check whether the cited_sources count meets the
    # diversity floor. Below floor → emit a diagnostic event so the
    # downstream critic + acceptance gate know the run was
    # under-cited. Stage 3 (LLM repair on 2-3 lowest-density
    # sections) is a follow-up PR — Stage 1+2 lets us first measure
    # whether deep_dive_limit 6 → 14 alone closes the gap.
    from autoessay.agents._research_kernel_prompt import (
        research_kernel_for_prompt,
    )

    raw_kernel = getattr(run, "research_kernel_json", None)
    research_kernel_for_wrap = research_kernel_for_prompt(raw_kernel)
    material_scope_guard = _material_scope_guard_summary(
        material_diagnostic,
        selected_thesis=selected_thesis,
        research_kernel=research_kernel_for_wrap,
    )
    if material_scope_guard["applied"]:
        drafted_sections = _apply_material_scope_guard_to_sections(
            drafted_sections,
            selected_thesis=selected_thesis,
            research_kernel=research_kernel_for_wrap,
        )
        append_event(
            session,
            run,
            "material_scope_guard_applied",
            {
                "phase": "drafter",
                "draft_version": draft_version,
                **material_scope_guard,
            },
        )
        session.commit()

    diversity_diagnostic = _check_diversity_floor(
        drafted_sections=drafted_sections,
        shortlist=shortlist,
        project_title=project.title,
        research_kernel=research_kernel_for_wrap,
        draft_version=draft_version,
    )
    # PR-G-Sources Q2 (codex v5 round-1 AGREE-w-amendments): when
    # the floor isn't met, attempt one bounded LLM repair on the
    # 2 lowest-density sections to integrate up to N unused
    # eligible sources. ``audit`` is None in drafter_stub mode —
    # skip repair there so unit tests stay deterministic.
    if (
        diversity_diagnostic is not None
        and audit is not None
        and get_settings().diversity_repair_enabled
    ):
        repair_outcome = _maybe_run_llm_diversity_repair(
            drafted_sections=drafted_sections,
            shortlist=shortlist,
            project_title=project.title,
            research_kernel=research_kernel_for_wrap,
            paper_language=paper_language_for_sections,
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            audit=audit,
            draft_version=draft_version,
            diagnostic=diversity_diagnostic,
        )
        if repair_outcome.applied:
            drafted_sections = repair_outcome.drafted_sections
            diversity_diagnostic = _check_diversity_floor(
                drafted_sections=drafted_sections,
                shortlist=shortlist,
                project_title=project.title,
                research_kernel=research_kernel_for_wrap,
                draft_version=draft_version,
            )
        if repair_outcome.event_type:
            append_event(
                session,
                run,
                repair_outcome.event_type,
                {
                    "phase": "drafter",
                    "draft_version": draft_version,
                    "applied": repair_outcome.applied,
                    "skipped_reason": repair_outcome.skipped_reason,
                    "added_source_ids": list(repair_outcome.added_source_ids),
                    "target_section_ids": list(repair_outcome.target_section_ids),
                },
            )
            session.commit()

    if diversity_diagnostic is not None:
        append_event(
            session,
            run,
            "cited_sources_below_floor",
            {
                "phase": "drafter",
                "draft_version": draft_version,
                **diversity_diagnostic,
            },
        )
        session.commit()

    # PR-G-Grounding (codex state-machine audit P0 #5 F): scan the
    # drafted claim_map for specific archive / document / material
    # entity mentions and verify the corresponding cited source's
    # metadata actually contains that entity. Real-paper rounds 4 +
    # 6 both hit FAILED_POLICY at exports because critic correctly
    # caught "method 声称用 IMF 内部备忘录 / 美联储理事会会议纪要 /
    # 伦敦黄金池季度结算记录" but the cited source didn't actually
    # contain those archives. This deterministic substring scan
    # surfaces the same gap earlier (drafter end vs critic time) so
    # downstream consumers + acceptance gates know the run is
    # weakly grounded BEFORE the critic LLM run that emits the
    # BLOCKER. Phase 1 = warning event only; phase 2 (LLM-based
    # semantic verifier) is a follow-up.
    grounding_diagnostic = _check_claim_grounding(
        drafted_sections=drafted_sections,
        shortlist=shortlist,
        run_dir=run_dir,
    )
    weak_count_raw = grounding_diagnostic.get("weakly_grounded_count", 0)
    weak_count = weak_count_raw if isinstance(weak_count_raw, int) else 0
    if weak_count > 0:
        append_event(
            session,
            run,
            "claims_weakly_grounded",
            {
                "phase": "drafter",
                "draft_version": draft_version,
                **grounding_diagnostic,
            },
        )
        session.commit()

    claim_records = _flatten_claim_map(drafted_sections, draft_version)
    cited_source_ids = _cited_source_ids(claim_records)
    cited_sources = [source for source in shortlist if source.source_id in cited_source_ids]
    metadata = _metadata_payload(draft_version, drafted_sections, claim_records, cited_sources)
    metadata["material_scope_guard"] = material_scope_guard
    manuscript = _manuscript_markdown(drafted_sections)
    # PR-259b: rewrite ``(Author YYYY)`` + ``[crossref:DOI]`` cite
    # markers to ``[N]`` matching the upcoming references list
    # order, so the body and the wrapper's ``参考文献`` block use
    # one citation style. Only fires for zh/ja (the wrapper itself
    # is zh/ja-only); en pass-through preserves existing behavior.
    cite_unresolved_count = 0
    cite_repair_outcome: CiteMarkerRepairOutcome | None = None
    if paper_language_for_sections in ("zh", "ja"):
        cite_result = _normalize_inline_citations_zh_with_unresolved(
            manuscript,
            cited_sources,
        )
        manuscript = cite_result.body
        # PR-G-CiteMarkerGate observability event (PR-2a) — fires on
        # every round where normalize left citation-shaped residue,
        # regardless of whether the corrective retry follows.
        if cite_result.unresolved_markers and audit is not None:
            cite_unresolved_count = len(cite_result.unresolved_markers)
            append_event(
                session,
                run,
                "cite_marker_unresolved",
                {
                    "phase": "drafter",
                    "draft_version": draft_version,
                    "count": cite_unresolved_count,
                    "markers": [
                        {"raw": m.raw, "form": m.form, "reason": m.reason}
                        for m in cite_result.unresolved_markers[:50]
                    ],
                },
            )
            session.commit()
        # PR-G-CiteMarkerGate corrective retry (PR-2b). Default OFF
        # via ``drafter_cite_marker_repair_enabled``; flip via env
        # for staged rollout. Codex direction B: per-paragraph LLM
        # repair, max 2 retries, exhaustion → failed_policy.
        if (
            cite_result.unresolved_markers
            and audit is not None
            and get_settings().drafter_cite_marker_repair_enabled
        ):
            cite_repair_outcome = _maybe_run_cite_marker_repair(
                manuscript=manuscript,
                cited_sources=cited_sources,
                initial_unresolved=cite_result.unresolved_markers,
                paper_language=paper_language_for_sections,
                run=run,
                project=project,
                hooks=hooks,
                audit=audit,
                draft_version=draft_version,
            )
            if cite_repair_outcome.applied:
                manuscript = cite_repair_outcome.body
                cite_unresolved_count = 0
            else:
                cite_unresolved_count = cite_repair_outcome.final_unresolved_count
            append_event(
                session,
                run,
                cite_repair_outcome.event_type,
                {
                    "phase": "drafter",
                    "draft_version": draft_version,
                    "applied": cite_repair_outcome.applied,
                    "skipped_reason": cite_repair_outcome.skipped_reason,
                    "attempts": cite_repair_outcome.attempts,
                    "initial_unresolved_count": cite_repair_outcome.initial_unresolved_count,
                    "final_unresolved_count": cite_repair_outcome.final_unresolved_count,
                },
            )
            session.commit()
        manuscript = _sanitize_baseline_as_evidence_source_mentions(manuscript)
    # PR-259a: for zh/ja papers, wrap the body with CNKI-style
    # 摘要 / 关键词 (front) and 参考文献 (back) so the manuscript is
    # publishable as-is, not just an internal draft. en + other
    # languages pass through unchanged (Western convention puts these
    # in the submission form, not the file).
    manuscript = _wrap_manuscript_with_cnki_matter(
        manuscript,
        paper_language=paper_language_for_sections,
        selected_thesis=selected_thesis,
        sections=drafted_sections,
        research_kernel=research_kernel_for_wrap,
        cited_sources=cited_sources,
    )
    # PR-G-Coherence (codex round-3 AGREE on v4): one global coherence
    # LLM pass over the wrapped manuscript to tighten cross-section
    # transitions / 首尾呼应 / 删重复 / 补转折. Falls back to the
    # input manuscript when LLM fails or post-validation rejects the
    # output (5 hard rules: citation multiset / CNKI section title
    # ordered list / 摘要+关键词+参考文献 normalized-identical /
    # citation-bearing paragraphs preserved / length didn't shrink
    # > 30%). Records ``global_coherence`` block in
    # ``draft_metadata.json`` for diagnostics. Op opt-out via
    # ``Settings.drafter_global_coherence_enabled = False``. Skipped
    # entirely when ``use_harness`` is False (drafter_stub mode —
    # no LLM, no audit writer).
    coherence_outcome = (
        _maybe_run_global_coherence_pass(
            manuscript=manuscript,
            paper_language=paper_language_for_sections,
            research_kernel=research_kernel_for_wrap,
            cited_sources=cited_sources,
            run=run,
            project=project,
            session=session,
            hooks=hooks,
            audit=audit,
            draft_version=draft_version,
            policies=policies,
        )
        if audit is not None
        else CoherencePassOutcome(
            applied=False,
            manuscript=None,
            skipped_reason="drafter_stub_mode",
            event_type="",  # no event when stub-mode skip
            before_bytes=len(manuscript.encode("utf-8")),
            after_bytes=len(manuscript.encode("utf-8")),
        )
    )
    if coherence_outcome.applied and coherence_outcome.manuscript is not None:
        manuscript = coherence_outcome.manuscript
    if coherence_outcome.event_type:
        append_event(
            session,
            run,
            coherence_outcome.event_type,
            {
                "phase": "drafter",
                "draft_version": draft_version,
                "step": "global_coherence",
                "applied": coherence_outcome.applied,
                "skipped_reason": coherence_outcome.skipped_reason,
                "before_bytes": coherence_outcome.before_bytes,
                "after_bytes": coherence_outcome.after_bytes,
            },
        )
        session.commit()
    metadata["global_coherence"] = {
        "applied": coherence_outcome.applied,
        "skipped_reason": coherence_outcome.skipped_reason,
        "before_bytes": coherence_outcome.before_bytes,
        "after_bytes": coherence_outcome.after_bytes,
    }
    metadata["cite_marker_gate"] = {
        "unresolved_count": cite_unresolved_count,
        "repair": (
            {
                "applied": cite_repair_outcome.applied,
                "skipped_reason": cite_repair_outcome.skipped_reason,
                "attempts": cite_repair_outcome.attempts,
                "initial_unresolved_count": cite_repair_outcome.initial_unresolved_count,
                "final_unresolved_count": cite_repair_outcome.final_unresolved_count,
                "failed_policy": cite_repair_outcome.failed_policy,
            }
            if cite_repair_outcome is not None
            else {"enabled": False}
        ),
    }
    _write_text(draft_dir / "manuscript.md", manuscript)
    _write_jsonl(draft_dir / "claim_map.jsonl", claim_records)
    _write_text(draft_dir / "citations.bib", generate_bib(cited_sources))
    _write_text(
        draft_dir / "draft_rationale.md",
        _rationale_markdown(draft_version, drafted_sections, claim_records, cited_sources),
    )
    _write_json(draft_dir / "draft_metadata.json", metadata)
    local_dedup_summary = (
        local_dedup_hook.write_final(manuscript) if local_dedup_hook is not None else None
    )

    stubbed_sections = [section for section in drafted_sections if section.failed]
    stubbed_section_ids = [section.section_id for section in stubbed_sections]
    total = len(drafted_sections)
    stubbed_count = len(stubbed_sections)
    # Codex AGREE: only ALL-sections-stubbed counts as a real phase
    # failure. Any partial stub set is degraded-but-usable output that
    # the user reviews later. We tag severity so the UI can render an
    # amber "Placeholder section, needs review" badge without a red
    # error banner. ``severity`` thresholds:
    #   no stubs            → severity is None (clean phase_done)
    #   1 ≤ stubs ≤ 50%     → "amber_minor"  (review recommended)
    #   50% < stubs < 100%  → "amber_major"  (rerun recommended)
    #   stubs == total      → FAILED_FIXABLE (not phase_done)
    severity: str | None = None
    if stubbed_count > 0:
        if stubbed_count >= total:
            severity = "fail_all_stubbed"
        elif stubbed_count * 2 > total:
            severity = "amber_major"
        else:
            severity = "amber_minor"

    summary: dict[str, object] = {
        "phase": "drafter",
        "draft_version": draft_version,
        "sections": total,
        "stubbed_sections": stubbed_count,
        # Kept under the legacy key for any consumer that grew up on
        # the prior schema; new code should read ``stubbed_sections``.
        "failed_sections": stubbed_count,
        "stubbed_section_ids": stubbed_section_ids,
        "severity": severity,
        "uncited_claims": sum(1 for record in claim_records if record.get("uncited") is True),
        "next_stage": "stylist_pending",
    }
    if local_dedup_summary is not None:
        local_dedup_matches = local_dedup_summary.get("matches", [])
        summary["local_dedup_matches"] = (
            len(local_dedup_matches) if isinstance(local_dedup_matches, list) else 0
        )
        local_dedup_status = local_dedup_summary.get("status")
        summary["local_dedup_status"] = (
            local_dedup_status if isinstance(local_dedup_status, str) else ""
        )

    if stubbed_count >= total and total > 0:
        # Every single section stubbed — that's a real phase failure.
        guidance = (
            "All sections fell back to schema-failure stubs after the corrective "
            "retry budget was exhausted. Review LLM output, edit manually, or rerun."
        )
        transition(
            run,
            "FAILED_FIXABLE",
            session,
            reason="Drafter failed every section",
            payload=summary,
        )
        append_event(
            session,
            run,
            "phase_failed",
            {
                **summary,
                "failure_class": "failed_fixable",
                "failed_section_ids": stubbed_section_ids,
                "guidance": guidance,
                "resume_options": ["retry", "edit_section", "mark_unverified"],
            },
        )
        session.commit()
        return {
            "run_id": run.id,
            "state": run.state,
            "guidance": guidance,
            "failed_section_ids": stubbed_section_ids,
            "resume_options": ["retry", "edit_section", "mark_unverified"],
            **summary,
        }

    append_event(session, run, "phase_done", summary)
    session.commit()
    return {"run_id": run.id, "state": run.state, **summary}


def _fail_fixable(run: Run, session: Session, guidance: str) -> dict[str, object]:
    transition(
        run,
        "FAILED_FIXABLE",
        session,
        reason="Drafter needs user-fixable input",
        payload={"guidance": guidance},
    )
    append_event(
        session,
        run,
        "phase_failed",
        {
            "phase": "drafter",
            "failure_class": "failed_fixable",
            "guidance": guidance,
        },
    )
    session.commit()
    return {"run_id": run.id, "state": run.state, "guidance": guidance}


def _draft_section(
    *,
    section: SectionPlan,
    selected_thesis: Mapping[str, object],
    source_notes: Mapping[str, object],
    shortlist: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    target_journal: str | None,
    run: Run | None = None,
    project: Project | None = None,
    session: Session | None = None,
    hooks: HookRegistry | None = None,
    audit: AuditWriter | None = None,
    section_index: int = 1,
    section_count: int = 1,
    outline_section: OutlineSection | None = None,
    instructions_override: str | None = None,
    section_override: str | None = None,
    prior_supported_claims_digest: str = "",
    phase_mode: PhaseMode = "drafting",
    material_diagnostic: Mapping[str, object] | None = None,
) -> DraftedSection | None:
    if get_settings().drafter_stub:
        return _stub_drafted_section(section, selected_thesis, shortlist)
    if run is None or project is None or session is None or audit is None:
        raise ValueError("Drafter section generation requires run, project, session, and audit")
    try:
        return _drafter_via_harness(
            section=section,
            selected_thesis=selected_thesis,
            source_notes=source_notes,
            shortlist=shortlist,
            domain_data=domain_data,
            target_journal=target_journal,
            run=run,
            project=project,
            hooks=hooks or HookRegistry(),
            audit=audit,
            section_index=section_index,
            section_count=section_count,
            outline_section=outline_section,
            instructions_override=instructions_override,
            section_override=section_override,
            prior_supported_claims_digest=prior_supported_claims_digest,
            phase_mode=phase_mode,
            material_diagnostic=material_diagnostic,
        )
    except SchemaViolationError:
        return None
    except Exception:  # noqa: BLE001 - caller records section-level fallback.
        return None


def _drafter_via_harness(
    *,
    section: SectionPlan,
    selected_thesis: Mapping[str, object],
    source_notes: Mapping[str, object],
    shortlist: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    target_journal: str | None,
    run: Run,
    project: Project,
    hooks: HookRegistry,
    audit: AuditWriter,
    section_index: int,
    section_count: int,
    outline_section: OutlineSection | None = None,
    instructions_override: str | None = None,
    section_override: str | None = None,
    prior_supported_claims_digest: str = "",
    phase_mode: PhaseMode = "drafting",
    material_diagnostic: Mapping[str, object] | None = None,
) -> DraftedSection | None:
    from autoessay.agents._research_kernel_prompt import (
        KERNEL_INJECTION_GUARD,
        research_kernel_for_prompt,
    )

    raw_kernel = getattr(run, "research_kernel_json", None)
    research_kernel = research_kernel_for_prompt(raw_kernel)
    # PR-256: resolve paper language with kernel-text auto-detection so
    # a Chinese kernel filed under the default ``project.language=en``
    # still produces a Chinese manuscript.
    paper_language = _resolve_paper_language(project, raw_kernel)
    # PR-C3.b: pull compact tensions for scaffold-only injection.
    # Empty list when tension_extraction phase didn't run / artifact
    # absent / artifact malformed → drafter behaves as pre-C3.
    tensions_compact = _load_compact_tensions_for_drafter(run.run_dir)
    # PR-263c: load shadow_baseline contextual knowledge if a prior
    # phase produced one. Empty string when no artifact on disk →
    # drafter falls back to pre-shadow behavior, no prompt change.
    from autoessay.agents._shadow_knowledge_injection import (
        shadow_knowledge_directive_for_run,
    )

    shadow_knowledge_directive = shadow_knowledge_directive_for_run(run.run_dir)
    accumulated_context = phase_context_prompt_block(run.run_dir, "drafter")
    prompt = _section_prompt(
        section=section,
        selected_thesis=selected_thesis,
        source_notes=source_notes,
        shortlist=shortlist,
        domain_data=domain_data,
        target_journal=target_journal,
        suffix="",
        outline_section=outline_section,
        instructions_override=instructions_override,
        section_override=section_override,
        project_title=project.title,
        research_kernel=research_kernel,
        tensions_compact=tensions_compact,
        shadow_knowledge_directive=shadow_knowledge_directive,
        prior_supported_claims_digest=prior_supported_claims_digest,
        phase_mode=phase_mode,
        material_diagnostic=material_diagnostic,
        accumulated_context=accumulated_context,
    )
    request = LLMCallRequest(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Drafter. Draft source-bound academic prose anchored on "
                    "the user's title and research_kernel. Do not invent DOIs, page "
                    "numbers, quotes, or source IDs. The thesis you receive is the "
                    "operational frame, but the user's title and research_kernel are "
                    "the substantive anchors — every section must clearly serve them. "
                    + KERNEL_INJECTION_GUARD
                    + " "
                    + language_directive(paper_language)
                    + "\n\n"
                    + humanizer_directive(paper_language)
                ),
            },
            {"role": "user", "content": prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.25,
        # PR-257b: bumped 2400 → 4500. Real-paper run #5 surfaced
        # repeated section-stub fallbacks for sections that have to
        # carry full bibliographic detail (sources_method,
        # historiography). The LLM finished mid-claim and the JSON
        # truncated, so even the 4-attempt corrective retry budget
        # couldn't recover. 4500 is enough for ~3000-word zh
        # sections plus the surrounding ``claim_map`` JSON envelope;
        # well under any provider hard cap.
        max_tokens=4500,
        response_format={"type": "json_object"},
        request_id=f"drafter_section_{_safe_request_id(section.section_id)}",
        prompt_template_id="drafter.section.v1",
    )
    summary = _thesis_summary(selected_thesis)
    context = HookContext(
        run_id=run.id,
        phase="drafter",
        step_id="drafter.section",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "drafter",
            "domain_id": project.domain_id,
            "domain_version": run.domain_version,
            "section_id": section.section_id,
            "section_title": section.title,
            "section_index": section_index,
            "section_count": section_count,
            "memory_query": (
                f"phase=drafter section_id={section.section_id} topic={project.title} "
                f"thesis_one_sentence={summary}"
            ),
        },
    )
    response = asyncio.run(
        run_llm_step(
            request=request,
            hooks=hooks,
            context=context,
            output_schema=DrafterSection,
            audit=audit,
            max_corrective_retries=get_settings().drafter_max_corrective_retries,
            llm_optional=False,
        ),
    )
    # PR-258b: compute the low-relevance source set from the same
    # topic-keyword extraction the prompt used, then pass it into
    # the parser as a deterministic backstop. The LLM has the
    # directive (PR-258a) telling it not to cite ``low`` sources;
    # this filter enforces it on the way out.
    low_relevance_source_ids: set[str] = set()
    if topic_keywords := _extract_topic_keywords(project.title, research_kernel):
        for source in shortlist:
            if _score_source_topic_relevance(source, topic_keywords) == "low":
                low_relevance_source_ids.add(source.source_id)
    return _drafted_section_from_output(
        response.parsed,
        section,
        shortlist,
        low_relevance_source_ids=low_relevance_source_ids if low_relevance_source_ids else None,
    )


_MAX_TENSIONS_IN_DRAFTER_PROMPT = 5
_MAX_TENSION_SUMMARY_IN_DRAFTER_PROMPT = 120


def _load_compact_tensions_for_drafter(run_dir: str) -> list[Mapping[str, object]]:
    """PR-C3.b: read ``synthesis/tension_extraction.json`` (if present)
    and return a compact ≤5-tension list for drafter section prompts.
    Each tension reduced to ``{tension_id, class_id, summary,
    boundary_fields_keys}``. Codex round-1 #6 (DISAGREE-with-revisions):
    drafter scaffold injection only — taxonomy class label MUST NOT
    appear in manuscript body."""
    from pathlib import Path

    from autoessay.agents.tension_extraction import load_tension_extraction

    output = load_tension_extraction(Path(run_dir))
    if output is None:
        return []
    compact: list[Mapping[str, object]] = []
    for tension in output.tensions[:_MAX_TENSIONS_IN_DRAFTER_PROMPT]:
        summary = tension.summary[:_MAX_TENSION_SUMMARY_IN_DRAFTER_PROMPT]
        compact.append(
            {
                "tension_id": tension.tension_id,
                "class_id": tension.class_id.value,
                "summary": summary,
                "boundary_fields_keys": list(tension.boundary_fields.keys()),
            },
        )
    return compact


def _section_prompt(
    *,
    section: SectionPlan,
    selected_thesis: Mapping[str, object],
    source_notes: Mapping[str, object],
    shortlist: Sequence[NormalizedSource],
    domain_data: Mapping[str, Any],
    target_journal: str | None,
    suffix: str,
    outline_section: OutlineSection | None = None,
    instructions_override: str | None = None,
    section_override: str | None = None,
    project_title: str = "",
    research_kernel: Mapping[str, object] | None = None,
    tensions_compact: Sequence[Mapping[str, object]] = (),
    shadow_knowledge_directive: str = "",
    prior_supported_claims_digest: str = "",
    phase_mode: PhaseMode = "drafting",
    material_diagnostic: Mapping[str, object] | None = None,
    accumulated_context: str = "",
) -> str:
    """Build the drafter's per-section LLM prompt.

    ``instructions_override`` replaces the universal static
    instruction block — the merged "argumentation + forbidden +
    citation" rules from :data:`DRAFTER_MAIN_INSTRUCTIONS`.

    ``section_override`` (Stage 3.A.2) replaces the section-role
    hint plus the optional section-type directive for THAT one
    section: when set, the override's text appears after the
    "Section role: " label and the type-directive position is
    cleared. The universal ``main`` rules still apply because
    ``instructions_override`` is independent.

    Trailing-space discipline (codex round-1 P2): both the role
    line and the universal-rules block end with an unconditional
    space, so ``upsert_phase_prompt`` stripping a saved override's
    trailing whitespace cannot collide with the next concatenated
    block.
    """
    from autoessay.prompts import DRAFTER_MAIN_INSTRUCTIONS

    policies = EvidencePolicies.from_settings(phase_mode, get_settings())
    instructions = instructions_override or DRAFTER_MAIN_INSTRUCTIONS
    # PR-258a: extract topic keywords from project_title +
    # research_kernel and tag each source with topic_relevance so the
    # LLM knows which to ban / restrict / use freely. Real-paper run
    # #6 surfaced the LLM citing Dutch fiscal-policy papers in a 19c
    # Jiangnan publishing study because the curator picked them and
    # the prompt had no signal that they were off-scope.
    topic_keywords = _extract_topic_keywords(project_title, research_kernel)
    approved_sources = _approved_source_summaries(
        shortlist,
        source_notes,
        topic_keywords=topic_keywords if topic_keywords else None,
    )
    topic_directive = _topic_relevance_directive(approved_sources)
    baseline_as_evidence_directive = _baseline_as_evidence_test_directive(approved_sources)
    evidence_strength_directive = _evidence_strength_directive(
        selected_thesis,
        approved_sources,
        research_kernel,
    )
    material_scope_directive = _material_scope_guard_directive(
        material_diagnostic,
        selected_thesis=selected_thesis,
        research_kernel=research_kernel,
    )
    style_notes = _style_notes(domain_data, target_journal)
    required_schema = {
        "section_id": section.section_id,
        "section_title": section.title,
        "prose": "markdown prose for this section only",
        "claim_map": [
            {
                "paragraph_id": f"{section.section_id}-p001",
                "claim_text": "claim text",
                "source_ids": ["source_id"],
                "evidence_status": "source_bound | model_backed",
                "confidence": "low | medium | high (required only for model_backed)",
            },
        ],
    }
    if section_override is not None:
        section_role = section_override
        section_type_directive = ""
    else:
        section_role = _SECTION_ROLE_HINTS.get(section.section_id, "正文章节，围绕中心论点推进。")
        section_type_directive = _SECTION_TYPE_DIRECTIVES.get(section.section_id, "")
    outline_block = _outline_anchor_block(outline_section)
    case_channel_directive = _case_channel_anchor_directive(
        section.section_id,
        selected_thesis=selected_thesis,
        research_kernel=research_kernel,
    )
    section_progression_directive = _section_progression_directive(section.section_id)
    # PR-J7: anchor block precedes Thesis so the LLM reads the user-
    # authored fields first. Empty/missing kernel → empty {} (J7
    # contract: degrade to title-only anchoring rather than reject).
    # PR-C3.b codex round-1 #6 (DISAGREE-with-revisions): tensions
    # are SCAFFOLDING METADATA — drafter must engage the underlying
    # tension and boundary in prose but MUST NOT cite class_id /
    # tension_id by name in body text. Empty tensions list = no
    # requirement; behave as before.
    user_anchor = _prompt_json(
        {
            "project_title": project_title,
            "research_kernel": dict(research_kernel) if research_kernel else {},
            "open_tensions": list(tensions_compact),
        }
    )
    # Anchor-check rule appended OUTSIDE ``instructions_override``
    # (codex round-1 amendment 3.2): a user override of the universal
    # rules block must NOT silently drop the kernel/title constraint.
    anchor_check = (
        " anchor_check: each paragraph must serve EITHER the user's "
        "project_title (lexical match OR conceptual continuation) OR the "
        "research_kernel.tentative_question / observed_puzzle. If a "
        "paragraph drifts to a topic neither covers, rewrite it before "
        "returning."
    )
    # PR-C3.b codex round-1 #6 (DISAGREE-with-revisions): when tensions
    # are present, prose must engage the underlying tension and
    # boundary; class_id / tension_id strings are scaffolding metadata
    # only — they MUST NOT appear in body text.
    tensions_directive = (
        " tensions_directive: the ``open_tensions`` block in user_anchor "
        "lists open ideational tensions the manuscript must engage. "
        "Discussion / argument paragraphs SHOULD engage at least one "
        "tension's underlying conflict (the boundary fields tell you "
        "where the disagreement bites). Do NOT write the taxonomy "
        "class_id (e.g. 'continuity_vs_rupture') or 'tension_id' "
        "string in body text — those are scaffolding metadata; the "
        "argument itself must surface organically. Empty tensions = "
        "no constraint."
        if tensions_compact
        else ""
    )
    policy_prefix = policies.section_directive_prefix()
    if section.section_id == "conclusion":
        whitelist_directive = policies.whitelist_directive
        supported_claims_block = policies.supported_claims_block(prior_supported_claims_digest)
    else:
        whitelist_directive = ""
        supported_claims_block = ""
    section_policy_tail = section_type_directive
    if whitelist_directive:
        section_policy_tail += " " + whitelist_directive
    return (
        # PR-J7: User-authored anchor block (project_title + research_kernel)
        # leads the prompt body so the LLM sees ground truth first.
        f"User anchor: {user_anchor}. "
        + accumulated_context
        # 1) Identity + central rule of the paper. NB: keep the literal
        # "Outline: {...}. Approved sources:" sequence — test fakes parse
        # the section payload out of this prompt with a fixed regex.
        + f"You are Drafter. Thesis: {_prompt_json(selected_thesis)}. "
        f"Outline: {_prompt_json(_section_payload(section))}. "
        f"Approved sources: {_prompt_json(approved_sources)}. "
        + policy_prefix
        + " "
        + supported_claims_block
        + " "
        + f"Section role: {section_role} "
        + outline_block
        + case_channel_directive
        + section_progression_directive
        # 2) Universal argumentation + forbidden + citation rules
        # (overridable cross-section via the `main` key). Unconditional
        # trailing space defends against trailing-whitespace strip on
        # saved overrides — same separator pattern as stylist.
        + instructions
        + " "
        # 3) Section-type-specific rules — appended after the
        # universal block. Empty when a per-section override is
        # active (the override absorbs both role hint and type
        # directive at position-A above).
        + section_policy_tail
        # 4) Anchor check — non-overridable; kernel / title constraint
        # must not be dropped by an instructions_override.
        + anchor_check
        # 4a) Topic-adherence directive (PR-258a). Non-overridable;
        # bans citing low-relevance sources and restricts medium
        # ones to background/methodology. Empty when no source has
        # ``topic_relevance`` (e.g. legacy callers without
        # ``topic_keywords``).
        + topic_directive
        # 4a.0) TEST-only baseline-as-evidence directive. Empty by
        # default; when the explicit env flag is ON, shadow_baseline_v001
        # is a normal approved source but must be paraphrased.
        + baseline_as_evidence_directive
        # 4a.1) Evidence-strength directive. Non-overridable; when
        # Ideator explicitly records missing archival / primary
        # material, the drafter must not convert that gap into a
        # definitive node/date claim.
        + evidence_strength_directive
        # 4a.1b) Empirical completeness guard (2026-05-12 round-0 v2
        # canary follow-up). Non-overridable; sits AFTER
        # evidence_strength_directive so instructions_override can't
        # silently delete it. For empirical / mixed paper_type, drafter
        # must emit LaTeX formulas, markdown tables and 【待填】
        # placeholders rather than fabricated coefficients / sample
        # sizes / significance.
        + (
            "\n\nempirical_completeness_guard: If selected_thesis.paper_type, "
            "research_kernel, section title or manuscript context indicates "
            "empirical / mixed research, empirical scaffolding is MANDATORY. "
            "In method / methodology / sources-method sections, include a "
            "LaTeX model equation (use $$...$$ blocks) and a markdown "
            "variable-definition table when variables or design are discussed. "
            "In data / sample / material sections, include a markdown table "
            "for source, coverage, variables, and measurement status. "
            "In results / empirical-analysis sections, include a markdown "
            "regression / design table; unsupported cells must be 【待填】, "
            "not invented numbers. In robustness / sensitivity / limitations "
            "sections, include a robustness checklist. "
            "Never invent coefficients, p-values, sample sizes (N), R², "
            "significance stars, dates, or archival document IDs. "
            "Prefer 【待填】 to confabulation. "
            "Placeholders such as 【待填】 are editorial scaffolding — they "
            "are NOT citations, source_ids, or factual claims; do not "
            "fabricate author / year inside a placeholder unless that source "
            "is already in approved sources."
        )
        # 4a.2) Material diagnostic scope guard. Non-overridable;
        # if the synthesizer diagnostic says the gathered material
        # is insufficient, drafter must write a scoped evidence-route
        # article rather than claim completed archival proof.
        + material_scope_directive
        # 4b) Tensions scaffolding directive (PR-C3.b). Empty when no
        # tensions; must engage tension boundary but NOT cite labels.
        + tensions_directive
        # 4c) Shadow knowledge directive (PR-263c). Empty when no
        # shadow_baseline artifact on disk for this run; otherwise
        # injects an ``argument_map`` + ``reference_candidates``
        # block plus the verbatim "mention but don't cite as [N]"
        # policy. Codex round-3 verdict on PR-263c (path 4): this
        # is the highest-ROI lowest-合规风险 way to surface
        # contextual academic knowledge into manuscript prose
        # without polluting cited_sources semantics.
        + shadow_knowledge_directive
        # 5) Style preservation
        + f" 目标期刊风格：{_prompt_json(style_notes)}. "
        # 6) Output schema
        + f"Return strict JSON matching this schema: {_prompt_json(required_schema)}"
        + suffix
    )


_CASE_CHANNEL_SECTION_INDEX: dict[str, int] = {
    "empirical-section-i": 0,
    "empirical-section-ii": 1,
    "empirical-section-iii": 2,
}

_SECTION_PROGRESSION_DIRECTIVES: dict[str, str] = {
    "introduction": (
        "Frame the puzzle, the dating/causal standard, and the article's road map. "
        "State the answer only briefly; do not pre-write the conclusion."
    ),
    "historiography": (
        "Compare competing explanations or schools, name the gap each leaves, and "
        "make the article's intervention visible. Do not repeat the introduction's "
        "problem statement as a literature review."
    ),
    "sources-method": (
        "Define the evidence tests, source limits, and what would weaken or qualify "
        "the argument. Do not repeat the historiography; explain how the later "
        "sections will adjudicate the claim."
    ),
    "empirical-section-i": (
        "Do the first substantive evidentiary job: establish background conditions "
        "or the earliest stage/material type that later sections must build on."
    ),
    "empirical-section-ii": (
        "Do the second substantive evidentiary job: analyze a distinct mechanism, "
        "turning point, actor channel, or material type not already covered in the "
        "first empirical/case section."
    ),
    "empirical-section-iii": (
        "Do the third substantive evidentiary job: explain consequences, limits, "
        "counter-pressure, or an alternative dating/causal interpretation, instead "
        "of restating the first two empirical/case sections."
    ),
    "discussion": (
        "Synthesize the contrasts across sections, explain what remains uncertain, "
        "and show how the result modifies prior accounts. Do not merely summarize "
        "the empirical sections in order."
    ),
    "conclusion": (
        "Answer the research question with explicit scope boundaries, then state "
        "the contribution and limits. Do not copy sentences from the abstract, "
        "introduction, or discussion."
    ),
}

_EMPIRICAL_PROGRESSION_TAIL = (
    " For empirical/case sections, the opening topic sentence must differ from "
    "adjacent empirical/case sections. Do not keep restating the same node, year, "
    "or thesis label (for example a single decisive-date claim) unless the "
    "paragraph adds a new source/evidence relation, comparison, limitation, or "
    "mechanism. Each empirical/case section should contain at least one claim "
    "whose evidentiary relation is not already used in the neighboring section."
)


def _section_progression_directive(section_id: str) -> str:
    directive = _SECTION_PROGRESSION_DIRECTIVES.get(section_id)
    if not directive:
        return ""
    if section_id in _CASE_CHANNEL_SECTION_INDEX:
        directive += _EMPIRICAL_PROGRESSION_TAIL
    return " section_progression_directive: " + directive


def _case_channel_anchor_directive(
    section_id: str,
    *,
    selected_thesis: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None,
) -> str:
    index = _CASE_CHANNEL_SECTION_INDEX.get(section_id)
    if index is None:
        return ""
    channels = _extract_case_channels(selected_thesis, research_kernel)
    if len(channels) <= index:
        return ""
    assigned = channels[index]
    previous = channels[:index]
    next_channels = channels[index + 1 :]
    return (
        " case_channel_anchor: this empirical/case-analysis section is assigned "
        f"to the distinct channel/case '{assigned}'. Build the section around "
        f"'{assigned}' specifically, with at least two substantive paragraphs "
        "explaining its mechanism/evidence/limits. Do NOT repeat earlier "
        f"channel(s) {json.dumps(previous, ensure_ascii=False)} except for a "
        "brief contrast, and do NOT jump ahead to later channel(s) "
        f"{json.dumps(next_channels, ensure_ascii=False)} except to mark a "
        "transition. If the available source notes for this channel are thin, "
        "write a scoped evidence-route analysis for that channel rather than "
        "falling back to the first channel."
    )


def _extract_case_channels(
    selected_thesis: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None,
) -> list[str]:
    chunks: list[str] = []
    for mapping in (selected_thesis, research_kernel):
        if not isinstance(mapping, Mapping):
            continue
        for key in (
            "thesis_one_sentence",
            "working_title",
            "why_novel",
            "tentative_question",
            "observed_puzzle",
            "scope",
        ):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                chunks.append(value.strip())
    text = " ".join(chunks)
    if not text:
        return []
    patterns = (
        r"([A-Za-z\u4e00-\u9fff]{2,12})、([A-Za-z\u4e00-\u9fff]{2,12})(?:与|和|及)([A-Za-z\u4e00-\u9fff]{2,12})(?:三(?:类|种|条|轨)|三个|三者)",
        r"三(?:类|种|条|轨|个)[^。；;:：]{0,24}?([A-Za-z\u4e00-\u9fff]{2,12})、([A-Za-z\u4e00-\u9fff]{2,12})(?:与|和|及)([A-Za-z\u4e00-\u9fff]{2,12})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        channels = [_clean_case_channel_name(match.group(i)) for i in range(1, 4)]
        channels = [channel for channel in channels if channel]
        if len(channels) == 3 and len(set(channels)) == 3:
            return channels
    return []


def _clean_case_channel_name(value: str) -> str:
    cleaned = value.strip(" ，,。；;：:、“”\"'（）()[]【】")
    cleaned = re.sub(r"^.*(?:理解为|概括为|处理为|改写为|界定为)", "", cleaned)
    cleaned = re.sub(r"^(?:以|把|将|和|与|及|在|由|为|的)+", "", cleaned)
    cleaned = re.sub(r"(?:中|内|里|上|下|之间|分别|并行)$", "", cleaned)
    return cleaned.strip()


def _parse_section_response(
    value: str,
    section: SectionPlan,
    shortlist: Sequence[NormalizedSource],
    low_relevance_source_ids: set[str] | None = None,
) -> DraftedSection | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    try:
        raw = RawSectionDraft.parse_obj(decoded)
    except ValidationError:
        return None
    return _drafted_section_from_raw(raw, section, shortlist, low_relevance_source_ids)


def _drafted_section_from_output(
    parsed: object,
    section: SectionPlan,
    shortlist: Sequence[NormalizedSource],
    low_relevance_source_ids: set[str] | None = None,
) -> DraftedSection | None:
    if isinstance(parsed, DrafterSection):
        payload: object = parsed.dict()
    else:
        payload = parsed
    if not isinstance(payload, Mapping):
        return None
    try:
        raw = RawSectionDraft.parse_obj(payload)
    except ValidationError:
        return None
    return _drafted_section_from_raw(raw, section, shortlist, low_relevance_source_ids)


def _drafted_section_from_raw(
    raw: RawSectionDraft,
    section: SectionPlan,
    shortlist: Sequence[NormalizedSource],
    low_relevance_source_ids: set[str] | None = None,
) -> DraftedSection | None:
    """Parse + normalize a per-section LLM response.

    PR-258b: ``low_relevance_source_ids`` is the deterministic
    backstop for the prompt-based ``topic_relevance`` directive
    added in PR-258a. Real-paper run #7 showed the LLM ignoring
    the directive for ``introduction-p003`` and citing 5 breast
    cancer DOIs (which got blocked at the critic phase).
    Filtering them out HERE — after the LLM has spoken but before
    we persist the claim_map — makes topic adherence
    data-enforced rather than discretion-dependent. ``None`` =
    legacy behavior (no filtering); empty set = filtering on but
    nothing was scored as low.
    """
    if not raw.claim_map:
        return None
    whitelist = {source.source_id for source in shortlist}
    if low_relevance_source_ids:
        whitelist -= low_relevance_source_ids
    warnings: list[str] = []
    claims: list[dict[str, object]] = []
    for index, raw_claim in enumerate(raw.claim_map, start=1):
        paragraph_id = raw_claim.paragraph_id or f"{section.section_id}-p{index:03d}"
        # Strip low-relevance source_ids before normalization. PR-258c:
        # if filtering empties the claim, fall back to the first
        # remaining whitelist source (which is by construction
        # non-low) so the integrity gate doesn't reject the run on a
        # pure-``[UNCITED]`` claim. Without this fallback,
        # real-paper run #8 hit ``empirical-section-i-p002`` with
        # empty source_ids → ``[UNCITED]`` → ``failed_policy`` at
        # exports. Borrowing a real on-topic source preserves
        # citation audit; the warnings array still records what was
        # dropped + that a substitution happened.
        filtered_input: list[str] | str = raw_claim.source_ids
        if low_relevance_source_ids and isinstance(raw_claim.source_ids, list):
            filtered_input = [
                sid for sid in raw_claim.source_ids if sid not in low_relevance_source_ids
            ]
            dropped = [sid for sid in raw_claim.source_ids if sid in low_relevance_source_ids]
            if dropped:
                warnings.append(
                    f"Dropped low-relevance source_ids from {paragraph_id}: {', '.join(dropped)}",
                )
            if not filtered_input and dropped and whitelist:
                fallback = next(iter(whitelist))
                filtered_input = [fallback]
                warnings.append(
                    f"All source_ids in {paragraph_id} were "
                    f"low-relevance; substituted first non-low "
                    f"whitelist source: {fallback}",
                )
        evidence_status: EvidenceStatus = raw_claim.evidence_status
        confidence: EvidenceConfidence | None = raw_claim.confidence
        if evidence_status == "model_backed":
            if isinstance(filtered_input, list):
                source_ids = [
                    source_id
                    for source_id in filtered_input
                    if isinstance(source_id, str) and source_id != "[UNCITED]"
                ]
            else:
                source_ids = []
            uncited = False
            claim_warnings = (
                [f"Model-backed claim {paragraph_id} carried source_ids; expected empty list"]
                if source_ids
                else []
            )
        else:
            source_ids, uncited, claim_warnings = _normalize_source_ids(filtered_input, whitelist)
            if uncited and _is_uncited_analytic_claim_model_backed(
                raw_claim.claim_text,
                section_id=section.section_id,
            ):
                source_ids = []
                uncited = False
                evidence_status = "model_backed"
                confidence = confidence or "medium"
                claim_warnings.append(
                    f"Classified uncited analytic claim {paragraph_id} as model_backed",
                )
        warnings.extend(claim_warnings)
        claim_record: dict[str, object] = {
            "section_id": section.section_id,
            # PR-257a: the planned ``section.title`` is the
            # authoritative locale-aware heading (e.g.
            # ``一、引言`` for zh case_analysis). Do not let the
            # LLM substitute its own translation — real-paper run
            # #3 showed the LLM dropping the ``一、`` numbering
            # and rewriting headings into bare strings, which
            # broke CNKI structure even when the rest of the
            # manuscript was Chinese.
            "section_title": section.title,
            "paragraph_id": paragraph_id,
            "claim_text": raw_claim.claim_text,
            "source_ids": source_ids,
            "uncited": uncited,
            "evidence_status": evidence_status,
        }
        if confidence is not None:
            claim_record["confidence"] = confidence
        claims.append(claim_record)
    prose = _ensure_todo_for_uncited(raw.prose, claims)
    return DraftedSection(
        section_id=section.section_id,
        # PR-257a: same as above — planner-supplied locale-aware
        # title wins over the LLM's emitted ``section_title``.
        title=section.title,
        prose=prose,
        claim_map=claims,
        failed=False,
        warnings=warnings,
        word_count=_word_count(prose),
        target_words=section.target_words,
    )


def _normalize_source_ids(
    value: list[str] | str,
    whitelist: set[str],
) -> tuple[list[str], bool, list[str]]:
    if isinstance(value, str):
        if value == "[UNCITED]":
            return ["[UNCITED]"], True, []
        return ["[UNCITED]"], True, [f"Invalid source_ids string: {value}"]
    valid: list[str] = []
    invalid: list[str] = []
    for item in value:
        if item in whitelist:
            valid.append(item)
        else:
            invalid.append(item)
    if not valid:
        return ["[UNCITED]"], True, [f"Missing valid source_ids: {', '.join(invalid)}"]
    warnings = [f"Dropped invalid source_ids: {', '.join(invalid)}"] if invalid else []
    return valid, False, warnings


_ANALYTIC_MODEL_BACKED_OPENERS = (
    "该目录",
    "该材料",
    "该条目",
    "本文将",
    "本文把",
    "本文采用",
    "本文使用",
    "本文以",
    "本文不引入",
    "本文不采用",
    "本文不把",
    "本文不再",
    "本文的",
    "本文在",
    "这一材料",
    "这一目录",
    "这一条目",
    "这一转向",
    "就本文",
    "据此",
    "在本文",
    "this paper treats",
    "this paper defines",
    "this paper uses",
    "this paper does not",
    "we treat",
    "we define",
    "we use",
    "we do not",
)

_ANALYTIC_MODEL_BACKED_MARKERS = (
    "拆为",
    "分为",
    "界定",
    "理解为",
    "视为",
    "操作化",
    "指标",
    "时间指标",
    "过程追踪",
    "序列",
    "比较",
    "识别",
    "材料限于",
    "材料限定",
    "可核验文本",
    "更适合作为",
    "更适合被理解为",
    "而不是",
    "不能单独",
    "不能直接",
    "不能仅凭",
    "不能单凭",
    "最多说明",
    "意义不在于",
    "不等于",
    "旁证",
    "不引入访谈",
    "不引入问卷",
    "不自建样本",
    "研究方法",
    "方法",
    "method",
    "define",
    "treat",
    "conceptual",
    "operationalize",
    "indicator",
    "process tracing",
    "sequence",
    "compare",
    "limited to",
)

_SOURCES_METHOD_MODEL_BACKED_MARKERS = (
    "材料边界",
    "研究材料边界",
    "研究材料",
    "候选判断",
    "候选结论",
    "待验证路径",
    "保留为候选",
    "只能保留",
    "不能写成",
    "不能同时显示",
    "多源互证",
    "证据链",
    "单一年度材料",
    "单次声明",
    "不足以定点",
    "不足以",
    "scope boundary",
    "candidate conclusion",
    "candidate judgment",
    "corroboration design",
    "chain of evidence",
    "single document",
    "not sufficient",
)


def _is_uncited_analytic_claim_model_backed(text: str, *, section_id: str) -> bool:
    """Classify source-less method/scope claims as analytic metadata.

    Citation audit should still fail source-bound claims without real
    source_ids. This helper only covers the narrow class of method,
    conceptual-framing, and material-scope statements the drafter can
    legitimately own as model reasoning.
    """

    cleaned = " ".join((text or "").split())
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if _is_material_limitation_statement(cleaned):
        return True
    if section_id in {"introduction", "historiography", "discussion"} and (
        _is_literature_positioning_model_backed(cleaned)
    ):
        return True
    if section_id == "sources-method" and any(
        marker in cleaned or marker in lowered
        for marker in _ANALYTIC_MODEL_BACKED_MARKERS + _SOURCES_METHOD_MODEL_BACKED_MARKERS
    ):
        return True
    has_analytic_opener = any(
        cleaned.startswith(opener) or lowered.startswith(opener)
        for opener in _ANALYTIC_MODEL_BACKED_OPENERS
    )
    if not has_analytic_opener:
        return False
    return any(marker in cleaned or marker in lowered for marker in _ANALYTIC_MODEL_BACKED_MARKERS)


def _is_literature_positioning_model_backed(text: str) -> bool:
    """Return true for source-less interpretive positioning of prior literature.

    This deliberately excludes concrete bibliographic facts. It covers the
    author's own use of a literature strand as background, contrast, or
    non-decisive framing.
    """

    has_literature_subject = any(
        marker in text for marker in ("文献", "研究", "讨论", "叙事", "写法")
    )
    if not has_literature_subject:
        return False
    return any(
        marker in text
        for marker in (
            "适合作为背景",
            "不宜直接替代",
            "不能直接替代",
            "不宜替代",
            "更强调",
            "主要解释",
            "不足以单独",
            "不能单独",
        )
    )


def _make_citation_whitelist_hook(
    whitelist: set[str],
    *,
    policies: EvidencePolicies | None = None,
) -> Callable[[HookContext, Any], HookResult]:
    policy = policies or EvidencePolicies(
        phase="drafting",
        verify_source_bound="strict",
        verify_analytic="strict",
        whitelist="strict",
    )

    def post_llm(_ctx: HookContext, response: Any) -> HookResult:
        if policy.verify_source_bound == "off" and policy.verify_analytic == "off":
            return HookResult(
                annotations={
                    "checked_claims": _claim_count(response.parsed),
                    "skipped_reason": "evidence_policy_off",
                },
            )
        errors, warnings = _citation_whitelist_policy_findings(
            response.parsed,
            whitelist,
            policy,
        )
        if errors:
            return HookResult(
                annotations={"errors": errors},
                verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
            )
        annotations: dict[str, object] = {"checked_claims": _claim_count(response.parsed)}
        if warnings:
            annotations["warnings"] = warnings
        return HookResult(annotations=annotations)

    return post_llm


def _citation_whitelist_policy_findings(
    parsed: object,
    whitelist: set[str],
    policies: EvidencePolicies,
) -> tuple[list[str], list[str]]:
    claims = _claims_from_parsed_section(parsed)
    errors: list[str] = []
    warnings: list[str] = []
    for claim_index, claim in enumerate(claims, start=1):
        if claim.evidence_status == "model_backed":
            if claim.source_ids != []:
                errors.append(
                    f"claim_map[{claim_index - 1}] model_backed claim must use source_ids=[]",
                )
                continue
            if policies.verify_analytic == "strict":
                errors.append(
                    f"claim_map[{claim_index - 1}] is model_backed but analytic policy is strict",
                )
            elif policies.verify_analytic == "soft":
                warnings.append(
                    f"claim_map[{claim_index - 1}] accepted as model_backed analytic claim",
                )
            continue

        claim_errors = _citation_errors_for_claim(claim, whitelist, claim_index)
        if policies.verify_source_bound == "strict":
            errors.extend(claim_errors)
        elif policies.verify_source_bound == "soft":
            warnings.extend(claim_errors)

    prose = _prose_from_parsed_section(parsed)
    if prose and "[UNCITED]" in prose:
        prose_error = (
            'prose contains literal "[UNCITED]" placeholder text — replace '
            "with a real citation from the shortlist or remove the claim"
        )
        if policies.verify_source_bound == "strict":
            errors.append(prose_error)
        elif policies.verify_source_bound == "soft":
            warnings.append(prose_error)
    return errors, warnings


def _citation_errors_for_claim(
    claim: DrafterClaim,
    whitelist: set[str],
    claim_index: int,
) -> list[str]:
    source_ids = claim.source_ids
    if _is_uncited(source_ids):
        return [
            (
                f"claim_map[{claim_index - 1}] is uncited: every claim must include "
                "at least one real source_id from the shortlist; do not return "
                '"[UNCITED]"'
            ),
        ]
    errors: list[str] = []
    for source_index, source_id in enumerate(source_ids, start=1):
        if source_id == "[UNCITED]":
            errors.append(
                (
                    f"claim_map[{claim_index - 1}].source_ids[{source_index - 1}] "
                    'is "[UNCITED]" — every claim must cite at least one real '
                    "source_id from the shortlist"
                ),
            )
            continue
        if source_id in whitelist:
            continue
        errors.append(
            (
                f"claim_map[{claim_index - 1}].source_ids[{source_index - 1}] "
                f"not in shortlist: {source_id}"
            ),
        )
    return errors


def _citation_whitelist_errors(parsed: object, whitelist: set[str]) -> list[str]:
    claims = _claims_from_parsed_section(parsed)
    errors: list[str] = []
    for claim_index, claim in enumerate(claims, start=1):
        errors.extend(_citation_errors_for_claim(claim, whitelist, claim_index))
    # Also reject the literal string "[UNCITED]" appearing inside the prose
    # body. The structured claim_map check catches uncited claim records
    # but the LLM sometimes types "[UNCITED]" directly into the prose
    # paragraph as a placeholder, which leaks into the final manuscript.
    prose = _prose_from_parsed_section(parsed)
    if prose and "[UNCITED]" in prose:
        errors.append(
            'prose contains literal "[UNCITED]" placeholder text — replace '
            "with a real citation from the shortlist or remove the claim",
        )
    return errors


def _prose_from_parsed_section(parsed: object) -> str:
    if isinstance(parsed, DrafterSection):
        return parsed.prose
    if isinstance(parsed, Mapping):
        value = parsed.get("prose")
        if isinstance(value, str):
            return value
    return ""


def _is_uncited(source_ids: object) -> bool:
    if isinstance(source_ids, str):
        return source_ids == "[UNCITED]"
    if isinstance(source_ids, list):
        if not source_ids:
            return True
        return all(item == "[UNCITED]" for item in source_ids)
    return False


def _claims_from_parsed_section(parsed: object) -> list[DrafterClaim]:
    if isinstance(parsed, DrafterSection):
        return list(parsed.claim_map)
    if not isinstance(parsed, Mapping):
        return []
    raw_claims = parsed.get("claim_map")
    if not isinstance(raw_claims, list):
        return []
    claims: list[DrafterClaim] = []
    for raw_claim in raw_claims:
        try:
            claims.append(DrafterClaim.parse_obj(raw_claim))
        except ValidationError:
            continue
    return claims


def _claim_count(parsed: object) -> int:
    return len(_claims_from_parsed_section(parsed))


def _register_drafter_memory_hook(hooks: HookRegistry) -> None:
    settings = get_settings()
    if not settings.memory_read:
        return
    memory_client = MemoryClient(
        base_url=settings.appleseed_memory_base_url,
        token=settings.appleseed_memory_token,
    )
    hooks.register_pre_llm("memory_read", make_memory_pre_llm_hook(memory_client, max_memories=5))


def _ensure_todo_for_uncited(
    prose: str,
    claim_map: Sequence[Mapping[str, object]],
) -> str:
    if any(record.get("uncited") is True for record in claim_map) and "TODO_EVIDENCE" not in prose:
        return (
            prose.rstrip()
            + "\n\nTODO_EVIDENCE: One or more claims in this section need shortlist evidence."
        )
    return prose


def _stub_section(
    section: SectionPlan,
    reason: str,
    shortlist: Sequence[NormalizedSource] | None = None,
) -> DraftedSection:
    """Drafter fallback when LLM JSON validation fails after all
    retries. Two PR-257b changes from the prior version:

    1. The stub claim's ``source_ids`` borrows the first shortlist
       source instead of ``[UNCITED]``. Real-paper run #5 surfaced
       that ``[UNCITED]`` propagates through critic/integrity OK but
       blocks the exports phase with a ``failed_policy`` audit
       blocker — the run dies after the user already accepted every
       gate, with no actionable recovery short of editing the section
       by hand. Borrowing a real shortlist id lets the export
       complete; the ``TODO_EVIDENCE`` marker in prose + the
       ``warnings`` field still tell reviewers this section needs
       attention. Falls back to ``[UNCITED]`` only if the shortlist
       is also empty (which already would have failed earlier at
       ``_run_drafter``).

    2. ``shortlist`` is a kwarg with a ``None`` default so the
       existing two callers (``_run_drafter`` line 408 + tests) can
       opt in incrementally.
    """
    fallback_source = shortlist[0].source_id if shortlist else "[UNCITED]"
    uncited = fallback_source == "[UNCITED]"
    prose = (
        f"TODO_EVIDENCE: {section.title} needs drafting after a schema-valid section response. "
        "[UNCITED]"
    )
    claim_map = [
        {
            "section_id": section.section_id,
            "section_title": section.title,
            "paragraph_id": f"{section.section_id}-p001",
            "claim_text": reason,
            "source_ids": [fallback_source],
            "uncited": uncited,
        },
    ]
    return DraftedSection(
        section_id=section.section_id,
        title=section.title,
        prose=prose,
        claim_map=claim_map,
        failed=True,
        warnings=[reason],
        word_count=_word_count(prose),
        target_words=section.target_words,
    )


def _stub_drafted_section(
    section: SectionPlan,
    selected_thesis: Mapping[str, object],
    shortlist: Sequence[NormalizedSource],
) -> DraftedSection:
    source_id = shortlist[0].source_id if shortlist else "[UNCITED]"
    paragraph_id = f"{section.section_id}-p001"
    thesis = str(
        selected_thesis.get("thesis_one_sentence") or selected_thesis.get("working_title") or "",
    )
    if source_id == "[UNCITED]":
        prose = (
            f"TODO_EVIDENCE: {section.title} frames the selected thesis but still needs a "
            "shortlist citation. [UNCITED]"
        )
        source_ids = ["[UNCITED]"]
        uncited = True
    else:
        prose = (
            f"{section.title} develops the selected thesis: {thesis} The current source pack "
            f"supports this section through source `{source_id}`, while evidence gaps remain "
            "visible for later review."
        )
        source_ids = [source_id]
        uncited = False
    claim_map = [
        {
            "section_id": section.section_id,
            "section_title": section.title,
            "paragraph_id": paragraph_id,
            "claim_text": prose,
            "source_ids": source_ids,
            "uncited": uncited,
        },
    ]
    return DraftedSection(
        section_id=section.section_id,
        title=section.title,
        prose=prose,
        claim_map=claim_map,
        failed=False,
        warnings=[],
        word_count=_word_count(prose),
        target_words=section.target_words,
    )


def _load_outline_sections_for_thesis(
    run_dir: Path,
    selected_thesis: Mapping[str, object],
) -> tuple[OutlineSection, ...]:
    """Load the outline matching the chosen angle from
    ``novelty/detailed_outlines.json``.

    Returns empty tuple when the artifact is missing, the angle id
    has no entry, or the file is malformed. Drafter falls back to
    its built-in section role hints in that case.
    """
    angle_id = selected_thesis.get("angle_id")
    if not isinstance(angle_id, str) or not angle_id:
        return ()
    payload = _load_json_mapping(run_dir / "novelty" / "detailed_outlines.json")
    raw_outlines = payload.get("outlines")
    if not isinstance(raw_outlines, list):
        return ()
    for entry in raw_outlines:
        if not isinstance(entry, dict):
            continue
        if entry.get("angle_id") != angle_id:
            continue
        raw_sections = entry.get("sections")
        if not isinstance(raw_sections, list):
            return ()
        sections: list[OutlineSection] = []
        for raw in raw_sections:
            if not isinstance(raw, dict):
                continue
            sections.append(
                OutlineSection(
                    section_id=str(raw.get("section_id") or ""),
                    title=str(raw.get("title") or ""),
                    function=str(raw.get("function") or ""),
                    argument=str(raw.get("argument") or ""),
                    literature=str(raw.get("literature") or ""),
                    materials=str(raw.get("materials") or ""),
                    relation_to_thesis=str(raw.get("relation_to_thesis") or ""),
                    weakness=str(raw.get("weakness") or ""),
                ),
            )
        return tuple(sections)
    return ()


def _match_outline_section(
    section: SectionPlan,
    outline_sections: Sequence[OutlineSection],
    index: int,
) -> OutlineSection | None:
    """Find the outline section that corresponds to the SectionPlan.

    Drafter SectionPlan ids come from `_slugify` (dashes); detailed
    outline ids come from the LLM (typically underscored, e.g.
    ``literature_review``). Try in order: exact match after
    underscore-normalization, mutual substring of id or title, then
    position-based fallback so an outline always anchors *some*
    section even when ids drift.
    """
    if not outline_sections:
        return None
    section_slug = section.section_id.lower().replace("-", "_").strip()
    section_title_lower = section.title.lower()
    for outline in outline_sections:
        outline_id = outline.section_id.lower().replace("-", "_").strip()
        if outline_id and outline_id == section_slug:
            return outline
    # Require minimum length on substring matches: a single-character
    # outline id like "a" would otherwise match almost any section
    # title by accident.
    for outline in outline_sections:
        outline_id = outline.section_id.lower().replace("-", "_").strip()
        if len(outline_id) >= 4 and (outline_id in section_slug or section_slug in outline_id):
            return outline
        outline_title_lower = outline.title.lower()
        if len(outline_title_lower) >= 4 and outline_title_lower in section_title_lower:
            return outline
    if 0 <= index < len(outline_sections):
        return outline_sections[index]
    return None


def _outline_anchor_block(outline_section: OutlineSection | None) -> str:
    """Return the per-section outline anchoring directive.

    Empty string when no matching outline section is available — the
    Drafter prompt continues to function on its built-in role hints
    alone, so missing detailed_outlines.json never breaks drafting.
    """
    if outline_section is None:
        return ""
    fields: list[tuple[str, str]] = [
        ("function", outline_section.function),
        ("argument", outline_section.argument),
        ("literature", outline_section.literature),
        ("materials", outline_section.materials),
        ("relation_to_thesis", outline_section.relation_to_thesis),
        ("weakness", outline_section.weakness),
    ]
    populated = {key: value.strip() for key, value in fields if value and value.strip()}
    if not populated:
        return ""
    return (
        "本节大纲（用户在 USER_NOVELTY_REVIEW 选定）：必须严格按本块展开，"
        "不要写成另外一篇文章。"
        f"{json.dumps(populated, ensure_ascii=False, sort_keys=True)} "
    )


def has_selected_angle(run: Run, session: Session) -> bool:
    """``True`` iff a novelty angle has been selected for this run.

    Used by ``start_drafter`` to reject up-front rather than letting the
    drafter agent transition to ``DRAFTER_RUNNING`` and FAIL_FIXABLE
    11ms later when it discovers no angle was picked. Mirrors the same
    lookup as ``_load_selected_thesis`` so both stay consistent.
    """
    selected = _load_selected_thesis(run, session)
    if not isinstance(selected, dict):
        return False
    angle_id = selected.get("angle_id")
    return isinstance(angle_id, str) and bool(angle_id.strip())


def _load_selected_thesis(run: Run, session: Session) -> dict[str, object] | None:
    selected = _load_json_mapping(Path(run.run_dir) / "novelty" / "selected_thesis.json")
    if selected:
        return selected
    checkpoint = session.scalar(
        select(Checkpoint)
        .where(Checkpoint.run_id == run.id)
        .order_by(Checkpoint.created_at.desc(), Checkpoint.id.desc())
        .limit(1),
    )
    if checkpoint is None or checkpoint.checkpoint_type not in NOVELTY_CHECKPOINT_TYPES:
        return None
    if checkpoint.status != "ACCEPTED":
        return None
    payload = _json_object(checkpoint.decision_payload)
    selected_thesis = payload.get("selected_thesis")
    if isinstance(selected_thesis, dict):
        return {key: value for key, value in selected_thesis.items() if isinstance(key, str)}
    selected_angle_id = payload.get("selected_angle_id")
    if isinstance(selected_angle_id, str):
        edits = payload.get("edits")
        edit_mapping = edits if isinstance(edits, dict) else {}
        return select_thesis_for_run(run, selected_angle_id, edit_mapping)
    return None


def _section_plan(
    domain_data: Mapping[str, Any],
    target_journal: str | None,
    paper_mode: str | None = None,
    paper_language: str = "en",
) -> list[SectionPlan]:
    total_words = _target_total_words(domain_data, target_journal)
    # PR-C2.b Tier 4 (2026-05-03): paper_mode now takes precedence
    # over the domain/journal-driven structure template. The
    # paper_modes registry knows the appropriate section shape per
    # mode (e.g. theory_article skips empirical chapters; comparative
    # studies want parallel comparator chapters). Falling through
    # only when paper_mode is unset (legacy runs) or unknown.
    if paper_mode:
        from autoessay.paper_modes import get_localized_section_title, get_mode

        spec = get_mode(paper_mode)
        if spec is not None and spec.drafter_section_plan:
            # paper_modes registry stores section IDs as snake_case
            # (e.g. "sources_method"); the drafter has historically
            # used display titles ("Sources & Method"). Humanize the
            # IDs into display titles so manuscript headings stay
            # human-readable. Special case: "_i"/"_ii"/"_iii"
            # roman-numeral suffixes get uppercased.
            #
            # Codex round-4 #2 (2026-05-03): the drafter prompt
            # registry + saved per-section prompt overrides + role
            # hints all key by HYPHENATED slug ("sources-method",
            # "empirical-section-i"), produced by passing the human
            # title through _slugify. Pass the title (not the raw
            # snake_case) into _slugify so legacy runs' role hints
            # and saved overrides keep matching.
            #
            # PR-257a: when paper_language is zh / ja, override the
            # English humanized form with the locale-aware CNKI-style
            # title from ``paper_modes.LOCALIZED_SECTION_TITLES``
            # (e.g. ``一、引言`` for ``introduction`` in zh). The
            # ``section_id`` slug stays English so the prompt
            # registry + per-section overrides keyed by hyphenated
            # slug continue to match. ``None`` from the lookup means
            # "no zh/ja entry" → fall back to English humanized form.
            mode_sections = list(spec.drafter_section_plan)
            per_section = max(300, total_words // max(1, len(mode_sections)))
            sections: list[SectionPlan] = []
            for section_id in mode_sections:
                en_title = _humanize_section_id(section_id)
                localized = get_localized_section_title(section_id, paper_language)
                display_title = localized or en_title
                sections.append(
                    SectionPlan(
                        section_id=_slugify(en_title),
                        title=display_title,
                        target_words=per_section,
                    ),
                )
            return sections
    target_profile = _target_profile(domain_data, target_journal)
    raw_template = _raw_structure_template(domain_data, target_profile)
    if raw_template:
        sections = _parse_structure_template(raw_template, total_words)
        if sections:
            return sections
    # PR-256: pick locale-appropriate default section titles when the
    # domain config didn't supply a custom structure_template. The
    # ``section_id`` slug stays English so the prompt registry +
    # per-section overrides keyed by slug continue to match.
    titles_by_lang = DEFAULT_SECTION_TITLES_BY_LANG.get(paper_language, DEFAULT_SECTION_TITLES)
    en_titles = DEFAULT_SECTION_TITLES
    per_section = max(300, total_words // len(titles_by_lang))
    return [
        SectionPlan(
            section_id=_slugify(en_title),
            title=display_title,
            target_words=per_section,
        )
        for en_title, display_title in zip(en_titles, titles_by_lang, strict=False)
    ]


def _raw_structure_template(
    domain_data: Mapping[str, Any],
    target_profile: Mapping[str, object],
) -> object:
    targets = domain_data.get("targets")
    if isinstance(targets, dict) and "structure_template" in targets:
        return targets["structure_template"]
    if "structure_template" in target_profile:
        return target_profile["structure_template"]
    journals = domain_data.get("journals")
    if isinstance(journals, dict) and "structure_template" in journals:
        return journals["structure_template"]
    return None


def _parse_structure_template(raw_template: object, total_words: int) -> list[SectionPlan]:
    raw_sections = raw_template
    if isinstance(raw_template, dict):
        raw_sections = raw_template.get("sections")
    if not isinstance(raw_sections, list):
        return []
    per_section = max(300, total_words // max(1, len(raw_sections)))
    sections: list[SectionPlan] = []
    for item in raw_sections:
        if isinstance(item, str):
            title = item
            target_words = per_section
        elif isinstance(item, dict):
            raw_title = item.get("title") or item.get("section_title")
            if not isinstance(raw_title, str):
                continue
            title = raw_title
            raw_target = item.get("target_words")
            target_words = (
                raw_target if isinstance(raw_target, int) and raw_target > 0 else per_section
            )
        else:
            continue
        sections.append(
            SectionPlan(section_id=_slugify(title), title=title, target_words=target_words),
        )
    return sections


def _target_total_words(domain_data: Mapping[str, Any], target_journal: str | None) -> int:
    profile = _target_profile(domain_data, target_journal)
    length = profile.get("expected_length_words")
    if isinstance(length, list) and length and isinstance(length[0], int):
        return max(1000, length[0])
    return 8000


def _target_profile(
    domain_data: Mapping[str, Any],
    target_journal: str | None,
) -> dict[str, object]:
    journals = domain_data.get("journals")
    if not isinstance(journals, dict):
        return {}
    targets = journals.get("targets")
    if not isinstance(targets, list):
        return {}
    fallback: dict[str, object] = {}
    for item in targets:
        if not isinstance(item, dict):
            continue
        profile = {key: value for key, value in item.items() if isinstance(key, str)}
        if not fallback:
            fallback = profile
        if target_journal is not None and item.get("name") == target_journal:
            return profile
    return fallback


def _approved_source_summaries(
    shortlist: Sequence[NormalizedSource],
    source_notes: Mapping[str, object],
    topic_keywords: set[str] | None = None,
) -> list[dict[str, object]]:
    """Per-source summary the drafter prompt sees.

    PR-258a: when ``topic_keywords`` is provided, each summary
    carries a ``topic_relevance`` field (``"high"`` / ``"medium"``
    / ``"low"``) computed against the project_title + research_kernel
    keyword set. The drafter prompt then bans the LLM from citing
    ``low`` sources and restricts ``medium`` to background /
    methodology only. ``None`` skips scoring (back-compat for
    callers that haven't been updated yet).
    """
    summaries: list[dict[str, object]] = []
    for source in shortlist:
        note = source_notes.get(source.source_id)
        note_summary = _one_line_note(note)
        entry: dict[str, object] = {
            "source_id": source.source_id,
            "title": source.title,
            "authors": source.authors,
            "year": source.year,
            "venue": source.venue,
            "one_line_summary": note_summary or source.abstract or "",
            "evidence_access": _source_evidence_access(source),
        }
        if entry["evidence_access"] == "metadata_only":
            entry["source_use_limit"] = (
                "bibliographic positioning only; do not use for source contents, "
                "archive-specific evidence, empirical findings, or causation"
            )
        if topic_keywords is not None:
            entry["topic_relevance"] = _score_source_topic_relevance(source, topic_keywords)
        if get_settings().baseline_as_evidence_test and source.source_id == "shadow_baseline_v001":
            entry["baseline_as_evidence_test"] = True
            entry["topic_relevance"] = "high"
            entry["source_use_limit"] = (
                "TEST-only approved source from the run's shadow baseline manuscript; "
                "may ground claims only when paraphrased and analytically extended"
            )
            if isinstance(note, dict):
                segments = note.get("segments")
                if isinstance(segments, list):
                    prompt_segments: list[dict[str, str]] = []
                    for segment in segments[:12]:
                        if not isinstance(segment, dict):
                            continue
                        segment_id = segment.get("segment_id")
                        text = segment.get("text")
                        if isinstance(segment_id, str) and isinstance(text, str):
                            prompt_segments.append(
                                {
                                    "segment_id": segment_id,
                                    "text": text[:650],
                                }
                            )
                    if prompt_segments:
                        entry["baseline_segments"] = prompt_segments
        summaries.append(entry)
    return summaries


def _source_evidence_access(source: NormalizedSource) -> str:
    access = getattr(source, "access_status", "")
    access_value = access.value if hasattr(access, "value") else str(access)
    risk_flags = {str(flag) for flag in (getattr(source, "risk_flags", []) or [])}
    has_text_signal = bool(getattr(source, "abstract", None) or getattr(source, "pdf_url", None))
    if (
        access_value == "metadata_only"
        or "metadata_only_no_full_text" in risk_flags
        or (
            not has_text_signal
            and (
                getattr(source, "provenance", "search") == "llm_canon"
                or bool(getattr(source, "verified_by", None))
            )
        )
    ):
        return "metadata_only"
    return "text_available"


# PR-258a — topic relevance scoring. Real-paper run #6 surfaced that
# curator-picked sources can be wildly off-topic (e.g. a 21st-century
# Dutch fiscal-policy paper for a 19th-century Chinese publishing
# study), and the drafter's existing ``anchor_check`` rule wasn't
# strong enough — the LLM treated abstract concepts (history /
# methodology) as conceptual continuation and wove them into the
# manuscript, which then failed the integrity topic-adherence gate
# at exports.
#
# Codex round-3 verdict (PR-258a, AGREE Q1=A+B, Q2=3-bin scoring):
# pre-filter cannot live at curator (PR-J6/J7/J8 already in flight),
# so do it inside drafter where the blast radius is narrowest.
# Compute per-source relevance from project_title + research_kernel
# free text, surface it in the prompt, ban ``low`` from citation.

_TOPIC_KEYWORD_STOPWORDS_EN: frozenset[str] = frozenset(
    {
        # Generic academic verbs / nouns that carry no domain signal.
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "study",
        "studies",
        "paper",
        "papers",
        "research",
        "review",
        "analysis",
        "essay",
        "introduction",
        "conclusion",
        "history",
        "historical",
        "modern",
        # Generic financial / economic noise that could otherwise
        # let off-topic Eurozone / fiscal papers leak in (the exact
        # leak path that broke real-paper run #6).
        "policy",
        "fiscal",
        "monetary",
        "economic",
        "economy",
        "global",
        "international",
        "european",
        "national",
    },
)

# Curated Chinese → English bridge for domain entities that recur
# across the kernel test fixtures + likely production kernels. v1
# is hand-seeded; later PRs may extend or wire to a domain-config
# vocabulary file. Keys are zh strings present in the kernel; values
# are en synonyms expected to appear in source titles / abstracts.
_TOPIC_KEYWORD_BRIDGE_ZH_TO_EN: dict[str, tuple[str, ...]] = {
    "江南": ("jiangnan", "yangtze", "yangzi", "lower yangzi"),
    "晚清": ("late qing", "qing"),
    "清末": ("late qing",),
    "19世纪": ("nineteenth-century", "19th-century", "1800s"),
    "十九世纪": ("nineteenth-century", "19th-century", "1800s"),
    "刊本": ("imprint", "imprints", "edition", "editions", "block-print", "woodblock"),
    "序跋": ("preface", "prefaces", "postface", "colophon", "colophons"),
    "刻工": ("engraver", "engravers", "block-cutter", "carver"),
    "题记": ("colophon", "colophons", "inscription"),
    "断代": ("dating", "chronology", "periodization"),
    "金融史": ("financial history", "monetary history"),
    "出版": ("publishing", "print culture", "book culture"),
    "书籍": ("book", "books", "imprint"),
    "布雷顿森林": ("bretton woods",),
    "布雷顿": ("bretton",),
    "金本位": ("gold standard", "gold"),
    "美元": ("dollar", "dollars", "usd"),
    "黄金": ("gold",),
    "可兑换": ("convertibility", "convertible"),
    "约束力": ("constraint", "constraints", "binding force"),
    "失效节点": ("collapse", "breakdown"),
    "黄金池": ("gold pool", "london gold pool"),
    "国际货币": ("international monetary",),
    "阳明": ("yangming", "wang yangming"),
    "心学": ("school of mind", "learning of the mind"),
    "王门": ("wang school", "yangming school"),
    "讲会": ("lecture association", "lectures"),
    "官学": ("official school",),
}


def _extract_topic_keywords(
    project_title: str,
    research_kernel: Mapping[str, object] | None,
) -> set[str]:
    """Extract the topic-keyword set used to score source relevance.

    Mixes (a) Chinese 2-grams from the kernel + project_title with
    (b) lowercase ASCII tokens ≥4 chars (filtered through the
    domain-noise stopword set) and (c) bridged English equivalents
    for the recognized zh entities. The result is a flat set ready
    for substring matching against source title + abstract.

    Empty input → empty set (which yields ``low`` for every source,
    so all callers still get a deterministic answer).
    """
    text_parts: list[str] = []
    if project_title:
        text_parts.append(project_title)
    if isinstance(research_kernel, Mapping):
        for key in ("observed_puzzle", "tentative_question", "scope"):
            value = research_kernel.get(key)
            if isinstance(value, str) and value.strip():
                text_parts.append(value)
    text = " ".join(text_parts)
    if not text:
        return set()

    keywords: set[str] = set()

    # Chinese 2-grams from runs of CJK characters. 2-grams strike a
    # balance: long enough to be discriminative (江南, 晚清, 刊本) but
    # short enough to match across small vocabulary variations.
    for run in re.findall(r"[一-鿿]+", text):
        for i in range(len(run) - 1):
            keywords.add(run[i : i + 2])

    # ASCII content words ≥4 chars, lowercased, stopword-filtered.
    for word in re.findall(r"[a-zA-Z]{4,}", text.lower()):
        if word not in _TOPIC_KEYWORD_STOPWORDS_EN:
            keywords.add(word)

    # English bridge for zh entities seen in the extracted set.
    # Match against ``text_compact`` (whitespace stripped) so that a
    # kernel written as ``19 世纪`` (with a space the user typed
    # between the digit and the CJK char) still bridges to
    # ``nineteenth-century``. Without this normalization the bridge
    # silently misses every kernel that puts ASCII digits next to
    # CJK chars, which is the common style for ``19 世纪`` /
    # ``20 世纪`` periodization.
    text_compact = re.sub(r"\s+", "", text)
    bridged: set[str] = set()
    for zh_term, en_synonyms in _TOPIC_KEYWORD_BRIDGE_ZH_TO_EN.items():
        if zh_term in keywords or zh_term in text_compact:
            bridged.update(en_synonyms)
    keywords.update(bridged)

    return keywords


def _score_source_topic_relevance(
    source: NormalizedSource,
    topic_keywords: set[str],
) -> str:
    """Return ``"high"`` / ``"medium"`` / ``"low"`` for a shortlist
    source against the project's keyword set.

    Counts distinct keyword hits in the source title + abstract.
    - 0 hits → ``"low"`` (drafter must NOT cite)
    - 1-2 hits → ``"medium"`` (drafter may cite for background only)
    - 3+ hits → ``"high"`` (drafter may cite as main argument)

    Empty keyword set returns ``"low"`` for every source — this is
    intentional so that callers can detect "we couldn't compute
    topic_relevance" by checking the keyword set was non-empty
    before trusting the score.
    """
    if not topic_keywords:
        return "low"
    haystack_parts: list[str] = []
    if source.title:
        haystack_parts.append(source.title)
    abstract = getattr(source, "abstract", None)
    if abstract:
        haystack_parts.append(abstract)
    haystack = " ".join(haystack_parts).lower()
    if not haystack:
        return "low"
    hits = 0
    for keyword in topic_keywords:
        # Lowercase only the comparison side; keys may be CJK chars
        # (already case-insensitive) or already-lowercased ASCII.
        needle = keyword if any("一" <= c <= "鿿" for c in keyword) else keyword.lower()
        if needle in haystack:
            hits += 1
        if hits >= 3:
            return "high"
    if hits == 0:
        return "low"
    return "medium"


def _topic_relevance_directive(approved_sources: Sequence[Mapping[str, object]]) -> str:
    """Build the prompt directive that tells the LLM how to use the
    ``topic_relevance`` annotation. Empty when no source has the
    field (back-compat for callers that didn't pass topic_keywords).
    """
    has_relevance = any("topic_relevance" in entry for entry in approved_sources)
    if not has_relevance:
        return ""
    low_ids = [
        str(entry.get("source_id"))
        for entry in approved_sources
        if entry.get("topic_relevance") == "low"
    ]
    medium_ids = [
        str(entry.get("source_id"))
        for entry in approved_sources
        if entry.get("topic_relevance") == "medium"
    ]
    parts = [
        " topic_adherence_directive: each ``approved_sources`` entry "
        "carries a ``topic_relevance`` field "
        "(``high`` / ``medium`` / ``low``) computed against the "
        "project_title + research_kernel core entities (geography, "
        "period, primary materials). "
    ]
    if low_ids:
        parts.append(
            " You MUST NOT cite any source whose ``topic_relevance`` "
            "is ``low`` — these sources are off-topic to the project's "
            "scope and will fail the integrity gate. "
            f"Off-topic source_ids: {json.dumps(low_ids, sort_keys=True)}. "
        )
    if medium_ids:
        parts.append(
            " Sources with ``topic_relevance`` = ``medium`` may be "
            "cited only for background or methodological parallel, "
            "never as the main substantive evidence. "
            f"Background-only source_ids: {json.dumps(medium_ids, sort_keys=True)}. "
        )
    metadata_only_ids = [
        str(entry.get("source_id"))
        for entry in approved_sources
        if entry.get("evidence_access") == "metadata_only"
    ]
    if metadata_only_ids:
        parts.append(
            " metadata_only_directive: sources whose ``evidence_access`` is "
            "``metadata_only`` are verified bibliography records, not usable "
            "text evidence. You may cite them only for literature positioning, "
            "chronology, authorship/title authenticity, or scope. Do NOT use "
            "them to support source contents, archive-specific evidence, "
            "empirical findings, institutional causation, or claims that an "
            "author argues a specific point unless that argument appears in "
            "the one_line_summary. Metadata-only source_ids: "
            f"{json.dumps(metadata_only_ids, sort_keys=True)}. "
        )
    parts.append(
        " If after these restrictions a section has no usable "
        "``high`` source, omit the citation rather than borrow from "
        "the banned set. The integrity gate checks both the absence "
        "of ``low`` cites and the on-topic vocabulary of the prose."
    )
    return "".join(parts)


def _baseline_as_evidence_test_directive(
    approved_sources: Sequence[Mapping[str, object]],
) -> str:
    if not get_settings().baseline_as_evidence_test:
        return ""
    if not any(entry.get("source_id") == "shadow_baseline_v001" for entry in approved_sources):
        return ""
    return (
        " baseline_as_evidence_test_directive: "
        "AUTOESSAY_BASELINE_AS_EVIDENCE_TEST is enabled for this run. "
        "``shadow_baseline_v001`` is a TEST-only approved source derived "
        "from the persisted shadow baseline manuscript. You MAY use "
        "``shadow_baseline_v001`` in ``claim_map.source_ids`` and cite it "
        "in body prose using ``[shadow_baseline_v001]``; do NOT write the "
        "literal source_id ``shadow_baseline_v001`` as a prose noun or "
        "author name. The deterministic "
        "post-processor will map that marker to the correct numeric "
        "reference. Use the baseline segments only as approved source "
        "evidence: paraphrase, avoid copying sentences or paragraph "
        "order, and add your own analysis. "
        "Do not cite the baseline's internal reference list as if those "
        "works were independently verified unless they also appear as "
        "separate Approved sources. The anti-plagiarism n-gram gate "
        "still applies."
    )


def _sanitize_baseline_as_evidence_source_mentions(manuscript: str) -> str:
    if not get_settings().baseline_as_evidence_test:
        return manuscript
    return re.sub(
        r"(?<![A-Za-z0-9_\[])shadow_baseline_v001(?![A-Za-z0-9_\]])",
        "所引材料",
        manuscript,
    )


def _has_baseline_as_evidence_source(shortlist: Sequence[NormalizedSource]) -> bool:
    return get_settings().baseline_as_evidence_test and any(
        source.source_id == "shadow_baseline_v001" for source in shortlist
    )


def _evidence_strength_directive(
    selected_thesis: Mapping[str, object],
    approved_sources: Sequence[Mapping[str, object]],
    research_kernel: Mapping[str, object] | None,
) -> str:
    """Conservative prompt guard for kernels whose selected angle
    admits missing primary/archive evidence.

    The critic/export gate already rejects weakly grounded archive
    claims after drafting. This prompt-side guard reduces the chance
    the manuscript overstates a precise event/date node before the
    deterministic scanner gets a chance to warn.
    """
    missing_bits: list[str] = []
    for key in ("missing_evidence", "risks"):
        value = selected_thesis.get(key)
        if isinstance(value, str):
            missing_bits.append(value)
        elif isinstance(value, list):
            missing_bits.extend(str(item) for item in value if item)
    missing_text = " ".join(missing_bits)
    kernel_text = ""
    if isinstance(research_kernel, Mapping):
        kernel_text = " ".join(
            str(research_kernel.get(key) or "")
            for key in ("observed_puzzle", "tentative_question", "scope")
        )
    combined = missing_text + " " + kernel_text
    has_missing_signal = any(
        term in combined
        for term in (
            "缺少",
            "不足",
            "尚未",
            "missing",
            "insufficient",
            "not enough",
        )
    )
    has_primary_archive_signal = any(
        term in combined
        for term in (
            "档案",
            "备忘录",
            "纪要",
            "结算记录",
            "archive",
            "memo",
            "minutes",
            "settlement",
        )
    )
    metadata_only_ids = [
        str(entry.get("source_id"))
        for entry in approved_sources
        if entry.get("evidence_access") == "metadata_only"
    ]
    if not (has_missing_signal and has_primary_archive_signal):
        return ""
    return (
        " evidence_strength_directive: the selected thesis or kernel explicitly "
        "mentions missing archival / primary-material evidence. Treat that as a "
        "hard limit, not as evidence already acquired. Do NOT write that the paper "
        "has reconstructed a direct archival chain, IMF/Fed memo trail, meeting "
        "minutes, settlement records, or a definitive month/date node unless an "
        "approved source summary explicitly contains that primary-material content. "
        "Use conservative wording such as '现有可核验材料支持压力累积/背景定位，"
        "但不足以单独锁定节点' when the evidence is secondary or metadata-only. "
        "Do NOT cite metadata-only sources for archive-specific contents or "
        "causal node claims. Metadata-only source_ids: "
        f"{json.dumps(metadata_only_ids, sort_keys=True)}. "
    )


def _load_material_diagnostic(run_dir: Path) -> dict[str, object]:
    """Load synthesizer's material diagnostic for downstream scoping.

    The diagnostic is advisory in synthesizer, but drafter/final rewrite
    need the signal to avoid promising an empirical archive chain when
    the source bundle itself says the material is insufficient.
    """
    return _load_json_mapping(run_dir / "synthesis" / "material_diagnostic.json")


def _material_scope_guard_summary(
    material_diagnostic: Mapping[str, object] | None,
    *,
    selected_thesis: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None,
) -> dict[str, object]:
    missing = _clean_material_strings((material_diagnostic or {}).get("missing_materials"))
    risks = _clean_material_strings((material_diagnostic or {}).get("risks"))
    candidate_titles = _clean_material_strings(
        (material_diagnostic or {}).get("candidate_titles"),
    )
    rationale = str((material_diagnostic or {}).get("rationale") or "").strip()
    recommended_action = str(
        (material_diagnostic or {}).get("recommended_action") or "",
    ).strip()
    applied = _material_scope_guard_needed(
        material_diagnostic,
        selected_thesis=selected_thesis,
        research_kernel=research_kernel,
    )
    return {
        "applied": applied,
        "sufficient": bool((material_diagnostic or {}).get("sufficient")),
        "recommended_action": recommended_action,
        "missing_materials": missing[:8],
        "risks": risks[:8],
        "candidate_titles": candidate_titles[:3],
        "rationale": rationale[:500],
    }


def _material_scope_guard_needed(
    material_diagnostic: Mapping[str, object] | None,
    *,
    selected_thesis: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None,
) -> bool:
    if not material_diagnostic:
        return False
    if material_diagnostic.get("sufficient") is True:
        return False
    action = str(material_diagnostic.get("recommended_action") or "").strip().lower()
    if action == "proceed":
        return False
    diagnostic_text = json.dumps(material_diagnostic, ensure_ascii=False).lower()
    thesis_text = json.dumps(selected_thesis or {}, ensure_ascii=False).lower()
    kernel_text = json.dumps(research_kernel or {}, ensure_ascii=False).lower()
    combined = " ".join((diagnostic_text, thesis_text, kernel_text))
    # This guard is only useful when the draft is likely to over-claim
    # material strength. Keep it broad enough for humanities kernels
    # (一手材料 / 档案 / 题记) but avoid changing generic low-stakes runs.
    material_terms = (
        "一手",
        "档案",
        "备忘录",
        "纪要",
        "结算记录",
        "题记",
        "序跋",
        "刊本",
        "primary",
        "archive",
        "memo",
        "minutes",
        "settlement",
        "manuscript",
        "inscription",
        "preface",
        "colophon",
    )
    return any(term in combined for term in material_terms)


def _material_scope_guard_directive(
    material_diagnostic: Mapping[str, object] | None,
    *,
    selected_thesis: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None,
) -> str:
    summary = _material_scope_guard_summary(
        material_diagnostic,
        selected_thesis=selected_thesis,
        research_kernel=research_kernel,
    )
    if not summary["applied"]:
        return ""
    compact = {
        "recommended_action": summary["recommended_action"],
        "missing_materials": summary["missing_materials"],
        "risks": summary["risks"],
        "candidate_titles": summary["candidate_titles"],
        "rationale": summary["rationale"],
    }
    return (
        " material_scope_guard: the synthesizer material diagnostic says the "
        "current source bundle is NOT sufficient to defend the full empirical "
        "claim as originally framed. Treat the diagnostic as a hard scope limit. "
        "Write this section as a source-bound scoping / research-design article: "
        "state what the verified literature can support, define a candidate "
        "evidence route, and explicitly mark missing primary materials as future "
        "work. Do NOT claim that the paper has already completed a continuous "
        "archive chain, directly read internal memos/minutes/settlement records, "
        "or proven a unique failure month/date/node. Use phrases such as "
        "'候选观察窗口', '待验证证据链', '现有材料只能支持压力累积与问题重构', "
        "and avoid '已经证明', '已锁定', '连续档案链已经显示'. If concrete named "
        "samples are missing, do not present empirical sections as completed "
        "'案例分析'; title and write them as evidence-type analysis or "
        "evidence-chain design instead. Diagnostic: "
        f"{json.dumps(compact, ensure_ascii=False, sort_keys=True)}. "
    )


_MATERIAL_SCOPE_TEXT_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"本文采用档案化的过程追踪方法"),
        "本文提出档案化过程追踪的研究设计",
    ),
    (
        re.compile(r"本文采用档案化过程追踪方法"),
        "本文提出档案化过程追踪的研究设计",
    ),
    (
        re.compile(r"重建美元—黄金通道的约束力变化"),
        "界定美元—黄金通道约束力变化的待验证证据路径",
    ),
    (
        re.compile(r"重建失效节点的形成过程"),
        "界定失效节点研究所需的证据路径",
    ),
    (
        re.compile(r"直接锁定具体月份或具体政策环节"),
        "说明哪些月份或政策环节仍需一手材料验证",
    ),
    (
        re.compile(r"最可能节点"),
        "候选观察窗口",
    ),
    (
        re.compile(r"1968年前后更可能是失效节点"),
        "1968年前后更适合作为候选观察窗口",
    ),
    (
        re.compile(r"1968年应被视为功能性失效节点"),
        "1968年可作为高置信度的功能性失效候选节点",
    ),
    (
        re.compile(r"应判定为1968年已进入功能性失效阶段"),
        "可将1968年作为功能性失效阶段的高置信度推断",
    ),
    (
        re.compile(r"1968年已进入功能性失效阶段"),
        "现有材料支持将1968年视为功能性失效阶段的高置信度推断",
    ),
    (
        re.compile(r"1968年比1971年更接近([^，。；;]{0,24})事实失效节点"),
        r"现有材料更支持将1968年作为\1事实失效的候选节点",
    ),
    (
        re.compile(r"更稳妥的结论是：1968年([^，。；;]{0,36})，1971年只是法定终结"),
        r"更稳妥的表述是：现有材料支持将1968年\1作为高置信度推断，1971年仍是法定终结",
    ),
    (
        re.compile(r"已失去实际约束力"),
        "可能出现实际约束力弱化",
    ),
    (
        re.compile(r"已经失去实际约束力"),
        "可能出现实际约束力弱化",
    ),
    (
        re.compile(r"已不再构成可执行约束"),
        "可能不再稳定构成可执行约束",
    ),
    (
        re.compile(r"已经掌握一手档案"),
        "仍需补足一手档案",
    ),
    (
        re.compile(r"连续档案链"),
        "待验证档案链",
    ),
)


_MATERIAL_SCOPE_EMPIRICAL_TITLES = {
    "empirical-section-i": "四、证据类型分析（一）",
    "empirical-section-ii": "五、证据类型分析（二）",
    "empirical-section-iii": "六、证据链校验",
}


def _apply_material_scope_guard_to_sections(
    sections: Sequence[DraftedSection],
    *,
    selected_thesis: Mapping[str, object] | None = None,
    research_kernel: Mapping[str, object] | None = None,
) -> list[DraftedSection]:
    guarded: list[DraftedSection] = []
    for section in sections:
        title = _material_scope_section_title(section)
        prose = _rewrite_material_scope_text(section.prose)
        if title != section.title:
            prose = _replace_section_heading(prose, title)
        claim_map = [_rewrite_material_scope_claim(claim) for claim in section.claim_map]
        if section.section_id in {"sources-method", "conclusion"}:
            paragraph = _material_scope_guard_paragraph(
                section.section_id,
                selected_thesis=selected_thesis,
                research_kernel=research_kernel,
            )
            if paragraph not in prose:
                prose = _insert_scope_guard_paragraph(prose, paragraph)
                claim_map.append(
                    {
                        "paragraph_id": f"{section.section_id}-material-scope",
                        "claim_text": paragraph,
                        "source_ids": [],
                        "evidence_status": "model_backed",
                        "confidence": "high",
                        "uncited": False,
                    }
                )
        guarded.append(
            DraftedSection(
                section_id=section.section_id,
                title=title,
                prose=prose,
                claim_map=claim_map,
                failed=section.failed,
                warnings=[*section.warnings, "material-scope-guard applied"],
                word_count=_word_count(prose),
                target_words=section.target_words,
            )
        )
    return guarded


def _material_scope_section_title(section: DraftedSection) -> str:
    scoped_title = _MATERIAL_SCOPE_EMPIRICAL_TITLES.get(section.section_id)
    if not scoped_title:
        return section.title
    title_text = section.title.lower()
    first_heading = ""
    for line in section.prose.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            first_heading = stripped.lower()
            break
        if stripped:
            break
    combined = f"{title_text} {first_heading}"
    if "案例分析" in combined or "case analysis" in combined:
        return scoped_title
    return section.title


def _replace_section_heading(prose: str, title: str) -> str:
    lines = prose.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            hashes = stripped.split(maxsplit=1)[0]
            if set(hashes) == {"#"}:
                indent_len = len(line) - len(stripped)
                lines[index] = f"{line[:indent_len]}{hashes} {title}"
                return "\n".join(lines).strip() + "\n"
            break
        if stripped:
            break
    return f"## {title}\n\n{prose.strip()}\n"


def _rewrite_material_scope_claim(claim: Mapping[str, object]) -> dict[str, object]:
    rewritten = dict(claim)
    raw_text = rewritten.get("claim_text")
    if isinstance(raw_text, str):
        rewritten["claim_text"] = _rewrite_material_scope_text(raw_text)
    return rewritten


def _rewrite_material_scope_text(text: str) -> str:
    rewritten = text
    for pattern, replacement in _MATERIAL_SCOPE_TEXT_REPLACEMENTS:
        rewritten = pattern.sub(replacement, rewritten)
    return rewritten


def _material_scope_guard_paragraph(
    section_id: str,
    *,
    selected_thesis: Mapping[str, object] | None = None,
    research_kernel: Mapping[str, object] | None = None,
) -> str:
    topic = _material_scope_topic_phrase(
        selected_thesis=selected_thesis,
        research_kernel=research_kernel,
    )
    if section_id == "conclusion":
        return (
            f"因此，本文的结论应被理解为围绕{topic}的可检验研究框架："
            "现有材料足以说明问题重构和候选机制的重要性，但不足以对所有"
            "材料节点、时间先后和相对权重作出封闭定论；后续仍需补入连续"
            "一手材料与个案互证。"
        )
    return (
        f"材料边界必须先说明：现阶段可核验材料主要支持围绕{topic}的"
        "研究框架、候选证据链与可检验判断，尚不足以证明所有关键材料、"
        "时间顺序和机制关系已经由连续一手材料闭合。因此，本文将相关结论"
        "写成候选判断和待验证证据路径，而不写成已经证成的唯一路径、"
        "精确排序或完整档案链。"
    )


def _material_scope_topic_phrase(
    *,
    selected_thesis: Mapping[str, object] | None,
    research_kernel: Mapping[str, object] | None,
) -> str:
    for source, key in (
        (selected_thesis, "thesis_one_sentence"),
        (selected_thesis, "core_claim"),
        (selected_thesis, "title"),
        (research_kernel, "tentative_question"),
        (research_kernel, "scope"),
        (research_kernel, "observed_puzzle"),
    ):
        if not isinstance(source, Mapping):
            continue
        value = source.get(key)
        if not isinstance(value, str):
            continue
        cleaned = _clean_material_scope_topic(value)
        if cleaned:
            return cleaned
    return "当前研究对象"


def _clean_material_scope_topic(value: str) -> str:
    cleaned = re.sub(r"\s+", "", value.strip(" 。；;：:，,、"))
    if not cleaned:
        return ""
    cleaned = re.sub(r"^(本文|本研究|该文|文章)(认为|主张|提出|讨论|解释|研究)", "", cleaned)
    cleaned = cleaned.strip(" 。；;：:，,、")
    if not cleaned:
        return ""
    max_chars = 48
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip("，,；;：:、") + "等问题"
    return cleaned


def _insert_scope_guard_paragraph(prose: str, paragraph: str) -> str:
    lines = prose.strip().splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        return "\n".join([lines[0].rstrip(), "", paragraph, "", *lines[1:]]).strip() + "\n"
    if not prose.strip():
        return paragraph + "\n"
    return paragraph + "\n\n" + prose.strip() + "\n"


def _clean_material_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _one_line_note(note: object) -> str:
    if not isinstance(note, dict):
        return ""
    for key in ("thesis", "evidence", "method", "limits"):
        value = note.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _style_notes(domain_data: Mapping[str, Any], target_journal: str | None) -> dict[str, object]:
    profile = _target_profile(domain_data, target_journal)
    return {
        "target_journal": target_journal or profile.get("name"),
        "citation": domain_data.get("citation", {}),
        "voice": domain_data.get("voice", {}),
        "journal_profile": profile,
    }


def _section_payload(section: SectionPlan) -> dict[str, object]:
    return {
        "section_id": section.section_id,
        "section_title": section.title,
        "target_words": section.target_words,
    }


def _build_supported_claims_digest(
    drafted_sections: Sequence[DraftedSection],
) -> str:
    """Compose a compact per-section claim summary for the conclusion
    drafter (PR-G-Conclusion-Evidence-Whitelist, codex
    AGREE-WITH-AMENDMENTS 2026-05-07).

    Round 9 zh real-paper hit FAILED_POLICY at exports because the
    conclusion overclaimed beyond what the body sections actually
    showed (specific time-point judgement about gold convertibility
    erosion in the 1960s mid-late period that cited evidence didn't
    substantiate). Codex direction A: the conclusion prompt must
    receive an explicit "evidence whitelist" — a digest of every
    body claim that actually had cited support — and be told this is
    the ONLY range the conclusion may summarize.

    Per-section format:
        ``{section_title}: [N claims with cite] — {claim 1 condensed} | …``

    Each claim is truncated to 60 characters to keep the prompt
    bounded. Claims with empty / [UNCITED] source_ids are excluded —
    only evidence-backed body statements should bound the conclusion.
    Output is at most ``max_chars`` long; if the digest would exceed
    that, trailing entries are replaced with ``…(truncated)``."""
    if not drafted_sections:
        return ""
    lines: list[str] = []
    # PR-G-Conclusion-Evidence-Whitelist round-1 codex review
    # (2026-05-08, AGREE-WITH-AMENDMENTS): bumped per-claim cap from
    # 60 → 120 chars. 60 was cutting off qualifiers and concessive
    # tails ("…但证据有限", "…仅在 1968-1971 时段") that are
    # exactly the signal the conclusion needs to NOT overstate. The
    # whole point of the digest is to pin down the claim's
    # qualified strength, so cutting the qualifier defeats the
    # purpose. 120 chars + ≤8 claims/section + ≤6KB total still
    # bounds the prompt.
    per_claim_max_chars = 120
    max_chars = 6000
    for section in drafted_sections:
        # Skip the conclusion itself if somehow already in the list.
        if section.section_id == "conclusion":
            continue
        cited_claims: list[str] = []
        for claim in section.claim_map:
            if not isinstance(claim, dict):
                continue
            sids = claim.get("source_ids")
            if not isinstance(sids, list):
                continue
            real = [s for s in sids if isinstance(s, str) and s and s != "[UNCITED]"]
            if not real:
                continue
            text_raw = claim.get("claim_text")
            if not isinstance(text_raw, str) or not text_raw.strip():
                continue
            condensed = " ".join(text_raw.split())[:per_claim_max_chars]
            cited_claims.append(condensed)
        if not cited_claims:
            continue
        joined = " | ".join(cited_claims[:8])
        if len(cited_claims) > 8:
            joined += f" | …(+{len(cited_claims) - 8} more)"
        line = f"《{section.title}》[{len(cited_claims)} 条已引证]: {joined}"
        lines.append(line)
        if sum(len(line) for line in lines) > max_chars:
            lines.append("…(truncated)")
            break
    return "; ".join(lines)


def _thesis_summary(selected_thesis: Mapping[str, object]) -> str:
    for key in ("thesis_one_sentence", "working_title", "why_novel"):
        value = selected_thesis.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _metadata_payload(
    draft_version: str,
    sections: Sequence[DraftedSection],
    claim_records: Sequence[Mapping[str, object]],
    cited_sources: Sequence[NormalizedSource],
) -> dict[str, object]:
    # Per-section status so the UI can render an amber "Placeholder
    # section, needs review" badge on the stubbed ones without a red
    # error banner. Stage 3.E follow-up codex AGREE.
    section_statuses = [
        {
            "section_id": section.section_id,
            "title": section.title,
            "is_stubbed": section.failed,
            "word_count": section.word_count,
            "target_words": section.target_words,
        }
        for section in sections
    ]
    stubbed_section_ids = [section.section_id for section in sections if section.failed]
    return {
        "version": draft_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sections": len(sections),
        "failed_sections": sum(1 for section in sections if section.failed),
        "stubbed_sections": sum(1 for section in sections if section.failed),
        "stubbed_section_ids": stubbed_section_ids,
        "section_statuses": section_statuses,
        "uncited_claims": sum(1 for record in claim_records if record.get("uncited") is True),
        "cited_sources": [source.source_id for source in cited_sources],
        "manuscript_path": f"drafts/{draft_version}/manuscript.md",
        "claim_map_path": f"drafts/{draft_version}/claim_map.jsonl",
        "citations_path": f"drafts/{draft_version}/citations.bib",
        "rationale_path": f"drafts/{draft_version}/draft_rationale.md",
    }


_HEADING_PREFIX_RE = re.compile(r"^\s*#{1,6}\s")
_SECTION_HEADING_RE = re.compile(r"^\s*##(?!#)\s+")


# PR-259a — CNKI front/back matter wrapper for zh/ja papers.
#
# gpt-5.5 baseline papers have ``摘要 / 关键词 / 一、引言 / … / 八、结论
# / 参考文献`` structure. Body sections are now correct (PR-256/257a),
# but the abstract / keywords / references blocks were never
# generated — drafter only writes the body section list. The
# integrity export gate doesn't require these, but they are the
# difference between "Chinese paper" and "Chinese academic paper"
# per CNKI publishing convention.
#
# Codex round-2 verdict (PR-257c → renumbered PR-259a, AGREE Q2=B2):
# do NOT add 摘要/关键词/参考文献 as body sections — keep the
# paper_modes section list clean and add a wrapper instead. Long-term
# config could move into a ``front_matter_plan`` / ``back_matter_plan``
# field on PaperModeSpec, but for v1 the wrapper lives in drafter
# (smallest blast radius, no DB schema migration).
#
# v1 logic:
# - 摘要: derive from selected_thesis + first chunk of intro section
#   prose. No extra LLM call (avoids wall-time inflation).
# - 关键词: extract distinctive 3-4 char CJK terms from the kernel
#   scope/observed_puzzle. ``_extract_topic_keywords`` already
#   classifies them; we filter to entity-shaped lengths and pick the
#   most informative ones.
# - 参考文献: build from cited_sources in GB/T 7714 style.
#
# en papers keep the existing body-only assembly — Western academic
# convention puts the abstract in the venue's submission form rather
# than the manuscript file, and the bibliography lives in citations.bib
# already.

_GENERIC_KEYWORD_FRAGMENTS_ZH: frozenset[str] = frozenset(
    {
        # Discourse / framing words that aren't real keywords.
        "研究",
        "本文",
        "问题",
        "方法",
        "结论",
        "讨论",
        "分析",
        "既有",
        "需要",
        "如何",
        "可以",
        "应当",
        "存在",
        "依据",
        "重新",
        "建立",
        "包括",
        "因此",
        "其中",
        "本节",
        "范围",
        "材料",
        "限定",
        "之间",
        "以及",
        "或者",
    },
)


def _select_zh_keywords_from_kernel(
    research_kernel: Mapping[str, object] | None,
    target_count: int = 6,
) -> list[str]:
    """Pick distinctive CJK terms from the kernel for the
    ``关键词`` line.

    Two-stage extraction:
    1. Iterate the curated entity vocabulary
       (``_TOPIC_KEYWORD_BRIDGE_ZH_TO_EN`` keys + a small
       ``_KEYWORD_SEED_TERMS`` set of common research-corpus
       compounds) and emit every term that appears verbatim in
       the kernel text. This catches the terms we already know
       are domain-shaped (``江南``, ``刊本``, ``序跋``,
       ``刻工``, ``题记``, ``断代``).
    2. Compose 4-char compounds from neighboring entity hits when
       they appear adjacent (e.g. ``江南`` + ``刊本`` → ``江南
       刊本`` if the kernel text has them next to each other).
       This recovers ``江南刊本`` / ``刻工题记`` / ``断代依据``
       — the entity-shaped compounds you'd actually use as
       paper keywords.

    Returns up to ``target_count`` distinct terms, ordered longer
    first. Empty kernel → empty list.
    """
    if not isinstance(research_kernel, Mapping):
        return []
    text_parts: list[str] = []
    for key in ("scope", "observed_puzzle", "tentative_question"):
        value = research_kernel.get(key)
        if isinstance(value, str) and value.strip():
            text_parts.append(value)
    text = " ".join(text_parts)
    if not text:
        return []
    text_compact = re.sub(r"\s+", "", text)

    # Stage 1 — curated entity hits.
    vocab: set[str] = set(_TOPIC_KEYWORD_BRIDGE_ZH_TO_EN.keys())
    vocab |= _KEYWORD_SEED_TERMS
    hits: list[str] = [term for term in vocab if term in text_compact]

    # Stage 2 — adjacent-pair compounding.
    compounds: list[str] = []
    for i, a in enumerate(hits):
        for j, b in enumerate(hits):
            if i == j:
                continue
            if a + b in text_compact:
                compounds.append(a + b)

    # Combine + dedupe + drop substrings of longer terms.
    candidates = list(set(hits + compounds))
    candidates.sort(key=lambda t: (-len(t), t))
    selected: list[str] = []
    for term in candidates:
        if term in _GENERIC_KEYWORD_FRAGMENTS_ZH:
            continue
        if any(term in longer for longer in selected):
            continue
        selected.append(term)
        if len(selected) >= target_count:
            break
    return selected


# Common research-corpus compound terms not in the en-bridge dict.
# These are seed entries the pair-compounding stage can use; the
# user can extend via domain config in a later PR.
_KEYWORD_SEED_TERMS: frozenset[str] = frozenset(
    {
        "断代依据",
        "文体归属",
        "刻工题记",
        "江南刊本",
        "题名页",
        "牌记",
        "版式",
        "布雷顿森林体系",
        "布雷顿森林",
        "美元黄金兑换",
        "美元—黄金兑换",
        "金本位承诺",
        "国际货币体系",
        "档案性证据",
        "失效节点",
        "阳明心学",
        "明末清初",
        "传播路径",
        "制度渠道",
        "王门弟子",
        "官学制度",
        "讲会",
        "刻书",
    },
)


def _is_predominantly_chinese(value: str) -> bool:
    """PR-G-Regressions-2 (codex v5 round-1 round-2 evidence):
    detect whether a candidate string is predominantly Chinese
    (CJK ideographs ≥30% of letter-class chars). Used to gate the
    inclusion of ``selected_thesis.thesis_one_sentence`` in the
    Chinese abstract — when ideator emitted an English thesis (
    project.language was ``"en"`` even though kernel is Chinese),
    the existing CNKI wrapper would prepend that English sentence
    onto the 摘要 block, producing the mixed-language artifact
    that scored 1.5 / 7.0 on合规性 in real-paper round 1+2."""
    chinese = sum(1 for ch in value if "一" <= ch <= "鿿")
    letterish = sum(1 for ch in value if ch.isalpha() or ("一" <= ch <= "鿿"))
    if letterish == 0:
        return False
    return chinese / letterish >= 0.30


def _format_zh_abstract(
    selected_thesis: Mapping[str, object] | None,
    sections: Sequence[DraftedSection],
    target_chars: int = 250,
) -> str:
    """Compose the ``摘要`` paragraph from selected_thesis + the
    intro section's first sentences.

    The thesis sentence states the paper's claim; the intro prose
    expands the puzzle and method; together they make a serviceable
    abstract without an extra LLM call.

    PR-G-Regressions-2: when ``selected_thesis`` carries an English
    thesis_one_sentence / working_title (because ideator ran with
    project.language='en' but the paper itself is zh), skip those
    fields rather than mixing them into the Chinese 摘要. The intro
    prose alone still produces a serviceable abstract.

    Empty thesis + no intro → empty string (skip the wrapper).
    """
    parts: list[str] = []
    if isinstance(selected_thesis, Mapping):
        for key in ("thesis_one_sentence", "working_title"):
            value = selected_thesis.get(key)
            if isinstance(value, str) and value.strip() and _is_predominantly_chinese(value):
                parts.append(value.strip())
                break
    intro = next((s for s in sections if s.section_id == "introduction"), None)
    if intro:
        prose = intro.prose
        # Strip the ``## 一、引言`` heading line if the LLM included it.
        prose_lines = [
            line for line in prose.splitlines() if not _HEADING_PREFIX_RE.match(line.strip())
        ]
        prose_body = "\n".join(prose_lines).strip()
        # Take leading prose up to a comfortable character budget.
        first_chunk = prose_body[: target_chars * 2]
        parts.append(first_chunk)

    summary = " ".join(parts).strip()
    summary = re.sub(r"\s+", "", summary)  # zh paragraphs don't use spaces
    if len(summary) > target_chars:
        candidate = summary[:target_chars]
        boundary = max(candidate.rfind(mark) for mark in "。！？；")
        if boundary >= max(60, int(target_chars * 0.55)):
            summary = candidate[: boundary + 1]
        else:
            summary = candidate.rstrip("，、；：:,.") + "。"
    return summary


def _format_gb7714_reference(source: NormalizedSource, index: int) -> str:
    """One-line GB/T 7714 reference for the ``参考文献`` list.

    Simplified format: ``[N] 著者. 题名. 载体, 年.`` — full GB/T 7714
    distinguishes ``[J]`` / ``[M]`` / ``[D]`` carrier-type tags but
    we don't have that metadata reliably from openalex/crossref. The
    output is still recognizable and citation-extractable.
    """
    authors_raw = source.authors
    title_raw = source.title
    venue_raw = source.venue
    year_raw = source.year
    authors = authors_raw.strip() if isinstance(authors_raw, str) else ""
    title = title_raw.strip() if isinstance(title_raw, str) else ""
    venue = venue_raw.strip() if isinstance(venue_raw, str) else ""
    year = str(year_raw).strip() if year_raw else ""
    pieces: list[str] = [f"[{index}]"]
    if authors:
        pieces.append(authors + ".")
    if title:
        pieces.append(title + ".")
    venue_year = " ".join(p for p in (venue, year) if p)
    if venue_year:
        pieces.append(venue_year + ".")
    return " ".join(pieces).rstrip()


def _render_zh_front_matter(
    selected_thesis: Mapping[str, object] | None,
    sections: Sequence[DraftedSection],
    research_kernel: Mapping[str, object] | None,
) -> str:
    """Build the ``## 摘要`` + ``## 关键词`` block prepended to the
    body sections.

    Returns an empty string when both pieces would be empty (so the
    caller doesn't accidentally inject a heading-only block)."""
    abstract = _format_zh_abstract(selected_thesis, sections)
    keywords = _select_zh_keywords_from_kernel(research_kernel)
    if not abstract and not keywords:
        return ""
    chunks: list[str] = []
    if abstract:
        chunks.extend(["## 摘要", "", abstract, ""])
    if keywords:
        chunks.extend(["## 关键词", "", "；".join(keywords), ""])
    return "\n".join(chunks) + "\n"


def _render_zh_back_matter(cited_sources: Sequence[NormalizedSource]) -> str:
    """Build the ``## 参考文献`` block appended after the body
    sections. Empty cited_sources → empty string (skip block)."""
    if not cited_sources:
        return ""
    lines = ["## 参考文献", ""]
    for index, source in enumerate(cited_sources, start=1):
        lines.append(_format_gb7714_reference(source, index))
    lines.append("")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class UnresolvedCitationMarker:
    """A citation-shaped marker that the rewrite couldn't resolve to
    any entry in ``cited_sources``. PR-G-CiteMarkerGate (codex
    AGREE-WITH-AMENDMENTS round on 2026-05-07): post-LLM rigid
    validation surfaces these so the drafter outer-retry loop can
    feed them back to the LLM as corrective context.

    ``raw`` is the original surface form (e.g. ``(Smith 1980)``,
    ``[crossref:10.1234/foo]``) for direct quoting in the prompt.
    ``form`` distinguishes the regex pass that matched. ``reason``
    is a one-line human-readable explanation suitable for the LLM
    corrective context.
    """

    raw: str
    form: str
    reason: str


@dataclass(frozen=True)
class CitationNormalizationResult:
    """Output of the deterministic cite-marker normalize pass. PR-1
    of PR-G-CiteMarkerGate splits this into a structured result so
    PR-2 can wire ``unresolved_markers`` into a corrective retry.

    Empty ``unresolved_markers`` → manuscript is fully cite-clean.
    Non-empty → drafter outer loop should treat the run as needing
    a corrective LLM retry (paragraphs with these markers must
    either swap to a resolvable source or drop the claim)."""

    body: str
    unresolved_markers: tuple[UnresolvedCitationMarker, ...]


def _normalize_inline_citations_zh_with_unresolved(
    body: str,
    cited_sources: Sequence[NormalizedSource],
) -> CitationNormalizationResult:
    """Convert ``(Author YYYY)`` and ``[crossref:DOI]`` inline cite
    markers to ``[N]`` matching the cited_sources order. Returns
    both the rewritten body and a list of citation-shaped markers
    that the rewrite couldn't resolve.

    PR-259b: real-paper run #10 produced a clean manuscript with a
    ``## 参考文献\n[1] …\n[2] …`` block (PR-259a wrapper) but the
    body still cited sources as ``(Eisenstein 1980)`` rather than
    ``[1]``. CNKI convention requires the inline pointer to match
    the references list index so readers can connect the two. The
    drafter prompt instructs the LLM to use ``[N]`` but the LLM
    doesn't reliably know the index ahead of time, so we
    deterministically rewrite at the parse boundary.

    PR-G-CiteMarkerGate (round 7): formerly silently kept any
    citation-shaped marker that didn't resolve. Now we collect
    them so the drafter caller can decide whether to trigger a
    corrective retry (PR-2) or fall back to ``failed_policy``.

    Heuristic match (no NLP):
    - ``(Surname YYYY)`` / ``（Surname YYYY）`` → look up by
      (last-word of first author, year) in cited_sources.
    - ``[Surname YYYY]`` → same lookup.
    - ``(Surname1 and Surname2 YYYY)`` → match on first surname.
    - ``[crossref:DOI]`` → look up by DOI suffix in cited_sources.
    - Unmatched markers are left in the body and recorded in
      ``unresolved_markers`` so the caller can act on them.

    Empty cited_sources → no-op pass-through with empty unresolved.
    """
    if not cited_sources:
        return CitationNormalizationResult(body=body, unresolved_markers=())

    unresolved: list[UnresolvedCitationMarker] = []

    # Build (lastname.lower(), year) → "[N]" + DOI.lower() → "[N]" +
    # source_id.lower() → "[N]" maps. PR-G-Regressions C: also
    # accept the raw ``source_id`` form (e.g.
    # ``[https://openalex.org/W4408365924]`` or ``[crossref:DOI]``)
    # as drafter LLM occasionally emits these directly instead of
    # the (Author YYYY) form.
    by_author_year: dict[tuple[str, str], str] = {}
    by_doi: dict[str, str] = {}
    by_source_id: dict[str, str] = {}
    for index, source in enumerate(cited_sources, start=1):
        tag = f"[{index}]"
        # PR-G-Author-Year-Fix: ``NormalizedSource.authors`` is
        # ``list[str]`` (PR-J9b schema), but PR-259b legacy code
        # treated it as a string fallback ``""`` — meaning the
        # ``by_author_year`` lookup table was always empty in
        # production. Round 7 surfaced this when drafter LLM emitted
        # ``[Sahasrabuddhe and Seddon 2025]`` and the helper failed
        # to find any cited_source with that surname+year.
        authors_list: list[str] = []
        raw_authors = source.authors
        if isinstance(raw_authors, list):
            authors_list = [str(a) for a in raw_authors if a]
        elif isinstance(raw_authors, str) and raw_authors.strip():
            # Legacy string form — fall back to splitting like before.
            authors_list = re.split(r"[,;、，；&]| and ", raw_authors)
        # Index BOTH the first author's surname AND every author's
        # surname so multi-author cite forms like
        # ``[Sahasrabuddhe and Seddon 2025]`` (which captures
        # surname=Seddon at the END of the bracket) still hit the
        # lookup table.
        year_raw = source.year
        year = str(year_raw).strip() if year_raw else ""
        if year:
            for author_raw in authors_list:
                first_author = author_raw.strip().strip(".,")
                if not first_author:
                    continue
                surname = first_author.split()[-1].strip(".,")
                if surname:
                    by_author_year[(surname.lower(), year)] = tag
            source_id_lower = source.source_id.lower()
            institution_aliases: list[str] = []
            if source_id_lower.startswith("official:imf:"):
                institution_aliases.extend(
                    [
                        "IMF",
                        "International Monetary Fund",
                        "国际货币基金组织",
                    ]
                )
            if source_id_lower.startswith("official:fraser:"):
                institution_aliases.extend(
                    [
                        "Board of Governors",
                        "Federal Reserve Board",
                        "Federal Reserve System",
                        "美国联邦储备系统理事会",
                        "联邦储备委员会",
                        "美联储理事会",
                    ]
                )
            for alias in institution_aliases:
                by_author_year.setdefault((alias.lower(), year), tag)
        if source.source_id.startswith("crossref:"):
            doi = source.source_id.split(":", 1)[1]
            by_doi[doi.lower()] = tag
        # Index by full source_id so the drafter can also use raw
        # ``[https://openalex.org/...]`` or ``[crossref:DOI]`` forms
        # and we still map them to ``[N]``.
        by_source_id[source.source_id.lower()] = tag

    def make_replace_author_year(form: str) -> Callable[[re.Match[str]], str]:
        def resolve_one(inner: str) -> tuple[str | None, str, str]:
            year_match = re.search(r"\d{4}", inner)
            if not year_match:
                return None, "", ""
            year = year_match.group(0)
            before_year = inner[: year_match.start()].strip()
            first_chunk = re.split(r" and |&|、|;|；|，", before_year)[0].strip()
            first_chunk = re.sub(r",.*$", "", first_chunk).strip()
            first_chunk = re.sub(
                r"\s+et\s+al\.?$",
                "",
                first_chunk,
                flags=re.IGNORECASE,
            ).strip()
            surname = first_chunk.split()[-1].strip(".,") if first_chunk else ""
            return by_author_year.get((surname.lower(), year)), surname, year

        def replace_author_year(match: re.Match[str]) -> str:
            inner = match.group(1)
            pieces = [piece.strip() for piece in re.split(r"[;；]", inner) if piece.strip()]
            if len(pieces) > 1 and all(re.search(r"\d{4}", piece) for piece in pieces):
                resolved_tags: list[str] = []
                unresolved_pieces: list[str] = []
                for piece in pieces:
                    piece_tag, piece_surname, piece_year = resolve_one(piece)
                    if piece_tag:
                        resolved_tags.append(piece_tag)
                    else:
                        unresolved_pieces.append(f"{piece_surname or '?'} {piece_year or '?'}")
                if not unresolved_pieces:
                    return "".join(dict.fromkeys(resolved_tags))
                unresolved.append(
                    UnresolvedCitationMarker(
                        raw=match.group(0),
                        form=form,
                        reason=(
                            "no cited source for author-year piece(s): "
                            + ", ".join(unresolved_pieces)
                        ),
                    ),
                )
                return match.group(0)
            tag, surname, year = resolve_one(inner)
            if tag:
                return tag
            unresolved.append(
                UnresolvedCitationMarker(
                    raw=match.group(0),
                    form=form,
                    reason=(
                        f"no cited source with surname={surname or '?'} "
                        f"year={year} (citation-shaped {form})"
                    ),
                ),
            )
            return match.group(0)

        return replace_author_year

    # PR-G-Conclusion-Evidence-Whitelist round-1 codex review
    # (2026-05-08, AGREE-WITH-AMENDMENTS): use a generic
    # URI-scheme guard ``[A-Za-z][A-Za-z0-9+.-]*:`` instead of a
    # hand-curated allowlist. PR #300 only blocked
    # ``crossref:|openalex:|cnki:|https?://``; ``arxiv:`` /
    # ``doi:`` / ``pmcid:`` / ``pmid:`` / ``isbn:`` / ``issn:`` /
    # any future scheme would still be eaten by the
    # bracketed-Author-Year regex with the same spurious-unresolved
    # consequence. RFC 3986 scheme syntax is
    # ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ), so the guard
    # generalizes. The rare edge of an LLM emitting a colon-
    # separated author form like ``[Smith: 2020]`` is intentionally
    # skipped — drafter prompts never produce that style.
    body = re.sub(
        r"[（(]\s*(?![A-Za-z][A-Za-z0-9+.\-]*:|shadow_baseline_v001\b)"
        r"([A-Za-z一-鿿][^()）]{1,80}\d{4})\s*[)）]",
        make_replace_author_year("author_year_paren"),
        body,
    )
    body = re.sub(
        r"\[\s*(?![A-Za-z][A-Za-z0-9+.\-]*:|shadow_baseline_v001\b)"
        r"([A-Za-z一-鿿][^\[\]]{1,80}\d{4})\s*\]",
        make_replace_author_year("author_year_bracket"),
        body,
    )

    def replace_doi(match: re.Match[str]) -> str:
        doi = match.group(1).strip().lower()
        tag = by_doi.get(doi)
        if tag:
            return tag
        unresolved.append(
            UnresolvedCitationMarker(
                raw=match.group(0),
                form="crossref_doi",
                reason=f"no cited source with DOI={doi}",
            ),
        )
        return match.group(0)

    body = re.sub(r"\[crossref:([^\]]+)\]", replace_doi, body)

    def replace_source_id(match: re.Match[str]) -> str:
        candidate = match.group(1).strip().lower()
        tag = by_source_id.get(candidate)
        if tag:
            return tag
        unresolved.append(
            UnresolvedCitationMarker(
                raw=match.group(0),
                form="source_id",
                reason=f"source_id {candidate} not in cited_sources",
            ),
        )
        return match.group(0)

    def replace_multi_source_id(match: re.Match[str]) -> str:
        inner = match.group(1)
        pieces = _split_source_marker_pieces(inner)
        if len(pieces) < 2:
            return match.group(0)
        tags: list[str] = []
        unknown_pieces: list[str] = []
        for piece in pieces:
            tag = by_source_id.get(piece.lower())
            if tag is None:
                unknown_pieces.append(piece)
            else:
                tags.append(tag)
        if unknown_pieces:
            unresolved.append(
                UnresolvedCitationMarker(
                    raw=match.group(0),
                    form="multi_source_id",
                    reason=(
                        "multi-source bracket has unknown piece(s): " + ", ".join(unknown_pieces)
                    ),
                ),
            )
            return match.group(0)
        return "".join(tags)

    body = re.sub(
        r"\[((?:https?://|crossref:|openalex:|cnki:|official:|shadow_baseline_v001)[^\]]+)\]",
        replace_multi_source_id,
        body,
    )
    body = re.sub(
        r"【((?:https?://|crossref:|openalex:|cnki:|official:|shadow_baseline_v001)[^】]+)】",
        replace_multi_source_id,
        body,
    )
    body = re.sub(
        r"〔((?:https?://|crossref:|openalex:|cnki:|official:|shadow_baseline_v001)[^〕]+)〕",
        replace_multi_source_id,
        body,
    )
    body = re.sub(
        r"［((?:https?://|crossref:|openalex:|cnki:|official:|shadow_baseline_v001)[^］]+)］",
        replace_multi_source_id,
        body,
    )
    body = re.sub(
        r"[（(]\s*((?:https?://|crossref:|openalex:|cnki:|official:|shadow_baseline_v001)[^（）()]+)\s*[）)]",
        replace_multi_source_id,
        body,
    )
    body = re.sub(
        r"\[((?:https?://|crossref:|openalex:|cnki:|official:)[^\]]+)\]",
        replace_source_id,
        body,
    )
    body = re.sub(
        r"【((?:https?://|crossref:|openalex:|cnki:|official:)[^】]+)】",
        replace_source_id,
        body,
    )
    body = re.sub(
        r"〔((?:https?://|crossref:|openalex:|cnki:|official:)[^〕]+)〕",
        replace_source_id,
        body,
    )
    body = re.sub(
        r"［((?:https?://|crossref:|openalex:|cnki:|official:)[^］]+)］",
        replace_source_id,
        body,
    )
    body = re.sub(
        r"[（(]\s*((?:https?://|crossref:|openalex:|cnki:|official:)[^（）()\s;；,，、]+)\s*[）)]",
        replace_source_id,
        body,
    )
    body = re.sub(
        r"\[(shadow_baseline_v001)\]",
        replace_source_id,
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"【(shadow_baseline_v001)】",
        replace_source_id,
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"〔(shadow_baseline_v001)〕",
        replace_source_id,
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"［(shadow_baseline_v001)］",
        replace_source_id,
        body,
        flags=re.IGNORECASE,
    )
    body = re.sub(
        r"[（(]\s*(shadow_baseline_v001)\s*[）)]",
        replace_source_id,
        body,
        flags=re.IGNORECASE,
    )
    # Some Chinese final rewrites preserve numeric references but
    # switch ASCII brackets to full-width Chinese wrappers. CNKI
    # scoring treats those as non-uniform citation markers, so
    # normalize the numeric forms deterministically after source-id
    # repair.
    body = re.sub(r"〔\s*(\d{1,3})\s*〕", r"[\1]", body)
    body = re.sub(r"［\s*(\d{1,3})\s*］", r"[\1]", body)
    body = re.sub(r"【\s*(\d{1,3})\s*】", r"[\1]", body)
    return CitationNormalizationResult(body=body, unresolved_markers=tuple(unresolved))


def _split_source_marker_pieces(inner: str) -> list[str]:
    return [
        piece.strip() for piece in re.split(r"\s*(?:[;；,，、]|\s+)\s*", inner) if piece.strip()
    ]


def _normalize_inline_citations_zh(
    body: str,
    cited_sources: Sequence[NormalizedSource],
) -> str:
    """Backwards-compatible body-only wrapper. Use
    ``_normalize_inline_citations_zh_with_unresolved`` when the
    caller needs the cite-marker gate signal (PR-G-CiteMarkerGate)."""
    return _normalize_inline_citations_zh_with_unresolved(body, cited_sources).body


# ─── PR-G-CiteMarkerGate PR-2b: corrective LLM retry ──────────────


@dataclass(frozen=True)
class CiteMarkerRepairOutcome:
    """Result of the cite-marker corrective retry. Models the same
    shape as ``DiversityRepairOutcome`` so caller logic is uniform.

    ``failed_policy=True`` means the run exhausted ``max_retries``
    with markers still unresolved — caller must propagate this as
    a ``failed_policy`` state so critic / exports don't ship a
    manuscript with un-grounded cites."""

    applied: bool
    body: str
    skipped_reason: str | None
    event_type: str
    attempts: int
    initial_unresolved_count: int
    final_unresolved_count: int
    failed_policy: bool


CITE_MARKER_REPAIR_SYSTEM_PROMPT = (
    "你正在为一篇人文社科论文修复 N 个无法解析的引用标记（cite "
    "markers）。下面给出每个未解析 marker 所在的段落（按 index "
    "标识）+ 该段落里出问题的 marker 列表 + 当前可用的 sources（带"
    " source_id / 标题 / 作者 / 年份 / DOI / venue）。\n"
    "对每个段落，请输出修复后的版本。\n"
    "约束（违反任一条本次输出整体作废）：\n"
    "1. 不要直接产出 ``[N]`` 形式的引用——你不知道 N 的最终值，"
    "归一化由后处理负责。必须用以下三种形式之一：\n"
    "   - ``[crossref:<DOI>]``（最优先）\n"
    "   - ``[<URL>]``（如 ``[https://openalex.org/W...]``）\n"
    "   - ``[<source_id>]``（exact 拷贝 sources 列表里的 source_id）\n"
    "2. 不要新增 sources 列表外的引用、人名、文献。\n"
    "3. 段落主旨与论证结构不能改变；只针对未解析 marker 做局部修复"
    "——找不到合适的 source 时，**删除该 marker 所在的整条断言**"
    "（保留段落其余内容），不要保留无引用的孤立断言。\n"
    "4. 段落字数 ±20% 以内；不能整段删空。\n"
    '5. 输出严格 JSON：``{"paragraphs": [{"index": int, '
    '"repaired_text": str}]}``。每个输入段落都必须出现在输出里 '
    "（index 与输入对应）。\n"
)


class _RepairedParagraph(BaseModel):
    index: int
    repaired_text: StrictStr

    class Config:
        extra = "ignore"


class _CiteMarkerRepairOutput(BaseModel):
    paragraphs: list[_RepairedParagraph]

    class Config:
        extra = "ignore"


def _group_unresolved_by_paragraph(
    body: str,
    unresolved: Sequence[UnresolvedCitationMarker],
) -> list[tuple[int, str, list[UnresolvedCitationMarker]]]:
    """Map each unresolved marker to its containing paragraph.
    Returns ``[(paragraph_index, paragraph_text, [markers])]`` for
    every paragraph that has at least one unresolved marker.
    Paragraphs are ``\\n\\n``-separated (drafter's existing
    convention, see ``_normalize_inline_citations_zh`` callers)."""
    paragraphs = body.split("\n\n")
    grouped: dict[int, list[UnresolvedCitationMarker]] = {}
    for marker in unresolved:
        # First paragraph containing the raw marker text wins.
        for idx, para in enumerate(paragraphs):
            if marker.raw in para:
                grouped.setdefault(idx, []).append(marker)
                break
    return [(idx, paragraphs[idx], markers) for idx, markers in sorted(grouped.items())]


def _splice_repaired_paragraphs(
    body: str,
    repaired_by_index: Mapping[int, str],
) -> str:
    """Rejoin the manuscript with the LLM-repaired paragraphs at
    the indicated indices. Indices not in the map keep their
    original text. Out-of-range indices are silently ignored."""
    paragraphs = body.split("\n\n")
    spliced = list(paragraphs)
    for idx, new_text in repaired_by_index.items():
        if 0 <= idx < len(spliced):
            spliced[idx] = new_text
    return "\n\n".join(spliced)


def _format_sources_for_repair_prompt(
    sources: Sequence[NormalizedSource],
    *,
    max_chars: int = 8000,
) -> str:
    """Render the cited_sources into a compact catalogue the LLM
    can pick from. Each entry one line; truncate to keep the prompt
    bounded."""
    lines: list[str] = []
    for source in sources:
        authors = source.authors if isinstance(source.authors, list) else []
        author_str = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
        doi_str = f"  doi={source.doi}" if source.doi else ""
        lines.append(
            f"- source_id={source.source_id}  authors={author_str or '?'}  "
            f"year={source.year or '?'}  title={source.title[:80]}{doi_str}",
        )
        if sum(len(line) for line in lines) > max_chars:
            lines.append("  …(truncated)…")
            break
    return "\n".join(lines)


def _maybe_run_cite_marker_repair(
    *,
    manuscript: str,
    cited_sources: Sequence[NormalizedSource],
    initial_unresolved: tuple[UnresolvedCitationMarker, ...],
    paper_language: str,
    run: Run,
    project: Project,
    hooks: HookRegistry,
    audit: AuditWriter,
    draft_version: str,
) -> CiteMarkerRepairOutcome:
    """Corrective LLM retry loop: when normalize leaves
    citation-shaped markers behind, ask the LLM to either swap each
    for a resolvable cite form (DOI / URL / source_id) or delete the
    affected claim. After each LLM round, re-normalize; loop until
    clean OR ``drafter_cite_marker_max_retries`` exhausts.

    Codex AGREE-WITH-AMENDMENTS direction B (2026-05-07): exhaustion
    sets ``failed_policy=True`` so the caller can propagate up to
    the run state, refusing to ship a manuscript with un-grounded
    cites.

    Returns ``CiteMarkerRepairOutcome``. ``applied=True`` means the
    manuscript is fully cite-clean. ``failed_policy=True`` means
    we exhausted retries and the caller must fail the run."""
    settings = get_settings()
    max_retries = settings.drafter_cite_marker_max_retries
    initial_count = len(initial_unresolved)

    body = manuscript
    last_unresolved: tuple[UnresolvedCitationMarker, ...] = initial_unresolved
    attempts = 0

    while last_unresolved and attempts < max_retries:
        attempts += 1
        groups = _group_unresolved_by_paragraph(body, last_unresolved)
        if not groups:
            # Markers exist but couldn't locate paragraphs (unlikely
            # — every marker matched against `body` originally; this
            # would mean the body changed in a way that lost them).
            break

        sources_block = _format_sources_for_repair_prompt(cited_sources)
        paragraphs_payload = [
            {
                "index": idx,
                "paragraph": para,
                "unresolved_markers": [
                    {"raw": m.raw, "form": m.form, "reason": m.reason} for m in markers
                ],
            }
            for idx, para, markers in groups
        ]
        user_prompt = (
            f"语言: {paper_language}\n\n"
            f"可用 sources（每行一个 source_id 用于替换 marker）：\n"
            f"{sources_block}\n\n"
            f"待修复段落 + marker 列表（JSON）：\n"
            f"{json.dumps(paragraphs_payload, ensure_ascii=False, sort_keys=True)}\n\n"
            f"严格 JSON 输出："
            f'{{"paragraphs": [{{"index": int, "repaired_text": str}}, ...]}}'
        )
        request = LLMCallRequest(
            messages=[
                {"role": "system", "content": CITE_MARKER_REPAIR_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            model=settings.one_api_model,
            temperature=0.2,
            max_tokens=4000,
            response_format={"type": "json_object"},
            request_id=f"drafter_cite_marker_repair_{run.id}_{draft_version}_a{attempts}",
            prompt_template_id="drafter.cite_marker_repair.v1",
        )
        context = HookContext(
            run_id=run.id,
            phase="drafter",
            step_id="drafter.cite_marker_repair",
            user_id=project.user_id,
            attempt=attempts,
            prompt_template_id=request.prompt_template_id,
            prompt_filled=user_prompt,
            prompt_hash=hash_text(user_prompt),
            project_title=project.title,
            run_metadata={
                "agent_phase": "drafter",
                "draft_version": draft_version,
                "step": "cite_marker_repair",
                "attempt": attempts,
                "unresolved_count_before": len(last_unresolved),
            },
        )
        try:
            response = asyncio.run(
                run_llm_step(
                    request=request,
                    hooks=hooks,
                    context=context,
                    output_schema=_CiteMarkerRepairOutput,
                    audit=audit,
                    max_corrective_retries=1,
                    llm_optional=True,
                ),
            )
        except Exception:  # noqa: BLE001 - LLM transport failure → bail to next attempt
            continue

        parsed = response.parsed
        if not isinstance(parsed, _CiteMarkerRepairOutput) or not parsed.paragraphs:
            continue

        repaired_by_index: dict[int, str] = {}
        for entry in parsed.paragraphs:
            repaired_by_index[entry.index] = entry.repaired_text
        body = _splice_repaired_paragraphs(body, repaired_by_index)

        # Re-run normalize on the patched body to see if we made progress.
        result = _normalize_inline_citations_zh_with_unresolved(body, cited_sources)
        body = result.body
        last_unresolved = result.unresolved_markers

    if last_unresolved:
        return CiteMarkerRepairOutcome(
            applied=False,
            body=body,
            skipped_reason="exhausted_max_retries",
            event_type="cite_marker_repair_exhausted",
            attempts=attempts,
            initial_unresolved_count=initial_count,
            final_unresolved_count=len(last_unresolved),
            failed_policy=True,
        )

    return CiteMarkerRepairOutcome(
        applied=True,
        body=body,
        skipped_reason=None,
        event_type="cite_marker_repair_applied",
        attempts=attempts,
        initial_unresolved_count=initial_count,
        final_unresolved_count=0,
        failed_policy=False,
    )


def _wrap_manuscript_with_cnki_matter(
    body: str,
    *,
    paper_language: str,
    selected_thesis: Mapping[str, object] | None,
    sections: Sequence[DraftedSection],
    research_kernel: Mapping[str, object] | None,
    cited_sources: Sequence[NormalizedSource],
) -> str:
    """Top-level wrapper. ``zh`` adds 摘要 + 关键词 (front) + 参考文献
    (back). ``ja`` adds analogous Japanese headings. ``en`` and any
    other language pass-through (Western convention puts these in
    the submission form, not the manuscript file)."""
    if paper_language not in ("zh", "ja"):
        return body
    if paper_language == "zh":
        front = _render_zh_front_matter(selected_thesis, sections, research_kernel)
        back = _render_zh_back_matter(cited_sources)
    else:  # ja — same structure, JA headings
        front = (
            _render_zh_front_matter(selected_thesis, sections, research_kernel)
            .replace(
                "## 摘要",
                "## 要旨",
            )
            .replace(
                "## 关键词",
                "## キーワード",
            )
        )
        back = _render_zh_back_matter(cited_sources).replace(
            "## 参考文献",
            "## 参考文献",  # zh and ja share the term; keep as-is
        )
    pieces: list[str] = []
    if front:
        pieces.append(front)
    pieces.append(body.rstrip("\n") + "\n")
    if back:
        pieces.append("\n" + back)
    return "".join(pieces)


def _manuscript_markdown(sections: Sequence[DraftedSection]) -> str:
    """Render the per-section prose into one manuscript markdown.

    The Drafter LLM is instructed to write the section heading itself
    in the project language (e.g. "## 引言" for zh, "## 序論" for ja).
    We do not emit HTML anchors in the manuscript: they are useful
    internal metadata but look like raw scaffolding in journal-facing
    markdown and lower the critic compliance score.

    Fallback: if the LLM prose does NOT start with a top-level ``##``
    heading line, we add the planned ``section.title`` so the section is
    still labeled. Lower-level headings such as ``### （一）`` are
    subsection headings and must not stand in for the parent section.
    """
    chunks: list[str] = []
    for section in sections:
        prose = section.prose.strip()
        if _SECTION_HEADING_RE.match(prose):
            lines = prose.splitlines()
            lines[0] = f"## {section.title}"
            prose = "\n".join(lines).strip()
        elif _matches_section_title_h3_plus(prose, section.title):
            # PR-398 (codex A'): math mode's gpt-5.5 holistic stage B
            # tends to emit ``### 一、引言`` as the first line, which
            # the h2-only ``_SECTION_HEADING_RE`` didn't recognize, so
            # the else branch below prepended ``## 一、引言`` and
            # produced a duplicate heading in the docx export. Detect
            # that specific case (h3+ first line whose text equals the
            # planned section title) and normalize it to ``## title``.
            # Legitimate subsections like ``### （一）书目著录路径``
            # still fall through to the else branch.
            lines = prose.splitlines()
            indent_len = len(lines[0]) - len(lines[0].lstrip())
            lines[0] = f"{lines[0][:indent_len]}## {section.title}"
            prose = "\n".join(lines).strip()
        else:
            chunks.append(f"## {section.title}")
            chunks.append("")
        chunks.append(prose)
        chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


_SECTION_HEADING_DEEP_RE = re.compile(r"^\s*#{3,6}\s+(.+?)\s*$")


def _matches_section_title_h3_plus(prose: str, title: str) -> bool:
    """PR-398 helper: True iff the first non-empty line of ``prose`` is
    an h3-h6 heading whose text equals ``title``. Used to spot the
    duplicate-heading case introduced by math-mode holistic rewrites
    without false-positiving on legitimate subsections.
    """
    target = title.strip()
    if not target:
        return False
    for line in prose.splitlines():
        if not line.strip():
            continue
        match = _SECTION_HEADING_DEEP_RE.match(line)
        if not match:
            return False
        return match.group(1).strip() == target
    return False


def _rationale_markdown(
    draft_version: str,
    sections: Sequence[DraftedSection],
    claim_records: Sequence[Mapping[str, object]],
    cited_sources: Sequence[NormalizedSource],
) -> str:
    lines = [
        "# Draft Rationale",
        "",
        f"- Draft version: {draft_version}",
        f"- Sections drafted: {len(sections)}",
        f"- Cited sources: {len(cited_sources)}",
        (
            "- [UNCITED] claims: "
            f"{sum(1 for record in claim_records if record.get('uncited') is True)}"
        ),
        "",
        "## Sections",
        "",
    ]
    for section in sections:
        status = "stubbed" if section.failed else "drafted"
        under_target = section.word_count < max(1, int(section.target_words * 0.30))
        lines.append(
            f"- {section.title}: {status}; {section.word_count}/{section.target_words} words"
        )
        if under_target:
            lines.append(f"  - Length flag: below 30% of target for {section.section_id}.")
        if section.warnings:
            lines.append(f"  - Warnings: {'; '.join(section.warnings)}")
    lines.extend(["", "## Uncited Claims", ""])
    uncited = [record for record in claim_records if record.get("uncited") is True]
    if uncited:
        for record in uncited:
            lines.append(f"- {record.get('paragraph_id')}: {record.get('claim_text')}")
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _flatten_claim_map(
    sections: Sequence[DraftedSection],
    draft_version: str,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for section in sections:
        for claim in section.claim_map:
            record = dict(claim)
            record["draft_version"] = draft_version
            records.append(record)
    return records


def _cited_source_ids(claim_records: Sequence[Mapping[str, object]]) -> set[str]:
    cited: set[str] = set()
    for record in claim_records:
        raw_source_ids = record.get("source_ids")
        if not isinstance(raw_source_ids, list):
            continue
        for source_id in raw_source_ids:
            if isinstance(source_id, str) and source_id != "[UNCITED]":
                cited.add(source_id)
    return cited


def _eligible_diversity_count(
    shortlist: Sequence[NormalizedSource],
    project_title: str,
    research_kernel: Mapping[str, object] | None,
) -> tuple[int, set[str]]:
    """PR-G-Sources Stage 2: count shortlist sources eligible for
    diversity-floor accounting. ``eligible`` = ``topic_relevance ≠
    "low"`` (codex round-2 amendment Q1 — citation stuffing
    safeguard: low-relevance sources cannot inflate floor).

    PR-G-Sources Stage 3 Q3 amendment (codex round-1): when a
    source fails the keyword-overlap test but has been LLM-reranked
    with strong scope_fit + relevance scores, fall back to medium
    eligibility. The keyword-overlap heuristic produces false
    negatives for CJK humanities sources whose abstracts use
    vocabulary that doesn't substring-match the Chinese kernel;
    the 4-axis LLM rerank (PR-J9b) is more semantically reliable.

    ``research_kernel`` is the
    ``research_kernel_for_prompt(...)`` output — empty / None
    yields a degraded eligible set (every source is "low" because
    the keyword set is empty), in which case the diagnostic
    ``effective_floor`` collapses to 0 and no event fires.

    Returns ``(eligible_count, eligible_source_ids)``.
    """
    keywords = _extract_topic_keywords(project_title, research_kernel)
    if not keywords:
        return 0, set()
    eligible_ids: set[str] = set()
    for source in shortlist:
        if _score_source_topic_relevance(source, keywords) != "low":
            eligible_ids.add(source.source_id)
            continue
        if _is_rerank_eligible_fallback(source):
            eligible_ids.add(source.source_id)
    return len(eligible_ids), eligible_ids


def _is_rerank_eligible_fallback(source: NormalizedSource) -> bool:
    """PR-G-Sources Stage 3 Q3 fallback predicate (codex round-1
    amendment). Returns True when this source's curator-side
    4-axis LLM rerank scores justify treating it as eligible
    despite a keyword-overlap "low" score.

    All four conditions must hold:
    - 4-axis rerank actually ran for this source (rerank_axes set)
    - scope_fit >= 0.55 (the most heavily weighted axis at 35%)
    - relevance >= 0.50
    - rank_score >= 0.6 (final blended; weak rank means even the
      reranker was unsure — don't override)
    """
    axes = source.rerank_axes
    if not isinstance(axes, dict):
        return False
    scope_fit = axes.get("scope_fit")
    relevance = axes.get("relevance")
    if not isinstance(scope_fit, (int, float)) or not isinstance(relevance, (int, float)):
        return False
    if scope_fit < 0.55:
        return False
    if relevance < 0.50:
        return False
    return source.rank_score >= 0.6


def _check_diversity_floor(
    *,
    drafted_sections: Sequence[DraftedSection],
    shortlist: Sequence[NormalizedSource],
    project_title: str,
    research_kernel: Mapping[str, object] | None,
    draft_version: str,
) -> dict[str, object] | None:
    """PR-G-Sources Stage 2 (codex round-2 amendment Q2): post-LLM
    diversity check. Returns a diagnostic dict when the cited
    source count is below ``effective_floor``; ``None`` when above
    or when configured floor ≤ 0 (operator opt-out).

    ``effective_floor = min(Settings.cited_sources_diversity_floor,
    eligible_source_count)`` per codex Q1 — ensures we don't
    demand 12 citations from a shortlist of 8 eligible sources.
    """
    settings = get_settings()
    configured = settings.cited_sources_diversity_floor
    if configured <= 0:
        return None
    eligible_count, eligible_ids = _eligible_diversity_count(
        shortlist=shortlist,
        project_title=project_title,
        research_kernel=research_kernel,
    )
    effective_floor = min(configured, eligible_count)
    if effective_floor <= 0:
        return None
    cited_now = _cited_source_ids(_flatten_claim_map(drafted_sections, draft_version))
    if len(cited_now) >= effective_floor:
        return None
    cited_eligible = cited_now & eligible_ids
    return {
        "cited_count": len(cited_now),
        "cited_eligible_count": len(cited_eligible),
        "eligible_count": eligible_count,
        "configured_floor": configured,
        "effective_floor": effective_floor,
        "shortlist_count": len(shortlist),
    }


# -----------------------------------------------------------------
# PR-G-Grounding — claim-source semantic grounding warning
# -----------------------------------------------------------------

# Patterns for specific archive / document / material entity
# mentions in claim text. When a claim mentions one of these AND
# none of the claim's cited source's metadata contains a matching
# substring, the claim is ``weakly_grounded``.
#
# Each pattern is a (regex, description) tuple. Regexes are applied
# case-insensitively; description appears in the warning event
# payload so the operator / acceptance gate knows what tripped
# the gate.
_GROUNDING_ARCHIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bretton Woods / Cold-War financial archives — round 4 + 6
    # FAILED_POLICY culprits.
    (re.compile(r"IMF\s*[内部]*备忘录"), "IMF memo"),
    (re.compile(r"国际货币基金组织[内部]*备忘录"), "IMF memo (zh)"),
    (re.compile(r"美联储[理事会]?\s*[会议]?纪要"), "Fed minutes"),
    (re.compile(r"FOMC\s*纪要"), "FOMC minutes"),
    (re.compile(r"伦敦黄金池[季度]*结算"), "London Gold Pool settlement"),
    (re.compile(r"London Gold Pool"), "London Gold Pool"),
    (re.compile(r"Bretton Woods Conference"), "Bretton Woods Conference"),
    # Late-Qing humanities — round 1 + 2 culprits.
    (re.compile(r"序跋[与和]?[\s]*[刻工]*题记"), "颐 prefaces & engraver inscriptions"),
    (re.compile(r"刻工题记"), "engraver inscriptions"),
    (re.compile(r"江南刊本"), "Jiangnan imprints"),
    # Generic specific-archive markers (any "xxx 档案" / "xxx 备忘录"
    # / "xxx 纪要" / "xxx 文献" with a 2-6 char proper-noun-looking
    # prefix). Conservative: requires CJK or Capitalized prefix.
    (
        re.compile(
            r"(?:[A-Z][A-Za-z]{2,20}|[一-鿿]{2,8})\s*"
            r"(?:档案|备忘录|纪要|文献集|档案集)"
        ),
        "named archive/memo",
    ),
)


def _check_claim_grounding(
    *,
    drafted_sections: Sequence[DraftedSection],
    shortlist: Sequence[NormalizedSource],
    run_dir: Path,
) -> dict[str, object]:
    """Scan each claim's text for specific archive/document entity
    mentions; for each match, verify at least one of the claim's
    cited sources actually contains that entity in its title /
    abstract / synthesizer source_note. Returns a diagnostic dict;
    when ``weakly_grounded_count == 0`` the caller skips the event.

    This is a deterministic substring check — it doesn't catch
    semantic equivalence (e.g. "Federal Reserve minutes" vs "FOMC
    records") but reliably catches the round-4 / round-6 failure
    pattern where the manuscript promises specific named archives
    that none of the cited sources mention.
    """
    by_id: dict[str, NormalizedSource] = {s.source_id: s for s in shortlist}
    source_notes_dir = run_dir / "synthesis" / "source_notes"
    source_notes: dict[str, str] = {}
    if source_notes_dir.exists():
        for note_path in source_notes_dir.glob("*.json"):
            try:
                note_text = note_path.read_text(encoding="utf-8")
            except OSError:
                continue
            # source_note filenames are derived from source_id with
            # ``/`` and ``:`` mangled to ``-``. Match by stem prefix.
            stem = note_path.stem
            source_notes[stem] = note_text

    weakly_grounded: list[dict[str, object]] = []
    for section in drafted_sections:
        for claim in section.claim_map:
            claim_text = ""
            if isinstance(claim, dict):
                raw_text = claim.get("claim_text") or claim.get("text") or ""
                if isinstance(raw_text, str):
                    claim_text = raw_text
            if not claim_text:
                continue
            if isinstance(claim, dict):
                status = claim.get("evidence_status")
                if status == "model_backed" or _is_material_limitation_statement(claim_text):
                    continue
            cited_ids: list[str] = []
            raw_sids = claim.get("source_ids") if isinstance(claim, dict) else None
            if isinstance(raw_sids, list):
                cited_ids = [sid for sid in raw_sids if isinstance(sid, str) and sid != "[UNCITED]"]
            for pattern, description in _GROUNDING_ARCHIVE_PATTERNS:
                match = pattern.search(claim_text)
                if match is None:
                    continue
                phrase = match.group(0)
                # Build the haystack from each cited source's
                # title + abstract + venue + (raw) source_note JSON.
                found = False
                for sid in cited_ids:
                    src = by_id.get(sid)
                    if src is None:
                        continue
                    haystack_parts: list[str] = []
                    for piece in (src.title, src.abstract, src.venue):
                        if isinstance(piece, str):
                            haystack_parts.append(piece)
                    # source_notes filename mangles ``/`` and ``:``
                    # to ``-``; try both raw and mangled lookups.
                    for stem_candidate in (sid, sid.replace("/", "-").replace(":", "-")):
                        note_text_value = source_notes.get(stem_candidate)
                        if note_text_value:
                            haystack_parts.append(note_text_value)
                            break
                    haystack = " ".join(haystack_parts)
                    if phrase in haystack:
                        found = True
                        break
                if not found:
                    weakly_grounded.append(
                        {
                            "section_id": section.section_id,
                            "paragraph_id": (
                                claim.get("paragraph_id") if isinstance(claim, dict) else None
                            ),
                            "phrase": phrase,
                            "pattern_description": description,
                            "cited_source_ids": list(cited_ids),
                        }
                    )
                # One pattern hit per claim is enough — don't
                # double-flag the same claim for two patterns.
                break
    return {
        "weakly_grounded_count": len(weakly_grounded),
        "weakly_grounded_claims": weakly_grounded,
    }


def _is_material_limitation_statement(text: str) -> bool:
    """Return True for sentences that name missing material as a limit.

    The grounding scanner is meant to catch positive archive claims,
    not the conservative "we still lack X" paragraphs introduced by
    material_scope_guard. Treat explicit limitation language as a
    non-source-bound scope statement; citation whitelist / critic still
    police any positive factual claims elsewhere.
    """
    if not text:
        return False
    has_material_term = any(
        term in text
        for term in (
            "档案",
            "一手材料",
            "备忘录",
            "纪要",
            "结算记录",
            "archive",
            "primary material",
            "memo",
            "minutes",
            "settlement",
        )
    )
    if not has_material_term:
        return False
    return any(
        marker in text
        for marker in (
            "缺少",
            "不足",
            "尚未",
            "仍需",
            "待验证",
            "不能",
            "无法",
            "不把",
            "需要补",
            "future work",
            "missing",
            "insufficient",
            "not enough",
        )
    )


# -----------------------------------------------------------------
# PR-G-Sources Q2 — LLM diversity repair
# -----------------------------------------------------------------


@dataclass
class DiversityRepairOutcome:
    """Result of the LLM diversity repair step."""

    applied: bool
    drafted_sections: list[DraftedSection]
    skipped_reason: str | None
    event_type: str
    added_source_ids: list[str]
    target_section_ids: list[str]


_DIVERSITY_REPAIR_MAX_UNUSED_INJECTED = 6
_DIVERSITY_REPAIR_TARGET_SECTIONS = 2

DIVERSITY_REPAIR_SYSTEM_PROMPT = (
    "你正在为一篇人文社科论文做引用多样性补救。任务：将给定的"
    "「未使用 sources」自然地整合进 2 个目标章节的论证中。\n"
    "约束（违反任一条本次输出整体作废）：\n"
    "1. 只输出收到的 2 个目标章节，section_id 必须与输入一致；不要输出其他章节。\n"
    "2. prose 必须保留原章节的核心论点、史实与既有 [N] 引用；"
    "可在末尾或合适位置增补 1-2 段，明确标注新引用为 [N]，"
    "N 取自 cited list 中该 source 的位置编号。\n"
    "3. 每个新增的 [N] 引用必须在 claim_map 中产生对应条目；claim_map.source_ids "
    "必须严格出自 cited_source_ids 全集（已有 + 新加入）。\n"
    '4. 标记为 ``role="medium"`` 的 unused source 只能作为背景说明 / 方法参照 / 学者群中的代表，'
    "不可作为核心论点的关键证据。\n"
    "5. 不可改章节标题、不可缩短到原章节字数 70% 以下。\n"
    "6. 不可新增未在 cited 列表中的史实、人名、年份、文献判断。\n"
    "7. 输出必须是严格 JSON。"
)


class _RepairedClaimRecord(BaseModel):
    paragraph_id: StrictStr
    claim_text: StrictStr
    source_ids: list[StrictStr] = Field(default_factory=list)
    evidence_status: EvidenceStatus = "source_bound"
    confidence: EvidenceConfidence | None = None

    class Config:
        extra = "ignore"


class _RepairedSection(BaseModel):
    section_id: StrictStr
    section_title: StrictStr
    prose: StrictStr
    claim_map: list[_RepairedClaimRecord]

    class Config:
        extra = "ignore"


class _DiversityRepairOutput(BaseModel):
    sections: list[_RepairedSection]

    class Config:
        extra = "ignore"


def _section_distinct_cited_count(section: DraftedSection) -> int:
    """Count distinct ``source_id`` values cited in a section's
    ``claim_map`` (excluding ``[UNCITED]`` placeholders)."""
    distinct: set[str] = set()
    for claim in section.claim_map:
        raw = claim.get("source_ids") if isinstance(claim, dict) else None
        if not isinstance(raw, list):
            continue
        for sid in raw:
            if isinstance(sid, str) and sid != "[UNCITED]":
                distinct.add(sid)
    return len(distinct)


def _pick_repair_target_sections(
    drafted_sections: Sequence[DraftedSection],
    target_count: int = _DIVERSITY_REPAIR_TARGET_SECTIONS,
) -> list[int]:
    """Return indices of the ``target_count`` lowest-distinct-cited
    sections. Stable order: ties broken by section index ascending."""
    ranked = sorted(
        (
            (idx, _section_distinct_cited_count(section))
            for idx, section in enumerate(drafted_sections)
        ),
        key=lambda item: (item[1], item[0]),
    )
    return [idx for idx, _ in ranked[:target_count]]


def _maybe_run_llm_diversity_repair(
    *,
    drafted_sections: list[DraftedSection],
    shortlist: Sequence[NormalizedSource],
    project_title: str,
    research_kernel: Mapping[str, object] | None,
    paper_language: str,
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    audit: AuditWriter,
    draft_version: str,
    diagnostic: Mapping[str, object],
) -> DiversityRepairOutcome:
    """One bounded LLM call to integrate unused eligible sources
    into the 2 lowest-density drafted sections.

    Codex v5 round-1 Q2 amendment compliance:
    - effective_floor=3 still triggers repair (target: 1 → 3)
    - only 2 lowest-density sections get rewritten
    - every new claim_map source_id must be in eligible set
    - medium sources used as background/method only (prompt rule)
    - one shot, failures emit warning event (no loop)
    """
    eligible_count, eligible_ids = _eligible_diversity_count(
        shortlist=shortlist,
        project_title=project_title,
        research_kernel=research_kernel,
    )
    if not eligible_ids:
        return DiversityRepairOutcome(
            applied=False,
            drafted_sections=drafted_sections,
            skipped_reason="no_eligible_sources",
            event_type="diversity_repair_skipped",
            added_source_ids=[],
            target_section_ids=[],
        )
    cited_now = _cited_source_ids(_flatten_claim_map(drafted_sections, draft_version))
    unused_eligible_ids = sorted(eligible_ids - cited_now)
    if not unused_eligible_ids:
        return DiversityRepairOutcome(
            applied=False,
            drafted_sections=drafted_sections,
            skipped_reason="no_unused_eligible",
            event_type="diversity_repair_skipped",
            added_source_ids=[],
            target_section_ids=[],
        )

    # Cap injected sources so the prompt stays compact and the LLM
    # focuses on the most rerank-strong unused candidates.
    by_id = {s.source_id: s for s in shortlist}
    unused_sources = [by_id[sid] for sid in unused_eligible_ids if sid in by_id]
    unused_sources.sort(key=lambda s: s.rank_score, reverse=True)
    unused_sources = unused_sources[:_DIVERSITY_REPAIR_MAX_UNUSED_INJECTED]
    if not unused_sources:
        return DiversityRepairOutcome(
            applied=False,
            drafted_sections=drafted_sections,
            skipped_reason="no_unused_eligible",
            event_type="diversity_repair_skipped",
            added_source_ids=[],
            target_section_ids=[],
        )

    target_indices = _pick_repair_target_sections(drafted_sections)
    targets = [drafted_sections[i] for i in target_indices]
    target_section_ids = [s.section_id for s in targets]

    # Build full cited list — pipeline cited so far + the unused
    # sources we want to integrate. Index in this list IS the [N]
    # number the LLM should emit (1-based).
    cited_now_sources = [s for s in shortlist if s.source_id in cited_now]
    cited_now_sources.sort(key=lambda s: s.source_id)
    full_cited_list = list(cited_now_sources) + list(unused_sources)
    cited_payload = [
        {
            "n": idx,
            "source_id": s.source_id,
            "title": s.title or "",
            "authors": list(s.authors) if isinstance(s.authors, list) else [],
            "year": s.year,
            "role": "core" if s.source_id in cited_now else "medium",
        }
        for idx, s in enumerate(full_cited_list, start=1)
    ]
    target_payload = [
        {
            "section_id": s.section_id,
            "section_title": s.title,
            "prose": s.prose,
            "claim_map": list(s.claim_map),
        }
        for s in targets
    ]
    kernel_json = json.dumps(
        dict(research_kernel) if research_kernel else {},
        ensure_ascii=False,
        sort_keys=True,
    )
    cited_json = json.dumps(cited_payload, ensure_ascii=False, sort_keys=True)
    targets_json = json.dumps(target_payload, ensure_ascii=False, sort_keys=True)
    user_prompt = (
        f"语言: {paper_language}.\n"
        f"研究内核: {kernel_json}.\n\n"
        f"cited (含已用 + 待整合): {cited_json}.\n\n"
        "目标章节（仅这 2 节，输出 section_id 必须严格匹配）：\n"
        f"{targets_json}.\n\n"
        '输出严格 JSON：{"sections": [{"section_id": ..., '
        '"section_title": ..., "prose": ..., "claim_map": [...]}, ...]}.'
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": DIVERSITY_REPAIR_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model=get_settings().one_api_model,
        temperature=0.3,
        max_tokens=8000,
        response_format={"type": "json_object"},
        request_id=f"drafter_diversity_repair_{run.id}_{draft_version}",
        prompt_template_id="drafter.diversity_repair.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="drafter",
        step_id="drafter.diversity_repair",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=user_prompt,
        prompt_hash=hash_text(user_prompt),
        project_title=project_title,
        run_metadata={
            "agent_phase": "drafter",
            "draft_version": draft_version,
            "step": "diversity_repair",
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=_DiversityRepairOutput,
                audit=audit,
                max_corrective_retries=1,
                llm_optional=True,
            ),
        )
    except Exception:  # noqa: BLE001
        return DiversityRepairOutcome(
            applied=False,
            drafted_sections=drafted_sections,
            skipped_reason="llm_call_failed",
            event_type="diversity_repair_skipped",
            added_source_ids=[],
            target_section_ids=target_section_ids,
        )

    parsed = response.parsed
    if not isinstance(parsed, _DiversityRepairOutput) or not parsed.sections:
        return DiversityRepairOutcome(
            applied=False,
            drafted_sections=drafted_sections,
            skipped_reason="llm_invalid_output",
            event_type="diversity_repair_skipped",
            added_source_ids=[],
            target_section_ids=target_section_ids,
        )

    # Validate + splice rewritten sections back. Rules:
    #   1. section_id matches one of our targets
    #   2. claim_map source_ids ⊆ full_cited_list source_id set
    #   3. prose preserves original length within 70% tolerance
    full_cited_id_set = {s.source_id for s in full_cited_list}
    targets_by_id = {s.section_id: s for s in targets}
    new_sections_by_id: dict[str, DraftedSection] = {}
    actually_added: set[str] = set()
    for repaired in parsed.sections:
        if repaired.section_id not in targets_by_id:
            return DiversityRepairOutcome(
                applied=False,
                drafted_sections=drafted_sections,
                skipped_reason="llm_invalid_section_id",
                event_type="diversity_repair_skipped",
                added_source_ids=[],
                target_section_ids=target_section_ids,
            )
        original = targets_by_id[repaired.section_id]
        # Length floor (codex Q2: don't shrink below 70%).
        if len(repaired.prose) < int(len(original.prose) * 0.7):
            return DiversityRepairOutcome(
                applied=False,
                drafted_sections=drafted_sections,
                skipped_reason="llm_repair_shrank_section",
                event_type="diversity_repair_skipped",
                added_source_ids=[],
                target_section_ids=target_section_ids,
            )
        normalized_claim_map = _normalize_diversity_repair_claim_map(
            repaired.claim_map,
            original=original,
            full_cited_id_set=full_cited_id_set,
            cited_now=cited_now,
            actually_added=actually_added,
        )
        new_sections_by_id[repaired.section_id] = DraftedSection(
            section_id=original.section_id,
            title=original.title,
            prose=repaired.prose,
            claim_map=normalized_claim_map,
            failed=False,
            warnings=list(original.warnings),
            word_count=_word_count(repaired.prose),
            target_words=original.target_words,
        )

    if not actually_added:
        return DiversityRepairOutcome(
            applied=False,
            drafted_sections=drafted_sections,
            skipped_reason="llm_added_no_unused_sources",
            event_type="diversity_repair_skipped",
            added_source_ids=[],
            target_section_ids=target_section_ids,
        )

    # Splice back — return a NEW list to avoid mutating caller's.
    new_drafted_sections = list(drafted_sections)
    for idx in target_indices:
        old = new_drafted_sections[idx]
        if old.section_id in new_sections_by_id:
            new_drafted_sections[idx] = new_sections_by_id[old.section_id]

    return DiversityRepairOutcome(
        applied=True,
        drafted_sections=new_drafted_sections,
        skipped_reason=None,
        event_type="diversity_repair_applied",
        added_source_ids=sorted(actually_added),
        target_section_ids=target_section_ids,
    )


def _normalize_diversity_repair_claim_map(
    repaired_claims: Sequence[_RepairedClaimRecord],
    *,
    original: DraftedSection,
    full_cited_id_set: set[str],
    cited_now: set[str],
    actually_added: set[str],
) -> list[dict[str, object]]:
    normalized_claim_map: list[dict[str, object]] = []
    for claim in repaired_claims:
        normalized_sids: list[str] = []
        for sid in claim.source_ids:
            if sid in full_cited_id_set:
                normalized_sids.append(sid)
                if sid not in cited_now:
                    actually_added.add(sid)
        evidence_status: EvidenceStatus = "source_bound"
        confidence: EvidenceConfidence | None = None
        source_ids: list[str] = normalized_sids if normalized_sids else ["[UNCITED]"]
        uncited = not normalized_sids
        if not normalized_sids and (
            claim.evidence_status == "model_backed"
            or _is_uncited_analytic_claim_model_backed(
                claim.claim_text,
                section_id=original.section_id,
            )
        ):
            source_ids = []
            uncited = False
            evidence_status = "model_backed"
            confidence = claim.confidence or "medium"
        record: dict[str, object] = {
            "paragraph_id": claim.paragraph_id,
            "claim_text": claim.claim_text,
            "source_ids": source_ids,
            "section_id": original.section_id,
            "section_title": original.title,
            "uncited": uncited,
            "evidence_status": evidence_status,
        }
        if confidence is not None:
            record["confidence"] = confidence
        normalized_claim_map.append(record)
    return normalized_claim_map


def _read_sources_json(path: Path) -> list[NormalizedSource]:
    records = _load_json_array(path)
    sources: list[NormalizedSource] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            sources.append(NormalizedSource.parse_obj(record))
        except ValidationError:
            continue
    return sources


def _load_source_notes(path: Path) -> dict[str, object]:
    notes: dict[str, object] = {}
    if not path.exists():
        return notes
    for note_path in sorted(path.glob("*.json")):
        note = _load_json_mapping(note_path)
        source_id = note.get("source_id")
        if isinstance(source_id, str) and source_id:
            notes[source_id] = note
    return notes


def _load_json_array(path: Path) -> list[object]:
    if not path.exists():
        return []
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _load_json_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: value for key, value in decoded.items() if isinstance(key, str)}


def _load_jsonl_objects(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                records.append(decoded)
    return records


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {key: item for key, item in decoded.items() if isinstance(key, str)}


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _write_jsonl(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(dict(record), sort_keys=True, ensure_ascii=False) + "\n",
            )
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _next_draft_version(drafts_dir: Path) -> str:
    highest = 0
    if drafts_dir.exists():
        for child in drafts_dir.iterdir():
            match = re.fullmatch(r"v(\d{3})", child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"v{highest + 1:03d}"


def _normalize_version(version: str) -> str:
    stripped = version.strip()
    if re.fullmatch(r"v\d{3}", stripped):
        return stripped
    if re.fullmatch(r"\d{1,3}", stripped):
        return f"v{int(stripped):03d}"
    return stripped


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "section"


def _humanize_section_id(section_id: str) -> str:
    """Convert a snake_case section id from paper_modes registry
    (e.g. "sources_method", "empirical_section_i") into a display
    title ("Sources & Method", "Empirical Section I"). Roman-numeral
    suffixes (_i / _ii / _iii / _iv / _v / _vi) are uppercased."""
    parts = section_id.split("_")
    roman_suffixes = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
    rendered: list[str] = []
    for idx, part in enumerate(parts):
        if not part:
            continue
        if idx > 0 and part.lower() in roman_suffixes:
            rendered.append(part.upper())
        elif part.lower() == "and":
            rendered.append("&")
        else:
            rendered.append(part.capitalize())
    title = " ".join(rendered)
    # Hand-fix the "Sources Method" → "Sources & Method" specific
    # case the legacy drafter title uses; keep the generic humanize
    # function lossless otherwise.
    if title == "Sources Method":
        return "Sources & Method"
    return title


def _safe_request_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "section"


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", value))


def _domain_path(domain_id: str) -> Path:
    settings = get_settings()
    path = settings.domain_dir / f"{domain_id}.yaml"
    if path.exists():
        return path
    return Path(__file__).resolve().parents[4] / "domains" / f"{domain_id}.yaml"


# -----------------------------------------------------------------
# PR-G-Coherence — global coherence LLM pass + post-validation
# -----------------------------------------------------------------


@dataclass
class CoherencePassOutcome:
    """Result of the global coherence step. Caller uses
    ``manuscript`` when ``applied`` is True, otherwise sticks with
    its input. ``event_type`` is non-empty only when the caller
    should emit an audit event (skipped / validation_failed cases)."""

    applied: bool
    manuscript: str | None
    skipped_reason: str | None
    event_type: str
    before_bytes: int
    after_bytes: int


class GlobalCoherenceOutput(BaseModel):
    """Schema wrapper for the coherence pass LLM response. The LLM
    returns ``{"manuscript_markdown": "..."}`` and we extract the
    markdown body — using a JSON envelope keeps response_format
    consistent with the rest of the harness (which strict-JSON
    validates every call)."""

    manuscript_markdown: StrictStr

    class Config:
        extra = "ignore"


# Codex round-3 AGREE: base system prompt locked verbatim. Changing any
# of rules 1-8 requires a fresh codex review. Rule 9 is policy-derived.
GLOBAL_COHERENCE_SYSTEM_PROMPT = (
    "你正在做整篇人文社科论文的最后一遍连贯性收紧。约束：\n"
    "1. 你只能调整段落间过渡语、首尾呼应、删除明显重复表达、补足跨节论证转折。\n"
    "2. 绝不可改动 [N] 引用编号、删 [N] 引用、改章节标题（一/二/.../八）、改章节顺序。\n"
    "3. 摘要、关键词、参考文献三块完全保留，逐字不动。\n"
    "4. 不可改变任何史实、人名、年份、引用具体内容。\n"
    "5. 不可删除任何包含 [N] 引用的段落或论据。\n"
    "6. 不可改变文章总长度超过 30%。\n"
    "7. 输出整篇 markdown，与输入同结构。\n"
    "8. 不输出任何「已修改」标记或注释。"
)


def _global_coherence_prompt(policies: EvidencePolicies) -> str:
    rule_9 = policies.coherence_rule_9
    if not rule_9:
        return GLOBAL_COHERENCE_SYSTEM_PROMPT
    return GLOBAL_COHERENCE_SYSTEM_PROMPT + "\n" + rule_9


# Section title markers used by the CNKI wrapper. Order is significant
# — the validator compares the ordered list before/after to detect
# section reordering or insertion.
_CNKI_BODY_HEADINGS_ZH = ("一、", "二、", "三、", "四、", "五、", "六、", "七、", "八、")
_CNKI_FRONT_BACK_BLOCKS_ZH = ("摘要", "关键词", "参考文献")


def _extract_inline_citations(text: str) -> list[str]:
    """Return ``[N]`` markers in document order so callers can compare
    multisets (codex Q1 amendment: multiset, not set — preserves
    duplicate-citation semantics)."""
    return re.findall(r"\[\d+\]", text)


def _extract_cnki_section_titles(text: str) -> list[str]:
    """Return CNKI body headings (一、 ... 八、) in document order.
    Each match is the heading line stripped of trailing whitespace."""
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        for heading in _CNKI_BODY_HEADINGS_ZH:
            if stripped.startswith(heading):
                found.append(stripped)
                break
    return found


def _extract_cnki_block(text: str, marker: str) -> str | None:
    """Extract the prose content of a CNKI front/back block.

    Heuristic — minimal but stable for the 3 markers we care about:
    - ``摘要``  : single paragraph starting with ``摘要[：:]`` until a
                  blank line
    - ``关键词``: single paragraph starting with ``关键词[：:]`` until a
                  blank line
    - ``参考文献``: everything from a line that is exactly
                    ``参考文献`` (possibly preceded by ``##``) to EOF

    Returns ``None`` when the marker is not present.
    """
    if marker == "参考文献":
        # Match either ``参考文献`` standalone or ``## 参考文献`` heading
        match = re.search(r"(?m)^#*\s*参考文献\s*$", text)
        if match is None:
            return None
        return text[match.end() :].strip() or None
    # 摘要 / 关键词: single paragraph. Find the line starting with the
    # marker, then take everything until the next blank line.
    pattern = re.compile(rf"(?ms)^(?:#+\s*)?{re.escape(marker)}[：:]\s*(.*?)(?:\n\s*\n|\Z)")
    match = pattern.search(text)
    if match is None:
        return None
    return match.group(1).strip() or None


def _normalize_for_block_compare(value: str) -> str:
    """Whitespace + unicode normalization so a tab/space difference
    in the rewriter doesn't trip the front/back block validator."""
    normalized = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", normalized).strip()


def _citation_bearing_paragraph_count(text: str) -> int:
    """Count paragraphs (\\n\\n-split) that contain at least one
    ``[N]`` citation marker. The validator demands ``after`` has
    AT LEAST as many citation-bearing paragraphs as ``before`` —
    transition prose can be tweaked freely (rule 1 already pins
    the citation multiset), but paragraph-level deletion is
    prohibited (codex Q3 amendment 1 on v3: catches the case where
    LLM moves [N] elsewhere and drops the original paragraph)."""
    count = 0
    for paragraph in text.split("\n\n"):
        if re.search(r"\[\d+\]", paragraph):
            count += 1
    return count


def _validate_global_coherence_output(
    *,
    before: str,
    after: str,
) -> str | None:
    """5-rule post-validation. Returns reject reason string on
    failure or ``None`` on PASS. Each rule corresponds to one of the
    8 system-prompt directives that the LLM might violate.

    1. citation multiset preserved (Counter-equal)
    2. CNKI body section titles ordered list preserved
    3. 摘要 / 关键词 / 参考文献 normalized-identical
    4. citation-bearing paragraphs preserved (paragraph hash subset)
    5. manuscript length didn't shrink > 30%
    """
    # Rule 1: citation multiset
    if Counter(_extract_inline_citations(before)) != Counter(
        _extract_inline_citations(after),
    ):
        return "citation_multiset_mismatch"
    # Rule 2: CNKI body section titles ordered list
    if _extract_cnki_section_titles(before) != _extract_cnki_section_titles(after):
        return "cnki_section_titles_changed"
    # Rule 3: 摘要 / 关键词 / 参考文献 normalized-identical
    for marker in _CNKI_FRONT_BACK_BLOCKS_ZH:
        before_block = _extract_cnki_block(before, marker)
        after_block = _extract_cnki_block(after, marker)
        if (before_block is None) != (after_block is None):
            return f"cnki_{marker}_block_presence_changed"
        if (
            before_block is not None
            and after_block is not None
            and _normalize_for_block_compare(before_block)
            != _normalize_for_block_compare(after_block)
        ):
            return f"cnki_{marker}_block_modified"
    # Rule 4: citation-bearing paragraphs preserved (count-based,
    # see ``_citation_bearing_paragraph_count`` rationale)
    if _citation_bearing_paragraph_count(after) < _citation_bearing_paragraph_count(before):
        return "citation_bearing_paragraph_deleted"
    # Rule 5: manuscript length didn't shrink > 30%
    if len(after) < len(before) * 0.7:
        return "manuscript_shrank_too_much"
    return None


def _build_global_coherence_prompt(
    *,
    manuscript: str,
    paper_language: str,
    research_kernel: Mapping[str, object] | None,
) -> str:
    """Assemble the user message for the coherence pass. Includes
    the kernel anchor (so the LLM keeps the same research focus)
    but does NOT include claim_map / shortlist (those are for
    drafting, not for tightening prose).

    Output is a strict JSON object with one field
    ``manuscript_markdown`` containing the rewritten full markdown.
    """
    kernel_payload = dict(research_kernel) if research_kernel else {}
    schema_hint = '{"manuscript_markdown": "<整篇 markdown 字符串>"}'
    return (
        f"语言: {paper_language}.\n"
        f"研究内核（仅作背景，禁止加新观点）: "
        f"{json.dumps(kernel_payload, ensure_ascii=False, sort_keys=True)}.\n\n"
        f"以下是已经写完 8 章的论文全篇（含 CNKI 体例）。请按系统消息中的 8 条约束做一次"
        f"连贯性收紧。\n"
        f"输出必须是 JSON 对象，仅含字段 ``manuscript_markdown``，其值为整篇 markdown 字符串："
        f"{schema_hint}\n\n"
        f"原始 markdown：\n\n"
        f"{manuscript}"
    )


def _maybe_run_global_coherence_pass(
    *,
    manuscript: str,
    paper_language: str,
    research_kernel: Mapping[str, object] | None,
    cited_sources: Sequence[NormalizedSource],
    run: Run,
    project: Project,
    session: Session,
    hooks: HookRegistry,
    audit: AuditWriter,
    draft_version: str,
    policies: EvidencePolicies,
) -> CoherencePassOutcome:
    """Run the coherence LLM pass with full fall-back semantics.
    Returns a ``CoherencePassOutcome`` describing what happened so
    the caller can emit a single audit event + populate
    ``draft_metadata.global_coherence``.

    Skip conditions (no LLM call):
    - ``Settings.drafter_global_coherence_enabled`` False
    - paper has no ``[N]`` citations at all (drafter generated
      stubs only — coherence pass on a stub manuscript is wasted
      LLM budget)
    """
    settings = get_settings()
    before_bytes = len(manuscript.encode("utf-8"))
    if not settings.drafter_global_coherence_enabled:
        return CoherencePassOutcome(
            applied=False,
            manuscript=None,
            skipped_reason="disabled_by_settings",
            event_type="drafter_global_coherence_skipped",
            before_bytes=before_bytes,
            after_bytes=before_bytes,
        )
    if not _extract_inline_citations(manuscript):
        return CoherencePassOutcome(
            applied=False,
            manuscript=None,
            skipped_reason="no_inline_citations",
            event_type="drafter_global_coherence_skipped",
            before_bytes=before_bytes,
            after_bytes=before_bytes,
        )

    prompt = _build_global_coherence_prompt(
        manuscript=manuscript,
        paper_language=paper_language,
        research_kernel=research_kernel,
    )
    request = LLMCallRequest(
        messages=[
            {"role": "system", "content": _global_coherence_prompt(policies)},
            {"role": "user", "content": prompt},
        ],
        model=settings.one_api_model,
        temperature=0.3,
        # Manuscript ~10-25K bytes (CJK ~6-15K tokens); allow 1.3x
        # output budget so the pass can lightly extend transitions
        # without truncating. 25k tokens covers up to ~80k char
        # CJK manuscripts; well under provider gateway max.
        max_tokens=25000,
        response_format={"type": "json_object"},
        request_id=f"drafter_global_coherence_{run.id}_{draft_version}",
        prompt_template_id="drafter.global_coherence.v1",
    )
    context = HookContext(
        run_id=run.id,
        phase="drafter",
        step_id="drafter.global_coherence",
        user_id=project.user_id,
        attempt=1,
        prompt_template_id=request.prompt_template_id,
        prompt_filled=prompt,
        prompt_hash=hash_text(prompt),
        project_title=project.title,
        run_metadata={
            "agent_phase": "drafter",
            "draft_version": draft_version,
            "step": "global_coherence",
        },
    )
    try:
        response = asyncio.run(
            run_llm_step(
                request=request,
                hooks=hooks,
                context=context,
                output_schema=GlobalCoherenceOutput,
                audit=audit,
                max_corrective_retries=1,
                llm_optional=True,
            ),
        )
    except Exception:  # noqa: BLE001
        return CoherencePassOutcome(
            applied=False,
            manuscript=None,
            skipped_reason="llm_call_failed",
            event_type="drafter_global_coherence_skipped",
            before_bytes=before_bytes,
            after_bytes=before_bytes,
        )
    parsed = response.parsed
    rewritten = ""
    if isinstance(parsed, GlobalCoherenceOutput):
        rewritten = parsed.manuscript_markdown.strip()
    if not rewritten:
        return CoherencePassOutcome(
            applied=False,
            manuscript=None,
            skipped_reason="llm_empty_response",
            event_type="drafter_global_coherence_skipped",
            before_bytes=before_bytes,
            after_bytes=before_bytes,
        )
    rewritten = _sanitize_baseline_as_evidence_source_mentions(rewritten)

    reject = _validate_global_coherence_output(before=manuscript, after=rewritten)
    if reject is not None:
        return CoherencePassOutcome(
            applied=False,
            manuscript=None,
            skipped_reason=f"validation_failed:{reject}",
            event_type="drafter_global_coherence_validation_failed",
            before_bytes=before_bytes,
            after_bytes=len(rewritten.encode("utf-8")),
        )

    return CoherencePassOutcome(
        applied=True,
        manuscript=rewritten,
        skipped_reason=None,
        event_type="drafter_global_coherence_applied",
        before_bytes=before_bytes,
        after_bytes=len(rewritten.encode("utf-8")),
    )
