from httpx import ASGITransport, AsyncClient

from autoessay.auth.middleware import SESSION_COOKIE_NAME
from autoessay.auth.session import create_session
from autoessay.config import get_settings
from autoessay.main import app
from autoessay.models import User


async def test_corpus_upload_list_delete_and_ingest_status(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    text = (
        "Financial archives may show depositor behavior. "
        "The institutional record suggests a delayed policy response. "
    ) * 4
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload_response = await client.post(
            "/api/corpus/upload",
            files={"file": ("prior.txt", text.encode(), "text/plain")},
        )
        list_response = await client.get("/api/corpus")
        document_id = upload_response.json()["document"]["id"]
        delete_response = await client.delete(f"/api/corpus/{document_id}")
        list_after_delete = await client.get("/api/corpus")

    assert upload_response.status_code == 201
    assert upload_response.json()["document"]["ingest_status"] == "extracted"
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [document_id]
    assert delete_response.status_code == 204
    assert list_after_delete.json() == []


async def test_corpus_upload_rejects_bad_type_and_oversize(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bad_type = await client.post(
            "/api/corpus/upload",
            files={"file": ("prior.pdf", b"not actually treated as pdf", "text/plain")},
        )
        too_large = await client.post(
            "/api/corpus/upload",
            files={"file": ("large.txt", b"x" * (30 * 1024 * 1024 + 1), "text/plain")},
        )

    assert bad_type.status_code == 400
    assert too_large.status_code == 413


async def test_corpus_requires_auth_when_bypass_disabled(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/api/corpus")

    assert response.status_code == 401


async def test_corpus_documents_are_scoped_to_authenticated_user(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_AUTH_BYPASS", "0")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    first_cookie = _session_cookie(app_session, "user_corpus_one", "subject-corpus-one")
    second_cookie = _session_cookie(app_session, "user_corpus_two", "subject-corpus-two")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        upload_response = await client.post(
            "/api/corpus/upload",
            headers={"Cookie": first_cookie},
            files={"file": ("first.txt", b"first user's private prior paper", "text/plain")},
        )
        first_list = await client.get("/api/corpus", headers={"Cookie": first_cookie})
        second_list = await client.get("/api/corpus", headers={"Cookie": second_cookie})

    assert upload_response.status_code == 201
    assert len(first_list.json()) == 1
    assert second_list.json() == []


def _session_cookie(app_session, user_id: str, subject: str) -> str:  # type: ignore[no-untyped-def]
    with app_session() as session:
        user = User(
            id=user_id,
            oidc_subject=subject,
            oidc_issuer="https://auth.example.test/casdoor",
            email=f"{user_id}@example.test",
            display_name=user_id,
        )
        session.add(user)
        session.commit()
        session_id = create_session(user.id, db_session=session)
    return f"{SESSION_COOKIE_NAME}={session_id}"
