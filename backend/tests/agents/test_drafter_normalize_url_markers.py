"""PR-G-Regressions C: ``_normalize_inline_citations_zh`` extended
to accept raw URL/source_id-style cite markers.

Round-1 real-paper run produced 5 raw ``[https://openalex.org/...]``
markers in the manuscript body that the previous PR-259b
normalizer didn't catch. This regression dropped合规性 by ~3
points in the codex+Claude joint scoring.

Tests cover:
- ``[crossref:DOI]`` still maps to ``[N]`` (existing behavior)
- ``[https://openalex.org/W...]`` now maps to ``[N]``
- ``【https://...】`` (FW brackets the LLM uses in zh prompt mode)
  also maps
- Unknown raw marker stays as-is (no false positives)
- Empty cited_sources is still a no-op pass-through
"""

from __future__ import annotations

from autoessay.agents.drafter import _normalize_inline_citations_zh
from autoessay.clients.common import AccessStatus, NormalizedSource


def _src(source_id: str, year: int = 2020) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title="Test Paper",
        authors=["Author"],
        year=year,
        venue="Venue",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=None,
        abstract="Abstract",
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=0.5,
        risk_flags=[],
    )


def test_crossref_doi_marker_still_works() -> None:
    """Existing PR-259b behavior preserved: ``[crossref:DOI]`` →
    ``[N]`` based on cited_sources index."""
    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 [crossref:10.1093/llc/fqz046] 之后的话。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "[crossref:" not in out


