"""Prompt-content regression tests for empirical_completeness guard (PR-3).

2026-05-12 round-0 v2 canary surfaced that pipeline output for empirical
topics has no LaTeX formulas, no tables, no robustness section — yet
the V2 critic gives 10/10 anyway. The fix (codex AGREE-WITH-AMENDMENTS
2026-05-12 PR-3) injects empirical scaffolding requirements into critic,
stage C rewriter, critic_loop rewriter, drafter, and stylist prompts.

These tests pin the key phrases so a future prompt rewrite doesn't
silently drop the guard. The deterministic regex validator that codex
recommended as the real enforcement layer is deferred to a follow-up PR.
"""

from __future__ import annotations

from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[2] / "src" / "autoessay" / "agents"


def _read(path: str) -> str:
    return (AGENTS_DIR / path).read_text(encoding="utf-8")


def test_critic_v2_carries_empirical_completeness_checklist() -> None:
    from autoessay.agents._critic_polish_loop import (
        CONTROLLED_POLISH_EXPERT_V2_SYSTEM_PROMPT,
    )

    prompt = CONTROLLED_POLISH_EXPERT_V2_SYSTEM_PROMPT
    # Pre-scoring audit checklist
    assert "empirical_completeness" in prompt
    assert "has_method_formula" in prompt
    assert "has_results_table_or_placeholder" in prompt
    assert "has_robustness_section" in prompt
    assert "has_variable_table" in prompt
    assert "unsupported_empirical_claims" in prompt
    assert "suspicious_numeric_results" in prompt
    # Hard score caps
    assert "methodological_rigor 不得高于 5" in prompt
    assert "evidence_strength 不得高于 5" in prompt
    assert "reproducibility 不得高于 5" in prompt
    assert "compliance 不得高于 5" in prompt
    assert "DESK_REJECT" in prompt


def test_critic_v2_does_not_double_number_rules() -> None:
    from autoessay.agents._critic_polish_loop import (
        CONTROLLED_POLISH_EXPERT_V2_SYSTEM_PROMPT,
    )

    # Earlier edit accidentally produced two "13." sections (numbering bug).
    # Regression: ensure numbered top-level rules are unique 11/12/13/14/15.
    prompt = CONTROLLED_POLISH_EXPERT_V2_SYSTEM_PROMPT
    for rule_num in ("11.", "12.", "13.", "14.", "15."):
        # At least one occurrence
        assert prompt.count(f"\n{rule_num} ") >= 1, f"missing rule {rule_num}"
    # Specifically: rule 12 should be the new empirical_completeness one,
    # rule 14 the JSON output rule (used to be 13)
    rule14_idx = prompt.index("\n14. 输出必须是严格 JSON")
    rule15_idx = prompt.index("\n15. 你必须在 JSON 中明确声明")
    assert rule14_idx < rule15_idx


def test_stage_c_rewriter_preserves_latex_tables_placeholders() -> None:
    from autoessay.agents.final_rewrite import (
        CONTROLLED_POLISH_REWRITE_SYSTEM_PROMPT,
    )

    p = CONTROLLED_POLISH_REWRITE_SYSTEM_PROMPT
    assert "empirical_preservation_guard" in p
    assert "$$...$$" in p
    assert "markdown 表格" in p
    assert "【待填】" in p
    assert "【TBD】" in p
    # Placeholder is NOT a citation directive
    assert "占位符是 editorial scaffolding" in p
    # Downgrade rule for fabricated-style claims
    assert "理论预期" in p or "若实证检验支持" in p


def test_critic_loop_rewriter_preserves_latex_tables_placeholders() -> None:
    src = _read("critic_loop.py")
    assert "empirical_preservation_guard" in src
    assert "$$...$$" in src
    assert "【待填】" in src
    # Same placeholder ≠ citation rule
    assert "占位符是 editorial scaffolding" in src


def test_drafter_carries_empirical_completeness_guard() -> None:
    src = _read("drafter.py")
    assert "empirical_completeness_guard" in src
    assert "LaTeX model equation" in src
    assert "markdown" in src and "variable-definition table" in src
    assert "【待填】" in src
    assert "Never invent coefficients" in src
    # Placeholders are editorial scaffolding, not citations
    assert "Placeholders such as 【待填】 are editorial scaffolding" in src


def test_stylist_guard_constant_carries_preservation_phrases() -> None:
    from autoessay.agents.stylist import _STYLIST_EMPIRICAL_PRESERVATION_GUARD

    g = _STYLIST_EMPIRICAL_PRESERVATION_GUARD
    assert "empirical_preservation_guard" in g
    assert "LaTeX equations" in g
    assert "【待填】" in g
    assert "Placeholders are editorial scaffolding" in g
    assert "downgrade it" in g.lower()


def test_stylist_section_prompt_carries_guard() -> None:
    src = _read("stylist.py")
    # _section_prompt and _repolish_prompt both inject the guard before schema
    assert src.count("_STYLIST_EMPIRICAL_PRESERVATION_GUARD") >= 2


def test_stylist_guard_does_not_widen_validator_sentinel_regex() -> None:
    # The existing hard validator regex bans [UNCITED], TODO, FIXME, <TODO.
    # 【待填】 / 【TBD】 / 【待补】 / [FILL] must not match this regex so the
    # guard's recommended placeholders pass validation.

    from autoessay.agents._round0_helpers import _SENTINEL_RE

    sample = "本节包含【待填】占位，以及【TBD】、【待补】、[FILL] 三种变体。"
    matches = _SENTINEL_RE.findall(sample)
    assert matches == [], f"sentinel regex incorrectly flagged placeholders: {matches}"


def test_stage_b_open_prompt_carries_conditional_empirical_support() -> None:
    """2026-05-12 PR-361: PR-360 D+ canary showed Stage B gpt-5.5 streaming
    runs end-to-end but the model defaults to prose-only empirical design,
    no LaTeX / no markdown tables / no 待填 placeholders — because the
    open prompt left it free to choose. User direction: only require
    formulas/tables when conclusions actually need them ("如需公式、数据
    证明结论，则必须加"), not unconditionally. The turn-2 directive must
    encode this conditional rule.
    """
    from autoessay.agents.final_rewrite import _OPEN_PROMPT_TURN_2

    p = _OPEN_PROMPT_TURN_2
    # The conditional trigger ("if conclusions need formula/data support")
    assert "结论需要" in p
    # The mandated artifacts when the trigger fires
    assert "公式" in p
    assert "LaTeX" in p
    assert "$$" in p
    assert "markdown 表" in p
    assert "【待填】" in p
    # The anti-fabrication backstop — Stage B must not invent coefficients
    # / p-values / etc. when data is missing; prefer 【待填】.
    assert "宁可用【待填】" in p or "不要编造" in p


def test_validator_still_bans_legacy_todo_uncited() -> None:
    # Inverse check: legacy markers must still fail so the validator
    # doesn't regress along with the new placeholders.

    from autoessay.agents._round0_helpers import _SENTINEL_RE

    bad_samples = [
        "[UNCITED] 段落",
        "TODO_EVIDENCE: 补数据",
        "FIXME 这一段",
        "<TODO 待补>",
    ]
    for sample in bad_samples:
        assert _SENTINEL_RE.search(sample), f"sentinel regex missed: {sample!r}"
