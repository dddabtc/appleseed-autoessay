"""PR-379 coverage for cache-busting download URLs.

Field bug 2026-05-13 (PR-378 follow-up): even after the API started
emitting ``Cache-Control: no-store``, Cloudflare kept serving the
4-hour-cached old docx file from before the fix because the bare
URL ``/api/runs/.../exports/manuscript.docx`` already had a cached
entry. The cached response carried the OLD ``max-age=14400``
header, so CF stuck with it for the full TTL.

Fix: ``_download_links`` now appends ``?v={sha256[:8]}`` to every
URL. Content-hashed URLs ⇒ new content gets a new URL ⇒ CF cache
miss ⇒ origin hit ⇒ ``no-store`` response ⇒ never cached.
"""

from __future__ import annotations


def test_download_link_carries_sha256_version_qualifier() -> None:
    from autoessay.agents.exporter import _download_links

    manifest = {
        "files": {
            "docx": {
                "path": "exports/manuscript.docx",
                "sha256": "3589c74c3578edd0abcdef0123456789",
                "size_bytes": 47754,
            },
            "html": {
                "path": "exports/manuscript.html",
                "sha256": "ff00ee11dd22cc33",
                "size_bytes": 14793,
            },
        },
    }
    links = _download_links("run_abc123", manifest)
    by_format = {link["format"]: link for link in links}
    assert by_format["docx"]["url"] == ("/api/runs/run_abc123/exports/manuscript.docx?v=3589c74c")
    assert by_format["html"]["url"] == ("/api/runs/run_abc123/exports/manuscript.html?v=ff00ee11")


def test_download_link_skips_qualifier_when_sha256_missing() -> None:
    """Backwards compatibility: an older manifest without ``sha256``
    still produces a working link (just without the cache-bust)."""
    from autoessay.agents.exporter import _download_links

    manifest = {
        "files": {
            "docx": {"path": "exports/manuscript.docx"},
        },
    }
    links = _download_links("run_legacy", manifest)
    assert links[0]["url"] == "/api/runs/run_legacy/exports/manuscript.docx"


def test_download_link_qualifier_changes_when_content_changes() -> None:
    """Two exports of the same run with different content should
    produce different URLs (the whole point of the cache buster)."""
    from autoessay.agents.exporter import _download_links

    v1 = _download_links(
        "run_id",
        {
            "files": {
                "docx": {
                    "path": "exports/manuscript.docx",
                    "sha256": "aaaa1111bbbb2222",
                },
            },
        },
    )
    v2 = _download_links(
        "run_id",
        {
            "files": {
                "docx": {
                    "path": "exports/manuscript.docx",
                    "sha256": "cccc3333dddd4444",
                },
            },
        },
    )
    assert v1[0]["url"] != v2[0]["url"]
    assert "?v=aaaa1111" in v1[0]["url"]
    assert "?v=cccc3333" in v2[0]["url"]


async def test_get_export_file_accepts_v_query_param(app_session) -> None:  # type: ignore[no-untyped-def]
    """FastAPI ignores unknown query params by default; verify the
    handler still serves the file when ``?v=abc`` is appended."""
    from pathlib import Path

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select

    from autoessay.main import app
    from autoessay.models import Run

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={
                "title": "Cache-bust test",
                "domain_id": "financial_history",
                "target_journal": None,
            },
        )
        run_create = await client.post(
            f"/api/projects/{project_response.json()['id']}/runs",
        )
        run_id = run_create.json()["id"]
        with app_session() as session:
            run = session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            exports_dir = Path(run.run_dir) / "exports"
            exports_dir.mkdir(parents=True, exist_ok=True)
            (exports_dir / "manuscript.docx").write_bytes(b"PK\x03\x04test")
            session.commit()
        for url in (
            f"/api/runs/{run_id}/exports/manuscript.docx",
            f"/api/runs/{run_id}/exports/manuscript.docx?v=abc123",
            f"/api/runs/{run_id}/exports/manuscript.docx?v=different",
        ):
            response = await client.get(url)
            assert response.status_code == 200, url
            # PR-378 cache headers still apply.
            assert "no-store" in response.headers.get("cache-control", "")
