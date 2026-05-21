"""PR-262 — shadow-baseline runner.

Validates the agent module's contract:
- stub-mode returns a deterministic well-formed artifact
- prompt builder includes both project_title + research_kernel
  fields and the kernel-injection guard
- persistence round-trips the JSON artifact
- output schema rejects empty manuscript_markdown

The actual LLM call is exercised in the real-paper acceptance walk
(see /tmp/real_paper_*.log) once the backend Settings flag is
flipped OFF; we don't burn budget here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from autoessay.agents._research_kernel_prompt import KERNEL_INJECTION_GUARD
from autoessay.agents.shadow_baseline import (
    ArgumentMapEntry,
    ReferenceCandidate,
    SectionPlanEntry,
    ShadowBaselineOutput,
    _build_user_prompt,
    _stub_output,
    load_shadow_baseline,
    persist_shadow_baseline,
    run_shadow_baseline,
    shadow_baseline_paths,
)

JIANGNAN_KERNEL = {
    "scope": "以 19 世纪后期江南刊本为限，仅含序跋与刻工题记。",
    "observed_puzzle": "既有研究在断代与文体归属上存在反复张力。",
    "tentative_question": "此组文献的断代依据如何被重新建立？",
}


# ----- _stub_output -----------------------------------------------


def test_stub_output_has_all_eight_section_ids() -> None:
    out = _stub_output()
    plan_ids = {entry.section_id for entry in out.section_plan}
    map_ids = {entry.section_id for entry in out.argument_map}
    expected = {
        "introduction",
        "historiography",
        "sources_method",
        "empirical_section_i",
        "empirical_section_ii",
        "empirical_section_iii",
        "discussion",
        "conclusion",
    }
    assert plan_ids == expected
    assert map_ids == expected


def test_stub_output_manuscript_has_cnki_blocks() -> None:
    out = _stub_output()
    md = out.manuscript_markdown
    assert "## 摘要" in md
    assert "## 关键词" in md
    assert "## 一、引言" in md
    assert "## 八、结论" in md
    assert "## 参考文献" in md


def test_stub_output_has_at_least_one_reference_candidate() -> None:
    out = _stub_output()
    assert len(out.reference_candidates) >= 1


# ----- run_shadow_baseline (stub mode) ----------------------------


def test_run_shadow_baseline_returns_stub_when_setting_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shadow_baseline_stub default is True so this is the
    code path tests exercise — no LLM call, deterministic
    output. ``audit`` is a required arg in the LLM-call path but
    the stub branch bails out before it's used, so passing
    ``None`` via cast is fine here."""
    from typing import cast

    from autoessay.harness import AuditWriter

    out = run_shadow_baseline(
        run_id="run_test",
        project_title="测试项目",
        user_id="user_test",
        research_kernel=JIANGNAN_KERNEL,
        audit=cast(AuditWriter, None),
    )
    assert out is not None
    assert isinstance(out, ShadowBaselineOutput)
    assert "## 摘要" in out.manuscript_markdown


# ----- _build_user_prompt -----------------------------------------


def test_user_prompt_contains_kernel_fields() -> None:
    prompt = _build_user_prompt("江南刊本断代", JIANGNAN_KERNEL)
    assert "江南刊本断代" in prompt
    assert "断代依据" in prompt
    assert "刻工题记" in prompt


def test_user_prompt_contains_kernel_injection_guard() -> None:
    """The guard is applied to every kernel-aware agent (drafter /
    ideator / critic / synthesizer / scout); shadow baseline must
    apply it too so the model can't be tricked by malicious kernel
    text into rewriting its instructions."""
    prompt = _build_user_prompt("X", JIANGNAN_KERNEL)
    assert KERNEL_INJECTION_GUARD in prompt


def test_user_prompt_lists_all_eight_section_ids() -> None:
    """The schema hint enumerates the 8 body section ids so the LLM
    can't return a 5-section or 12-section plan that downstream
    consumers would reject."""
    prompt = _build_user_prompt("X", JIANGNAN_KERNEL)
    for section_id in (
        "introduction",
        "historiography",
        "sources_method",
        "empirical_section_i",
        "empirical_section_ii",
        "empirical_section_iii",
        "discussion",
        "conclusion",
    ):
        assert section_id in prompt


def test_user_prompt_with_empty_kernel_still_emits_anchor_block() -> None:
    """Edge case: kernel was never filled (legacy run). The user
    anchor JSON still emits an empty research_kernel object so the
    structure is consistent."""
    prompt = _build_user_prompt("Title", None)
    assert "Title" in prompt
    assert "research_kernel" in prompt


# ----- persistence ------------------------------------------------


def test_persist_and_load_round_trip(tmp_path: Path) -> None:
    out = ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\n测试摘要内容。\n",
        argument_map=[
            ArgumentMapEntry(
                section_id="introduction",
                central_claim="测试论点",
                key_evidence=["证据 a", "证据 b"],
            ),
        ],
        reference_candidates=[
            ReferenceCandidate(
                author="作者甲",
                year="2025",
                title="题名",
                venue="出版社",
                type="book",
                doi_or_isbn="10.x/y",
                why_relevant="for test",
            ),
        ],
        section_plan=[
            SectionPlanEntry(
                section_id="introduction",
                title="一、引言",
                target_words=1000,
                key_argument="本节核心",
            ),
        ],
    )
    json_path, md_path = persist_shadow_baseline(tmp_path, out)
    assert json_path.exists()
    assert md_path.exists()
    # JSON gets the structured form.
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["argument_map"][0]["central_claim"] == "测试论点"
    # MD gets the standalone manuscript.
    assert "## 摘要" in md_path.read_text(encoding="utf-8")

    loaded = load_shadow_baseline(tmp_path)
    assert loaded is not None
    assert loaded.manuscript_markdown == out.manuscript_markdown
    assert loaded.reference_candidates[0].author == "作者甲"


def test_load_returns_none_when_no_artifact(tmp_path: Path) -> None:
    assert load_shadow_baseline(tmp_path) is None


def test_load_returns_none_when_artifact_corrupt(tmp_path: Path) -> None:
    """Corrupt JSON → caller can regenerate without seeing a
    pydantic exception bubbling up."""
    json_path, _ = shadow_baseline_paths(tmp_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text("not valid json {{{", encoding="utf-8")
    assert load_shadow_baseline(tmp_path) is None


# ----- schema validation ------------------------------------------


def test_output_rejects_empty_manuscript() -> None:
    with pytest.raises((ValueError, ValidationError)):
        ShadowBaselineOutput(manuscript_markdown="")


def test_output_rejects_whitespace_only_manuscript() -> None:
    with pytest.raises((ValueError, ValidationError)):
        ShadowBaselineOutput(manuscript_markdown="   \n\n  \t  ")


def test_reference_candidate_accepts_null_doi() -> None:
    """Codex round-1 verdict on PR-262: null doi_or_isbn is acceptable
    because PR-263 enrichment will look up the work via title +
    author + year against Crossref / OpenAlex when no DOI is given."""
    rc = ReferenceCandidate(
        author="X",
        year="2020",
        title="T",
        doi_or_isbn=None,
    )
    assert rc.doi_or_isbn is None
