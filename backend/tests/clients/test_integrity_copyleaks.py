import base64
import json
import re

import respx
from httpx import Response

from autoessay.clients.integrity.copyleaks import CopyleaksClient
from autoessay.config import get_settings


@respx.mock
async def test_copyleaks_scan_uses_jwt_login_and_normalizes_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("COPYLEAKS_EMAIL", "user@example.com")
    monkeypatch.setenv("COPYLEAKS_API_KEY", "secret")
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "0")
    get_settings.cache_clear()
    login_route = respx.post("https://id.test/v3/account/login/api").mock(
        return_value=Response(200, json={"access_token": "jwt-token"}),
    )
    scan_route = respx.post(re.compile(r"https://api\.test/v3/scans/submit/file/.+")).mock(
        return_value=Response(
            200,
            json={
                "status": "complete",
                "score": 0.18,
                "spans": [
                    {
                        "start": 4,
                        "end": 14,
                        "label": "possible_match",
                        "confidence": 0.62,
                        "source_url": "https://example.test/source",
                    },
                ],
            },
        ),
    )
    client = CopyleaksClient(auth_base_url="https://id.test", api_base_url="https://api.test")
    try:
        result = await client.scan("draft text", "plagiarism")
    finally:
        await client.aclose()

    login_payload = json.loads(login_route.calls[0].request.content)
    scan_payload = json.loads(scan_route.calls[0].request.content)
    assert login_payload == {"email": "user@example.com", "key": "secret"}
    assert base64.b64decode(scan_payload["base64"]).decode("utf-8") == "draft text"
    assert scan_route.calls[0].request.headers["authorization"] == "Bearer jwt-token"
    assert result.vendor == "copyleaks"
    assert result.scan_type == "plagiarism"
    assert result.spans[0].source_url == "https://example.test/source"
