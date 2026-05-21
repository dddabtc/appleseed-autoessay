import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from autoessay.auth.middleware import SESSION_COOKIE_NAME, validate_auth_boot_settings
from autoessay.auth.session import create_session
from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import User


async def test_api_requires_session_without_bypass(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    monkeypatch.setenv("AUTOESSAY_SAFETY_GATE_ENABLED", "0")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post("/api/projects", json={"title": "Blocked"})
    assert response.status_code == 401


async def test_api_accepts_valid_session(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    with app_session() as session:
        user = User(
            id="user_test",
            oidc_subject="subject-test",
            oidc_issuer="https://auth.example.test/casdoor",
            email="ada@example.test",
            display_name="Ada Lovelace",
        )
        session.add(user)
        session.commit()
        session_id = create_session(user.id, db_session=session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post(
            "/api/projects",
            json={"title": "Allowed"},
            headers={"Cookie": f"{SESSION_COOKIE_NAME}={session_id}"},
        )
    assert response.status_code == 201
    assert response.json()["user_id"] == "user_test"


async def test_auth_bypass_allows_api_without_cookie(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SAFETY_GATE_ENABLED", "0")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/projects", json={"title": "Bypassed"})
    assert response.status_code == 201
    assert response.json()["user_id"] == "single-user"


def test_production_rejects_auth_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_ENV", "production")
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "1")
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="AUTOESSAY_AUTH_BYPASS"):
        validate_auth_boot_settings()
    get_settings.cache_clear()
