"""PR-C2c step 3+4: ``_run_framework_lens_with_session`` via the LLM
enrichment path (``AUTOESSAY_FRAMEWORK_LENS_STUB=0``).

Mocks ``autoessay.harness.runner.LLMClient`` to make the harness
deterministic so we can pin all five behaviors:

1. LLM mode + valid signals → payload uses LLM signals (not stub)
2. LLM mode + transport failure (provider exception) → fallback to stub
   + ``framework_lens_stub_fallback`` event (reason_kind=transport_or_provider)
3. LLM mode + persistent schema violation → fallback to stub +
   ``framework_lens_stub_fallback`` event (reason_kind=schema_or_integrity)
4. LLM mode + integrity rejection (banned ``lens_name``) → harness retries;
   if persistent → SchemaViolationError → fallback + event
5. theory_article + eligible source + LLM returns empty signals →
   integrity-hook reject → corrective retry → if persistent → fallback
   + event (codex round-1 amendment E + I)
6. theory_article + 0 eligible source → FAILED_FIXABLE BEFORE LLM
   (existing path; covered in test_framework_lens.py — not duplicated here)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.framework_lens import run_framework_lens
from autoessay.config import get_settings
from autoessay.framework_lens import (
    FRAMEWORK_LENS_ARTIFACT_PATH,
    _stub_signals,
)
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory

# ----------------------------------------------------------------------
# Fake LLMs (one per behavior)
# ----------------------------------------------------------------------


def _valid_signal_payload(source_id: str = "lens_bourdieu") -> str:
    return json.dumps(
        {
            "signals": [
                {
                    "lens_name": "Bourdieu: habitus",
                    "key_concepts": ["habitus", "field"],
                    "source_id": source_id,
                    "applicability_to_kernel": (
                        "Habitus explains how dispositions reproduce within "
                        "the kernel's stated puzzle about elite circulation."
                    ),
                }
            ]
        }
    )


class _ValidSignalsLLM:
    calls = 0

    async def chat_completion(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        _ValidSignalsLLM.calls += 1
        return {
            "content": _valid_signal_payload(),
            "raw_content": _valid_signal_payload(),
            "usage": {"total_tokens": 1},
        }

    async def aclose(self) -> None:
        return None


class _TransportFailureLLM:
    calls = 0

    async def chat_completion(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        _TransportFailureLLM.calls += 1
        raise RuntimeError("simulated provider 503")

    async def aclose(self) -> None:
        return None


class _PersistentSchemaViolationLLM:
    """Always returns syntactically-broken JSON. Forces
    SchemaViolationError after the harness's corrective retries
    are exhausted."""

    calls = 0

    async def chat_completion(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        _PersistentSchemaViolationLLM.calls += 1
        return {
            "content": "not-json-at-all",
            "raw_content": "not-json-at-all",
            "usage": {"total_tokens": 1},
        }

    async def aclose(self) -> None:
        return None


class _BannedLensNameLLM:
    """Returns valid Pydantic shape but banned lens_name. Integrity
    hook should reject; on persistent retry exhaustion the runner
    catches SchemaViolationError and falls back."""

    calls = 0

    async def chat_completion(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        _BannedLensNameLLM.calls += 1
        payload = json.dumps(
            {
                "signals": [
                    {
                        "lens_name": "Default Lens",
                        "key_concepts": ["x"],
                        "source_id": "lens_bourdieu",
                        "applicability_to_kernel": (
                            "Stub-shaped placeholder applicability to kernel "
                            "for the persistent banned-name test case."
                        ),
                    }
                ]
            }
        )
        return {
            "content": payload,
            "raw_content": payload,
            "usage": {"total_tokens": 1},
        }

    async def aclose(self) -> None:
        return None


class _EmptySignalsLLM:
    """Returns valid Pydantic shape with empty signals list. Integrity
    hook accepts for case_analysis but rejects for theory_article (with
    eligible inputs); on persistent retry exhaustion → fallback."""

    calls = 0

    async def chat_completion(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        _EmptySignalsLLM.calls += 1
        payload = json.dumps({"signals": []})
        return {
            "content": payload,
            "raw_content": payload,
            "usage": {"total_tokens": 1},
        }

    async def aclose(self) -> None:
        return None


# ----------------------------------------------------------------------
# Fixture: prepare a run at USER_FIELD_REVIEW with a lens-tagged source
# ----------------------------------------------------------------------


def _seed_lens_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    paper_mode: str = "case_analysis",
    include_lens_source: bool = True,
    research_kernel: dict[str, object] | None = None,
) -> tuple[str, Path]:
    run_id = f"run_lens_llm_{paper_mode}_{include_lens_source}"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_FIELD_REVIEW",
        domain_id="financial_history",
    )
    (run_dir / "sources").mkdir(parents=True, exist_ok=True)
    shortlist: list[dict[str, object]] = [
        {
            "source_id": "secondary_argument_1",
            "title": "Some secondary argument source",
            "research_role": "secondary_argument",
        }
    ]
    if include_lens_source:
        shortlist.append(
            {
                "source_id": "lens_bourdieu",
                "title": "Bourdieu: Outline of a Theory of Practice",
                "venue": "Cambridge UP",
                "research_role": "theoretical_lens",
                "abstract": (
                    "Bourdieu's framework articulates habitus and field "
                    "as dispositional structures shaping practice."
                ),
            }
        )
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps(shortlist, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "synthesis").mkdir(parents=True, exist_ok=True)
    # Seed a synthesizer.json with a theoretical_lens_track entry so
    # the prompt's claim summaries are non-empty.
    (run_dir / "synthesis" / "synthesizer.json").write_text(
        json.dumps(
            {
                "theoretical_lens_track": [
                    {
                        "claim_id": "claim_1",
                        "source_id": "lens_bourdieu",
                        "text": (
                            "Habitus structures predispose actors toward field-specific strategies."
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with app_session() as session:
        seed_project(session)
        run = Run(
            id=run_id,
            project_id="proj_test",
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="USER_FIELD_REVIEW",
            paper_mode=paper_mode,
            research_kernel_json=research_kernel
            or {
                "kernel_schema_version": 1,
                "tentative_question": (
                    "How did dispositional structures shape Jiangnan literati "
                    "circulation across the late Qing transition?"
                ),
                "observed_puzzle": (
                    "Jiangnan literati moved between official and merchant "
                    "fields with surprising fluidity in 1890-1911."
                ),
                "scope": "Late-Qing Jiangnan, 1890-1911",
            },
            baseline_hash="t",
        )
        session.add(run)
        session.commit()
    return run_id, run_dir


def _read_events(app_session, run_id: str) -> list[RunEvent]:  # type: ignore[no-untyped-def]
    with app_session() as session:
        return list(
            session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at)
            )
        )


def _set_llm_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_FRAMEWORK_LENS_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    get_settings.cache_clear()


# ----------------------------------------------------------------------
# 1. LLM mode + valid signals → payload uses LLM signals (not stub)
# ----------------------------------------------------------------------


def test_llm_path_writes_llm_signals_not_stub(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _ValidSignalsLLM.calls = 0
    _set_llm_path_env(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", _ValidSignalsLLM)

    run_id, run_dir = _seed_lens_run(app_session, tmp_path)

    with app_session() as session:
        run_framework_lens(run_id, session)

    artifact = json.loads((run_dir / FRAMEWORK_LENS_ARTIFACT_PATH).read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 2
    assert artifact["paper_mode"] == "case_analysis"
    assert isinstance(artifact["signals"], list)
    assert len(artifact["signals"]) == 1
    sig = artifact["signals"][0]
    assert sig["lens_name"] == "Bourdieu: habitus"
    assert sig["key_concepts"] == ["habitus", "field"]
    assert sig["source_id"] == "lens_bourdieu"
    # The stub would have used the title as lens_name; LLM result differs.
    stub = _stub_signals(
        [
            {
                "source_id": "lens_bourdieu",
                "title": "Bourdieu: Outline of a Theory of Practice",
                "venue": "Cambridge UP",
                "research_role": "theoretical_lens",
            }
        ]
    )
    assert stub[0].lens_name != sig["lens_name"], (
        "LLM signal should differ from deterministic stub; otherwise the "
        "runner silently used the stub."
    )

    # No fallback event was written.
    events = _read_events(app_session, run_id)
    kinds = [e.event_type for e in events]
    assert "framework_lens_stub_fallback" not in kinds, kinds
    assert "phase_done" in kinds


# ----------------------------------------------------------------------
# 2. Transport failure → fallback to stub + event with reason_kind=
#    "transport_or_provider"
# ----------------------------------------------------------------------


def test_transport_failure_falls_back_with_provider_reason(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _TransportFailureLLM.calls = 0
    _set_llm_path_env(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", _TransportFailureLLM)

    run_id, run_dir = _seed_lens_run(app_session, tmp_path)

    with app_session() as session:
        run_framework_lens(run_id, session)

    artifact = json.loads((run_dir / FRAMEWORK_LENS_ARTIFACT_PATH).read_text(encoding="utf-8"))
    # Stub signal would name the source by title fragment.
    assert artifact["signals"][0]["source_id"] == "lens_bourdieu"
    assert "Bourdieu" in artifact["signals"][0]["lens_name"]

    events = _read_events(app_session, run_id)
    fallback_events = [e for e in events if e.event_type == "framework_lens_stub_fallback"]
    assert len(fallback_events) == 1, [e.event_type for e in events]
    payload = json.loads(fallback_events[0].payload or "{}")
    assert payload["reason_kind"] == "transport_or_provider"
    assert "503" in payload["reason_summary"]


# ----------------------------------------------------------------------
# 3. Persistent JSON parse failure → SchemaViolationError → fallback +
#    reason_kind="schema_or_integrity"
# ----------------------------------------------------------------------


def test_persistent_schema_violation_falls_back_with_schema_reason(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _PersistentSchemaViolationLLM.calls = 0
    _set_llm_path_env(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", _PersistentSchemaViolationLLM)

    run_id, run_dir = _seed_lens_run(app_session, tmp_path)

    with app_session() as session:
        run_framework_lens(run_id, session)

    events = _read_events(app_session, run_id)
    fallback_events = [e for e in events if e.event_type == "framework_lens_stub_fallback"]
    assert len(fallback_events) == 1, [e.event_type for e in events]
    payload = json.loads(fallback_events[0].payload or "{}")
    assert payload["reason_kind"] == "schema_or_integrity"

    artifact = json.loads((run_dir / FRAMEWORK_LENS_ARTIFACT_PATH).read_text(encoding="utf-8"))
    # Stub fallback shape (deterministic from shortlist).
    assert len(artifact["signals"]) == 1


# ----------------------------------------------------------------------
# 4. Persistent banned-name integrity violation → fallback w/ schema reason
# ----------------------------------------------------------------------


def test_persistent_banned_lens_name_falls_back(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _BannedLensNameLLM.calls = 0
    _set_llm_path_env(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", _BannedLensNameLLM)

    run_id, run_dir = _seed_lens_run(app_session, tmp_path)

    with app_session() as session:
        run_framework_lens(run_id, session)

    events = _read_events(app_session, run_id)
    fallback_events = [e for e in events if e.event_type == "framework_lens_stub_fallback"]
    assert len(fallback_events) == 1, [e.event_type for e in events]
    payload = json.loads(fallback_events[0].payload or "{}")
    assert payload["reason_kind"] == "schema_or_integrity"

    artifact = json.loads((run_dir / FRAMEWORK_LENS_ARTIFACT_PATH).read_text(encoding="utf-8"))
    # Stub-shaped, not the banned LLM signal.
    assert artifact["signals"][0]["lens_name"] != "Default Lens"


# ----------------------------------------------------------------------
# 5. theory_article + eligible source + LLM returns empty signals →
#    integrity reject → fallback (codex amendment E)
# ----------------------------------------------------------------------


def test_theory_article_empty_signals_falls_back_via_integrity(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _EmptySignalsLLM.calls = 0
    _set_llm_path_env(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", _EmptySignalsLLM)

    run_id, run_dir = _seed_lens_run(app_session, tmp_path, paper_mode="theory_article")

    with app_session() as session:
        run_framework_lens(run_id, session)

    events = _read_events(app_session, run_id)
    fallback_events = [e for e in events if e.event_type == "framework_lens_stub_fallback"]
    assert len(fallback_events) == 1, [e.event_type for e in events]
    payload = json.loads(fallback_events[0].payload or "{}")
    assert payload["reason_kind"] == "schema_or_integrity"

    # Stub fallback writes ≥1 signal from the shortlist's lens-tagged source.
    artifact = json.loads((run_dir / FRAMEWORK_LENS_ARTIFACT_PATH).read_text(encoding="utf-8"))
    assert len(artifact["signals"]) >= 1


# ----------------------------------------------------------------------
# 5b. case_analysis + empty signals → ACCEPT (no fallback, no event)
# ----------------------------------------------------------------------


def test_case_analysis_empty_signals_accepted_no_fallback(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _EmptySignalsLLM.calls = 0
    _set_llm_path_env(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", _EmptySignalsLLM)

    run_id, run_dir = _seed_lens_run(app_session, tmp_path, paper_mode="case_analysis")

    with app_session() as session:
        run_framework_lens(run_id, session)

    events = _read_events(app_session, run_id)
    fallback_events = [e for e in events if e.event_type == "framework_lens_stub_fallback"]
    assert fallback_events == [], [e.event_type for e in events]

    artifact = json.loads((run_dir / FRAMEWORK_LENS_ARTIFACT_PATH).read_text(encoding="utf-8"))
    assert artifact["signals"] == []
