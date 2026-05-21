"""PR-263d — shadow_baseline wiring into scout (W4 best-effort).

Verifies the contract codex round-3 settled:
- scout calls ``run_shadow_baseline`` at start
- success → ``persist_shadow_baseline`` + ``shadow_baseline_done``
  event
- failure → ``shadow_baseline_failed`` event, scout continues
- already-on-disk → skipped (idempotent on retries)
- 1 retry on parse failure / exception

Tests use stub-mode (default) so no real LLM call. Failure cases
patch ``run_shadow_baseline`` to raise.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import seed_project

from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


@pytest.fixture
def scout_helper_args(tmp_path: Path):
    """Build the (run, project, session, run_dir) tuple
    ``_run_shadow_baseline_best_effort`` expects. Uses a SimpleNamespace-
    style fake session so we can assert what got committed without
    spinning up a real DB."""
    from types import SimpleNamespace

    class _FakeSession:
        def __init__(self) -> None:
            self.events: list[dict[str, object]] = []
            self.commits = 0

        def commit(self) -> None:
            self.commits += 1

        def add(self, _obj) -> None:
            pass

        def flush(self) -> None:
            pass

    session = _FakeSession()
    run = SimpleNamespace(
        id="run_test",
        project_id="proj_test",
        run_dir=str(tmp_path),
        research_kernel_json={
            "scope": "以 19 世纪后期江南刊本为限。",
            "observed_puzzle": "puzzle.",
            "tentative_question": "question?",
        },
    )
    project = SimpleNamespace(
        id="proj_test",
        title="测试项目",
        user_id="user_test",
    )
    return run, project, session, tmp_path


def _patch_append_event(captured: list[tuple[str, dict[str, object]]]):
    """Patch ``append_event`` to capture events without writing to a
    DB. Returns a context manager."""
    from autoessay.agents import scout as scout_module

    def fake_append_event(_session, _run, event_type, payload):
        captured.append((event_type, payload))

    return patch.object(scout_module, "append_event", side_effect=fake_append_event)


def _shadow_verified_source() -> NormalizedSource:
    return NormalizedSource(
        source_id="crossref:10.1234/shadow",
        title="Shadow Verified Test Project Financial History Source",
        authors=["Barry Eichengreen"],
        year=1996,
        venue="Economic History Review",
        doi="10.1234/shadow",
        url="https://doi.org/10.1234/shadow",
        pdf_url=None,
        abstract="A verified abstract for the Test project financial history synthesis.",
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
        provenance="llm_canon",
        canonical_bucket="frontier",
        canonical_rationale="shadow baseline reference_candidate; verified before merge",
        verified_by="crossref",
    )


def test_helper_emits_shadow_baseline_done_on_success(scout_helper_args) -> None:
    """In stub-mode (default), ``run_shadow_baseline`` returns the
    canned stub and the helper persists it + emits done event."""
    from autoessay.agents.scout import _run_shadow_baseline_best_effort
    from autoessay.agents.shadow_baseline import load_shadow_baseline

    run, project, session, run_dir = scout_helper_args
    captured: list[tuple[str, dict[str, object]]] = []
    with _patch_append_event(captured):
        _run_shadow_baseline_best_effort(
            run=run,
            project=project,
            session=session,
            run_dir=run_dir,
        )

    # Artifact persisted.
    assert load_shadow_baseline(run_dir) is not None
    # Done event emitted with shape codex Q2 specified.
    done_events = [e for e in captured if e[0] == "shadow_baseline_done"]
    assert len(done_events) == 1
    payload = done_events[0][1]
    assert payload["phase"] == "scout"
    assert payload["attempt"] == 1
    assert payload["manuscript_chars"] > 0
    assert payload["argument_map_entries"] >= 1
    assert session.commits >= 1


def test_run_scout_merges_shadow_enrichment_into_skim_candidates(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan A wiring: verified shadow-baseline references enter Scout's
    dedup pool before curator, while still going through normal
    classify_source verification semantics."""
    from autoessay.agents import scout as scout_module
    from autoessay.agents.scout import run_scout

    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CANONICAL_MINING_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_SHADOW_BASELINE_STUB", "1")
    get_settings.cache_clear()

    async def fake_enrich(**_kwargs):
        return [_shadow_verified_source()], []

    monkeypatch.setattr(
        scout_module,
        "_enrich_shadow_baseline_sources_best_effort",
        fake_enrich,
    )
    run_id = "run_shadow_enrichment_merge"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()

        summary = run_scout(run_id, session)

    assert summary["state"] == "USER_SEARCH_REVIEW"
    rows = [
        json.loads(line)
        for line in (run_dir / "discovery" / "skim_candidates.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    shadow_rows = [row for row in rows if row["source_id"] == "crossref:10.1234/shadow"]
    assert len(shadow_rows) == 1
    assert shadow_rows[0]["provenance"] == "llm_canon"
    assert shadow_rows[0]["verified_by"] == "crossref"
    assert shadow_rows[0]["verification_status"] == "verified"


def test_enrichment_helper_emits_done_event_and_warnings(scout_helper_args) -> None:
    from autoessay.agents.scout import _enrich_shadow_baseline_sources_best_effort
    from autoessay.agents.shadow_baseline import (
        ReferenceCandidate,
        ShadowBaselineOutput,
        persist_shadow_baseline,
    )

    run, project, session, run_dir = scout_helper_args
    del project
    persist_shadow_baseline(
        run_dir,
        ShadowBaselineOutput(
            manuscript_markdown="## 摘要\n\ntest\n",
            reference_candidates=[
                ReferenceCandidate(
                    author="Barry Eichengreen",
                    year="1996",
                    title="Globalizing Capital",
                )
            ],
        ),
    )
    captured: list[tuple[str, dict[str, object]]] = []

    async def fake_enrich(*_args, **_kwargs):
        return [_shadow_verified_source()], [{"work_title": "Dropped Work", "reason": "no_match"}]

    with (
        _patch_append_event(captured),
        patch(
            "autoessay.agents.source_enrichment.enrich_with_shadow_baseline",
            side_effect=fake_enrich,
        ),
    ):
        verified, warnings = asyncio.run(
            _enrich_shadow_baseline_sources_best_effort(
                run=run,
                session=session,
                run_dir=run_dir,
            )
        )

    assert [source.source_id for source in verified] == ["crossref:10.1234/shadow"]
    assert warnings[0]["source_id"] == "shadow_baseline_ref:Dropped Work"
    done_events = [event for event in captured if event[0] == "shadow_baseline_enrichment_done"]
    assert len(done_events) == 1
    assert done_events[0][1]["verified_count"] == 1
    assert done_events[0][1]["dropped_count"] == 1


def test_enrichment_helper_failure_is_non_fatal(scout_helper_args) -> None:
    from autoessay.agents.scout import _enrich_shadow_baseline_sources_best_effort
    from autoessay.agents.shadow_baseline import _stub_output, persist_shadow_baseline

    run, project, session, run_dir = scout_helper_args
    del project
    persist_shadow_baseline(run_dir, _stub_output())
    captured: list[tuple[str, dict[str, object]]] = []

    async def fail_enrich(*_args, **_kwargs):
        raise RuntimeError("simulated verifier outage")

    with (
        _patch_append_event(captured),
        patch(
            "autoessay.agents.source_enrichment.enrich_with_shadow_baseline",
            side_effect=fail_enrich,
        ),
    ):
        verified, warnings = asyncio.run(
            _enrich_shadow_baseline_sources_best_effort(
                run=run,
                session=session,
                run_dir=run_dir,
            )
        )

    assert verified == []
    assert warnings[0]["source_id"] == "shadow_baseline_enrichment"
    failed_events = [event for event in captured if event[0] == "shadow_baseline_enrichment_failed"]
    assert len(failed_events) == 1
    assert failed_events[0][1]["error_class"] == "RuntimeError"


def test_helper_skips_when_artifact_already_on_disk(scout_helper_args) -> None:
    """Idempotency: if a previous run already produced a
    shadow_baseline artifact (e.g. scout retry), don't burn another
    LLM call."""
    from autoessay.agents.scout import _run_shadow_baseline_best_effort
    from autoessay.agents.shadow_baseline import (
        _stub_output,
        persist_shadow_baseline,
    )

    run, project, session, run_dir = scout_helper_args
    # Pre-populate the artifact.
    persist_shadow_baseline(run_dir, _stub_output())

    captured: list[tuple[str, dict[str, object]]] = []
    with _patch_append_event(captured):
        _run_shadow_baseline_best_effort(
            run=run,
            project=project,
            session=session,
            run_dir=run_dir,
        )

    # No event emitted because the helper returned early.
    assert captured == []


def test_helper_emits_failed_event_after_retries_exhausted(scout_helper_args) -> None:
    """When ``run_shadow_baseline`` raises twice (initial + 1 retry),
    the helper emits ``shadow_baseline_failed`` and lets the caller
    proceed. Codex Q2: failure is non-fatal."""
    from autoessay.agents.scout import _run_shadow_baseline_best_effort

    run, project, session, run_dir = scout_helper_args
    captured: list[tuple[str, dict[str, object]]] = []

    def always_raise(**_kwargs):
        raise RuntimeError("simulated LLM gateway timeout")

    with (
        _patch_append_event(captured),
        patch(
            "autoessay.agents.shadow_baseline.run_shadow_baseline",
            side_effect=always_raise,
        ),
    ):
        # Helper must not raise.
        _run_shadow_baseline_best_effort(
            run=run,
            project=project,
            session=session,
            run_dir=run_dir,
        )

    failed_events = [e for e in captured if e[0] == "shadow_baseline_failed"]
    assert len(failed_events) == 1
    payload = failed_events[0][1]
    assert payload["phase"] == "scout"
    assert payload["attempts"] == 2
    assert payload["error_class"] == "RuntimeError"
    assert "timeout" in payload["error_message"]
    # No done event.
    done_events = [e for e in captured if e[0] == "shadow_baseline_done"]
    assert done_events == []


def test_helper_retries_once_on_first_failure(scout_helper_args) -> None:
    """1 retry policy: if the initial call fails but the retry
    succeeds, the helper still emits done event with attempt=2."""
    from autoessay.agents.scout import _run_shadow_baseline_best_effort
    from autoessay.agents.shadow_baseline import _stub_output

    run, project, session, run_dir = scout_helper_args
    captured: list[tuple[str, dict[str, object]]] = []
    call_count = {"n": 0}

    def fail_then_succeed(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient failure")
        return _stub_output()

    with (
        _patch_append_event(captured),
        patch(
            "autoessay.agents.shadow_baseline.run_shadow_baseline",
            side_effect=fail_then_succeed,
        ),
    ):
        _run_shadow_baseline_best_effort(
            run=run,
            project=project,
            session=session,
            run_dir=run_dir,
        )

    done_events = [e for e in captured if e[0] == "shadow_baseline_done"]
    assert len(done_events) == 1
    assert done_events[0][1]["attempt"] == 2
    assert call_count["n"] == 2


def test_helper_emits_failed_when_shadow_baseline_returns_none(
    scout_helper_args,
) -> None:
    """``run_shadow_baseline`` can return None (parse failure inside).
    The helper treats that the same as an exception — retry once,
    then emit failed event."""
    from autoessay.agents.scout import _run_shadow_baseline_best_effort

    run, project, session, run_dir = scout_helper_args
    captured: list[tuple[str, dict[str, object]]] = []

    with (
        _patch_append_event(captured),
        patch(
            "autoessay.agents.shadow_baseline.run_shadow_baseline",
            return_value=None,
        ),
    ):
        _run_shadow_baseline_best_effort(
            run=run,
            project=project,
            session=session,
            run_dir=run_dir,
        )

    failed_events = [e for e in captured if e[0] == "shadow_baseline_failed"]
    assert len(failed_events) == 1
    assert failed_events[0][1]["error_class"] == "RuntimeError"
    assert "returned None" in failed_events[0][1]["error_message"]
