"""PR-J8: tighten scout title-overlap hook to the entity + concept
gate (codex round-1 amendment 8 from the J9 design review).

Pins the new behavior:
  * Both title and kernel populated → AND-gate (entity + concept)
  * Only one populated → OR-fallback (PR-J6 behavior, degrade open)
  * Neither populated → no hook registered
  * ``_gather_kernel_concept_keywords`` pulls from 5 kernel fields
  * RCEP-style failure (entity-only query passing under PR-J6) now
    rejected by the AND-gate

Also pins the curator ranking prompt's new ``research_kernel`` field
(part of the same J8 codex amendment — pass kernel/scope into curator
ranking as immediate stopgap so the Curator phase doesn't undo
J6/J7's anchoring with its own topic-only relevance scoring).
"""

from __future__ import annotations

import json

from autoessay.agents.scout import (
    ScoutQuerySet,
    _gather_kernel_concept_keywords,
    _register_scout_title_overlap_hook,
)
from autoessay.harness import (
    AuditVerdict,
    HookContext,
    HookRegistry,
    LLMCallResponse,
    ValidationResult,
)


def _ctx(run_id: str = "run_x") -> HookContext:
    return HookContext(
        run_id=run_id,
        phase="discovery",
        step_id="scout.query_expansion",
        user_id="u",
        attempt=1,
        prompt_template_id="scout.query_expansion.v1",
        prompt_filled="prompt body",
        prompt_hash="hash",
        project_title="韩国经济起飞",
        run_metadata={},
    )


def _resp(parsed: object) -> LLMCallResponse:
    return LLMCallResponse(
        content="{}",
        parsed=parsed,
        raw_content="{}",
        reasoning_text="",
        usage={},
        latency_ms=0,
        attempt=1,
        validation_result=ValidationResult(valid=True, parsed=parsed, errors=[]),
    )


# ----------------------------------------------------------------------
# _gather_kernel_concept_keywords — pulls from 5 fields
# ----------------------------------------------------------------------


def test_gather_concept_keywords_from_all_five_fields() -> None:
    out = _gather_kernel_concept_keywords(
        {
            "tentative_question": "How did dispositional structures shape literati?",
            "observed_puzzle": "Cross-field mobility durable under reform",
            "theory_preference": "Bourdieu / Polanyi",
            "method_preference": "archival prosopography",
            "scope": "1890-1911 Jiangnan",
        }
    )
    # Spot-check a few keywords from each field show up. We don't
    # depend on exact tokenization; jieba may split "Jiangnan" /
    # English regex extracts "literati" / etc.
    assert "dispositional" in out
    assert "literati" in out
    assert "mobility" in out
    assert "bourdieu" in out
    assert "polanyi" in out
    assert "prosopography" in out


def test_gather_concept_keywords_empty_for_missing_kernel() -> None:
    assert _gather_kernel_concept_keywords(None) == set()
    assert _gather_kernel_concept_keywords({}) == set()
    assert _gather_kernel_concept_keywords({"unknown_field": "x"}) == set()


def test_gather_concept_keywords_handles_non_mapping() -> None:
    assert _gather_kernel_concept_keywords(["not a mapping"]) == set()  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# AND-gate behavior (both buckets non-empty)
# ----------------------------------------------------------------------


def test_and_gate_accepts_query_with_entity_and_concept() -> None:
    """Realistic kernel: user filled all 5 fields (matching the
    2026-05-04 prod intake screenshot), so concept_keywords spans
    tentative_question + observed_puzzle + theory_preference +
    method_preference + scope. The AND-gate accepts queries that
    pair the title entity (韩国) with at least one of those
    concepts."""
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="韩国经济起飞",
        research_kernel={
            "tentative_question": "韩国经济起飞的根因是出口导向工业化吗",
            "observed_puzzle": "汉江奇迹的政治经济条件",
            "theory_preference": "国家主导发展, 产业政策, 制度变迁",
            "method_preference": "重化工业化, 历史制度分析, 比较政治经济",
            "scope": "1945-1990 韩国",
        },
        ratio=0.5,
    )
    parsed = ScoutQuerySet(
        queries=[
            "韩国 出口导向 1960年代",
            "韩国 产业政策 制度",
            "韩国 国家主导 工业化",
        ],
        rationale="entity + concept anchored",
    )
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    # All 3 queries contain 韩国 (entity from title) AND a concept
    # token from the kernel's theory/method fields.
    assert result.verdict is None or result.verdict == AuditVerdict.ACCEPTED, result.annotations


def test_and_gate_rejects_entity_only_query_rcep_style() -> None:
    """The exact RCEP-style failure: query contains the entity (韩国/韩)
    but no concept from the kernel. Under PR-J6 this passed (entity
    match alone counted as anchored); under PR-J8 it is rejected
    because there's no concept anchor."""
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="韩国经济起飞",
        research_kernel={
            "tentative_question": "韩国经济起飞的根因是出口导向工业化吗",
            "scope": "1945-1990 韩国",
        },
        ratio=0.5,
    )
    parsed = ScoutQuerySet(
        queries=[
            # Entity-only — would pass PR-J6 (contains 韩国) but no
            # kernel concept term (no 出口/工业化/制度/产业 etc).
            "韩国 RCEP 机电产品",
            "韩国 对外贸易 进口",
            "韩国 营商环境 投资",
        ],
        rationale="entity only — RCEP-style off-topic",
    )
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    assert result.verdict == AuditVerdict.REJECTED_SCHEMA_VIOLATION, result.annotations
    annotations = result.annotations.get("scout_title_overlap")
    assert annotations is not None
    assert annotations["gate_mode"] == "entity_and_concept"
    # Corrective message must mention BOTH "entity" and "concept"
    msg = annotations["message"].lower()
    assert "entity" in msg and "concept" in msg


