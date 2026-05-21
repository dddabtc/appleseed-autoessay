"""PR-J6: scout query expansion now anchors on title +
``research_kernel`` (the user-authored intake from PR-C0). These tests
pin the prompt-payload shape, the ``_kernel_query_payload`` helper,
``_extract_keywords`` (jieba CJK + Latin regex), the
``_register_scout_title_overlap_hook`` rejection + corrective-message
contract, and the kernel-first ``_fallback_queries`` ordering.

Codex round-1 amendments folded:
  * 2.1 — title + kernel co-primary anchors; domain templates auxiliary.
  * 2.2 — long string fields capped before prompt insertion.
  * 2.3 — jieba for CJK with bigram fallback; hook annotations include
    ``message`` + ``errors`` keys (the harness's corrective-suffix
    retry only extracts those keys).
  * 2.4 — kernel-first fallback ordering before domain templates.
  * 6 — gate is a structural guardrail, not a semantic relevance
    proof; default ratio is bounded ``0.25..1.0`` (env override).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from autoessay.agents.scout import (
    ScoutQuerySet,
    _extract_keywords,
    _fallback_queries,
    _kernel_query_payload,
    _query_object_prompt,
    _register_scout_title_overlap_hook,
)
from autoessay.config import get_settings
from autoessay.harness import (
    AuditVerdict,
    HookContext,
    HookRegistry,
    LLMCallResponse,
    ValidationResult,
)

# ----------------------------------------------------------------------
# _kernel_query_payload — clamps + opaque-blob safety
# ----------------------------------------------------------------------


def test_kernel_payload_extracts_known_string_fields() -> None:
    out = _kernel_query_payload(
        {
            "kernel_schema_version": 1,
            "tentative_question": "How did Jiangnan literati circulate?",
            "observed_puzzle": "Cross-field mobility durable under Qing reform.",
            "scope": "1890-1911 Jiangnan",
            "method_preference": "archival + prosopography",
            "theory_preference": "Bourdieu / Polanyi",
            "rogue_unknown_field": "ignored",
        }
    )
    assert out == {
        "tentative_question": "How did Jiangnan literati circulate?",
        "observed_puzzle": "Cross-field mobility durable under Qing reform.",
        "scope": "1890-1911 Jiangnan",
        "method_preference": "archival + prosopography",
        "theory_preference": "Bourdieu / Polanyi",
    }


def test_kernel_payload_handles_missing_or_empty_kernel() -> None:
    assert _kernel_query_payload(None) == {}
    assert _kernel_query_payload({}) == {}
    assert _kernel_query_payload({"tentative_question": "  "}) == {}
    assert _kernel_query_payload({"tentative_question": None}) == {}


def test_kernel_payload_clamps_long_observed_puzzle() -> None:
    huge = "x" * 5000
    out = _kernel_query_payload({"observed_puzzle": huge})
    assert "observed_puzzle" in out
    assert len(out["observed_puzzle"]) == 500


def test_kernel_payload_handles_non_mapping_gracefully() -> None:
    assert _kernel_query_payload(["not a mapping"]) == {}  # type: ignore[arg-type]
    assert _kernel_query_payload("nonsense") == {}  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# _extract_keywords — jieba for CJK + Latin regex + stopwords
# ----------------------------------------------------------------------


def test_extract_keywords_latin_drops_stopwords_and_singles() -> None:
    out = _extract_keywords("How did the Jiangnan literati circulate?")
    assert "jiangnan" in out
    assert "literati" in out
    assert "circulate" in out
    assert "the" not in out
    assert "did" not in out


def test_extract_keywords_chinese_via_jieba_or_bigram() -> None:
    """Chinese-only text should yield meaningful tokens. jieba is the
    preferred path; bigram fallback runs only when jieba fails to
    import (defensive — backend pyproject pins jieba)."""
    out = _extract_keywords("明清江南棉布业市场结构演变")
    # We don't assert on exact tokens because jieba's segmentation is
    # version-dependent. We do require ≥3 tokens of length ≥2 — this
    # rules out the all-stopwords-empty-set failure mode.
    assert len([t for t in out if len(t) >= 2]) >= 3, sorted(out)


def test_extract_keywords_mixed_script() -> None:
    """Mixed-script titles produce both CJK tokens and Latin tokens."""
    out = _extract_keywords("Bourdieu in 江南 literati networks")
    assert "bourdieu" in out
    assert "literati" in out
    assert "networks" in out
    # At least one CJK fragment
    assert any("江" in t for t in out), sorted(out)


def test_extract_keywords_blank_or_invalid_returns_empty() -> None:
    assert _extract_keywords("") == set()
    assert _extract_keywords("   ") == set()
    assert _extract_keywords(None) == set()  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# _query_object_prompt — payload shape
# ----------------------------------------------------------------------


def test_prompt_payload_contains_title_and_kernel_at_top() -> None:
    domain_data: dict[str, Any] = {
        "id": "financial_history",
        "search": {
            "default_query_terms": ["monetary policy", "central banking"],
            "exclusion_terms": ["fiction"],
        },
    }
    research_kernel = {
        "tentative_question": "How did Jiangnan cotton trade evolve?",
    }
    proposal = {
        "research_question": "Generic financial history question",
        "preliminary_keywords": ["finance"],
    }
    prompt = _query_object_prompt(
        "明清江南棉布业市场结构",
        domain_data,
        proposal,
        research_kernel,
    )
    # Sanity: instruction headers + payload presence.
    assert "Anchor the queries on" in prompt
    assert "highest priority" in prompt
    # Payload JSON contains kernel + renamed-domain-default key.
    assert '"title"' in prompt
    assert '"research_kernel"' in prompt
    assert '"domain_default_query_terms_for_inspiration_only"' in prompt
    # The bare ``"default_query_terms"`` key SHOULD NOT appear at top
    # level — it has been renamed to make the demotion explicit.
    # JSON dump is on its own line at the end (preceded by \n)
    last_newline = prompt.rfind("\n")
    parsed = json.loads(prompt[last_newline + 1 :])
    assert "default_query_terms" not in parsed
    assert parsed["title"] == "明清江南棉布业市场结构"
    assert parsed["research_kernel"]["tentative_question"] == (
        "How did Jiangnan cotton trade evolve?"
    )
    assert parsed["domain_default_query_terms_for_inspiration_only"] == [
        "monetary policy",
        "central banking",
    ]


def test_prompt_payload_handles_missing_kernel() -> None:
    prompt = _query_object_prompt(
        "Topic",
        {"id": "financial_history", "search": {}},
        proposal=None,
        research_kernel=None,
    )
    # JSON dump is on its own line at the end (preceded by \n)
    last_newline2 = prompt.rfind("\n")
    parsed = json.loads(prompt[last_newline2 + 1 :])
    assert parsed["research_kernel"] == {}


def test_prompt_uses_json_object_schema_not_list() -> None:
    """Codex round-1 amendment 2.1: ``ScoutQuerySet`` requires
    ``{queries: [...], rationale: ...}``; the prompt must instruct the
    LLM to return a JSON OBJECT, not a list[str]."""
    prompt = _query_object_prompt("Topic", {"search": {}}, None, None)
    assert "JSON object" in prompt
    assert "queries" in prompt
    assert "rationale" in prompt


# ----------------------------------------------------------------------
# _fallback_queries — kernel-first ordering
# ----------------------------------------------------------------------


def test_fallback_queries_kernel_first_then_proposal_then_domain() -> None:
    """Codex round-1 amendment 2.4: ``research_kernel.tentative_question``
    is the FIRST candidate; domain templates come last."""
    out = _fallback_queries(
        "Topic",
        domain_data={
            "id": "financial_history",
            "search": {"default_query_terms": ["monetary policy"]},
            "search_sources": [
                {
                    "id": "openalex",
                    "enabled": True,
                    "query_templates": ["{topic} financial history"],
                }
            ],
        },
        proposal={
            "research_question": "Proposal Q",
            "preliminary_keywords": ["finance"],
        },
        research_kernel={
            "tentative_question": "Kernel Q",
            "observed_puzzle": "Kernel P",
        },
    )
    assert out, out
    # Kernel question must precede proposal question and the bare topic.
    kernel_idx = out.index("Kernel Q")
    proposal_idx = out.index("Proposal Q")
    assert kernel_idx < proposal_idx, out


def test_fallback_queries_without_kernel_keeps_proposal_path() -> None:
    """No kernel → fallback degrades to the prior behaviour with
    proposal_question + topic + domain templates (backward compat)."""
    out = _fallback_queries(
        "Topic",
        domain_data={
            "id": "financial_history",
            "search": {"default_query_terms": ["monetary policy"]},
            "search_sources": [],
        },
        proposal={"research_question": "Proposal Q"},
        research_kernel=None,
    )
    # First candidate should be the proposal question (kernel skipped).
    assert out[0] == "Proposal Q"


# ----------------------------------------------------------------------
# _register_scout_title_overlap_hook — rejects under-anchored responses
# ----------------------------------------------------------------------


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
        project_title="Jiangnan literati circulation",
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


def test_overlap_hook_accepts_anchored_response() -> None:
    """OR-fallback path (kernel-less; only title bucket populated).
    PR-J8 tightened the both-buckets case to entity+concept AND-gate;
    this test now explicitly covers the kernel=None degrade-open
    behavior (the AND-gate cases live in
    test_scout_entity_concept_anchor.py)."""
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="Jiangnan literati circulation",
        research_kernel=None,
        ratio=0.5,
    )
    parsed = ScoutQuerySet(
        queries=[
            "Jiangnan literati patron-client networks",
            "literati cross-field mobility late Qing",
            "Jiangnan reform circles 1890",
        ],
        rationale="title-only anchored (OR-fallback)",
    )
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    assert result.verdict is None or result.verdict == AuditVerdict.ACCEPTED, (
        result.verdict,
        result.annotations,
    )


def test_overlap_hook_rejects_under_anchored_response_with_corrective_message() -> None:
    """Codex round-1 amendment 2.3: rejection annotations MUST include
    both ``message`` and ``errors`` so ``run_llm_step``'s corrective-
    suffix retry has actionable feedback."""
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="Jiangnan literati circulation",
        research_kernel={"tentative_question": "How did Jiangnan literati circulate?"},
        ratio=0.5,
    )
    # All 3 queries contain "monetary policy" / domain default — none
    # share keywords with title or kernel.
    parsed = ScoutQuerySet(
        queries=[
            "monetary policy 18th century",
            "central banking failure",
            "credit crunch 1772",
        ],
        rationale="domain templates only",
    )
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    assert result.verdict == AuditVerdict.REJECTED_SCHEMA_VIOLATION, result.annotations
    annotations = result.annotations.get("scout_title_overlap")
    assert annotations is not None, result.annotations
    assert "message" in annotations, annotations
    assert "errors" in annotations, annotations
    assert (
        "queries" in annotations["message"].lower() and "title" in annotations["message"].lower()
    ), annotations["message"]
    assert annotations["anchored_count"] == 0
    assert annotations["total_count"] == 3


def test_overlap_hook_skips_when_anchor_keywords_empty() -> None:
    """Empty title + kernel → don't reject every response (degrade
    open). The hook simply isn't registered in this case."""
    hooks = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks,
        title="",
        research_kernel=None,
        ratio=0.5,
    )
    # No hook registered ⇒ the empty parsed list passes through.
    parsed = ScoutQuerySet(queries=["any query"], rationale="x")
    result = hooks.run_post_llm(_ctx(), _resp(parsed))
    assert result.annotations == {}, result.annotations