def test_openalex_url_marker_now_maps_to_n() -> None:
    """PR-G-Regressions C — round-1 real-paper output had 5 of
    these raw markers; previously they survived to export."""
    sources = [_src("https://openalex.org/W4408365924")]
    body = "段落 [https://openalex.org/W4408365924] 之后。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "[https://openalex.org" not in out


def test_fw_bracket_marker_maps_too() -> None:
    """drafter LLM sometimes emits 【https://…】 in zh-prompt mode;
    same source_id, FW brackets — also normalize."""
    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 【crossref:10.1093/llc/fqz046】 之后。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "【crossref" not in out


def test_unknown_raw_marker_stays_unchanged() -> None:
    """Markers that don't match any cited_source must NOT be touched
    (avoids corrupting drafter's intentional contextual mentions)."""
    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 [https://openalex.org/UNKNOWN_ID] 不在 cited_sources。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[https://openalex.org/UNKNOWN_ID]" in out


def test_empty_cited_sources_still_passes_through() -> None:
    """Empty cited_sources → no-op (preserves PR-259b contract)."""
    body = "段落 [https://openalex.org/W4408365924] 没有任何 sources。"
    out = _normalize_inline_citations_zh(body, [])
    assert out == body


def test_multiple_url_and_crossref_markers_all_handled() -> None:
    """Realistic case: drafter mixes [crossref:DOI] and
    [URL] forms across the manuscript. All map to the same
    [N] index for the same source."""
    sources = [
        _src("crossref:10.1093/llc/fqz046"),
        _src("https://openalex.org/W4408365924"),
    ]
    body = (
        "段落一 [crossref:10.1093/llc/fqz046] 内容。\n"
        "段落二 [crossref:10.1093/llc/fqz046] 内容。\n"
        "段落三 [https://openalex.org/W4408365924] 内容。"
    )
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "[2]" in out
    # All raw forms gone
    assert "[crossref:10.1093" not in out
    assert "[https://openalex.org/W" not in out


def test_multi_doi_bracket_split_on_semicolon() -> None:
    """PR-G-Regressions-3: round 3 surfaced 5 markers of form
    ``[crossref:DOI1; crossref:DOI2]`` (multi DOIs in one bracket).
    Multi-id pass splits on ``;`` and emits ``[N1][N2]``."""
    sources = [
        _src("crossref:10.1007/abc"),
        _src("crossref:10.1017/def"),
    ]
    body = "段落 [crossref:10.1007/abc; crossref:10.1017/def] 之后。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "[2]" in out
    assert "crossref:" not in out


def test_multi_doi_bracket_three_dois() -> None:
    sources = [
        _src("crossref:10.1007/abc"),
        _src("crossref:10.1017/def"),
        _src("crossref:10.2139/ghi"),
    ]
    body = "段落 [crossref:10.1007/abc; crossref:10.1017/def; crossref:10.2139/ghi] 三 DOI."
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out and "[2]" in out and "[3]" in out
    assert "crossref:" not in out


def test_multi_doi_bracket_with_unknown_piece_kept_intact() -> None:
    """When ANY piece in a multi-id bracket isn't in cited_sources,
    keep the whole bracket — avoids half-translated output."""
    sources = [_src("crossref:10.1007/abc")]
    body = "段落 [crossref:10.1007/abc; crossref:10.UNKNOWN/xyz] 之后。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[crossref:10.1007/abc; crossref:10.UNKNOWN/xyz]" in out


def test_bracketed_author_year_round_7_form() -> None:
    """Round 7 reproducer: drafter LLM emits ``[Author and Author Year]``
    (square brackets, multi-author). Previously bypassed the
    paren-only regex and survived to export."""
    sources = [
        NormalizedSource(
            source_id="s1",
            title="Real-paper round 7 source",
            authors=["Sahasrabuddhe", "Seddon"],
            year=2025,
            venue="V",
            doi=None,
            url=None,
            pdf_url=None,
            abstract="",
            source_client="crossref",
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=0.5,
            risk_flags=[],
        ),
    ]
    body = "段落 [Sahasrabuddhe and Seddon 2025] 之后。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "[Sahasrabuddhe" not in out


def test_authors_list_fix_paren_form_resolves() -> None:
    """Pre-existing bug: ``NormalizedSource.authors`` is ``list[str]``
    but PR-259b treated it as ``str`` so ``by_author_year`` was
    always empty in production. Now ``(Sahasrabuddhe 2025)`` resolves."""
    sources = [
        NormalizedSource(
            source_id="s1",
            title="X",
            authors=["Sahasrabuddhe"],
            year=2025,
            venue="V",
            doi=None,
            url=None,
            pdf_url=None,
            abstract="",
            source_client="crossref",
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=0.5,
            risk_flags=[],
        ),
    ]
    out = _normalize_inline_citations_zh("段落 (Sahasrabuddhe 2025) 之后", sources)
    assert "[1]" in out


def test_plain_n_brackets_not_corrupted_by_author_year_pass() -> None:
    """The new ``[Author Year]`` regex must NOT match plain ``[1]``,
    ``[12]`` references — those start with digit, not letter."""
    sources = [_src("crossref:10.1000/test")]
    body = "段落 [1] 与 [12] 引用。"
    out = _normalize_inline_citations_zh(body, sources)
    assert "[1]" in out
    assert "[12]" in out  # untouched


def test_bare_year_in_brackets_kept() -> None:
    """``[1980]`` is just a year in brackets, not a cite. Must
    not be replaced by the new author-year pass."""
    sources = [_src("crossref:10.1000/test")]
    out = _normalize_inline_citations_zh("某事件发生于 [1980]", sources)
    assert "[1980]" in out


def test_bracketed_unknown_author_year_kept() -> None:
    """Unknown (Author Year) tuples not in cited_sources stay raw."""
    sources = [_src("crossref:10.1000/test")]
    out = _normalize_inline_citations_zh("[Unknown 2025]", sources)
    assert "[Unknown 2025]" in out


# PR-G-CiteMarkerGate (PR-1 of 2): _normalize_inline_citations_zh_with_unresolved
# returns a CitationNormalizationResult so the drafter outer loop can gate
# on unresolved citation-shaped markers and trigger a corrective LLM retry.


def test_with_unresolved_clean_manuscript() -> None:
    """All cite-shaped markers resolve → unresolved is empty."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 [crossref:10.1093/llc/fqz046] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    assert "[1]" in result.body
    assert result.unresolved_markers == ()


def test_with_unresolved_unknown_author_year_paren_recorded() -> None:
    """``(Unknown 2025)`` is cite-shaped but no source matches →
    body keeps the marker AND it shows up in unresolved_markers
    so the gate can act."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 (Unknown 2025) 后续 (Author 2020) 已知。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    # Author 2020 resolves (sources default year=2020, surname=Author).
    assert "[1]" in result.body
    # Unknown 2025 does not resolve.
    assert "(Unknown 2025)" in result.body
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_paren" in forms
    raws = [m.raw for m in result.unresolved_markers]
    assert any("Unknown 2025" in r for r in raws)


def test_with_unresolved_unknown_author_year_bracket_recorded() -> None:
    """Round-7 form ``[Surname1 and Surname2 Year]`` not in cited_sources."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 [Sahasrabuddhe and Seddon 2025] 不在 cited_sources。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    assert "[Sahasrabuddhe and Seddon 2025]" in result.body
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" in forms


def test_with_unresolved_unknown_doi_recorded() -> None:
    """``[crossref:DOI]`` form where DOI not in cited_sources."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 [crossref:10.9999/unknown] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    assert "[crossref:10.9999/unknown]" in result.body
    forms = [m.form for m in result.unresolved_markers]
    assert "crossref_doi" in forms


def test_with_unresolved_unknown_source_id_recorded() -> None:
    """``[https://openalex.org/UNKNOWN]`` not in cited_sources."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 [https://openalex.org/UNKNOWN] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    assert "[https://openalex.org/UNKNOWN]" in result.body
    forms = [m.form for m in result.unresolved_markers]
    assert "source_id" in forms


def test_with_unresolved_multi_source_partial_unknown_recorded() -> None:
    """``[crossref:KNOWN; crossref:UNKNOWN]`` — multi-source bracket
    with at least one unknown piece keeps the whole bracket and
    records as ``multi_source_id``."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 [crossref:10.1093/llc/fqz046; crossref:10.9999/unknown] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    assert "10.9999/unknown" in result.body
    forms = [m.form for m in result.unresolved_markers]
    assert "multi_source_id" in forms


def test_with_unresolved_multi_source_chinese_semicolon_normalized() -> None:
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [
        _src("official:fraser:bog-minutes-1968-03-20"),
        _src("official:imf:annual-report-1968"),
    ]
    body = "段落 [official:fraser:bog-minutes-1968-03-20；official:imf:annual-report-1968] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)

    assert "[1][2]" in result.body
    assert "official:" not in result.body
    assert result.unresolved_markers == ()


def test_with_unresolved_empty_cited_sources_returns_empty() -> None:
    """No cited_sources → no-op rewrite AND no unresolved markers
    (we can't gate without sources to compare against)."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    body = "段落 (Smith 1980) 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, ())
    assert result.body == body
    assert result.unresolved_markers == ()


def test_with_unresolved_legacy_wrapper_returns_str() -> None:
    """The body-only wrapper still works for callers that just want
    the rewritten manuscript (exporter idempotency check)."""
    sources = [_src("crossref:10.1093/llc/fqz046")]
    body = "段落 [crossref:10.1093/llc/fqz046] 后续 [crossref:10.9999/unknown] 之后。"
    out = _normalize_inline_citations_zh(body, sources)
    assert isinstance(out, str)
    assert "[1]" in out
    assert "[crossref:10.9999/unknown]" in out


# PR-G-CiteMarkerGate PR-2b: corrective LLM retry helpers
# (paragraph grouping, paragraph splicing, source rendering).
# The actual ``_maybe_run_cite_marker_repair`` LLM-call function is
# integration-test territory; these unit tests cover the
# deterministic helpers it composes.


def test_group_unresolved_by_paragraph_basic() -> None:
    from autoessay.agents.drafter import (
        UnresolvedCitationMarker,
        _group_unresolved_by_paragraph,
    )

    body = (
        "Para A 这是 (Smith 1980) 出现的位置。\n\n"
        "Para B 这一段没有未解析 marker。\n\n"
        "Para C 又一个 [Unknown 2025] 错误标记。"
    )
    markers = (
        UnresolvedCitationMarker(
            raw="(Smith 1980)",
            form="author_year_paren",
            reason="x",
        ),
        UnresolvedCitationMarker(
            raw="[Unknown 2025]",
            form="author_year_bracket",
            reason="y",
        ),
    )
    groups = _group_unresolved_by_paragraph(body, markers)
    assert len(groups) == 2
    indices = [idx for idx, _, _ in groups]
    assert indices == [0, 2]
    # Para B (index 1) is not in the result.
    assert 1 not in indices
    # First group has the Smith marker; third group has the Unknown one.
    assert groups[0][2][0].raw == "(Smith 1980)"
    assert groups[1][2][0].raw == "[Unknown 2025]"


def test_group_unresolved_multiple_markers_same_paragraph() -> None:
    """Two unresolved markers in one paragraph → one group entry
    with both markers attached."""
    from autoessay.agents.drafter import (
        UnresolvedCitationMarker,
        _group_unresolved_by_paragraph,
    )

    body = "段落 (A 2020) 然后 [B 2021] 同一段。"
    markers = (
        UnresolvedCitationMarker(raw="(A 2020)", form="author_year_paren", reason="x"),
        UnresolvedCitationMarker(
            raw="[B 2021]",
            form="author_year_bracket",
            reason="y",
        ),
    )
    groups = _group_unresolved_by_paragraph(body, markers)
    assert len(groups) == 1
    assert len(groups[0][2]) == 2


def test_splice_repaired_paragraphs_basic() -> None:
    from autoessay.agents.drafter import _splice_repaired_paragraphs

    body = "P0 original.\n\nP1 original.\n\nP2 original."
    repaired = {0: "P0 repaired.", 2: "P2 repaired."}
    out = _splice_repaired_paragraphs(body, repaired)
    assert "P0 repaired." in out
    assert "P1 original." in out  # untouched
    assert "P2 repaired." in out
    # Order preserved.
    assert (
        out.index("P0 repaired.")
        < out.index("P1 original.")
        < out.index(
            "P2 repaired.",
        )
    )


def test_splice_out_of_range_index_silently_ignored() -> None:
    """Robustness: LLM may emit invalid index; we don't crash."""
    from autoessay.agents.drafter import _splice_repaired_paragraphs

    body = "P0.\n\nP1."
    out = _splice_repaired_paragraphs(body, {99: "ignored"})
    assert out == body


def test_format_sources_for_repair_prompt_truncates() -> None:
    """Bounded prompt: many sources → truncate marker appended."""
    from autoessay.agents.drafter import _format_sources_for_repair_prompt

    sources = [_src(f"crossref:10.1000/{i}") for i in range(200)]
    out = _format_sources_for_repair_prompt(sources, max_chars=500)
    assert "(truncated)" in out
    # All entries shouldn't be present.
    assert out.count("source_id=") < 200


def test_format_sources_for_repair_prompt_compact() -> None:
    """Each source one line; first 3 authors + et al for >3."""
    from autoessay.agents.drafter import _format_sources_for_repair_prompt

    sources = [
        NormalizedSource(
            source_id="crossref:10.1000/abc",
            title="A clear title",
            authors=["A", "B", "C", "D", "E"],
            year=2020,
            venue="V",
            doi="10.1000/abc",
            url=None,
            pdf_url=None,
            abstract="",
            source_client="crossref",
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=0.5,
            risk_flags=[],
        ),
    ]
    out = _format_sources_for_repair_prompt(sources)
    assert "crossref:10.1000/abc" in out
    assert "et al." in out
    assert "doi=10.1000/abc" in out


def test_bracket_authoryear_regex_does_not_eat_crossref_doi() -> None:
    """Round 9 zh real-paper regression: the PR #290
    ``[Author Year]`` bracket regex was over-matching scheme-
    prefixed identifiers like ``[crossref:10.32629/as.v8i11.3452]``
    (letter ``c`` cleared the prefix gate, ``3452`` matched
    ``\\d{4}``). Lookup failed → marker recorded as unresolved
    → ``cite_marker_unresolved`` event fired spuriously even though
    the dedicated ``[crossref:]`` regex below it correctly normalized
    the marker. Now scheme-prefixed brackets are excluded from the
    author-year pass entirely."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.32629/as.v8i11.3452")]
    body = "段落 [crossref:10.32629/as.v8i11.3452] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    # Body normalizes via the [crossref:] resolver downstream.
    assert "[1]" in result.body
    # Crucially — no spurious unresolved entry from author_year_bracket.
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" not in forms
    assert "crossref_doi" not in forms


def test_bracket_authoryear_regex_excludes_openalex_url() -> None:
    """Same exclusion for ``[https://openalex.org/Wxxxx]`` and friends."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("https://openalex.org/W4408365924")]
    body = "段落 [https://openalex.org/W4408365924] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    assert "[1]" in result.body
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" not in forms


def test_paren_authoryear_regex_excludes_scheme_prefix_too() -> None:
    """Drafter LLM rarely emits ``(crossref:...)`` but the same gate
    is applied to the paren regex for symmetry."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1000/foo")]
    # If LLM ever emits this paren form, we should not spuriously
    # flag it as author_year_paren. The source-id resolver should own
    # scheme-prefixed parens and normalize known cited sources.
    body = "段落 (crossref:10.1000/foo) 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    assert "[1]" in result.body
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_paren" not in forms
    assert "source_id" not in forms


# Codex round-1 review amendments (2026-05-08): generic URI-scheme
# guard tests — covers arxiv:/doi:/pmcid:/pmid:/isbn:/issn: + future
# RFC-3986 schemes that the original allowlist would have missed.


def test_arxiv_scheme_not_eaten_by_authoryear() -> None:
    """``[arxiv:2401.12345]`` should not register as author-year
    (the prior allowlist only blocked crossref/openalex/cnki/https)."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1000/test")]
    body = "段落 [arxiv:2401.12345] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" not in forms


def test_doi_scheme_not_eaten_by_authoryear() -> None:
    """``[doi:10.1000/foo]`` form."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1000/test")]
    body = "段落 [doi:10.1000/foo2024] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" not in forms


def test_pmcid_scheme_not_eaten_by_authoryear() -> None:
    """PubMed Central style identifiers."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1000/test")]
    body = "段落 [pmcid:PMC12345] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" not in forms


