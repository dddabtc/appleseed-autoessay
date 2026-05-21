"""Tests for the Drafter -> detailed-outline anchoring."""

from __future__ import annotations

import json
from pathlib import Path

from autoessay.agents.detailed_outline import OutlineSection
from autoessay.agents.drafter import (
    SectionPlan,
    _load_outline_sections_for_thesis,
    _match_outline_section,
    _outline_anchor_block,
)


def _outline(section_id: str, title: str = "T", **overrides: str) -> OutlineSection:
    base = {
        "function": "",
        "argument": "",
        "literature": "",
        "materials": "",
        "relation_to_thesis": "",
        "weakness": "",
    }
    base.update(overrides)
    return OutlineSection(section_id=section_id, title=title, **base)  # type: ignore[arg-type]


def test_load_returns_empty_when_artifact_missing(tmp_path: Path) -> None:
    out = _load_outline_sections_for_thesis(tmp_path, {"angle_id": "a1"})
    assert out == ()


def test_load_returns_empty_when_thesis_has_no_angle_id(tmp_path: Path) -> None:
    novelty = tmp_path / "novelty"
    novelty.mkdir()
    (novelty / "detailed_outlines.json").write_text(
        json.dumps(
            {
                "outlines": [
                    {
                        "angle_id": "a1",
                        "working_title": "t",
                        "sections": [{"section_id": "introduction", "title": "Intro"}],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    assert _load_outline_sections_for_thesis(tmp_path, {}) == ()


def test_load_finds_matching_angle_outline(tmp_path: Path) -> None:
    novelty = tmp_path / "novelty"
    novelty.mkdir()
    (novelty / "detailed_outlines.json").write_text(
        json.dumps(
            {
                "outlines": [
                    {
                        "angle_id": "a1",
                        "working_title": "Title A1",
                        "sections": [
                            {
                                "section_id": "introduction",
                                "title": "引言",
                                "function": "陈述问题",
                            },
                            {"section_id": "conclusion", "title": "结论"},
                        ],
                    },
                    {
                        "angle_id": "a2",
                        "working_title": "Title A2",
                        "sections": [{"section_id": "introduction", "title": "Intro"}],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    out = _load_outline_sections_for_thesis(tmp_path, {"angle_id": "a1"})
    assert len(out) == 2
    assert out[0].section_id == "introduction"
    assert out[0].function == "陈述问题"
    assert out[1].section_id == "conclusion"


def test_load_returns_empty_when_angle_not_in_outlines(tmp_path: Path) -> None:
    novelty = tmp_path / "novelty"
    novelty.mkdir()
    (novelty / "detailed_outlines.json").write_text(
        json.dumps({"outlines": [{"angle_id": "a1", "sections": []}]}),
        encoding="utf-8",
    )
    assert _load_outline_sections_for_thesis(tmp_path, {"angle_id": "missing"}) == ()


def test_match_exact_section_id_after_underscore_normalize() -> None:
    section = SectionPlan(
        section_id="literature-review",
        title="Literature Review",
        target_words=500,
    )
    outlines = (_outline("literature_review"), _outline("conclusion"))
    matched = _match_outline_section(section, outlines, index=0)
    assert matched is not None
    assert matched.section_id == "literature_review"


def test_match_falls_through_to_substring_then_position() -> None:
    section = SectionPlan(
        section_id="empirical-section-i",
        title="Empirical Section I",
        target_words=500,
    )
    outlines = (
        _outline("introduction"),
        _outline("empirical", title="正文"),
        _outline("conclusion"),
    )
    matched = _match_outline_section(section, outlines, index=1)
    assert matched is not None
    assert matched.section_id == "empirical"


def test_match_uses_position_when_ids_drift() -> None:
    section = SectionPlan(section_id="weird-name", title="Weird", target_words=500)
    outlines = (_outline("a"), _outline("b"), _outline("c"))
    matched = _match_outline_section(section, outlines, index=2)
    assert matched is not None
    assert matched.section_id == "c"


def test_match_returns_none_when_no_outlines() -> None:
    section = SectionPlan(section_id="introduction", title="I", target_words=500)
    assert _match_outline_section(section, (), index=0) is None


def test_anchor_block_returns_empty_string_for_none() -> None:
    assert _outline_anchor_block(None) == ""


def test_anchor_block_returns_empty_when_all_fields_blank() -> None:
    outline = _outline("introduction", title="Intro")
    assert _outline_anchor_block(outline) == ""


def test_anchor_block_emits_only_populated_fields() -> None:
    outline = _outline(
        "introduction",
        title="Intro",
        function="陈述问题",
        argument="支持论点",
    )
    block = _outline_anchor_block(outline)
    assert "USER_NOVELTY_REVIEW" in block
    assert "陈述问题" in block
    assert "支持论点" in block
    # Empty fields not in the output JSON.
    assert "literature" not in block
    assert "materials" not in block


def test_anchor_block_emits_sorted_keys_for_determinism() -> None:
    outline = _outline(
        "introduction",
        title="Intro",
        function="f",
        argument="a",
        literature="l",
    )
    block = _outline_anchor_block(outline)
    # `argument` < `function` < `literature` alphabetically — JSON
    # output uses sort_keys=True so the same input always produces the
    # same prompt (so the prompt cache stays warm).
    assert block.index('"argument"') < block.index('"function"') < block.index('"literature"')
