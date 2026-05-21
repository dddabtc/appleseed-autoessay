"""PR-G-Conclusion-Evidence-Whitelist tests (codex AGREE-WITH-AMENDMENTS,
2026-05-07). Covers ``_build_supported_claims_digest`` — feeds the
conclusion drafter a compact whitelist of evidence-backed body claims
so it can only summarize what was actually shown."""

from __future__ import annotations

from autoessay.agents.drafter import (
    DraftedSection,
    _build_supported_claims_digest,
)


def _section(
    section_id: str,
    title: str,
    claims: list[tuple[str, list[str]]],
) -> DraftedSection:
    return DraftedSection(
        section_id=section_id,
        title=title,
        prose="prose",
        claim_map=[
            {
                "claim_id": f"{section_id}_c{i}",
                "paragraph_id": f"{section_id}-p{i}",
                "claim_text": text,
                "source_ids": sids,
            }
            for i, (text, sids) in enumerate(claims, start=1)
        ],
        failed=False,
        warnings=[],
        word_count=200,
        target_words=1500,
    )


def test_empty_drafted_sections_returns_empty() -> None:
    assert _build_supported_claims_digest([]) == ""


def test_uncited_claims_excluded() -> None:
    """``[UNCITED]`` claims should NOT appear in the digest — the
    point of the whitelist is that conclusion can only reference
    evidence-backed body content."""
    sections = [
        _section(
            "introduction",
            "引言",
            [
                ("有据可查的论断 A", ["crossref:10.1000/foo"]),
                ("没引证的论断 B", ["[UNCITED]"]),
                ("空 source list 论断 C", []),
            ],
        ),
    ]
    digest = _build_supported_claims_digest(sections)
    assert "论断 A" in digest
    assert "论断 B" not in digest
    assert "论断 C" not in digest


def test_per_section_claim_count_recorded() -> None:
    sections = [
        _section(
            "introduction",
            "引言",
            [
                ("断言 A1", ["s1"]),
                ("断言 A2", ["s2"]),
            ],
        ),
        _section(
            "historiography",
            "文献综述",
            [
                ("断言 B1", ["s1"]),
            ],
        ),
    ]
    digest = _build_supported_claims_digest(sections)
    assert "《引言》[2 条已引证]" in digest
    assert "《文献综述》[1 条已引证]" in digest


def test_conclusion_section_itself_skipped_if_present() -> None:
    """Round 9 evidence: when iterating with conclusion already
    drafted (defensive), don't include it in its own digest."""
    sections = [
        _section(
            "introduction",
            "引言",
            [("引言断言", ["s1"])],
        ),
        _section(
            "conclusion",
            "结论",
            [("结论断言", ["s1"])],
        ),
    ]
    digest = _build_supported_claims_digest(sections)
    assert "《引言》" in digest
    assert "《结论》" not in digest


def test_long_claim_text_truncated_to_120_chars() -> None:
    """Codex round-1 review (2026-05-08): per-claim cap bumped from
    60 to 120 to preserve qualifiers / concessive tails. Anything
    beyond 120 is still truncated to keep the digest bounded."""
    long = "这是一段非常长的论断文本" * 20
    sections = [
        _section("introduction", "引言", [(long, ["s1"])]),
    ]
    digest = _build_supported_claims_digest(sections)
    label = "[1 条已引证]: "
    assert label in digest
    claim_portion = digest.split(label, 1)[1].strip()
    # Per-claim cap is 120 chars in the digest builder.
    assert len(claim_portion) <= 120
    # And the full long string is NOT preserved.
    assert long not in digest


def test_section_with_zero_cited_claims_omitted() -> None:
    """If a body section has only [UNCITED] claims, it should not
    contribute an empty-bracket entry to the digest."""
    sections = [
        _section(
            "empirical-section-i",
            "正文一",
            [("空引断言", ["[UNCITED]"])],
        ),
    ]
    digest = _build_supported_claims_digest(sections)
    assert "正文一" not in digest
    assert digest == ""


def test_more_than_8_claims_collapsed_with_more_marker() -> None:
    sections = [
        _section(
            "introduction",
            "引言",
            [(f"断言 #{i}", ["s1"]) for i in range(12)],
        ),
    ]
    digest = _build_supported_claims_digest(sections)
    assert "[12 条已引证]" in digest
    assert "(+4 more)" in digest


# Codex round-1 review amendment 4 (2026-05-08): qualifier-at-tail
# preservation. 60-char truncation was cutting off concessive tails
# ("…但证据有限", "…仅在 1968-1971 时段") that are exactly the signal
# the conclusion drafter needs to NOT overstate.


def test_qualifier_at_tail_preserved_under_120_chars() -> None:
    """The conclusion drafter MUST see the qualifier — that's the
    signal that the body claim is conditional / scoped / weak.
    Under 120 chars, a normal claim with a tail qualifier should
    survive in full."""
    sources_cited = ["s1"]
    claim_with_tail = (
        "金本位承诺在 1968-1971 年表层延续，但内部备忘录显示"
        "实际可兑换约束已失效；证据有限于黄金池结算记录。"
    )
    assert len(claim_with_tail) <= 120, (
        f"sanity: test fixture should fit in 120 chars, got {len(claim_with_tail)}"
    )
    sections = [
        _section("introduction", "引言", [(claim_with_tail, sources_cited)]),
    ]
    digest = _build_supported_claims_digest(sections)
    # The qualifier "证据有限于黄金池结算记录" must appear in digest.
    assert "证据有限" in digest
    # And the time-window qualifier "1968-1971" too.
    assert "1968-1971" in digest


def test_short_claim_text_preserved_verbatim() -> None:
    """Round 9 evidence: most body claims are < 120 chars; they
    should appear verbatim in the digest (no information loss)."""
    short_claim = "1971 年 8 月美元正式脱离金本位。"
    sections = [_section("introduction", "引言", [(short_claim, ["s1"])])]
    digest = _build_supported_claims_digest(sections)
    assert short_claim in digest
