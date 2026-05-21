"""PR-375 coverage for markdown table rendering in docx + auto-numbered
表/图 captions across docx/html/latex.

Field bug 2026-05-13: user-reported docx exports rendered markdown
table syntax as garbled paragraphs because ``_write_docx`` had no
table parser. New feature: 表 N captions ABOVE tables and 图 N
captions BELOW figures (codex AGREE-WITH-AMENDMENTS amendment 1 —
Chinese journal convention).
"""

from __future__ import annotations

from pathlib import Path


def _read_zip_text(docx_path: Path, member: str = "word/document.xml") -> str:
    """Pull the raw XML out of a .docx so we can pattern-match on it."""
    import zipfile

    with zipfile.ZipFile(docx_path) as zf, zf.open(member) as f:
        return f.read().decode("utf-8")


# ---- docx ----------------------------------------------------------


def test_docx_renders_markdown_table_as_real_table(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_docx

    md = (
        "# Paper\n"
        "\n"
        "| Variable | Source | Status |\n"
        "|---|---|---|\n"
        "| GDP | WDI | filled |\n"
        "| Distance | CEPII | pending |\n"
    )
    out = tmp_path / "manuscript.docx"
    _write_docx(out, md)
    xml = _read_zip_text(out)
    # Real Word table element (not <w:p> with pipe characters).
    assert "<w:tbl>" in xml or "<w:tbl " in xml
    # Cell content present.
    assert "GDP" in xml
    assert "CEPII" in xml
    # No raw pipe row left over as paragraph.
    assert "|---|---|---|" not in xml


def test_docx_table_gets_above_caption_with_counter(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_docx

    md = (
        "# T\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nSome prose.\n\n| C | D |\n|---|---|\n| 3 | 4 |\n"
    )
    out = tmp_path / "two_tables.docx"
    _write_docx(out, md)
    xml = _read_zip_text(out)
    assert "表 1" in xml
    assert "表 2" in xml
    # Caption ABOVE: order of "表 1" should be before the first cell content "1".
    assert xml.index("表 1") < xml.index(">1<")
    assert xml.index("表 2") < xml.index(">3<")


def test_docx_image_gets_below_figure_caption(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_docx

    md = "# T\n\n![Trade flow diagram](images/fig1.png)\n\nMore prose."
    out = tmp_path / "with_image.docx"
    _write_docx(out, md)
    xml = _read_zip_text(out)
    # 图 caption present, ordered AFTER the alt-bearing placeholder.
    assert "图 1" in xml
    # Codex amendment 1: figure caption BELOW the figure placeholder.
    placeholder_pos = xml.find("Trade flow diagram")
    caption_pos = xml.rfind("图 1")
    assert placeholder_pos < caption_pos, "图 caption should be below the figure"


def test_docx_table_then_image_counters_independent(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_docx

    md = "| A | B |\n|---|---|\n| 1 | 2 |\n\n![pic](x.png)\n\n| C | D |\n|---|---|\n| 3 | 4 |\n"
    out = tmp_path / "mixed.docx"
    _write_docx(out, md)
    xml = _read_zip_text(out)
    # Tables: 表 1, 表 2 (separate from figures).
    assert "表 1" in xml
    assert "表 2" in xml
    # Figures: 图 1 (one image).
    assert "图 1" in xml
    # 图 2 should NOT exist (only one image).
    assert "图 2" not in xml


def test_docx_no_tables_still_works(tmp_path: Path) -> None:
    """Regression: plain manuscripts without tables/images should
    not regress from the old paragraph-only path."""
    from autoessay.agents.exporter import _write_docx

    md = "# T\n\nplain paragraph\n\n- item 1\n- item 2\n"
    out = tmp_path / "plain.docx"
    _write_docx(out, md)
    xml = _read_zip_text(out)
    assert "plain paragraph" in xml
    assert "item 1" in xml


# ---- latex ---------------------------------------------------------


def test_latex_table_gets_caption_above(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    md = "# Paper\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    out = tmp_path / "with_table.tex"
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\textbf{表 1}" in text
    # Caption ABOVE tabular.
    assert text.index(r"\textbf{表 1}") < text.index(r"\begin{tabular}")


def test_latex_counters_increment_across_two_tables(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    md = "# T\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n| C | D |\n|---|---|\n| 3 | 4 |\n"
    out = tmp_path / "two_tables.tex"
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\textbf{表 1}" in text
    assert r"\textbf{表 2}" in text


def test_latex_image_gets_figure_environment_with_caption_below(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    md = "# T\n\n![alt text](images/fig1.png)\n\nMore prose."
    out = tmp_path / "with_img.tex"
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\begin{figure}" in text
    assert r"\IfFileExists" in text  # graceful asset-missing fallback
    # Caption BELOW image (codex amendment 1).
    assert r"\textbf{图 1}" in text
    img_pos = text.index(r"\IfFileExists")
    cap_pos = text.index(r"\textbf{图 1}")
    assert img_pos < cap_pos


# ---- html ----------------------------------------------------------


def test_html_table_wrapped_in_figure_with_caption_above(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_html

    md = "# T\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    out = tmp_path / "with_table.html"
    _write_html(out, md, language="zh")
    text = out.read_text(encoding="utf-8")
    assert '<figure class="table-figure">' in text
    # <figcaption> before <table> inside the figure.
    fig_idx = text.index('<figure class="table-figure">')
    fc_idx = text.index('<figcaption class="table-caption">', fig_idx)
    tbl_idx = text.index("<table", fig_idx)
    assert fc_idx < tbl_idx
    assert "<strong>表 1</strong>" in text


def test_html_image_wrapped_in_figure_with_caption_below(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_html

    md = "# T\n\n![Diagram](images/fig.png)\n"
    out = tmp_path / "with_img.html"
    _write_html(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert '<figure class="image-figure">' in text
    assert "<strong>图 1</strong>" in text
    fig_idx = text.index('<figure class="image-figure">')
    img_idx = text.index("<img", fig_idx)
    cap_idx = text.index('<figcaption class="image-caption">', fig_idx)
    # img BEFORE figcaption (caption below).
    assert img_idx < cap_idx


def test_html_two_tables_get_independent_counters(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_html

    md = "| A | B |\n|---|---|\n| 1 | 2 |\n\n| C | D |\n|---|---|\n| 3 | 4 |\n"
    out = tmp_path / "two.html"
    _write_html(out, md, language="zh")
    text = out.read_text(encoding="utf-8")
    assert "<strong>表 1</strong>" in text
    assert "<strong>表 2</strong>" in text


# ---- helpers -------------------------------------------------------


def test_looks_like_table_header_requires_separator() -> None:
    from autoessay.agents.exporter import _looks_like_table_header

    lines = ["| A | B |", "|---|---|", "| 1 | 2 |"]
    assert _looks_like_table_header(lines, 0) is True
    # Header without separator → not a table.
    assert _looks_like_table_header(["| A | B |", "plain"], 0) is False
    # Last line: nothing after → False.
    assert _looks_like_table_header(["| A | B |"], 0) is False


def test_parse_md_table_handles_uneven_rows() -> None:
    from autoessay.agents.exporter import _parse_md_table

    lines = [
        "| A | B | C |",
        "|---|---|---|",
        "| 1 | 2 | 3 |",
        "| only-one |",
        "",
    ]
    header, rows, advance = _parse_md_table(lines, 0)
    assert header == ["A", "B", "C"]
    assert rows[0] == ["1", "2", "3"]
    # Short row preserved as-is — caller pads with empty cells.
    assert rows[1] == ["only-one"]
    assert advance == 4  # consumed 4 lines (header + sep + 2 data)