def test_and_gate_rejects_concept_only_query() -> None:
    """Symmetric case: queries contain concept terms but not the
    entity. Real-world: if scout went off topic and proposed
    queries about the kernel's concepts in a different country,
    we want to catch that too."""
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="韩国经济起飞",
        research_kernel={
            "tentative_question": "韩国经济起飞的根因是出口导向工业化吗",
        },
        ratio=0.5,
    )
    parsed = ScoutQuerySet(
        queries=[
            "日本 出口导向 工业化",
            "台湾 产业政策 国家主导",
            "新加坡 出口加工 制度",
        ],
        rationale="concept anchored, wrong country",
    )
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    assert result.verdict == AuditVerdict.REJECTED_SCHEMA_VIOLATION, result.annotations


# ----------------------------------------------------------------------
# OR-fallback behavior (one bucket empty)
# ----------------------------------------------------------------------


def test_or_fallback_when_kernel_empty_keeps_title_anchor() -> None:
    """Old run / kernel-less project: only title bucket populated.
    Hook degrades to the PR-J6 OR-anchor (any keyword from title)
    so we don't reject every response when the user hasn't filled
    out the kernel intake."""
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="Jiangnan literati circulation",
        research_kernel=None,
        ratio=0.5,
    )
    parsed = ScoutQuerySet(
        queries=[
            "Jiangnan literati patron networks",
            "literati cross-field mobility",
            "Jiangnan reform circles 1890",
        ],
        rationale="title-anchored",
    )
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    # OR-fallback: any title-keyword match counts. All 3 contain
    # jiangnan or literati → accepted.
    assert result.verdict is None or result.verdict == AuditVerdict.ACCEPTED


def test_or_fallback_when_title_empty_uses_concept_anchor() -> None:
    """Defensive: degenerate case where project.title is blank but
    kernel has content. OR-fallback keeps the kernel anchor live."""
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="",
        research_kernel={
            "tentative_question": "出口导向工业化与韩国经济起飞",
        },
        ratio=0.5,
    )
    parsed = ScoutQuerySet(
        queries=["出口导向 政策", "工业化 制度", "韩国 出口 1960"],
        rationale="concept-anchored",
    )
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    assert result.verdict is None or result.verdict == AuditVerdict.ACCEPTED


def test_no_hook_registered_when_both_empty() -> None:
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="",
        research_kernel=None,
        ratio=0.5,
    )
    parsed = ScoutQuerySet(queries=["any"], rationale="x")
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    assert result.annotations == {}, result.annotations


# ----------------------------------------------------------------------
# Curator ranking prompt — research_kernel injection (codex amendment 8)
# ----------------------------------------------------------------------


def test_curator_ranking_prompt_includes_research_kernel() -> None:
    from autoessay.agents.curator import _curator_ranking_prompt
    from autoessay.clients.common import NormalizedSource

    sources = [
        NormalizedSource(
            source_id="s1",
            title="Some study",
            authors=["a"],
            year=2020,
            venue="v",
            doi=None,
            url=None,
            pdf_url=None,
            abstract="abstract",
            source_client="openalex",
            access_status="open",
            license=None,
            risk_flags=[],
        ),
    ]
    prompt = _curator_ranking_prompt(
        "韩国经济起飞",
        sources,
        {"id": "economic_history"},
        suffix="",
        research_kernel={
            "tentative_question": "韩国经济起飞的根因",
            "scope": "1945-1990 韩国",
        },
    )
    # System-prompt-style guidance must mention research_kernel + scope.
    assert "research_kernel" in prompt
    assert "scope" in prompt.lower()
    # JSON payload must include research_kernel field with kernel content.
    json_start = prompt.rfind("{")
    while json_start >= 0:
        try:
            payload = json.loads(prompt[json_start:])
            break
        except json.JSONDecodeError:
            json_start = prompt.rfind("{", 0, json_start)
    assert isinstance(payload, dict)
    assert "research_kernel" in payload
    assert payload["research_kernel"]["tentative_question"] == "韩国经济起飞的根因"


def test_curator_ranking_prompt_handles_missing_kernel() -> None:
    """Backward compat: callers that don't pass research_kernel get
    an empty dict (degrade to topic-only anchoring; same contract as
    J6 / J7 fallback)."""
    from autoessay.agents.curator import _curator_ranking_prompt
    from autoessay.clients.common import NormalizedSource

    sources = [
        NormalizedSource(
            source_id="s1",
            title="t",
            authors=[],
            year=None,
            venue="",
            doi=None,
            url=None,
            pdf_url=None,
            abstract="",
            source_client="openalex",
            access_status="open",
            license=None,
            risk_flags=[],
        ),
    ]
    prompt = _curator_ranking_prompt(
        "Topic",
        sources,
        {},
        suffix="",
        research_kernel=None,
    )
    json_start = prompt.rfind("{")
    while json_start >= 0:
        try:
            payload = json.loads(prompt[json_start:])
            break
        except json.JSONDecodeError:
            json_start = prompt.rfind("{", 0, json_start)
    assert payload["research_kernel"] == {}
