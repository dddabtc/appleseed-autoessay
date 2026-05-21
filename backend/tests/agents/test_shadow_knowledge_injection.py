"""PR-263c — shadow knowledge prompt-injection helper tests.

Validates the directive builder + the disk-loader convenience
wrapper. Tests focus on:
- empty inputs return empty directive (caller can always invoke)
- argument_map + reference_candidates limits respected
- policy line is verbatim and includes the "mention but don't cite
  as [N]" rule
- author / year / title from candidates make it through to the
  prompt
"""

from __future__ import annotations

import json
from pathlib import Path

from autoessay.agents._shadow_knowledge_injection import (
    _MAX_ARGUMENT_MAP_INJECTED,
    _MAX_REFERENCE_CANDIDATES_INJECTED,
    _compact_argument_map,
    _compact_reference_candidates,
    build_shadow_knowledge_directive,
    shadow_knowledge_directive_for_run,
)
from autoessay.agents.shadow_baseline import (
    ArgumentMapEntry,
    ReferenceCandidate,
    SectionPlanEntry,
    ShadowBaselineOutput,
    persist_shadow_baseline,
)


def _build_full_output() -> ShadowBaselineOutput:
    """An 8-section + 10-candidate shadow baseline that exceeds both
    injection caps, so the compaction logic gets exercised."""
    return ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\n测试.\n",
        argument_map=[
            ArgumentMapEntry(
                section_id=f"section_{i}",
                central_claim=f"central claim {i}",
                key_evidence=[f"ev{i}-1", f"ev{i}-2", f"ev{i}-3"],
            )
            for i in range(8)
        ],
        reference_candidates=[
            ReferenceCandidate(
                author=f"作者{i}",
                year=str(2010 + i),
                title=f"经典著作{i}",
                venue="出版社",
                type="book",
                why_relevant=f"对研究主题的相关性 {i}",
            )
            for i in range(10)
        ],
        section_plan=[
            SectionPlanEntry(
                section_id=f"section_{i}",
                title=f"标题 {i}",
                target_words=1000,
                key_argument=f"key {i}",
            )
            for i in range(8)
        ],
    )


# ----- _compact_argument_map --------------------------------------


def test_compact_argument_map_caps_at_default_limit() -> None:
    out = _build_full_output()  # 8 entries
    compacted = _compact_argument_map(out)
    assert len(compacted) == _MAX_ARGUMENT_MAP_INJECTED


def test_compact_argument_map_truncates_evidence_to_2() -> None:
    """Each argument_map entry's key_evidence list (3 items) gets
    capped at 2 to keep the injected JSON small."""
    out = _build_full_output()
    compacted = _compact_argument_map(out)
    for entry in compacted:
        assert len(entry["key_evidence"]) == 2  # type: ignore[arg-type]


def test_compact_argument_map_respects_custom_limit() -> None:
    out = _build_full_output()
    compacted = _compact_argument_map(out, limit=2)
    assert len(compacted) == 2


def test_compact_argument_map_preserves_section_id_and_claim() -> None:
    out = _build_full_output()
    compacted = _compact_argument_map(out, limit=1)
    assert compacted[0]["section_id"] == "section_0"
    assert compacted[0]["central_claim"] == "central claim 0"


# ----- _compact_reference_candidates ------------------------------


def test_compact_reference_candidates_caps_at_default_limit() -> None:
    out = _build_full_output()  # 10 entries
    compacted = _compact_reference_candidates(out)
    assert len(compacted) == _MAX_REFERENCE_CANDIDATES_INJECTED


def test_compact_reference_candidates_drops_why_relevant() -> None:
    """``why_relevant`` is advisory and bloats the prompt; we keep
    only author / year / title / venue / type."""
    out = _build_full_output()
    compacted = _compact_reference_candidates(out, limit=1)
    assert "why_relevant" not in compacted[0]
    assert "author" in compacted[0]
    assert "year" in compacted[0]
    assert "title" in compacted[0]
    assert "venue" in compacted[0]
    assert "type" in compacted[0]


# ----- build_shadow_knowledge_directive ---------------------------


