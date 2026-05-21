"""PR-263e — synthesizer mirrors PR-263c drafter wiring.

Validates the shadow_knowledge_directive parameter on
``synthesizer._summary_prompt``. The directive is appended to the
prompt verbatim; an empty string leaves the prompt structurally
identical to the pre-PR-263e shape.

These are pure-function unit tests on ``_summary_prompt``. The
integration with ``shadow_knowledge_directive_for_run`` (load from
disk → directive) is covered by the existing PR-263c
``test_shadow_knowledge_injection.py`` for the helper itself; this
file only verifies the synthesizer call site consumes it.
"""

from __future__ import annotations

from pathlib import Path

from autoessay.agents._shadow_knowledge_injection import (
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
from autoessay.agents.synthesizer import _summary_prompt
from autoessay.clients.common import AccessStatus, NormalizedSource


def _shadow_output() -> ShadowBaselineOutput:
    return ShadowBaselineOutput(
        manuscript_markdown="## 摘要\n\n测试.\n",
        argument_map=[
            ArgumentMapEntry(
                section_id="introduction",
                central_claim="序跋与刻工题记并读可重建断代依据",
                key_evidence=["序跋纪年", "刻工题记"],
            ),
        ],
        reference_candidates=[
            ReferenceCandidate(
                author="郑振铎",
                year="1938",
                title="中国俗文学史",
                venue="商务印书馆",
                type="book",
                why_relevant="经典中文文学史著作",
            ),
        ],
        section_plan=[
            SectionPlanEntry(
                section_id="introduction",
                title="一、引言",
                target_words=1500,
                key_argument="提出问题",
            ),
        ],
    )


def _source() -> NormalizedSource:
    return NormalizedSource(
        source_id="crossref:10.1000/test",
        title="Test source paper",
        authors=["Test Author"],
        year=2020,
        venue="Test Journal",
        doi="10.1000/test",
        url="https://example.test/x",
        pdf_url=None,
        abstract="This source discusses related research questions and methods.",
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=0.8,
        risk_flags=[],
    )


def _domain_data() -> dict[str, object]:
    return {"id": "general_academic", "search": {"telescope": {}}}


# ----- _summary_prompt accepts shadow_knowledge_directive ----------


def test_summary_prompt_appends_shadow_knowledge_directive_when_provided() -> None:
    """When a non-empty directive is passed, it appears verbatim in
    the assembled prompt — caller is responsible for the leading
    space and the policy line."""
    directive = build_shadow_knowledge_directive(_shadow_output())
    assert directive  # sanity: helper returned something
    prompt = _summary_prompt(
        source=_source(),
        source_text="full source text body",
        domain_data=_domain_data(),
        project_title="江南刊本断代",
        proposal=None,
        suffix="",
        research_kernel={"observed_puzzle": "断代张力"},
        shadow_knowledge_directive=directive,
    )
    assert directive in prompt
    # Policy line must be present so the LLM applies the
    # "mention but don't [N] cite" rule.
    assert "shadow_knowledge_policy" in prompt
    # Compact argument_map central_claim makes it through.
    assert "序跋与刻工题记并读可重建断代依据" in prompt
    # Compact reference_candidates author/title makes it through
    # (CJK encoding-safe; ensure_ascii=False in the helper).
    assert "郑振铎" in prompt
    assert "中国俗文学史" in prompt


def test_summary_prompt_empty_directive_leaves_prompt_structurally_identical() -> None:
    """The default ``shadow_knowledge_directive=""`` must leave the
    prompt byte-identical to the pre-PR-263e shape (so prompt_hash
    drift only happens when a baseline artifact is actually
    available)."""
    prompt_with_default = _summary_prompt(
        source=_source(),
        source_text="full source text body",
        domain_data=_domain_data(),
        project_title="江南刊本断代",
        proposal=None,
        suffix="",
        research_kernel={"observed_puzzle": "断代张力"},
    )
    prompt_with_empty = _summary_prompt(
        source=_source(),
        source_text="full source text body",
        domain_data=_domain_data(),
        project_title="江南刊本断代",
        proposal=None,
        suffix="",
        research_kernel={"observed_puzzle": "断代张力"},
        shadow_knowledge_directive="",
    )
    assert prompt_with_default == prompt_with_empty
    # And critically: no shadow_knowledge_policy line appears when
    # the directive is empty (so the LLM doesn't see policy text
    # that has nothing to apply to).
    assert "shadow_knowledge_policy" not in prompt_with_default


def test_summary_prompt_directive_appears_after_schema_block() -> None:
    """Layout invariant: the schema spec must appear before the
    shadow-knowledge directive, otherwise the LLM may parse the
    directive's policy text into the schema. The synthesizer
    prompt's ``required schema`` JSON is the last structured block;
    shadow_knowledge appends after it."""
    directive = build_shadow_knowledge_directive(_shadow_output())
    prompt = _summary_prompt(
        source=_source(),
        source_text="full source text body",
        domain_data=_domain_data(),
        project_title="江南刊本断代",
        proposal=None,
        suffix="",
        research_kernel={},
        shadow_knowledge_directive=directive,
    )
    schema_idx = prompt.find("required schema")
    directive_idx = prompt.find("shadow_knowledge_policy")
    assert schema_idx >= 0
    assert directive_idx >= 0
    assert directive_idx > schema_idx


# ----- integration: directive_for_run is empty when no artifact ----


def test_synthesizer_via_directive_loader_returns_empty_when_no_artifact(
    tmp_path: Path,
) -> None:
    """``shadow_knowledge_directive_for_run`` must return ``""``
    when no ``shadow_baseline/baseline_v001.json`` exists on disk —
    the synthesizer caller relies on this graceful empty so v0.4.0
    runs with stub_baseline_stub=True (no artifact persisted) don't
    break the prompt."""
    assert shadow_knowledge_directive_for_run(tmp_path) == ""


def test_synthesizer_via_directive_loader_picks_up_persisted_artifact(
    tmp_path: Path,
) -> None:
    """When the artifact exists on disk, the loader returns a
    non-empty directive that contains the policy line and at least
    one argument_map central_claim. This is the same contract
    PR-263c drafter relies on."""
    persist_shadow_baseline(tmp_path, _shadow_output())
    directive = shadow_knowledge_directive_for_run(tmp_path)
    assert directive != ""
    assert "shadow_knowledge_policy" in directive
    assert "序跋与刻工题记并读可重建断代依据" in directive
