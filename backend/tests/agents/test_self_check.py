"""Tests for the self-check report builder."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from autoessay.agents import self_check as self_check_module
from autoessay.agents.self_check import (
    SELF_CHECK_ITEMS,
    SelfCheckItem,
    SelfCheckReport,
    render_self_check_markdown,
    report_to_dict,
    run_self_check,
)
from autoessay.config import get_settings


@pytest.fixture(autouse=True)
def _enable_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_SELF_CHECK_STUB", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _DummyRun:
    id: str = "run_test"
    run_dir: str = "/tmp/self-check-test-run"


_DUMMY_RUN = _DummyRun()
_DUMMY_SESSION: Any = object()


def test_stub_mode_returns_incomplete_for_every_item() -> None:
    report = run_self_check(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        manuscript_markdown="# Some paper",
        project_language="zh",
    )
    assert len(report.items) == len(SELF_CHECK_ITEMS)
    assert all(item.verdict == "incomplete" for item in report.items)
    assert report.overall_verdict == "incomplete"


def test_overall_verdict_priority() -> None:
    items = (
        SelfCheckItem(item_id="a", question="?", verdict="pass", rationale=""),
        SelfCheckItem(item_id="b", question="?", verdict="warn", rationale=""),
        SelfCheckItem(item_id="c", question="?", verdict="incomplete", rationale=""),
    )
    assert SelfCheckReport(items=items).overall_verdict == "warn"

    items_with_fail = items + (
        SelfCheckItem(item_id="d", question="?", verdict="fail", rationale=""),
    )
    assert SelfCheckReport(items=items_with_fail).overall_verdict == "fail"

    all_pass = (
        SelfCheckItem(item_id="a", question="?", verdict="pass", rationale=""),
        SelfCheckItem(item_id="b", question="?", verdict="pass", rationale=""),
    )
    assert SelfCheckReport(items=all_pass).overall_verdict == "pass"


def test_counts_match_verdict_distribution() -> None:
    items = (
        SelfCheckItem(item_id="a", question="?", verdict="pass", rationale=""),
        SelfCheckItem(item_id="b", question="?", verdict="pass", rationale=""),
        SelfCheckItem(item_id="c", question="?", verdict="warn", rationale=""),
        SelfCheckItem(item_id="d", question="?", verdict="fail", rationale=""),
    )
    report = SelfCheckReport(items=items)
    assert report.pass_count == 2
    assert report.warn_count == 1
    assert report.fail_count == 1


def test_render_markdown_zh_has_chinese_chrome() -> None:
    report = run_self_check(
        run=_DUMMY_RUN, session=_DUMMY_SESSION, manuscript_markdown="# x", project_language="zh"
    )
    out = render_self_check_markdown(report, "zh")
    assert "# 自检报告" in out
    assert "总体判定" in out
    # incomplete in stub mode → ⏳ emoji and "stub mode" rationale
    assert "⏳" in out
    assert "stub mode" in out


def test_render_markdown_en_uses_english_labels() -> None:
    report = run_self_check(
        run=_DUMMY_RUN, session=_DUMMY_SESSION, manuscript_markdown="# x", project_language="en"
    )
    out = render_self_check_markdown(report, "en")
    assert "# Self-Check Report" in out
    assert "Overall verdict" in out
    assert "Title accurately reflects the paper" in out


def test_render_markdown_includes_fix_when_present() -> None:
    items = tuple(
        SelfCheckItem(
            item_id=item_id,
            question=question,
            verdict="warn",
            rationale="needs work",
            fix="add concrete data",
        )
        for item_id, question in SELF_CHECK_ITEMS[:2]
    )
    out = render_self_check_markdown(SelfCheckReport(items=items), "zh")
    assert "建议修复" in out
    assert "add concrete data" in out


def test_report_to_dict_round_trips_via_json() -> None:
    report = run_self_check(
        run=_DUMMY_RUN, session=_DUMMY_SESSION, manuscript_markdown="# x", project_language="zh"
    )
    payload = report_to_dict(report)
    serialized = json.dumps(payload, ensure_ascii=False)
    restored = json.loads(serialized)
    assert restored["overall_verdict"] == "incomplete"
    assert restored["counts"]["incomplete"] == len(SELF_CHECK_ITEMS)
    assert len(restored["items"]) == len(SELF_CHECK_ITEMS)
    assert {item["item_id"] for item in restored["items"]} == {
        item_id for item_id, _ in SELF_CHECK_ITEMS
    }


def test_run_self_check_fail_open_on_llm_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable stub so we hit the LLM path.
    monkeypatch.setenv("AUTOESSAY_SELF_CHECK_STUB", "0")
    get_settings.cache_clear()

    async def boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated LLM failure")

    monkeypatch.setattr(self_check_module, "_run_self_check_via_llm", boom)
    report = run_self_check(
        run=_DUMMY_RUN, session=_DUMMY_SESSION, manuscript_markdown="# x", project_language="zh"
    )
    assert report.overall_verdict == "incomplete"
    assert all(item.verdict == "incomplete" for item in report.items)
    assert any("exception" in item.rationale for item in report.items)


def test_run_self_check_marks_incomplete_when_parser_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SELF_CHECK_STUB", "0")
    get_settings.cache_clear()

    async def returns_none(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(self_check_module, "_run_self_check_via_llm", returns_none)
    report = run_self_check(
        run=_DUMMY_RUN, session=_DUMMY_SESSION, manuscript_markdown="# x", project_language="zh"
    )
    assert report.overall_verdict == "incomplete"
    assert all("unparseable" in item.rationale for item in report.items)
