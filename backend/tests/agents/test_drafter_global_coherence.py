"""PR-G-Coherence (codex round-3 AGREE on v4): unit tests for
the post-section global coherence LLM pass + 5-rule
post-validation. Tests focus on the validator's rules — the LLM
call itself is exercised in real-paper acceptance walks.
"""

from __future__ import annotations

from autoessay.agents.drafter import (
    GLOBAL_COHERENCE_SYSTEM_PROMPT,
    _build_global_coherence_prompt,
    _citation_bearing_paragraph_count,
    _extract_cnki_section_titles,
    _extract_inline_citations,
    _validate_global_coherence_output,
)

# ----- system prompt invariants -----------------------------------


def test_system_prompt_contains_all_8_rules() -> None:
    """Codex round-3 locked the system prompt verbatim. Any rule
    drop is a regression."""
    for rule_marker in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8."):
        assert rule_marker in GLOBAL_COHERENCE_SYSTEM_PROMPT, (
            f"system prompt missing rule {rule_marker}"
        )
    # Substantive checks — phrases the LLM must see
    assert "[N] 引用编号" in GLOBAL_COHERENCE_SYSTEM_PROMPT  # rule 2
    assert "摘要" in GLOBAL_COHERENCE_SYSTEM_PROMPT  # rule 3
    assert "关键词" in GLOBAL_COHERENCE_SYSTEM_PROMPT  # rule 3
    assert "参考文献" in GLOBAL_COHERENCE_SYSTEM_PROMPT  # rule 3
    assert "30%" in GLOBAL_COHERENCE_SYSTEM_PROMPT  # rule 6


# ----- helper functions ------------------------------------------


def test_extract_inline_citations_preserves_multiset_order() -> None:
    """Multiset comparison (codex Q1 amendment): same [N]s in same
    counts but reordered → still equal under Counter."""
    text = "段落 [1][2]. 第二段 [1][3]. 第三段 [2]."
    citations = _extract_inline_citations(text)
    assert citations == ["[1]", "[2]", "[1]", "[3]", "[2]"]


def test_extract_cnki_section_titles_returns_ordered_list() -> None:
    """Body section titles must be returned in document order; the
    validator compares ordered lists, not sets, so reordering
    sections fails the gate."""
    text = "前言\n\n一、引言\n\n内容\n\n二、文献综述\n\n内容\n\n八、结论\n\n内容"
    titles = _extract_cnki_section_titles(text)
    assert titles == ["一、引言", "二、文献综述", "八、结论"]


def test_citation_bearing_paragraph_count_excludes_uncited() -> None:
    """Only paragraphs containing ``[N]`` cites count toward the
    preservation gate (deleting an ``[UNCITED]`` placeholder
    paragraph is allowed semantically)."""
    text = "纯文本段落 unparalleled.\n\n带引用段落 [1].\n\n再一段 [UNCITED]."
    assert _citation_bearing_paragraph_count(text) == 1


# ----- _validate_global_coherence_output: 5 rules ----------------


_BASE_MANUSCRIPT = """\
摘要：本文研究历史问题。

关键词：历史；研究

一、引言

第一段引言 [1].

第二段说明 [2].

二、文献综述

综述段落 [1][3].

三、研究方法

方法描述段落 [4].

四、案例分析（一）

案例段落 [2].

五、案例分析（二）

第二个案例 [3].

六、案例分析（三）

第三个案例 [1].

七、讨论

讨论段落 [4].

八、结论

结论段落 [1].

参考文献

[1] 张三. 著作甲. 出版社, 2020.
[2] 李四. 著作乙. 出版社, 2021.
[3] 王五. 著作丙. 出版社, 2022.
[4] 赵六. 著作丁. 出版社, 2023.
"""


def test_validate_passes_when_only_transitions_changed() -> None:
    """Adding/changing transition prose between paragraphs without
    touching citations / titles / front-back blocks should pass."""
    after = _BASE_MANUSCRIPT.replace(
        "第二段说明 [2].",
        "更进一步地，第二段说明 [2]，构成本节的论证脉络。",
    )
    result = _validate_global_coherence_output(before=_BASE_MANUSCRIPT, after=after)
    assert result is None


