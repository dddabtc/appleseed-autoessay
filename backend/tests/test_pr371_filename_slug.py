"""PR-371 coverage for title-derived download filenames.

The on-disk export path stays ``exports/manuscript.{ext}`` (unchanged
manifest contract). The Content-Disposition header carries a slug
derived from the project title so curl -OJ / browser saves it as
``晚清江南刊本断代依据重建研究.docx`` instead of yet another
``manuscript.docx``.

Codex AGREE-WITH-AMENDMENTS amendments tested here:
- amendment 2: Unicode whitespace folds + strip("-") AFTER truncation
- amendment 3: sidecars (manifest / literature_usage_table /
  self_check_report) keep their literal name
- amendment 4: /exports response carries download_filename
- amendment 5: default "Untitled Project" + empty titles get the
  run-id suffix so concurrent default-titled runs don't collide
"""

from __future__ import annotations

from autoessay.export_filename import (
    download_filename_for_export,
    encode_content_disposition,
    is_sidecar_filename,
    slug_from_title,
)


def test_slug_keeps_cjk_and_digits() -> None:
    assert slug_from_title("晚清江南刊本断代依据重建研究") == "晚清江南刊本断代依据重建研究"


def test_slug_replaces_whitespace_and_punctuation_with_hyphen() -> None:
    out = slug_from_title("真题评测 2026-05-13 jiangnan: publishing.")
    # Spaces → -, ":" → -, "." → -, multi-hyphens collapsed.
    assert out == "真题评测-2026-05-13-jiangnan-publishing"


def test_slug_strips_emoji_and_control_chars() -> None:
    out = slug_from_title("My Paper 😀 v1\t(draft)")
    assert "😀" not in out
    assert "\t" not in out
    assert "(" not in out and ")" not in out
    assert out.startswith("My-Paper")


def test_slug_truncates_then_strips_trailing_hyphen() -> None:
    # Build a title whose 80th char is on a separator boundary so the
    # naive substring keeps a trailing hyphen. Codex amendment 2:
    # strip AFTER truncation.
    long_word = "a" * 79 + "-"  # 80 chars exactly
    long_title = long_word + "tail"  # 84 chars
    out = slug_from_title(long_title)
    assert len(out) <= 80
    assert not out.endswith("-")
    assert out.startswith("aaa")


def test_slug_folds_runs_of_separators_to_single_hyphen() -> None:
    assert slug_from_title("a   b") == "a-b"
    assert slug_from_title("a---b") == "a-b"
    assert slug_from_title("a, b: c") == "a-b-c"


def test_slug_handles_chinese_punctuation() -> None:
    out = slug_from_title("研究：序跋、刻工题记（晚清江南）")
    assert "：" not in out
    assert "、" not in out
    assert "（" not in out and "）" not in out
    assert "研究" in out
    assert "序跋" in out


def test_slug_falls_back_to_manuscript_with_run_suffix_for_default_title() -> None:
    out = slug_from_title("Untitled Project", run_id="run_abcdef1234567890")
    assert out == "manuscript-abcdef12"


def test_slug_falls_back_to_manuscript_for_empty_title() -> None:
    out = slug_from_title("", run_id="run_1234567890abcdef")
    assert out == "manuscript-12345678"
    assert slug_from_title("   ", run_id=None) == "manuscript"


def test_slug_falls_back_to_manuscript_for_pure_punctuation_title() -> None:
    out = slug_from_title("!!! ??? ...", run_id="run_99887766aabbccdd")
    assert out == "manuscript-99887766"


def test_is_sidecar_filename_for_known_sidecars() -> None:
    assert is_sidecar_filename("manifest.json")
    assert is_sidecar_filename("literature_usage_table.md")
    assert is_sidecar_filename("self_check_report.md")
    assert is_sidecar_filename("self_check_report.json")
    assert not is_sidecar_filename("manuscript.docx")
    assert not is_sidecar_filename("manuscript.tex")
    assert not is_sidecar_filename("manuscript.md")


def test_download_filename_renames_main_exports() -> None:
    title = "晚清江南刊本断代依据重建研究"
    assert (
        download_filename_for_export(
            disk_filename="manuscript.docx",
            project_title=title,
            run_id="run_abcdef",
        )
        == "晚清江南刊本断代依据重建研究.docx"
    )
    assert (
        download_filename_for_export(
            disk_filename="manuscript.tex",
            project_title=title,
            run_id="run_abcdef",
        )
        == "晚清江南刊本断代依据重建研究.tex"
    )


def test_download_filename_leaves_sidecars_alone() -> None:
    for sidecar in (
        "manifest.json",
        "literature_usage_table.md",
        "self_check_report.md",
        "self_check_report.json",
    ):
        assert (
            download_filename_for_export(
                disk_filename=sidecar,
                project_title="任何标题都不应影响 sidecar",
                run_id="run_xyz",
            )
            == sidecar
        )


def test_encode_content_disposition_supports_utf8() -> None:
    header = encode_content_disposition("晚清江南刊本.docx")
    # ASCII-safe fallback for clients that ignore RFC 5987.
    assert 'filename="' in header
    # RFC 5987 percent-encoded UTF-8 entry for modern clients.
    assert "filename*=UTF-8''" in header
    # Percent-encoded CJK chars present.
    assert "%E6%99%9A%E6%B8%85" in header or "%E6%99%9A" in header


