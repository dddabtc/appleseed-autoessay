"""Tests for the detailed outline builder."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from autoessay.agents import detailed_outline as outline_module
from autoessay.agents.detailed_outline import (
    AngleOutline,
    OutlineSection,
    build_detailed_outlines,
    outlines_to_dict,
    render_outlines_markdown,
)
from autoessay.config import get_settings


@pytest.fixture(autouse=True)
def _reset_settings() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _DummyRun:
    id: str = "run_test"
    run_dir: str = "/tmp/detailed-outline-test-run"


_DUMMY_RUN = _DummyRun()
_DUMMY_SESSION: Any = object()


def _angle(angle_id: str, title: str = "Working Title") -> dict[str, object]:
    return {
        "angle_id": angle_id,
        "working_title": title,
        "thesis_one_sentence": "Some thesis.",
        "key_claim_ids": ["c1"],
        "evidence_so_far": "Three sources.",
        "missing_evidence": "Archival data missing.",
    }


def test_empty_angle_cards_returns_empty_tuple() -> None:
    out = build_detailed_outlines(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        angle_cards=[],
        claims=[],
        source_notes={},
        project_title="x",
        project_language="zh",
    )
    assert out == ()


def test_stub_mode_returns_5_section_skeleton_per_angle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_DETAILED_OUTLINE_STUB", "1")
    get_settings.cache_clear()
    out = build_detailed_outlines(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        angle_cards=[_angle("a1"), _angle("a2", "Other Title")],
        claims=[],
        source_notes={},
        project_title="x",
        project_language="zh",
    )
    assert len(out) == 2
    assert {o.angle_id for o in out} == {"a1", "a2"}
    for outline in out:
        assert len(outline.sections) == 5
        # zh stub uses Chinese titles.
        titles = [s.title for s in outline.sections]
        assert "引言" in titles
        assert "结论" in titles
        # All description fields are blank in stub — to be filled by the
        # real LLM call.
        for section in outline.sections:
            assert section.function == ""
            assert section.argument == ""


def test_stub_uses_english_titles_for_en_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_DETAILED_OUTLINE_STUB", "1")
    get_settings.cache_clear()
    out = build_detailed_outlines(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        angle_cards=[_angle("a1")],
        claims=[],
        source_notes={},
        project_title="x",
        project_language="en",
    )
    titles = [s.title for s in out[0].sections]
    assert "Introduction" in titles
    assert "Conclusion" in titles


def test_stub_skips_angle_without_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_DETAILED_OUTLINE_STUB", "1")
    get_settings.cache_clear()
    out = build_detailed_outlines(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        angle_cards=[_angle(""), _angle("ok")],
        claims=[],
        source_notes={},
        project_title="x",
        project_language="zh",
    )
    assert [o.angle_id for o in out] == ["ok"]


def test_llm_exception_falls_open_to_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_DETAILED_OUTLINE_STUB", "0")
    get_settings.cache_clear()

    async def boom(**_kwargs: object) -> None:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(outline_module, "_run_outlines_via_llm", boom)
    out = build_detailed_outlines(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        angle_cards=[_angle("a1")],
        claims=[],
        source_notes={},
        project_title="x",
        project_language="zh",
    )
    # Fail-open returns stub skeleton, not empty.
    assert len(out) == 1
    assert out[0].angle_id == "a1"
    assert len(out[0].sections) == 5


def test_render_markdown_zh_includes_chinese_chrome() -> None:
    outline = AngleOutline(
        angle_id="a1",
        working_title="某题目",
        sections=(
            OutlineSection(
                section_id="introduction",
                title="引言",
                function="陈述研究问题",
                argument="某子论点",
                literature="使用 Doe (2024)",
                materials="无",
                relation_to_thesis="奠定基调",
                weakness="文献偏少",
            ),
        ),
    )
    out = render_outlines_markdown([outline], "zh")
    assert "# 详细大纲" in out
    assert "某题目" in out and "`a1`" in out
    assert "本节作用" in out
    assert "陈述研究问题" in out
    assert "潜在弱点" in out


def test_render_markdown_empty_outlines_returns_empty_string() -> None:
    assert render_outlines_markdown([], "zh") == ""


def test_render_markdown_uses_placeholder_for_missing_fields() -> None:
    outline = AngleOutline(
        angle_id="a1",
        working_title="t",
        sections=(
            OutlineSection(
                section_id="introduction",
                title="Introduction",
                function="",
                argument="",
                literature="",
                materials="",
                relation_to_thesis="",
                weakness="",
            ),
        ),
    )
    out = render_outlines_markdown([outline], "en")
    assert "(to be filled)" in out


def test_outlines_to_dict_round_trips_via_json() -> None:
    outlines = (
        AngleOutline(
            angle_id="a1",
            working_title="t",
            sections=(
                OutlineSection(
                    section_id="introduction",
                    title="Introduction",
                    function="state question",
                    argument="x",
                    literature="y",
                    materials="z",
                    relation_to_thesis="r",
                    weakness="w",
                ),
            ),
        ),
    )
    payload = outlines_to_dict(outlines)
    restored = json.loads(json.dumps(payload, ensure_ascii=False))
    assert restored["outlines"][0]["angle_id"] == "a1"
    assert restored["outlines"][0]["sections"][0]["function"] == "state question"


def test_llm_path_drops_angles_without_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_DETAILED_OUTLINE_STUB", "0")
    get_settings.cache_clear()

    async def fake(**_kwargs: object) -> tuple[AngleOutline, ...]:
        return (
            AngleOutline(
                angle_id="a1",
                working_title="ok",
                sections=(
                    OutlineSection(
                        section_id="intro",
                        title="Intro",
                        function="f",
                        argument="ar",
                        literature="l",
                        materials="m",
                        relation_to_thesis="r",
                        weakness="w",
                    ),
                ),
            ),
        )

    monkeypatch.setattr(outline_module, "_run_outlines_via_llm", fake)
    out = build_detailed_outlines(
        run=_DUMMY_RUN,
        session=_DUMMY_SESSION,
        angle_cards=[_angle("a1")],
        claims=[],
        source_notes={},
        project_title="x",
        project_language="zh",
    )
    assert len(out) == 1
    assert out[0].sections[0].function == "f"