def test_directive_none_input_returns_empty_string() -> None:
    assert build_shadow_knowledge_directive(None) == ""


def test_directive_empty_argument_map_and_refs_returns_empty() -> None:
    """When the shadow baseline produced nothing useful, the
    directive must be empty so the prompt body length stays
    consistent for empty / failed runs."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\nx.\n",
        argument_map=[],
        reference_candidates=[],
    )
    assert build_shadow_knowledge_directive(out) == ""


def test_directive_includes_policy_line() -> None:
    """The verbatim "mention but don't cite as [N]" rule MUST be
    in every non-empty directive — codex Q4 explicit ban."""
    out = _build_full_output()
    directive = build_shadow_knowledge_directive(out)
    assert "shadow_knowledge_policy" in directive
    assert "[N]" in directive  # explicitly bans [N] citations
    assert "Approved sources" in directive


def test_directive_payload_uses_compact_shapes() -> None:
    """Spot-check: the embedded JSON payload contains the compact
    argument_map (≤6 entries, ≤2 evidence) and compact
    reference_candidates (≤8 entries, no why_relevant)."""
    out = _build_full_output()
    directive = build_shadow_knowledge_directive(out)
    # Pull the JSON payload out of the directive.
    start = directive.index("{")
    end = directive.rindex("}") + 1
    payload = json.loads(directive[start:end])
    assert len(payload["argument_map"]) == _MAX_ARGUMENT_MAP_INJECTED
    assert len(payload["reference_candidates"]) == _MAX_REFERENCE_CANDIDATES_INJECTED
    for entry in payload["argument_map"]:
        assert len(entry["key_evidence"]) == 2
    for cand in payload["reference_candidates"]:
        assert "why_relevant" not in cand


def test_directive_carries_chinese_authors_unencoded() -> None:
    """Author names like 郑振铎 must come through readable
    (ensure_ascii=False) so the LLM can match them to its
    parametric knowledge."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\nx.\n",
        argument_map=[],
        reference_candidates=[
            ReferenceCandidate(
                author="郑振铎",
                year="2011",
                title="中国俗文学史",
                venue="中华书局",
                type="book",
            ),
        ],
    )
    directive = build_shadow_knowledge_directive(out)
    assert "郑振铎" in directive
    assert "中国俗文学史" in directive


# ----- shadow_knowledge_directive_for_run -------------------------


def test_directive_for_run_empty_when_no_artifact(tmp_path: Path) -> None:
    """Run without a shadow_baseline call yet → empty directive,
    no exception. Drafter / synthesizer can ALWAYS invoke this
    helper without first checking artifact existence."""
    assert shadow_knowledge_directive_for_run(tmp_path) == ""


def test_directive_for_run_loads_persisted_artifact(tmp_path: Path) -> None:
    """End-to-end: persist a shadow baseline artifact via
    ``persist_shadow_baseline``, then ask the directive helper to
    load it. The directive must be non-empty and include the
    persisted candidate's author / title."""
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\ntest\n",
        argument_map=[
            ArgumentMapEntry(
                section_id="introduction",
                central_claim="test claim",
                key_evidence=["e1"],
            ),
        ],
        reference_candidates=[
            ReferenceCandidate(
                author="郑振铎",
                year="2011",
                title="中国俗文学史",
                venue="中华书局",
                type="book",
            ),
        ],
    )
    persist_shadow_baseline(tmp_path, out)
    directive = shadow_knowledge_directive_for_run(tmp_path)
    assert directive != ""
    assert "郑振铎" in directive
    assert "中国俗文学史" in directive


def test_directive_for_run_corrupt_artifact_returns_empty(tmp_path: Path) -> None:
    """Corrupt JSON on disk → ``load_shadow_baseline`` returns
    None → directive is empty; caller doesn't see an exception."""
    from autoessay.agents.shadow_baseline import shadow_baseline_paths

    json_path, _ = shadow_baseline_paths(tmp_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("not valid json {{{", encoding="utf-8")
    assert shadow_knowledge_directive_for_run(tmp_path) == ""
