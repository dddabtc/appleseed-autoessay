"""Tests for the Project.language field (multi-language PR-1)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from autoessay.main import app
from autoessay.models import Project


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "en"),
        ("zh", "zh"),
        ("ja", "ja"),
        ("ZH", "zh"),
        (" Ja ", "ja"),
    ],
)
async def test_create_project_persists_language(
    app_session,  # type: ignore[no-untyped-def]
    language: str,
    expected: str,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/projects",
            json={
                "title": "Bagehot Rule research",
                "domain_id": "financial_history",
                "target_journal": None,
                "language": language,
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["language"] == expected
    project_id = body["id"]
    with app_session() as session:
        project = session.get(Project, project_id)
        assert project is not None
        assert project.language == expected


async def test_create_project_defaults_language_to_en(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/projects",
            json={
                "title": "default-language project",
                "domain_id": "financial_history",
            },
        )
    assert response.status_code == 201
    assert response.json()["language"] == "en"


@pytest.mark.parametrize(
    "bad_language",
    ["fr", "spanish", "", "zhcn", "zh-cn"],
)
async def test_create_project_rejects_unsupported_language(
    app_session,  # type: ignore[no-untyped-def]
    bad_language: str,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/projects",
            json={
                "title": "bad lang",
                "domain_id": "financial_history",
                "language": bad_language,
            },
        )
    # Pydantic validation produces a 422 with field-level error.
    assert response.status_code == 422
    body = response.json()
    detail_str = str(body)
    assert "language" in detail_str.lower()
