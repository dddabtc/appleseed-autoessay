from httpx import ASGITransport, AsyncClient

from autoessay.config import get_settings
from autoessay.main import app


async def test_corpus_list_never_serializes_full_text_and_preview_caps(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    secret = "PRIOR_PAPER_PRIVATE_SENTENCE_SHOULD_NOT_BE_LISTED"
    text = (secret + " with surrounding methodological prose. ") * 12
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload_response = await client.post(
            "/api/corpus/upload",
            files={"file": ("private.txt", text.encode(), "text/plain")},
        )
        document_id = upload_response.json()["document"]["id"]
        list_response = await client.get("/api/corpus")
        preview_response = await client.get(
            f"/api/corpus/{document_id}/preview?max_chars=200",
        )

    serialized_list = list_response.text
    preview = preview_response.json()["preview"]
    assert list_response.status_code == 200
    assert secret not in serialized_list
    assert preview_response.status_code == 200
    assert len(preview) <= 200
    assert secret in preview


async def test_corpus_preview_caps_requests_above_limit(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    text = "A" * 500
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload_response = await client.post(
            "/api/corpus/upload",
            files={"file": ("long.txt", text.encode(), "text/plain")},
        )
        document_id = upload_response.json()["document"]["id"]
        preview_response = await client.get(
            f"/api/corpus/{document_id}/preview?max_chars=500",
        )

    payload = preview_response.json()
    assert preview_response.status_code == 200
    assert payload["max_chars"] == 200
    assert len(payload["preview"]) == 200
