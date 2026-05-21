"""PR-I2 retro fix #1 — curator 3-tier fallback chain tests.

Verifies that ``_score_relevance_batches_via_harness`` honors codex
round-1 A2's three-tier fallback:

  Tier 1 — 4-axis prompt (prod default)
  Tier 2 — legacy single-axis prompt (stub or 4-axis fail)
  Tier 3 — recency-only (Tier 2 also fails)

The original J9b cut went directly Tier 1 → Tier 3 on failure, and
stub mode sent the expensive 4-axis prompt then dropped axes. PR-I2
fixes both gaps.
"""

from __future__ import annotations

from unittest.mock import patch

from autoessay.agents.curator import (
    _BatchOutcome,
    _run_legacy_single_axis_batch,
)
from autoessay.clients.common import AccessStatus, NormalizedSource


def _src(source_id: str) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Title {source_id}",
        authors=["Author"],
        year=2020,
        venue="Venue",
        doi=None,
        url=None,
        pdf_url=None,
        abstract="Abstract",
        source_client="crossref",
        access_status=AccessStatus.OPEN,
        license=None,
        risk_flags=[],
    )


class _FakeRun:
    id = "run_3tier_test"
    run_dir = "/tmp/run_3tier_test"
    domain_version = "0.0"


class _FakeProject:
    user_id = "user_test"
    domain_id = "general_academic"
    title = "3-tier test"
    language = "en"


class _FakeSession:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []


class _FakeHooks:
    pass


class _FakeResponse:
    def __init__(self, text: str, attempt: int = 1) -> None:
        self.text = text
        self.attempt = attempt
        self.parsed = None


def test_legacy_single_axis_batch_parses_simple_format(monkeypatch) -> None:
    """Legacy path returns ``{scores: [{source_id, relevance_score}]}``;
    `_run_legacy_single_axis_batch` parses it via
    ``_parse_relevance_response`` and returns relevance_scores only —
    no rerank_axes (so ``_rank_sources`` runs 100% legacy formula)."""
    legacy_json = '{"scores": [{"source_id": "src_a", "relevance_score": 0.7}]}'

    async def fake_run_llm_step(*args, **kwargs):  # noqa: ARG001
        return _FakeResponse(legacy_json)

    with patch("autoessay.agents.curator.run_llm_step", fake_run_llm_step):
        outcome = _run_legacy_single_axis_batch(
            topic="test",
            batch=[_src("src_a")],
            domain_data={"id": "general_academic"},
            run=_FakeRun(),
            project=_FakeProject(),
            session=_FakeSession(),
            hooks=_FakeHooks(),
            instructions_override=None,
            batch_index=1,
        )
    assert outcome.relevance_scores == {"src_a": 0.7}
    # Legacy path produces NO rerank axes — _rank_sources falls
    # through to the legacy formula 100%, no blend, no hard cap.
    assert outcome.rerank_axes == {}
    assert outcome.rerank_rationales == {}
    assert outcome.rerank_retain == {}
    assert outcome.recency_only is False
    assert outcome.fell_back is False


def test_legacy_single_axis_batch_recency_when_unparseable(monkeypatch) -> None:
    """Tier 2 transport / parse failure → ``recency_only=True``
    forces Tier 3 in the caller."""

    async def fake_run_llm_step(*args, **kwargs):  # noqa: ARG001
        return _FakeResponse("not-json-at-all")

    with patch("autoessay.agents.curator.run_llm_step", fake_run_llm_step):
        outcome = _run_legacy_single_axis_batch(
            topic="test",
            batch=[_src("src_a")],
            domain_data={"id": "general_academic"},
            run=_FakeRun(),
            project=_FakeProject(),
            session=_FakeSession(),
            hooks=_FakeHooks(),
            instructions_override=None,
            batch_index=1,
        )
    assert outcome.recency_only is True
    assert outcome.relevance_scores == {}
    assert any("legacy single-axis" in str(w.get("message", "")) for w in outcome.warnings)


def test_legacy_single_axis_batch_recency_on_transport_error(monkeypatch) -> None:
    """Tier 2 transport error (LLM gateway down) → ``recency_only=True``."""

    async def failing_llm_step(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("provider 503")

    with patch("autoessay.agents.curator.run_llm_step", failing_llm_step):
        outcome = _run_legacy_single_axis_batch(
            topic="test",
            batch=[_src("src_a")],
            domain_data={"id": "general_academic"},
            run=_FakeRun(),
            project=_FakeProject(),
            session=_FakeSession(),
            hooks=_FakeHooks(),
            instructions_override=None,
            batch_index=1,
        )
    assert outcome.recency_only is True


def test_batch_outcome_default_state() -> None:
    outcome = _BatchOutcome()
    assert outcome.relevance_scores == {}
    assert outcome.rerank_axes == {}
    assert outcome.fell_back is False
    assert outcome.recency_only is False
    assert outcome.fallback_reason is None
