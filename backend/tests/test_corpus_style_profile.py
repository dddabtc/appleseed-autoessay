from httpx import ASGITransport, AsyncClient

from autoessay.config import get_settings
from autoessay.main import app


async def test_corpus_style_profile_rebuild_from_stub_pdfs(
    app_session,  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_upload = await client.post(
            "/api/corpus/upload",
            files={"file": ("prior-one.pdf", b"%PDF-1.4 first", "application/pdf")},
        )
        second_upload = await client.post(
            "/api/corpus/upload",
            files={"file": ("prior-two.pdf", b"%PDF-1.4 second", "application/pdf")},
        )
        rebuild_response = await client.post("/api/corpus/style-profile/rebuild")
        profile_response = await client.get("/api/corpus/style-profile")
        documents_response = await client.get("/api/corpus")

    profile = profile_response.json()
    assert first_upload.status_code == 201
    assert second_upload.status_code == 201
    assert rebuild_response.status_code == 202
    assert profile_response.status_code == 200
    assert profile["paragraph_length_distribution"]["mean"] > 0
    assert profile["sentence_length_distribution"]["mean"] > 0
    assert profile["opener_patterns"]
    assert profile["common_domain_terms"]
    assert {document["ingest_status"] for document in documents_response.json()} == {"profiled"}


async def test_corpus_style_profile_returns_404_before_rebuild(
    app_session,  # type: ignore[no-untyped-def]
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/corpus/style-profile")

    assert response.status_code == 404
