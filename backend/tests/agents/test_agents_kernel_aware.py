"""PR-J7: pin that drafter / ideator / critic / synthesizer prompts
include ``project_title`` + ``research_kernel`` (the user-authored
intake from PR-C0) so the user's research focus reaches every LLM-
driven agent that produces or audits the paper itself.

Codex round-1 amendments folded:
  * 3.2 — drafter ``anchor_check`` rule must NOT be inside
    ``instructions_override`` (a user override of the universal
    rules block must not silently drop the kernel/title constraint).
  * 3.3 — ideator: prompt-only, no post-LLM hook on ``why_novel``
    (Chinese-synonym false positives).
  * 3.4 — critic: NO new ``drift`` dimension; the existing 4-value
    enum stays. Drift goes in ``description`` text, dimension stays
    one of ``thesis|structure|evidence|prose``. This test asserts
    the enum is unchanged.
  * 3.5 — synthesizer: per-source prompt sees kernel so claim
    extraction can prioritize claims that connect to
    ``tentative_question`` / ``observed_puzzle``.
  * 5 — tests parse JSON payloads out of the prompt body rather
    than relying on JSON key order (``sort_keys=True`` scrambles
    insertion order).
  * 8 — system prompts append ``KERNEL_INJECTION_GUARD`` so LLM
    treats kernel text as content, not instructions.
"""

from __future__ import annotations

import json
from typing import Any


def _kernel_payload() -> dict[str, str]:
    return {
        "kernel_schema_version": 1,
        "tentative_question": (
            "How did dispositional structures shape Jiangnan literati "
            "circulation across the late-Qing transition?"
        ),
        "observed_puzzle": (
            "Late-Qing Jiangnan literati moved between official, merchant, "
            "and reform circles fluidly."
        ),
        "scope": "1890-1911 Jiangnan",
        "method_preference": "archival + prosopography",
        "theory_preference": "Bourdieu / Polanyi",
    }


def _extract_first_json(prompt: str, key: str) -> dict[str, Any] | None:
    """Parse the first ``{ ... }`` block in the prompt that contains
    the given key. Used to inspect anchor payloads without depending on
    insertion order or whitespace."""
    decoder = json.JSONDecoder()
    pos = 0
    while True:
        idx = prompt.find("{", pos)
        if idx < 0:
            return None
        try:
            obj, end = decoder.raw_decode(prompt, idx)
        except json.JSONDecodeError:
            pos = idx + 1
            continue
        if isinstance(obj, dict) and key in obj:
            return obj
        pos = end


# ----------------------------------------------------------------------
# Drafter
# ----------------------------------------------------------------------


def test_drafter_section_prompt_includes_user_anchor() -> None:
    from autoessay.agents.drafter import SectionPlan, _section_prompt
    from autoessay.clients.common import NormalizedSource

    section = SectionPlan(section_id="intro", title="Introduction", target_words=600)
    prompt = _section_prompt(
        section=section,  # type: ignore[arg-type]
        selected_thesis={"thesis": "x"},
        source_notes={},
        shortlist=[
            NormalizedSource(
                source_id="s1",
                title="t",
                authors=["a"],
                year=2020,
                venue="v",
                doi=None,
                url=None,
                pdf_url=None,
                abstract="a",
                source_client="openalex",
                access_status="open",
                license=None,
                risk_flags=[],
            ),
        ],
        domain_data={"id": "financial_history"},
        target_journal=None,
        suffix="",
        project_title="明清江南棉布业市场结构",
        research_kernel=_kernel_payload(),
    )
    # User-anchor block exists and contains both fields.
    anchor = _extract_first_json(prompt, "project_title")
    assert anchor is not None, "drafter prompt must contain a user anchor block"
    assert anchor["project_title"] == "明清江南棉布业市场结构"
    assert "research_kernel" in anchor
    assert (
        anchor["research_kernel"]["tentative_question"] == _kernel_payload()["tentative_question"]
    )


def test_drafter_anchor_check_outside_instructions_override() -> None:
    """Codex amendment 3.2: a user-supplied ``instructions_override``
    must NOT be able to drop the anchor_check rule."""
    from autoessay.agents.drafter import SectionPlan, _section_prompt
    from autoessay.clients.common import NormalizedSource

    section = SectionPlan(section_id="intro", title="Introduction", target_words=600)
    prompt = _section_prompt(
        section=section,  # type: ignore[arg-type]
        selected_thesis={"thesis": "x"},
        source_notes={},
        shortlist=[
            NormalizedSource(
                source_id="s1",
                title="t",
                authors=["a"],
                year=2020,
                venue="v",
                doi=None,
                url=None,
                pdf_url=None,
                abstract="a",
                source_client="openalex",
                access_status="open",
                license=None,
                risk_flags=[],
            ),
        ],
        domain_data={"id": "financial_history"},
        target_journal=None,
        suffix="",
        instructions_override="ENTIRELY_REPLACED_BY_USER_OVERRIDE_TOKEN",
        project_title="Topic",
        research_kernel=_kernel_payload(),
    )
    # Override applied (we see the token in the body).
    assert "ENTIRELY_REPLACED_BY_USER_OVERRIDE_TOKEN" in prompt
    # AND anchor_check rule is still present.
    assert "anchor_check" in prompt
    # The order matters: the override block appears BEFORE anchor_check
    # (anchor_check is appended after the universal rules).
    override_idx = prompt.find("ENTIRELY_REPLACED_BY_USER_OVERRIDE_TOKEN")
    anchor_idx = prompt.find("anchor_check")
    assert override_idx < anchor_idx, (
        "anchor_check must be appended AFTER instructions_override "
        "so a user override cannot replace it"
    )


