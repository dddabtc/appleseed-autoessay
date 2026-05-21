"""PR-263c — shadow knowledge prompt-injection helper.

Codex round-3 verdict (PR-263c, /tmp/codex_pr263c_design.md →
/tmp/codex_pr263c_reply.md): path 4 — inject the shadow baseline's
``argument_map`` and ``reference_candidates`` into drafter /
synthesizer prompts as "contextual knowledge" the LLM may MENTION
(in prose) but MAY NOT cite as ``[N]`` references unless the same
source has independently entered the pipeline's verified
``cited_sources`` shortlist.

Why this exists: empirical PR-263b validation (10:39 UTC) showed
OpenLibrary fallback verified only 1 of 15 LLM-emitted candidates
on a 中文人文 kernel (the rest are 9787-prefix mainland books that
no English-language metadata source covers reliably). Codex Q4:
the contextual-knowledge path lets the manuscript "feel like it
knows the field" — drafter can write 「正如郑振铎在《中国俗文学
史》中所论」without faking a [N] citation, because the directive
explicitly forbids that. This restores academic-tradition feel
without the 合规性 contamination of merging unverified refs.

Module-level invariants:
- The injection block is empty when no shadow_baseline artifact
  is on disk for the given run (callers can ALWAYS call this
  helper; it returns "" gracefully).
- The directive is compact (<= ~800 tokens) so it doesn't crowd
  out the section's working memory budget. We only inject the top
  N argument_map entries + reference_candidates per call.
- The directive's policy line is verbatim across drafter +
  synthesizer + ideator so all kernel-aware agents apply the
  same "mention but don't cite" rule.

PR-263c v1 scope is intentionally narrow:
- helper module producing a directive string from a loaded
  ShadowBaselineOutput
- drafter `_section_prompt` consumer (this PR)
- synthesizer `_summary_prompt` consumer follow-up
- NO automatic shadow_baseline trigger from a phase yet (PR-263d)

PR-263d will wire shadow_baseline to actually run before drafter
so the consumer in this PR has something to load. For now PR-263c
is dead code in the production path until PR-263d lands; tests
exercise the consumer with a fixture artifact on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoessay.agents.shadow_baseline import (
    ShadowBaselineOutput,
)

# Cap how many argument_map / reference_candidates we surface so a
# single shadow baseline can't blow the per-section prompt budget.
# Drafter sections run with max_tokens=4500 (PR-257b); leaving
# enough headroom for kernel + outline + section role + universal
# rules + topic_relevance directive means shadow knowledge gets
# at most ~6 + ~8 entries.
_MAX_ARGUMENT_MAP_INJECTED = 6
_MAX_REFERENCE_CANDIDATES_INJECTED = 8

# Verbatim policy line — same across drafter / synthesizer / ideator
# so any of them applies the "mention but don't cite" rule
# identically. Codex Q4: explicit ban so the LLM doesn't synthesize
# a [N] citation by accident.
_INJECTION_POLICY = (
    " shadow_knowledge_policy: 下面是为本题目准备的 background "
    "context document。以下 ``argument_map`` 与 ``reference_candidates`` "
    "来自 shadow baseline 的模型背景知识，仅用作论证结构 / 经典背景"
    "知识 / 参考写作框架；不要在正文中引用其中提到的具体文献、年份、"
    "作者、统计数字，除非这些已在 ``Approved sources`` / shortlist 中"
    "独立验证过。不允许据此生成 ``[N]`` 引用、伪造 DOI / ISBN / 页码、"
    "写入 ``claim_map.source_ids``。仅当 ``Approved sources`` 列表中"
    "已经包含相同 source 时，方可对其使用 ``[N]`` 引用。任何对仅在"
    "``shadow_knowledge`` 中出现的著作的提及，必须是未编号的背景性"
    "散文表述，而不是脚注或 ``[N]`` 标记。"
)


def _compact_argument_map(
    output: ShadowBaselineOutput,
    limit: int = _MAX_ARGUMENT_MAP_INJECTED,
) -> list[dict[str, object]]:
    """Trim each entry's ``key_evidence`` list to its first 2 items
    so the injected JSON stays small. Caller controls how many
    entries to keep via ``limit``."""
    compacted: list[dict[str, object]] = []
    for entry in output.argument_map[:limit]:
        compacted.append(
            {
                "section_id": entry.section_id,
                "central_claim": entry.central_claim,
                "key_evidence": list(entry.key_evidence)[:2],
            },
        )
    return compacted


def _compact_reference_candidates(
    output: ShadowBaselineOutput,
    limit: int = _MAX_REFERENCE_CANDIDATES_INJECTED,
) -> list[dict[str, object]]:
    """Drop the ``why_relevant`` field (it bloats the prompt and
    was advisory anyway) and cap the list. Author / year / title /
    venue are enough for the LLM to recognize the work."""
    compacted: list[dict[str, object]] = []
    for cand in output.reference_candidates[:limit]:
        compacted.append(
            {
                "author": cand.author,
                "year": cand.year,
                "title": cand.title,
                "venue": cand.venue,
                "type": cand.type,
            },
        )
    return compacted


def build_shadow_knowledge_directive(
    output: ShadowBaselineOutput | None,
) -> str:
    """Compose the prompt directive for the given shadow baseline,
    or ``""`` when no artifact is available. The directive includes:

    1. The compact ``argument_map`` (≤6 entries, ≤2 evidence each)
    2. The compact ``reference_candidates`` (≤8 entries, no
       why_relevant)
    3. The verbatim ``_INJECTION_POLICY`` enforcement line

    Empty ``argument_map`` AND empty ``reference_candidates`` →
    return ``""`` so the prompt body length is unchanged for runs
    where shadow baseline failed to produce useful content.
    """
    if output is None:
        return ""
    arg_map = _compact_argument_map(output)
    refs = _compact_reference_candidates(output)
    if not arg_map and not refs:
        return ""
    payload = {
        "argument_map": arg_map,
        "reference_candidates": refs,
    }
    return (
        " shadow_knowledge: "
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "."
        + _INJECTION_POLICY
    )


def shadow_knowledge_directive_for_run(
    run_dir: str | Path,
) -> str:
    """Convenience wrapper: load the shadow baseline artifact from
    ``run_dir/shadow_baseline/baseline_v001.json`` (PR-262 storage
    layout), build the directive, return it. Returns ``""`` when:
    - no artifact exists yet (run hasn't called shadow_baseline)
    - artifact is corrupt
    - artifact has no usable argument_map / reference_candidates

    All three failure modes are silent — drafter / synthesizer
    callers should always invoke this and accept the empty string
    when shadow baseline isn't ready."""
    from autoessay.agents.shadow_baseline import load_shadow_baseline

    output = load_shadow_baseline(run_dir)
    return build_shadow_knowledge_directive(output)


__all__ = [
    "build_shadow_knowledge_directive",
    "shadow_knowledge_directive_for_run",
]
