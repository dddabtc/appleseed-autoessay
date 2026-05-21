from httpx import ASGITransport, AsyncClient

from autoessay.auth.middleware import SESSION_COOKIE_NAME
from autoessay.auth.session import create_session
from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import User


def _session_cookie(app_session) -> str:  # type: ignore[no-untyped-def]
    with app_session() as session:
        user = User(
            id="user_domains",
            oidc_subject="subject-domains",
            oidc_issuer="https://auth.example.test/casdoor",
            email="domains@example.test",
            display_name="Domain Tester",
        )
        session.add(user)
        session.commit()
        session_id = create_session(user.id, db_session=session)
    return f"{SESSION_COOKIE_NAME}={session_id}"


async def test_list_domains_unauthenticated(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get("/api/domains")
    assert response.status_code == 401


async def test_list_domains_authenticated(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    cookie = _session_cookie(app_session)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get("/api/domains", headers={"Cookie": cookie})
    assert response.status_code == 200
    assert len(response.json()) >= 1


async def test_list_domains_includes_financial_history(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/domains")
    assert response.status_code == 200
    domains = response.json()
    financial_history = next(domain for domain in domains if domain["id"] == "financial_history")
    assert financial_history["display_name"] == "Financial History"
    assert financial_history["version"] == "0.1.0"
    assert financial_history["target_journals"] == [
        "Financial History Review",
        "Business History",
        "Economic History Review",
    ]


async def test_create_project_invalid_domain(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/projects",
            json={"title": "Invalid domain", "domain_id": "not_a_real_domain"},
        )
    assert response.status_code == 400
    assert "unknown domain_id: not_a_real_domain" in response.json()["detail"]
    assert "financial_history" in response.json()["detail"]


async def test_create_project_valid_domain(app_session) -> None:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/projects",
            json={"title": "Valid domain", "domain_id": "financial_history"},
        )
    assert response.status_code == 201
    payload = response.json()
    assert payload["domain_id"] == "financial_history"
    assert payload["domain_version"] == "0.1.0"
