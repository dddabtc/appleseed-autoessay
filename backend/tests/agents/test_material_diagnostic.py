"""Tests for the material diagnostic builder."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from autoessay.agents import material_diagnostic as md_module
from autoessay.agents.material_diagnostic import (
    MaterialDiagnostic,
    diagnostic_to_dict,
    render_material_diagnostic_markdown,
    run_material_diagnostic,
)
from autoessay.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _DummyRun:
    """Lightweight stand-in for ``models.Run``.

    PR-D2.1 (2026-05-03): ``run_material_diagnostic`` now requires
    ``run`` + ``session`` so it can build an ``AuditWriter`` for the
    hub call. The stub / empty-source short-circuits never reach
    that point, so the dummy values are never dereferenced. Tests
    that monkeypatch ``_run_diagnostic_via_llm`` likewise bypass the
    audit creation.
    """

    id: str = "run_test"
    run_dir: str = "/tmp/diagnostic-test-run"


@pytest.fixture
def _dummy_run() -> _DummyRun:
    return _DummyRun()


@pytest.fixture
def _dummy_session() -> Any:
    return object()


def _note(thesis: str = "thesis", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "thesis": thesis,
        "method": "method",
        "evidence": "evidence",
        "limits": "limits",
    }
    base.update(overrides)
    return base


def test_stub_mode_returns_incomplete_marker(
    monkeypatch: pytest.MonkeyPatch,
    _dummy_run: _DummyRun,
    _dummy_session: Any,
) -> None:
    monkeypatch.setenv("AUTOESSAY_MATERIAL_DIAGNOSTIC_STUB", "1")
    get_settings.cache_clear()
    diag = run_material_diagnostic(
        run=_dummy_run,
        session=_dummy_session,
        project_title="topic",
        project_language="zh",
        source_notes={"a": _note()},
        claims=[],
    )
    assert diag.recommended_action == "incomplete"
    assert diag.rationale == "stub mode"
    assert diag.candidate_titles == ()


def test_empty_source_notes_short_circuits_to_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    _dummy_run: _DummyRun,
    _dummy_session: Any,
) -> None:
    monkeypatch.setenv("AUTOESSAY_MATERIAL_DIAGNOSTIC_STUB", "0")
    get_settings.cache_clear()
    diag = run_material_diagnostic(
        run=_dummy_run,
        session=_dummy_session,
        project_title="topic",
        project_language="zh",
        source_notes={},
        claims=[],
    )
    assert diag.recommended_action == "incomplete"
    assert "no synthesizer source notes" in diag.rationale


def test_llm_exception_falls_open_to_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    _dummy_run: _DummyRun,
    _dummy_session: Any,
) -> None:
    monkeypatch.setenv("AUTOESSAY_MATERIAL_DIAGNOSTIC_STUB", "0")
    get_settings.cache_clear()

    async def boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated LLM failure")

    monkeypatch.setattr(md_module, "_run_diagnostic_via_llm", boom)
    diag = run_material_diagnostic(
        run=_dummy_run,
        session=_dummy_session,
        project_title="topic",
        project_language="zh",
        source_notes={"a": _note()},
        claims=[],
    )
    assert diag.recommended_action == "incomplete"
    assert "exception" in diag.rationale


def test_llm_returning_none_falls_open(
    monkeypatch: pytest.MonkeyPatch,
    _dummy_run: _DummyRun,
    _dummy_session: Any,
) -> None:
    monkeypatch.setenv("AUTOESSAY_MATERIAL_DIAGNOSTIC_STUB", "0")
    get_settings.cache_clear()

    async def returns_none(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(md_module, "_run_diagnostic_via_llm", returns_none)
    diag = run_material_diagnostic(
        run=_dummy_run,
        session=_dummy_session,
        project_title="topic",
        project_language="zh",
        source_notes={"a": _note()},
        claims=[],
    )
    assert diag.recommended_action == "incomplete"
    assert "unparseable" in diag.rationale


def test_render_markdown_zh_includes_chinese_chrome() -> None:
    diag = MaterialDiagnostic(
        sufficient=True,
        candidate_titles=("题目一", "题目二", "题目三"),
        missing_materials=("一手数据",),
        risks=("近五年文献覆盖不足",),
        recommended_action="proceed",
        rationale="资料覆盖三个角度，足以支持论文。",
    )
    out = render_material_diagnostic_markdown(diag, "zh")
    assert "# 资料诊断" in out
    assert "可继续生成大纲" in out
    assert "题目一" in out and "题目二" in out and "题目三" in out
    assert "一手数据" in out
    assert "近五年文献覆盖不足" in out


def test_render_markdown_en_uses_english_labels() -> None:
    diag = MaterialDiagnostic(
        sufficient=False,
        candidate_titles=(),
        missing_materials=(),
        risks=(),
        recommended_action="iterate",
        rationale="Only two of the assembled sources address the topic directly.",
    )
    out = render_material_diagnostic_markdown(diag, "en")
    assert "# Material Diagnostic" in out
    assert "Iterate on the topic or add sources" in out
    # Empty lists render as the localized "(none)" placeholder.
    assert "(none)" in out


def test_render_markdown_handles_empty_lists_gracefully() -> None:
    diag = MaterialDiagnostic(
        sufficient=False,
        candidate_titles=(),
        missing_materials=(),
        risks=(),
        recommended_action="incomplete",
        rationale="",
    )
    out = render_material_diagnostic_markdown(diag, "zh")
    assert "（无）" in out


def test_diagnostic_to_dict_round_trips_via_json() -> None:
    diag = MaterialDiagnostic(
        sufficient=True,
        candidate_titles=("a", "b", "c"),
        missing_materials=("x",),
        risks=(),
        recommended_action="proceed",
        rationale="ok",
    )
    payload = diagnostic_to_dict(diag)
    restored = json.loads(json.dumps(payload, ensure_ascii=False))
    assert restored["sufficient"] is True
    assert restored["candidate_titles"] == ["a", "b", "c"]
    assert restored["recommended_action"] == "proceed"


def test_run_diagnostic_via_llm_parses_well_formed_json(
    monkeypatch: pytest.MonkeyPatch,
    _dummy_run: _DummyRun,
    _dummy_session: Any,
) -> None:
    monkeypatch.setenv("AUTOESSAY_MATERIAL_DIAGNOSTIC_STUB", "0")
    get_settings.cache_clear()

    async def fake(**_kwargs: object) -> MaterialDiagnostic:
        return MaterialDiagnostic(
            sufficient=True,
            candidate_titles=("t1", "t2", "t3"),
            missing_materials=("m1",),
            risks=("r1",),
            recommended_action="proceed",
            rationale="ok",
        )

    monkeypatch.setattr(md_module, "_run_diagnostic_via_llm", fake)
    diag = run_material_diagnostic(
        run=_dummy_run,
        session=_dummy_session,
        project_title="topic",
        project_language="zh",
        source_notes={"a": _note()},
        claims=[],
    )
    assert diag.sufficient is True
    assert diag.recommended_action == "proceed"
    assert diag.candidate_titles == ("t1", "t2", "t3")
