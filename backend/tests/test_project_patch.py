"""Tests for PATCH /api/projects/{id} (paper-language editable post-create)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import Project


async def test_patch_project_language_persists(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/projects",
            json={"title": "test", "domain_id": "financial_history", "language": "en"},
        )
        project_id = create.json()["id"]
        # Change language en → ja
        patch = await client.patch(
            f"/api/projects/{project_id}",
            json={"language": "ja"},
        )
    assert patch.status_code == 200
    body = patch.json()
    assert body["language"] == "ja"
    with app_session() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert project.language == "ja"


@pytest.mark.parametrize("alias,expected", [("ZH", "zh"), (" Ja ", "ja")])
async def test_patch_project_language_normalizes(
    app_session,  # type: ignore[no-untyped-def]
    alias: str,
    expected: str,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/projects",
            json={"title": "case-test", "domain_id": "financial_history"},
        )
        project_id = create.json()["id"]
        patch = await client.patch(
            f"/api/projects/{project_id}",
            json={"language": alias},
        )
    assert patch.status_code == 200
    assert patch.json()["language"] == expected


@pytest.mark.parametrize("bad", ["fr", "spanish", "zh-cn", ""])
async def test_patch_project_language_rejects_unsupported(
    app_session,  # type: ignore[no-untyped-def]
    bad: str,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/projects",
            json={"title": "bad-lang", "domain_id": "financial_history"},
        )
        project_id = create.json()["id"]
        patch = await client.patch(
            f"/api/projects/{project_id}",
            json={"language": bad},
        )
    assert patch.status_code == 422


async def test_patch_project_404_when_unknown(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        patch = await client.patch(
            "/api/projects/proj_does_not_exist",
            json={"language": "zh"},
        )
    assert patch.status_code == 404


async def test_patch_project_no_change_no_op(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    """Patch with no fields should still 200 and return current state."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create = await client.post(
            "/api/projects",
            json={"title": "noop", "domain_id": "financial_history", "language": "zh"},
        )
        project_id = create.json()["id"]
        patch = await client.patch(f"/api/projects/{project_id}", json={})
    assert patch.status_code == 200
    assert patch.json()["language"] == "zh"
