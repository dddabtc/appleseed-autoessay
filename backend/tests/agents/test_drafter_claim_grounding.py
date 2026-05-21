"""PR-G-Grounding: deterministic claim-source grounding warning.

Real-paper rounds 4 + 6 (bretton_woods) hit FAILED_POLICY at
exports because the critic correctly caught "method 声称用 IMF
内部备忘录 / 美联储理事会会议纪要 / 伦敦黄金池季度结算记录"
but the cited source's metadata didn't actually contain those
archives. ``_check_claim_grounding`` surfaces the same gap at
drafter end (before critic) so the acceptance gate + downstream
consumers know the run is weakly grounded.
"""

from __future__ import annotations

from pathlib import Path

from autoessay.agents.drafter import (
    DraftedSection,
    _check_claim_grounding,
)
from autoessay.clients.common import AccessStatus, NormalizedSource


def _src(
    source_id: str,
    *,
    title: str = "Generic paper",
    abstract: str = "",
    venue: str = "",
) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=title,
        authors=["Author"],
        year=2020,
        venue=venue,
        doi=None,
        url=None,
        pdf_url=None,
        abstract=abstract,
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=0.5,
        risk_flags=[],
    )


def _section(
    section_id: str,
    claim_text: str,
    cited_ids: list[str],
) -> DraftedSection:
    return DraftedSection(
        section_id=section_id,
        title=f"Title {section_id}",
        prose="prose",
        claim_map=[
            {
                "claim_id": f"{section_id}_c1",
                "paragraph_id": f"{section_id}-p1",
                "claim_text": claim_text,
                "source_ids": cited_ids,
            }
        ],
        failed=False,
        warnings=[],
        word_count=200,
        target_words=1500,
    )


# ----- bretton_woods round 4 + 6 reproducer ----------------------


def test_imf_memo_not_in_any_source_flagged() -> None:
    """Round 4 + 6 reproducer: drafter writes 'IMF 内部备忘录'
    but cited source's title/abstract doesn't mention IMF memo."""
    src = _src("s1", title="A general Bretton Woods study", abstract="Cold-war era")
    section = _section(
        "method",
        "本研究采用 IMF 内部备忘录做三角互证",
        ["s1"],
    )
    result = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[src],
        run_dir=Path("/tmp/nonexistent_run_dir"),
    )
    assert result["weakly_grounded_count"] == 1
    weak = result["weakly_grounded_claims"][0]
    assert "IMF" in weak["phrase"]
    assert weak["section_id"] == "method"


def test_imf_memo_present_in_source_passes() -> None:
    """When the cited source's title/abstract DOES mention the
    archive, no warning fires."""
    src = _src(
        "s1",
        title="Reading IMF 内部备忘录 from 1968",
        abstract="An archival study of IMF memos",
    )
    section = _section(
        "method",
        "本研究采用 IMF 内部备忘录做三角互证",
        ["s1"],
    )
    result = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[src],
        run_dir=Path("/tmp/nonexistent"),
    )
    assert result["weakly_grounded_count"] == 0


def test_jiangnan_imprints_in_china_humanities_round_2() -> None:
    """Round 1 + 2 (江南刊本) pattern: '序跋与刻工题记' in claim
    but cited source doesn't mention either."""
    src = _src("s1", title="Generic late-Qing publishing study")
    section = _section(
        "method",
        "通过分析江南刊本的序跋与刻工题记重建断代依据",
        ["s1"],
    )
    result = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[src],
        run_dir=Path("/tmp/nonexistent"),
    )
    assert result["weakly_grounded_count"] >= 1


def test_no_specific_archive_no_warning() -> None:
    """A claim with only generic prose (no specific archive name)
    doesn't trigger the warning even when sources are sparse."""
    src = _src("s1", title="A paper")
    section = _section(
        "intro",
        "本节提出研究问题与基本框架",
        ["s1"],
    )
    result = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[src],
        run_dir=Path("/tmp/nonexistent"),
    )
    assert result["weakly_grounded_count"] == 0


def test_uncited_marker_skipped() -> None:
    """``[UNCITED]`` placeholder is NOT looked up as a source_id."""
    src = _src("s1", title="A paper that mentions IMF 内部备忘录")
    section = _section(
        "method",
        "本研究采用 IMF 内部备忘录做三角互证",
        ["[UNCITED]"],  # no real source cited
    )
    result = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[src],
        run_dir=Path("/tmp/nonexistent"),
    )
    # No real cited source → no source to verify against → flag
    assert result["weakly_grounded_count"] == 1


def test_multiple_cited_sources_one_match_passes() -> None:
    """When 1 of 2 cited sources contains the entity, the claim is
    considered grounded (any-match semantics)."""
    src_match = _src("s_good", title="Paper on IMF 内部备忘录")
    src_other = _src("s_other", title="Unrelated paper")
    section = _section(
        "method",
        "本研究采用 IMF 内部备忘录做三角互证",
        ["s_other", "s_good"],
    )
    result = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[src_match, src_other],
        run_dir=Path("/tmp/nonexistent"),
    )
    assert result["weakly_grounded_count"] == 0


def test_diagnostic_contains_useful_metadata() -> None:
    """The warning event payload should include enough to debug —
    section_id, paragraph_id, the matched phrase, and cited_source_ids."""
    src = _src("s1", title="Generic paper")
    section = _section(
        "method",
        "本研究采用 IMF 内部备忘录与美联储理事会会议纪要",
        ["s1"],
    )
    result = _check_claim_grounding(
        drafted_sections=[section],
        shortlist=[src],
        run_dir=Path("/tmp/nonexistent"),
    )
    weak = result["weakly_grounded_claims"][0]
    assert weak["section_id"] == "method"
    assert weak["paragraph_id"] == "method-p1"
    assert "phrase" in weak
    assert weak["cited_source_ids"] == ["s1"]
    assert "pattern_description" in weak
