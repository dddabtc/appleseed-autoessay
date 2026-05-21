import json

import respx
from httpx import Response

from autoessay.clients.integrity.originality import OriginalityClient
from autoessay.config import get_settings


@respx.mock
async def test_originality_scan_normalizes_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ORIGINALITY_API_KEY", "test-key")
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "0")
    get_settings.cache_clear()
    route = respx.post("https://originality.test/api/v1/scan/ai").mock(
        return_value=Response(
            200,
            json={
                "scan_id": "orig-1",
                "status": "complete",
                "score": 0.37,
                "spans": [
                    {
                        "start": 2,
                        "end": 12,
                        "label": "ai_likelihood_high",
                        "confidence": 0.81,
                    },
                ],
            },
        ),
    )
    client = OriginalityClient(base_url="https://originality.test")
    try:
        result = await client.scan("approved draft text", "ai_style")
    finally:
        await client.aclose()

    assert route.called
    request_payload = json.loads(route.calls[0].request.content)
    assert request_payload["text"] == "approved draft text"
    assert result.vendor == "originality_ai"
    assert result.scan_type == "ai_style"
    assert result.score == 0.37
    assert result.spans[0].label == "ai_likelihood_high"
