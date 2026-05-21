"""Tests for GET /api/runs and GET /api/projects (the missing list endpoints)."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from autoessay.main import app


async def test_list_runs_returns_users_runs_with_project_metadata(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create two projects, one zh, one en, with one run each.
        zh_project = await client.post(
            "/api/projects",
            json={
                "title": "Bagehot Rule",
                "domain_id": "financial_history",
                "language": "zh",
            },
        )
        assert zh_project.status_code == 201, zh_project.text
        zh_project_id = zh_project.json()["id"]
        en_project = await client.post(
            "/api/projects",
            json={"title": "West India trade", "domain_id": "financial_history"},
        )
        en_project_id = en_project.json()["id"]
        zh_run = await client.post(f"/api/projects/{zh_project_id}/runs")
        en_run = await client.post(f"/api/projects/{en_project_id}/runs")

        listing = await client.get("/api/runs")
        assert listing.status_code == 200
        body = listing.json()
        assert isinstance(body, list)
        ids = {row["id"] for row in body}
        assert zh_run.json()["id"] in ids
        assert en_run.json()["id"] in ids

        zh_row = next(row for row in body if row["id"] == zh_run.json()["id"])
        en_row = next(row for row in body if row["id"] == en_run.json()["id"])
        assert zh_row["project_title"] == "Bagehot Rule"
        assert zh_row["project_language"] == "zh"
        assert zh_row["domain_id"] == "financial_history"
        assert en_row["project_title"] == "West India trade"
        assert en_row["project_language"] == "en"


async def test_list_projects_returns_users_projects(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/projects",
            json={"title": "P1", "domain_id": "financial_history", "language": "zh"},
        )
        await client.post(
            "/api/projects",
            json={"title": "P2", "domain_id": "financial_history", "language": "ja"},
        )

        listing = await client.get("/api/projects")
        assert listing.status_code == 200
        rows = listing.json()
        assert isinstance(rows, list)
        titles = {row["title"]: row for row in rows}
        assert "P1" in titles and "P2" in titles
        assert titles["P1"]["language"] == "zh"
        assert titles["P2"]["language"] == "ja"


async def test_get_run_includes_project_title_and_language(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        project_response = await client.post(
            "/api/projects",
            json={"title": "Field title", "domain_id": "financial_history", "language": "ja"},
        )
        project_id = project_response.json()["id"]
        run_response = await client.post(f"/api/projects/{project_id}/runs")
        run_id = run_response.json()["id"]

        get_response = await client.get(f"/api/runs/{run_id}")
        assert get_response.status_code == 200
        body = get_response.json()
        assert body["project_title"] == "Field title"
        assert body["project_language"] == "ja"
        assert body["domain_id"] == "financial_history"