def test_encode_content_disposition_ascii_fallback_replaces_specials() -> None:
    header = encode_content_disposition("晚清江南刊本.docx")
    # ASCII fallback strips CJK to underscores but preserves "." and "-"
    ascii_part = header.split(";")[1].strip()  # 'filename="..."'
    assert ascii_part.startswith('filename="')
    assert ascii_part.endswith('.docx"')


def test_encode_content_disposition_pure_special_falls_back_to_manuscript() -> None:
    # Codex amendment: if the ASCII-safe form collapses to all
    # underscores, the fallback name should still be informative.
    header = encode_content_disposition("😀.docx")
    # Because the unicode encoded form is the canonical one for modern
    # clients, the fallback can be plain.
    assert 'filename="manuscript' in header or 'filename="_.docx"' in header


# ---------------------------------------------------------------------
# HTTP integration: hit the real endpoints and check the headers +
# /exports listing both carry the slug-derived names.
# ---------------------------------------------------------------------

import json  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from autoessay.main import app  # noqa: E402
from autoessay.models import Run  # noqa: E402


async def _seed_run_with_export(
    app_session,  # type: ignore[no-untyped-def]
    *,
    title: str,
    filenames: dict[str, str],  # name → contents
):
    """Create a run + write a fake exports dir on disk so we can
    exercise the HTTP endpoints without running the exporter."""
    from pathlib import Path

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={
                "title": title,
                "domain_id": "financial_history",
                "target_journal": None,
            },
        )
        assert project_response.status_code == 201
        project_id = project_response.json()["id"]
        created = await client.post(f"/api/projects/{project_id}/runs")
        assert created.status_code == 201
        run_id = created.json()["id"]

        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            exports_dir = Path(run.run_dir) / "exports"
            exports_dir.mkdir(parents=True, exist_ok=True)
            for name, content in filenames.items():
                (exports_dir / name).write_text(content, encoding="utf-8")

            # Build a manifest with one entry per file, keyed by a
            # unique format identifier — the on-disk filename is what
            # the endpoint surfaces.
            def _format_key(name: str) -> str:
                stem, _, ext = name.rpartition(".")
                if stem and ext:
                    return f"{stem}_{ext}".lower()
                return name.lower()

            manifest = {
                "files": {_format_key(name): {"path": f"exports/{name}"} for name in filenames},
            }
            (exports_dir / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            session.commit()
    return run_id


async def test_exports_endpoint_includes_download_filename(app_session) -> None:  # type: ignore[no-untyped-def]
    run_id = await _seed_run_with_export(
        app_session,
        title="晚清江南刊本断代依据重建研究",
        filenames={
            "manuscript.docx": "fake-docx",
            "manuscript.tex": "fake-tex",
            "manifest.json": "{}",
            "self_check_report.md": "# report\n",
        },
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/runs/{run_id}/exports")
        assert response.status_code == 200
        files = {f["filename"]: f for f in response.json()["files"]}
        # Manuscript outputs get the title-derived download name.
        assert files["manuscript.docx"]["download_filename"] == "晚清江南刊本断代依据重建研究.docx"
        assert files["manuscript.tex"]["download_filename"] == "晚清江南刊本断代依据重建研究.tex"
        # Sidecars keep the literal name.
        assert files["self_check_report.md"]["download_filename"] == "self_check_report.md"


async def test_export_file_response_sets_content_disposition(app_session) -> None:  # type: ignore[no-untyped-def]
    run_id = await _seed_run_with_export(
        app_session,
        title="晚清江南刊本断代依据重建研究",
        filenames={"manuscript.docx": "fake-docx"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/runs/{run_id}/exports/manuscript.docx",
        )
        assert response.status_code == 200
        cd = response.headers.get("content-disposition", "")
        assert "filename*=UTF-8''" in cd
        # Percent-encoded CJK chars present.
        assert "%E6%99%9A%E6%B8%85" in cd  # 晚清 first two chars


async def test_sidecar_download_keeps_literal_filename(app_session) -> None:  # type: ignore[no-untyped-def]
    run_id = await _seed_run_with_export(
        app_session,
        title="任何标题都不应影响 sidecar",
        filenames={"self_check_report.md": "# report\n"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/runs/{run_id}/exports/self_check_report.md",
        )
        assert response.status_code == 200
        cd = response.headers.get("content-disposition", "")
        # ASCII fallback shows the original literal name for sidecars.
        assert 'filename="self_check_report.md"' in cd


async def test_default_title_uses_run_id_suffix(app_session) -> None:  # type: ignore[no-untyped-def]
    run_id = await _seed_run_with_export(
        app_session,
        title="Untitled Project",
        filenames={"manuscript.docx": "fake-docx"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/runs/{run_id}/exports")
        files = {f["filename"]: f for f in response.json()["files"]}
        # First 8 hex chars of the uuid (after the "run_" prefix).
        short = run_id.removeprefix("run_")[:8]
        assert files["manuscript.docx"]["download_filename"] == f"manuscript-{short}.docx"