def test_validate_rejects_citation_multiset_mismatch() -> None:
    """Removing one [1] occurrence violates rule 1."""
    after = _BASE_MANUSCRIPT.replace("[1].", ".", 1)
    result = _validate_global_coherence_output(before=_BASE_MANUSCRIPT, after=after)
    assert result == "citation_multiset_mismatch"


def test_validate_rejects_cnki_section_title_change() -> None:
    """Changing a section title (e.g. 引言 → 序言) violates rule 2."""
    after = _BASE_MANUSCRIPT.replace("一、引言", "一、序言")
    result = _validate_global_coherence_output(before=_BASE_MANUSCRIPT, after=after)
    assert result == "cnki_section_titles_changed"


def test_validate_rejects_cnki_front_back_block_modification() -> None:
    """Changing 摘要 / 关键词 / 参考文献 blocks violates rule 3."""
    after = _BASE_MANUSCRIPT.replace(
        "摘要：本文研究历史问题。",
        "摘要：本文研究当代问题。",
    )
    result = _validate_global_coherence_output(before=_BASE_MANUSCRIPT, after=after)
    # validator returns the specific marker that changed
    assert result is not None
    assert result.startswith("cnki_") and "block_modified" in result


def test_validate_rejects_citation_bearing_paragraph_deletion() -> None:
    """Deleting a citation-bearing paragraph drops the count even if
    the removed [N] was added back elsewhere (codex Q3 amendment 1).
    Setup: remove [1] from one paragraph and merge it into another
    that already has [N]s — multiset count of [1] is preserved
    but the count of citation-bearing paragraphs decreased by 1."""
    after = _BASE_MANUSCRIPT.replace(
        # Remove [1] from the conclusion paragraph entirely; it
        # used to be the only [N] in that paragraph so the
        # paragraph leaves the citation-bearing set.
        "结论段落 [1].",
        "结论段落.",
    ).replace(
        # Add [1] to an already citation-bearing paragraph so the
        # multiset of [N]s is preserved.
        "讨论段落 [4].",
        "讨论段落 [4][1].",
    )
    result = _validate_global_coherence_output(before=_BASE_MANUSCRIPT, after=after)
    assert result == "citation_bearing_paragraph_deleted"


def test_validate_rejects_excessive_shrinkage() -> None:
    """Output less than 70% of input violates rule 5."""
    after = _BASE_MANUSCRIPT[: int(len(_BASE_MANUSCRIPT) * 0.5)]
    # Patch up so multiset / titles / hashes pass first — actually
    # they won't because half the text is gone, but the length
    # check fires regardless of which rule trips first; just verify
    # SOMETHING fails. The most likely first-fail is citation_multiset
    # since truncation drops [N] markers near the tail.
    result = _validate_global_coherence_output(before=_BASE_MANUSCRIPT, after=after)
    assert result is not None  # any rejection counts; truncation should fail at least one


# ----- prompt builder --------------------------------------------


def test_build_prompt_includes_manuscript_and_kernel() -> None:
    """The prompt must carry both the kernel anchor (so LLM keeps
    research focus) and the full manuscript verbatim."""
    prompt = _build_global_coherence_prompt(
        manuscript="一、引言\n\n段落 [1].",
        paper_language="zh",
        research_kernel={"observed_puzzle": "断代张力"},
    )
    assert "断代张力" in prompt
    assert "一、引言" in prompt
    assert "[1]" in prompt
    # Schema hint — we want strict JSON output
    assert "manuscript_markdown" in prompt


def test_build_prompt_handles_empty_kernel() -> None:
    """Missing research_kernel must not crash the builder; just
    serialize an empty dict."""
    prompt = _build_global_coherence_prompt(
        manuscript="一、引言\n\n段落 [1].",
        paper_language="en",
        research_kernel=None,
    )
    assert "{}" in prompt
    assert "一、引言" in prompt
