"""Unit tests for the LaTeX (.tex) export path added in 2026-05-12 PR-364.

Stage B round-0 manuscripts now carry LaTeX math (``$$...$$``), markdown
tables, and ``【待填】`` placeholders. The docx exporter does not render
math and the html exporter only previews via MathJax. ``manuscript.tex``
gives authors a CNKI / 顶刊 / arXiv-submittable LaTeX source.
"""

from __future__ import annotations

from pathlib import Path


def test_write_latex_preamble_uses_ctexart_for_chinese(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    _write_latex(out, "# 测试题目\n\n中文段落。", language="zh")
    text = out.read_text(encoding="utf-8")
    assert r"\documentclass[12pt,a4paper]{ctexart}" in text
    # ctexart already handles UTF-8 + CJK fonts, so the english-only
    # ``inputenc`` line must NOT be injected.
    assert r"\usepackage[utf8]{inputenc}" not in text


def test_write_latex_preamble_uses_article_with_inputenc_for_english(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    _write_latex(out, "# A Title\n\nAn English paragraph.", language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\documentclass[12pt,a4paper]{article}" in text
    assert r"\usepackage[utf8]{inputenc}" in text


def test_write_latex_first_h1_becomes_title(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    _write_latex(out, "# Paper One\n\nbody", language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\title{Paper One}" in text
    assert r"\maketitle" in text
    # Subsequent ``# `` headings (if any) become ``\section*{}`` since
    # there is only one title.
    out2 = tmp_path / "two_h1.tex"
    _write_latex(out2, "# First\n\nbody1\n\n# Second\n\nbody2", language="en")
    txt2 = out2.read_text(encoding="utf-8")
    assert r"\title{First}" in txt2
    assert r"\section*{Second}" in txt2


def test_write_latex_headings_map_to_section_levels(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    md = "# Title\n\n## H2\n\n### H3\n\n#### H4\n"
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\section{H2}" in text
    assert r"\subsection{H3}" in text
    assert r"\subsubsection{H4}" in text


def test_write_latex_display_math_block(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    md = "# T\n\nIntro.\n\n$$E[M_{jkt}] = \\exp(\\beta X_{jt})$$\n\nMore prose."
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\[E[M_{jkt}] = \exp(\beta X_{jt})\]" in text


def test_write_latex_inline_math_passes_through(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    md = "# T\n\nLet $x$ be in $[0, 1]$ and consider $y_t = \\alpha + \\beta x_t$."
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    # Inline math segments are emitted verbatim, NOT escaped.
    assert "$x$" in text
    assert "$y_t = \\alpha + \\beta x_t$" in text


def test_write_latex_markdown_table_becomes_tabular(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    md = (
        "# T\n\n"
        "| Variable | Source | Status |\n"
        "|---|---|---|\n"
        "| GDP | WDI | filled |\n"
        "| Distance | CEPII | filled |\n"
    )
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\begin{table}[h]" in text
    assert r"\begin{tabular}{lll}" in text
    assert r"\toprule" in text
    assert r"\midrule" in text
    assert r"\bottomrule" in text
    assert r"GDP & WDI & filled \\" in text


def test_write_latex_highlights_dai_tian_placeholder(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    md = "# T\n\n该位置应填入回归系数：【待填】，p 值【待填】。"
    _write_latex(out, md, language="zh")
    text = out.read_text(encoding="utf-8")
    # 待填 placeholders are visually flagged so reviewers don't miss
    # them in a long compiled PDF.
    assert r"\textcolor{red}{【待填】}" in text
    # And the surviving raw placeholder shouldn't appear without color
    # (i.e. all occurrences got wrapped).
    assert text.count("【待填】") == text.count(r"\textcolor{red}{【待填】}")


def test_write_latex_escapes_special_chars_in_prose(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    # `&` `%` `#` `_` are LaTeX-special and must be escaped in prose
    # to compile. ``$x$`` should still pass through as inline math.
    md = "# T\n\nAnderson & van Wincoop (2003) — 5% of trade — see ref_id_42, $x$ unchanged."
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"Anderson \& van Wincoop" in text
    assert r"5\% of trade" in text
    assert r"ref\_id\_42" in text
    assert "$x$" in text  # still untouched


def test_write_latex_bullet_list_becomes_itemize(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    md = "# T\n\nBefore.\n\n- first\n- second\n- third\n\nAfter."
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\begin{itemize}" in text
    assert r"  \item first" in text
    assert r"  \item second" in text
    assert r"  \item third" in text
    assert r"\end{itemize}" in text


def test_write_latex_emphasis_bold_italic(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    md = "# T\n\nThis is **strong** and this is *light*."
    _write_latex(out, md, language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\textbf{strong}" in text
    assert r"\textit{light}" in text


def test_write_latex_document_envelopes(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_latex

    out = tmp_path / "manuscript.tex"
    _write_latex(out, "# T\n\nbody", language="en")
    text = out.read_text(encoding="utf-8")
    assert r"\begin{document}" in text
    assert r"\end{document}" in text
    # \maketitle only inside the body, and the document body is after
    # \begin{document}.
    body_start = text.index(r"\begin{document}")
    body_end = text.index(r"\end{document}")
    assert text.index(r"\maketitle") > body_start
    assert text.index(r"\maketitle") < body_end


def test_export_formats_includes_latex() -> None:
    from autoessay.agents.exporter import DEFAULT_EXPORT_FORMATS, EXPORT_FORMATS

    assert "latex" in EXPORT_FORMATS
    assert "latex" in DEFAULT_EXPORT_FORMATS