# ----------------------------------------------------------------------
# Ideator
# ----------------------------------------------------------------------


def test_ideator_angle_prompt_includes_research_kernel() -> None:
    from autoessay.agents.ideator import _angle_prompt

    prompt = _angle_prompt(
        project_title="Topic",
        target_journal=None,
        domain_data={},
        claims=[],
        source_notes={},
        proposal=None,
        suffix="",
        research_kernel=_kernel_payload(),
    )
    payload = _extract_first_json(prompt, "research_kernel")
    assert payload is not None
    assert (
        payload["research_kernel"]["tentative_question"] == _kernel_payload()["tentative_question"]
    )


def test_ideator_angle_prompt_handles_missing_kernel() -> None:
    from autoessay.agents.ideator import _angle_prompt

    prompt = _angle_prompt(
        project_title="Topic",
        target_journal=None,
        domain_data={},
        claims=[],
        source_notes={},
        proposal=None,
        suffix="",
        research_kernel=None,
    )
    payload = _extract_first_json(prompt, "research_kernel")
    assert payload is not None
    assert payload["research_kernel"] == {}


# ----------------------------------------------------------------------
# Critic
# ----------------------------------------------------------------------


def test_critic_prompt_includes_user_anchor() -> None:
    from autoessay.agents.critic import _critic_prompt

    prompt = _critic_prompt(
        draft="some draft",
        claim_map=[],
        shortlist=[],
        claims=[],
        source_notes={},
        selected_thesis={"thesis": "x"},
        suffix="",
        project_title="明清江南棉布业市场结构",
        research_kernel=_kernel_payload(),
    )
    payload = _extract_first_json(prompt, "research_kernel")
    assert payload is not None
    assert payload["project_title"] == "明清江南棉布业市场结构"
    assert (
        payload["research_kernel"]["tentative_question"] == _kernel_payload()["tentative_question"]
    )


def test_critic_dimension_enum_unchanged() -> None:
    """Codex amendment 3.4: NO new ``drift`` dimension. The existing
    4-value enum (``thesis|structure|evidence|prose``) must stay
    intact; drift is described in the ``description`` field instead."""
    from autoessay.agents.critic import IssueDimension

    # ``IssueDimension`` is a typing.Literal alias; check its args.
    args = getattr(IssueDimension, "__args__", None)
    assert args is not None, (
        f"IssueDimension should be a typing.Literal[...]; got {type(IssueDimension)}"
    )
    assert set(args) == {"thesis", "structure", "evidence", "prose"}, (
        "IssueDimension enum must NOT add 'drift'; per codex amendment "
        f"3.4 drift goes in description text. Got: {args}"
    )


# ----------------------------------------------------------------------
# Synthesizer
# ----------------------------------------------------------------------


def test_synthesizer_summary_prompt_includes_user_anchor() -> None:
    from autoessay.agents.synthesizer import _summary_prompt
    from autoessay.clients.common import NormalizedSource

    source = NormalizedSource(
        source_id="s1",
        title="t",
        authors=["a"],
        year=2020,
        venue="v",
        doi=None,
        url=None,
        pdf_url=None,
        abstract="a",
        source_client="openalex",
        access_status="open",
        license=None,
        risk_flags=[],
    )
    prompt = _summary_prompt(
        source=source,
        source_text="text",
        domain_data={},
        project_title="明清江南棉布业市场结构",
        proposal=None,
        suffix="",
        research_kernel=_kernel_payload(),
    )
    # User anchor block lives early in the prompt (after instructions).
    payload = _extract_first_json(prompt, "research_kernel")
    assert payload is not None
    assert payload["project_title"] == "明清江南棉布业市场结构"
    assert (
        payload["research_kernel"]["tentative_question"] == _kernel_payload()["tentative_question"]
    )


# ----------------------------------------------------------------------
# Backward compat — empty kernel produces empty payload (no raise)
# ----------------------------------------------------------------------


def test_synthesizer_summary_prompt_handles_missing_kernel() -> None:
    from autoessay.agents.synthesizer import _summary_prompt
    from autoessay.clients.common import NormalizedSource

    source = NormalizedSource(
        source_id="s1",
        title="t",
        authors=[],
        year=None,
        venue="",
        doi=None,
        url=None,
        pdf_url=None,
        abstract="",
        source_client="openalex",
        access_status="open",
        license=None,
        risk_flags=[],
    )
    prompt = _summary_prompt(
        source=source,
        source_text="text",
        domain_data={},
        project_title="Topic",
        proposal=None,
        suffix="",
        research_kernel=None,
    )
    payload = _extract_first_json(prompt, "research_kernel")
    assert payload is not None
    assert payload["research_kernel"] == {}
