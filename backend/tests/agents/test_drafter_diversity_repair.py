"""PR-G-Sources Q2 (codex v5 round-1 amendment): drafter LLM
diversity repair — pure-function unit tests.

The LLM call itself is exercised in real-paper acceptance walks;
these tests cover the deterministic helpers + the section
selection algorithm.
"""

from __future__ import annotations

from autoessay.agents.drafter import (
    DIVERSITY_REPAIR_SYSTEM_PROMPT,
    DiversityRepairOutcome,
    DraftedSection,
    _normalize_diversity_repair_claim_map,
    _pick_repair_target_sections,
    _RepairedClaimRecord,
    _section_distinct_cited_count,
)


def _section(
    section_id: str,
    cited_source_ids: list[str],
    *,
    prose: str = "prose",
) -> DraftedSection:
    """Build a DraftedSection whose claim_map cites each source_id
    in ``cited_source_ids`` once."""
    return DraftedSection(
        section_id=section_id,
        title=f"Title {section_id}",
        prose=prose,
        claim_map=[
            {
                "claim_id": f"{section_id}_c{idx}",
                "paragraph_id": f"{section_id}_p1",
                "claim_text": f"claim {idx}",
                "source_ids": [sid] if sid != "[UNCITED]" else ["[UNCITED]"],
            }
            for idx, sid in enumerate(cited_source_ids)
        ],
        failed=False,
        warnings=[],
        word_count=200,
        target_words=1500,
    )


# ----- _section_distinct_cited_count -----------------------------


def test_distinct_count_excludes_uncited_marker() -> None:
    """``[UNCITED]`` placeholder doesn't count toward distinct
    sources (parallels existing _check_diversity_floor behavior)."""
    section = _section("intro", ["s1", "s2", "[UNCITED]"])
    assert _section_distinct_cited_count(section) == 2


def test_distinct_count_dedupes_repeats() -> None:
    """Same source_id cited in 3 claims still counts as 1
    distinct source."""
    section = _section("intro", ["s1", "s1", "s1"])
    assert _section_distinct_cited_count(section) == 1


def test_distinct_count_handles_empty_claim_map() -> None:
    """Section with empty claim_map → 0 distinct sources."""
    section = _section("intro", [])
    assert _section_distinct_cited_count(section) == 0


def test_distinct_count_handles_string_source_ids_field() -> None:
    """If a claim's source_ids was serialized as a string
    placeholder (legacy schema), it's treated as 0."""
    section = DraftedSection(
        section_id="intro",
        title="t",
        prose="p",
        claim_map=[{"source_ids": "[UNCITED]", "claim_text": "x"}],
        failed=False,
        warnings=[],
        word_count=10,
        target_words=100,
    )
    assert _section_distinct_cited_count(section) == 0


# ----- _pick_repair_target_sections ------------------------------


def test_target_picker_selects_lowest_density_first() -> None:
    """Two lowest distinct-cited sections selected (codex Q2)."""
    sections = [
        _section("intro", ["s1", "s2", "s3"]),
        _section("histo", ["s4"]),  # lowest
        _section("method", ["s2", "s5"]),  # second lowest (tie? no — 2 vs 1)
        _section("conc", ["s4"]),  # tied with histo at 1
    ]
    indices = _pick_repair_target_sections(sections, target_count=2)
    # Both "histo" (idx 1) and "conc" (idx 3) have 1 distinct;
    # tie-break by section index → indices [1, 3]
    assert indices == [1, 3]


def test_target_picker_handles_fewer_than_target_count() -> None:
    """Only 1 section in input → return [0] not error."""
    sections = [_section("intro", ["s1"])]
    assert _pick_repair_target_sections(sections, target_count=2) == [0]


def test_target_picker_default_count_is_two() -> None:
    """Codex Q2 amendment locked the count at 2 lowest-density
    sections."""
    sections = [_section(f"s{i}", []) for i in range(5)]
    indices = _pick_repair_target_sections(sections)
    assert len(indices) == 2


