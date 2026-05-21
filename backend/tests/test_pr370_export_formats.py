"""PR-370 coverage for the latex-format allow-list fix.

PR-364 shipped the LaTeX (.tex) writer in ``agents.exporter`` and
included ``"latex"`` in ``DEFAULT_EXPORT_FORMATS``. The API filter at
``main._export_formats_from_request`` had its own allow-list that did
NOT include ``"latex"``, so any client that sent
``export_formats=["html","markdown","docx","latex"]`` through the
USER_FINAL_ACCEPTANCE checkpoint would silently lose the latex entry
and the exporter agent would never write ``manuscript.tex``.

The 2026-05-13 数理增强模式 canary surfaced this — round-0 stage B
scaffolds LaTeX math that has nowhere to be rendered without a
``.tex`` export.
"""

from __future__ import annotations

from autoessay.main import CheckpointDecisionRequest, _export_formats_from_request


def test_export_formats_accepts_latex_in_top_level_field() -> None:
    request = CheckpointDecisionRequest(export_formats=["html", "latex"])
    formats = _export_formats_from_request(request)
    assert "latex" in formats
    assert "html" in formats


def test_export_formats_accepts_latex_in_decision_payload() -> None:
    request = CheckpointDecisionRequest(
        decision_payload={"export_formats": ["html", "markdown", "docx", "latex"]},
    )
    formats = _export_formats_from_request(request)
    assert formats == ["html", "markdown", "docx", "latex"]


def test_export_formats_default_now_includes_latex() -> None:
    # No formats requested → caller gets the API default, which after
    # PR-370 includes "latex" so the cheap path also produces
    # manuscript.tex (matches agents.exporter.DEFAULT_EXPORT_FORMATS).
    request = CheckpointDecisionRequest()
    formats = _export_formats_from_request(request)
    assert "latex" in formats
    # Other historical defaults preserved.
    for fmt in ("markdown", "docx", "html", "bibtex", "csl_json"):
        assert fmt in formats


def test_export_formats_strips_unknown_formats() -> None:
    # The allow-list still filters bogus entries; latex is the only
    # new entry, anything else still drops.
    request = CheckpointDecisionRequest(
        export_formats=["latex", "pdf", "epub", "html"],
    )
    formats = _export_formats_from_request(request)
    assert "latex" in formats
    assert "html" in formats
    assert "pdf" not in formats
    assert "epub" not in formats


def test_api_default_matches_exporter_default() -> None:
    # PR-370 also realigns the API default with
    # ``agents.exporter.DEFAULT_EXPORT_FORMATS`` so future PRs that
    # add or remove a format only have to edit one canonical list.
    from autoessay.agents.exporter import DEFAULT_EXPORT_FORMATS

    api_default = _export_formats_from_request(CheckpointDecisionRequest())
    assert set(api_default) == set(DEFAULT_EXPORT_FORMATS)
