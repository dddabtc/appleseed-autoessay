"""PR-381 coverage for table rows that carry trailing ``[N]`` citation
markers after the closing pipe.

Field bug 2026-05-13: user's manuscript had the last row of a table
written as ``| 互证强度_i | ... | 决定能否列为候选人文主义者 |[2]``
— a citation ``[2]`` glued onto the right of the row's terminal
pipe. PR-375's parser checked ``startswith('|') and endswith('|')``
which was False on this line, so the row got dropped from the
table and rendered as raw pipe-paragraph in docx/html/latex.

Fix: strip trailing ``[N]`` / ``[12]`` / ``[3][4]`` groups before the
row-shape check.
"""

from __future__ import annotations

import zipfile
from pathlib import Path


def _docx_xml(p: Path) -> str:
    with zipfile.ZipFile(p) as zf, zf.open("word/document.xml") as f:
        return f.read().decode("utf-8")


def test_strip_trailing_citations_handles_single_marker() -> None:
    from autoessay.agents.exporter import _strip_trailing_table_citations

    assert _strip_trailing_table_citations("| a | b | c |[2]") == "| a | b | c |"


def test_strip_trailing_citations_handles_multiple_markers() -> None:
    from autoessay.agents.exporter import _strip_trailing_table_citations

    assert _strip_trailing_table_citations("| a | b |[3][4]") == "| a | b |"


def test_strip_trailing_citations_handles_whitespace() -> None:
    from autoessay.agents.exporter import _strip_trailing_table_citations

    assert _strip_trailing_table_citations("| a | b |  [12]") == "| a | b |"


def test_strip_trailing_citations_leaves_inner_brackets_alone() -> None:
    """Citation markers in CELL content (not after a closing pipe)
    must survive — they're legitimate prose data."""
    from autoessay.agents.exporter import _strip_trailing_table_citations

    # Bracket is part of the cell, not after the final pipe.
    line = "| evidence [1] | reason |"
    assert _strip_trailing_table_citations(line) == line


def test_is_table_row_accepts_row_with_trailing_citation() -> None:
    from autoessay.agents.exporter import _is_table_row

    assert _is_table_row("| 互证强度 | 上述环节 | 决定 |[2]") is True
    assert _is_table_row("| 互证强度 | 上述环节 | 决定 | [12][34]") is True


def test_split_table_cells_drops_trailing_citation() -> None:
    from autoessay.agents.exporter import _split_table_cells

    cells = _split_table_cells("| a | b | c |[2]")
    # Citation peeled off; cells are the inner pipe-separated values.
    assert cells == ["a", "b", "c"]
    assert "[2]" not in " ".join(cells)


def test_docx_full_table_with_trailing_citation_last_row(tmp_path: Path) -> None:
    """End-to-end: a 5-row table whose last row has ``|[2]`` ends up
    as a complete 5-row docx table, not 4-row table + 1 stray
    paragraph."""
    from autoessay.agents.exporter import _write_docx

    md = (
        "# T\n"
        "\n"
        "| 变量 | 含义 |\n"
        "|---|---|\n"
        "| 教育实践_i | 是否参与教学 |\n"
        "| 文本生产_i | 是否参与写作 |\n"
        "| 布道传播_i | 是否参与宗教传播 |\n"
        "| 图像生产_i | 是否参与图像 |\n"
        "| 互证强度_i | 上述环节之间的相互支持 |[2]\n"
    )
    out = tmp_path / "with_citation.docx"
    _write_docx(out, md)
    xml = _docx_xml(out)
    # Real table with all 5 data rows (header + 5 body = 6 ``<w:tr>``).
    tr_count = xml.count("<w:tr>")
    assert tr_count >= 6, f"expected 6 rows (header + 5 body), got {tr_count}"
    # Trailing-citation row's content present.
    assert "互证强度_i" in xml
    assert "上述环节之间的相互支持" in xml
    # The ``[2]`` citation suffix shouldn't appear as a stray
    # pipe-paragraph (would mean parser dropped the row).
    assert "|[2]" not in xml
    # Caption still present.
    assert "表 1" in xml


def test_strip_trailing_citations_leaves_nonnumeric_brackets_alone() -> None:
    """Codex amendment 2: regex is numeric-only so cell content
    ending on a non-numeric bracket (e.g. ``| [evidence: §3.2] |``)
    stays untouched."""
    from autoessay.agents.exporter import _strip_trailing_table_citations

    # Cell ends on a non-numeric bracket; whole row preserved.
    line = "| 互证 | 决定 | [evidence: see §3.2] |"
    assert _strip_trailing_table_citations(line) == line


def test_docx_full_smoke_user_screenshot_repro(tmp_path: Path) -> None:
    """End-to-end repro of the 2026-05-13 user screenshot bugs:
    docx must render the ``$$`` math block as a real formula
    paragraph (NOT raw ``$$`` text) AND must include the trailing-
    citation last row inside the table. Codex amendment 4.
    """
    from autoessay.agents.exporter import _write_docx

    md = (
        "据此, 本文设置四项判准。对应关系可写为：\n"
        "\n"
        "$$\n"
        "\n"
        "人文主义者候选度_i = f(教育实践_i, 文本生产_i, 布道传播_i, 图像生产_i, 互证强度_i)\n"
        "\n"
        "$$\n"
        "\n"
        "| 变量 | 含义 | 可观察指标 | 判定作用 |\n"
        "|---|---|---|---|\n"
        "| 教育实践_i | 是否参与教学 | 讲学、教材 | 知识传递 |\n"
        "| 文本生产_i | 是否参与写作 | 文章、书信 | 文本扩散 |\n"
        "| 布道传播_i | 是否参与宗教 | 布道、讲章 | 宗教传播 |\n"
        "| 图像生产_i | 是否参与图像 | 委托、说明 | 视觉生产 |\n"
        "| 互证强度_i | 上述环节之间的相互支持 | 至少两类材料可互证 | 决定候选 |[2]\n"
        "\n"
        "本文的证据测试分成两层...\n"
    )
    out = tmp_path / "user_repro.docx"
    _write_docx(out, md)
    xml = _docx_xml(out)
    # ---- math block ----
    # Formula text present.
    assert "人文主义者候选度_i" in xml
    assert "f(教育实践_i" in xml
    # Raw ``$$`` markers must NOT appear as paragraph text.
    assert "<w:t>$$</w:t>" not in xml
    # ---- table ----
    # 6 ``<w:tr>`` rows: 1 header + 5 body (all 5 LLM rows, including
    # the trailing-citation one).
    assert xml.count("<w:tr>") >= 6
    # All 5 body cells must be in the table.
    for needle in (
        "教育实践_i",
        "文本生产_i",
        "布道传播_i",
        "图像生产_i",
        "互证强度_i",
        "决定候选",
    ):
        assert needle in xml, f"table cell missing: {needle}"
    # The pipe-with-citation suffix must NOT leak as a stray paragraph.
    assert "|[2]" not in xml


def test_latex_full_table_with_trailing_citation_last_row(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    md = "# T\n\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |[2]\n"
    out = tmp_path / "with_citation.tex"
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    # Both rows must land inside ``\begin{tabular}``.
    assert r"\begin{tabular}" in text
    # The last row's cells must be in the body.
    assert "3" in text
    assert "4" in text
    # The trailing ``[2]`` should NOT appear as a stray paragraph
    # (would happen if the parser dropped the row).
    assert "|[2]" not in text