def test_overlap_hook_does_not_leak_across_runs() -> None:
    """Codex round-1 amendment: the per-call HookRegistry pattern
    (in ``_expand_queries_via_harness``) prevents title/kernel leakage.
    This test pins that a fresh registry is the contract — registering
    twice with different titles produces two independent hooks; on a
    fresh registry per call neither leaks into the other.
    """
    hooks_a = HookRegistry()
    hooks_b = HookRegistry()
    _register_scout_title_overlap_hook(
        hooks_a,
        title="Jiangnan literati circulation",
        research_kernel=None,
        ratio=0.5,
    )
    _register_scout_title_overlap_hook(
        hooks_b,
        title="Monetary policy in early modern Europe",
        research_kernel=None,
        ratio=0.5,
    )
    # Queries that match A's title but not B's.
    parsed = ScoutQuerySet(
        queries=[
            "Jiangnan literati patron networks",
            "literati cross-field mobility",
            "Jiangnan reform circles",
        ],
        rationale="A-anchored",
    )
    result_a = hooks_a.run_post_llm(_ctx("run_a"), _resp(parsed))
    assert result_a.verdict is None or result_a.verdict == AuditVerdict.ACCEPTED, (
        result_a.annotations
    )
    result_b = hooks_b.run_post_llm(_ctx("run_b"), _resp(parsed))
    # Same queries should be REJECTED on hook B (different anchor).
    assert result_b.verdict == AuditVerdict.REJECTED_SCHEMA_VIOLATION, result_b.annotations


# ----------------------------------------------------------------------
# Settings ratio bounds
# ----------------------------------------------------------------------


def test_scout_title_anchor_ratio_default_is_half() -> None:
    get_settings.cache_clear()
    assert get_settings().scout_title_anchor_ratio == 0.5


def test_scout_title_anchor_ratio_rejects_out_of_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_TITLE_ANCHOR_RATIO", "0.1")
    get_settings.cache_clear()
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        # Re-instantiate to trigger field validators (Settings is
        # frozen by lru_cache; cache_clear forces a fresh build).
        from autoessay.config import Settings

        Settings()  # type: ignore[call-arg]


def test_scout_title_anchor_ratio_accepts_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_TITLE_ANCHOR_RATIO", "0.75")
    get_settings.cache_clear()
    assert get_settings().scout_title_anchor_ratio == 0.75


# Cleanup so other tests in the same session see the default
@pytest.fixture(autouse=True)
def _reset_ratio_after_test(monkeypatch: pytest.MonkeyPatch) -> None:
    yield
    monkeypatch.delenv("AUTOESSAY_SCOUT_TITLE_ANCHOR_RATIO", raising=False)
    get_settings.cache_clear()