def test_isbn_issn_scheme_not_eaten_by_authoryear() -> None:
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1000/test")]
    body = "段落 [isbn:9781234567890] 与 [issn:0028-0836] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" not in forms


def test_unknown_future_scheme_excluded() -> None:
    """Generic guard: any letter-prefixed scheme is excluded, so
    future schemes like ``[zenodo:...]`` / ``[ssrn:...]`` won't
    regress this bug."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1000/test")]
    body = "段落 [zenodo:zen.12345.2024] 与 [ssrn:abstract_id_2025] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" not in forms


def test_fullwidth_paren_with_scheme_prefix_not_eaten() -> None:
    """Fullwidth ``（...）`` paren regex covers the same scheme
    exclusion (codex risk: prior fix only had bracket coverage)."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("crossref:10.1000/test")]
    # Fullwidth paren wrapping a scheme-prefixed id.
    body = "段落 （crossref:10.1000/foo2024） 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_paren" not in forms


def test_real_openalex_url_full_form_not_eaten() -> None:
    """Cross-check: the actual openalex URL form
    ``[https://openalex.org/W4408365924]`` was the round 9
    reproducer; it stays correctly resolved by the dedicated
    ``[<URL>]`` resolver and is NOT flagged as author-year."""
    from autoessay.agents.drafter import (
        _normalize_inline_citations_zh_with_unresolved,
    )

    sources = [_src("https://openalex.org/W4408365924")]
    body = "段落 [https://openalex.org/W4408365924] 之后。"
    result = _normalize_inline_citations_zh_with_unresolved(body, sources)
    assert "[1]" in result.body
    forms = [m.form for m in result.unresolved_markers]
    assert "author_year_bracket" not in forms
    # And no spurious crossref_doi or source_id mismatch either —
    # the dedicated URL resolver maps it to [1].
    assert "source_id" not in forms
