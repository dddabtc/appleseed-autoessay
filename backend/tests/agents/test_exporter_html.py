import json
from pathlib import Path

from conftest import seed_styled_run

from autoessay.agents.critic import run_critic
from autoessay.agents.exporter import run_exports
from autoessay.config import get_settings
from autoessay.models import Checkpoint, Run, utcnow
from autoessay.state_machine import transition


def test_write_html_converts_markdown_with_embedded_print_css(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_html

    html_path = tmp_path / "manuscript.html"

    _write_html(html_path, "# Title\n\nA paragraph with **emphasis**.")

    html = html_path.read_text(encoding="utf-8")
    assert "<h1>Title</h1>" in html
    assert "<strong>emphasis</strong>" in html
    assert "@media print" in html
    assert '<html lang="en">' in html


def test_write_html_uses_project_language(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _write_html

    for code, expected in (("zh", "zh"), ("ja", "ja"), ("EN", "en"), (None, "en")):
        path = tmp_path / f"manuscript_{expected}.html"
        _write_html(path, "# Title", code)  # type: ignore[arg-type]
        assert f'<html lang="{expected}">' in path.read_text(encoding="utf-8")


def test_csl_items_carry_project_language(tmp_path: Path) -> None:
    from autoessay.agents.exporter import _csl_items
    from autoessay.clients.common import AccessStatus, NormalizedSource

    source = NormalizedSource(
        source_id="src_001",
        title="Sample paper",
        authors=["A. Author"],
        year=2024,
        venue="Journal X",
        doi="10.1/xyz",
        url=None,
        pdf_url=None,
        abstract=None,
        source_client="crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )
    items = _csl_items([source], {"src_001"}, project_language="zh")
    assert items[0]["language"] == "zh"
    items = _csl_items([source], {"src_001"}, project_language="ja")
    assert items[0]["language"] == "ja"


def test_manifest_carries_language_and_citation_style_hint() -> None:
    from autoessay.agents.exporter import _citation_style_hint, _manifest_payload

    assert _citation_style_hint("zh") == "gb-t-7714-2015-numeric"
    assert _citation_style_hint("ja") == "sist02"
    assert _citation_style_hint("en") == "apa"
    assert _citation_style_hint(None) == "apa"
    # _manifest_payload signature accepts project_language:
    payload = _manifest_payload(Path("/tmp"), {}, project_language="zh")
    assert payload["language"] == "zh"
    assert payload["citation_style_hint"] == "gb-t-7714-2015-numeric"


def test_repair_numeric_citations_uses_claim_map_source_order() -> None:
    from autoessay.agents.exporter import _repair_numeric_citations_from_claim_map
    from autoessay.clients.common import AccessStatus, NormalizedSource

    def source(source_id: str) -> NormalizedSource:
        return NormalizedSource(
            source_id=source_id,
            title=source_id,
            authors=["A"],
            year=2024,
            venue="J",
            doi="10.example/x",
            url=None,
            pdf_url=None,
            abstract=None,
            source_client="crossref",
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=1.0,
            risk_flags=[],
        )

    sources = [
        source("crossref:a"),
        source("crossref:b"),
        NormalizedSource(
            source_id="shadow_baseline_v001",
            title="Shadow",
            authors=["AutoEssay"],
            year=None,
            venue=None,
            doi=None,
            url="autoessay-shadow-baseline://v001",
            pdf_url=None,
            abstract=None,
            source_client="internal",
            access_status=AccessStatus.OPEN,
            license=None,
            rank_score=1.0,
            risk_flags=[],
        ),
    ]
    claim_map = [
        {"paragraph_id": "introduction-p001", "source_ids": ["shadow_baseline_v001"]},
        {"paragraph_id": "introduction-p002", "source_ids": ["crossref:a", "crossref:b"]},
    ]
    body = "## 一、引言\n\n基线段落[7]。\n\n跨域方法段落[6][7]。\n"

    repaired = _repair_numeric_citations_from_claim_map(
        body,
        claim_map=claim_map,
        cited_sources=sources,
    )

    assert "基线段落[3]。" in repaired
    assert "跨域方法段落[1][2]。" in repaired
    assert "[6]" not in repaired
    assert "[7]" not in repaired


def test_repair_numeric_citations_removes_raw_source_id_markers() -> None:
    from autoessay.agents.exporter import _repair_numeric_citations_from_claim_map
    from autoessay.clients.common import AccessStatus, NormalizedSource

    def source(source_id: str) -> NormalizedSource:
        return NormalizedSource(
            source_id=source_id,
            title=source_id,
            authors=["A"],
            year=2024,
            venue="J",
            doi=None,
            url=None,
            pdf_url=None,
            abstract=None,
            source_client="test",
            access_status=AccessStatus.METADATA_ONLY,
            license=None,
            rank_score=1.0,
            risk_flags=[],
        )

    sources = [
        source("official:fraser:bog-minutes-1968-03-20"),
        source("official:imf:annual-report-1968"),
        source("shadow_baseline_v001"),
    ]
    claim_map = [
        {
            "paragraph_id": "introduction-p001",
            "source_ids": ["shadow_baseline_v001"],
        },
        {
            "paragraph_id": "introduction-p002",
            "source_ids": ["official:fraser:bog-minutes-1968-03-20"],
        },
        {
            "paragraph_id": "introduction-p003",
            "source_ids": ["official:imf:annual-report-1968"],
        },
        {
            "paragraph_id": "introduction-p004",
            "source_ids": [
                "official:fraser:bog-minutes-1968-03-20",
                "official:imf:annual-report-1968",
            ],
        },
    ]
    body = (
        "## 一、引言\n\n"
        "基线判断[shadow_baseline_v001]。\n\n"
        "联储纪要（official:fraser:bog-minutes-1968-03-20）。\n\n"
        "IMF年报(official:imf:annual-report-1968)[9]。\n\n"
        "复合标记[official:fraser:bog-minutes-1968-03-20；official:imf:annual-report-1968]。\n"
    )

    repaired = _repair_numeric_citations_from_claim_map(
        body,
        claim_map=claim_map,
        cited_sources=sources,
    )

    assert "基线判断[3]。" in repaired
    assert "联储纪要[1]。" in repaired
    assert "IMF年报[2]。" in repaired
    assert "复合标记[1][2]。" in repaired
    assert "shadow_baseline_v001" not in repaired
    assert "official:" not in repaired
    assert "[9]" not in repaired


def test_run_exports_writes_printable_html_from_styled_markdown(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = seed_styled_run(app_session, tmp_path, monkeypatch, "run_exports_html")
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "1")
    get_settings.cache_clear()

    with app_session() as session:
        run_critic(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        transition(run, "USER_FINAL_ACCEPTANCE", session, reason="test final acceptance")
        session.add(
            Checkpoint(
                id=f"checkpoint_final_{run_id}",
                run_id=run_id,
                checkpoint_type="USER_FINAL_ACCEPTANCE",
                status="ACCEPTED",
                decision_payload=json.dumps(
                    {"accept": True, "export_formats": ["html"]},
                    sort_keys=True,
                ),
                decided_at=utcnow(),
            ),
        )
        session.commit()

        summary = run_exports(run_id, session)

    html_path = run_dir / "exports" / "manuscript.html"
    html = html_path.read_text(encoding="utf-8")
    manifest = json.loads((run_dir / "exports" / "manifest.json").read_text(encoding="utf-8"))

    assert summary["state"] == "EXPORTS_DONE"
    assert html.startswith("<!doctype html>")
    assert "<style>" in html
    assert "@media print" in html
    assert "<main>" in html
    assert "<h2" in html
    assert manifest["files"]["html"]["path"] == "exports/manuscript.html"
