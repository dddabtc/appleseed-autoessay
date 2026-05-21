import json

import respx
from httpx import Response

from autoessay.clients.integrity.gptzero import GPTZeroClient
from autoessay.config import get_settings


@respx.mock
async def test_gptzero_scan_normalizes_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GPTZERO_API_KEY", "test-key")
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "0")
    get_settings.cache_clear()
    route = respx.post("https://gptzero.test/v2/predict/text").mock(
        return_value=Response(
            200,
            json={
                "id": "gptz-1",
                "status": "complete",
                "ai_probability": 0.44,
                "sentences": [
                    {
                        "start": 0,
                        "end": 9,
                        "label": "ai_likelihood_medium",
                        "score": 0.44,
                    },
                ],
            },
        ),
    )
    client = GPTZeroClient(base_url="https://gptzero.test")
    try:
        result = await client.scan("draft text", "ai_style")
    finally:
        await client.aclose()

    request_payload = json.loads(route.calls[0].request.content)
    assert request_payload["document"] == "draft text"
    assert result.vendor == "gptzero"
    assert result.scan_type == "ai_style"
    assert result.score == 0.44
    assert result.spans[0].span_id == "gptzero-span-001"