# ----- system prompt invariants ----------------------------------


def test_system_prompt_pins_critical_constraints() -> None:
    """codex round-1 locked the system prompt verbatim. Drift checks."""
    # role=medium guard
    assert "medium" in DIVERSITY_REPAIR_SYSTEM_PROMPT
    # No new facts/sources
    assert "不可新增" in DIVERSITY_REPAIR_SYSTEM_PROMPT
    # Length floor
    assert "70" in DIVERSITY_REPAIR_SYSTEM_PROMPT
    # JSON output
    assert "JSON" in DIVERSITY_REPAIR_SYSTEM_PROMPT


# ----- repaired claim_map normalization --------------------------


def test_repair_claim_map_persists_evidence_status_and_added_sources() -> None:
    actually_added: set[str] = set()

    normalized = _normalize_diversity_repair_claim_map(
        [
            _RepairedClaimRecord.parse_obj(
                {
                    "paragraph_id": "empirical-section-i-p001",
                    "claim_text": "目录条目可与年报并读。",
                    "source_ids": ["s1", "s2", "not-in-cited"],
                },
            ),
        ],
        original=_section("empirical-section-i", ["s1"]),
        full_cited_id_set={"s1", "s2"},
        cited_now={"s1"},
        actually_added=actually_added,
    )

    assert normalized == [
        {
            "paragraph_id": "empirical-section-i-p001",
            "claim_text": "目录条目可与年报并读。",
            "source_ids": ["s1", "s2"],
            "section_id": "empirical-section-i",
            "section_title": "Title empirical-section-i",
            "uncited": False,
            "evidence_status": "source_bound",
        }
    ]
    assert actually_added == {"s2"}


def test_repair_claim_map_marks_uncited_interpretive_claim_model_backed() -> None:
    actually_added: set[str] = set()

    normalized = _normalize_diversity_repair_claim_map(
        [
            _RepairedClaimRecord.parse_obj(
                {
                    "paragraph_id": "empirical-section-i-p003",
                    "claim_text": (
                        "该目录更适合作为1968年前后黄金政策重估的中间入口，"
                        "而不是1971年正式终止的旁证。"
                    ),
                    "source_ids": [],
                },
            ),
        ],
        original=_section("empirical-section-i", []),
        full_cited_id_set={"s1"},
        cited_now=set(),
        actually_added=actually_added,
    )

    assert normalized[0]["source_ids"] == []
    assert normalized[0]["uncited"] is False
    assert normalized[0]["evidence_status"] == "model_backed"
    assert normalized[0]["confidence"] == "medium"
    assert actually_added == set()


def test_repair_claim_map_keeps_uncited_factual_claim_blockable() -> None:
    normalized = _normalize_diversity_repair_claim_map(
        [
            _RepairedClaimRecord.parse_obj(
                {
                    "paragraph_id": "empirical-section-i-p002",
                    "claim_text": "1968年3月20日的美联储纪要已经证明黄金政策失效。",
                    "source_ids": [],
                },
            ),
        ],
        original=_section("empirical-section-i", []),
        full_cited_id_set={"s1"},
        cited_now=set(),
        actually_added=set(),
    )

    assert normalized[0]["source_ids"] == ["[UNCITED]"]
    assert normalized[0]["uncited"] is True
    assert normalized[0]["evidence_status"] == "source_bound"


# ----- DiversityRepairOutcome dataclass --------------------------


def test_outcome_skipped_carries_diagnostic_fields() -> None:
    """Skipped outcomes (no LLM call) still need event_type +
    skipped_reason for the caller to emit a single audit event."""
    sections = [_section("intro", [])]
    outcome = DiversityRepairOutcome(
        applied=False,
        drafted_sections=sections,
        skipped_reason="no_unused_eligible",
        event_type="diversity_repair_skipped",
        added_source_ids=[],
        target_section_ids=[],
    )
    assert outcome.applied is False
    assert outcome.skipped_reason == "no_unused_eligible"
    assert outcome.event_type == "diversity_repair_skipped"
