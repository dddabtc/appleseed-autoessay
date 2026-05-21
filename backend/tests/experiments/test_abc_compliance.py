from __future__ import annotations

from autoessay.experiments.abc_compliance import repair_manuscript


def test_repair_normalizes_citations_and_removes_sentinels() -> None:
    result = repair_manuscript(
        "# 题目\n\n摘要\n\n正文引用【1】与（张三，2020）。{{TODO}}\n\n参考文献\n1. 张三. 文献.\n"
    )

    assert "【1】" not in result.manuscript
    assert "[1]" in result.manuscript
    assert "(张三,2020)" in result.manuscript
    assert "{{TODO}}" not in result.manuscript
    assert result.status == "passed"
    assert "citation_markers_normalized" in result.operations


def test_repair_aligns_reference_list() -> None:
    result = repair_manuscript(
        "# 题目\n\n## 引言\n\n正文 [1][2][3]\n\n### 参考文献\n1. A\n2. B\n4. D\n"
    )

    assert "[3]" not in result.manuscript
    assert "[1] A" in result.manuscript
    assert "[2] B" in result.manuscript
    assert "D" not in result.manuscript
    assert "orphan_citations_removed" in result.operations
    assert "uncited_reference_entries_removed" in result.operations


def test_repair_records_blocker_when_reference_list_missing() -> None:
    result = repair_manuscript("# 题目\n\n正文 [1]\n")

    assert result.manuscript == "# 题目\n\n正文 [1]\n"
    assert result.status == "blocked"
    assert result.blockers == ("reference_list_missing_for_numeric_citations",)


def test_repair_keeps_reference_list_for_author_year_only_citations() -> None:
    result = repair_manuscript("# 题目\n\n正文（张三，2020）。\n\n参考文献\n张三. 文献.\n")

    assert "(张三,2020)" in result.manuscript
    assert "张三. 文献." in result.manuscript
    assert result.status == "passed"


def test_repair_mechanical_cnki_heading_levels() -> None:
    result = repair_manuscript("# 题目\n# 摘要\n#### 一、问题提出\n# 参考文献\n")

    assert "# 题目" in result.manuscript
    assert "## 摘要" in result.manuscript
    assert "## 一、问题提出" in result.manuscript
    assert "## 参考文献" in result.manuscript
