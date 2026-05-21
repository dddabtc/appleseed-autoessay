import json

import httpx
import pytest
import respx

from autoessay.memory import MemoryClient


@pytest.mark.asyncio
async def test_search_rejects_empty_user_id_before_http() -> None:
    client = MemoryClient("https://memory.example.test", token="")

    with pytest.raises(ValueError, match="user_id"):
        await client.search("phase=scout", user_id="")


@pytest.mark.asyncio
async def test_search_rejects_none_user_id_before_http() -> None:
    client = MemoryClient("https://memory.example.test", token="")

    with pytest.raises(ValueError, match="user_id"):
        await client.search("phase=scout", user_id=None)


@pytest.mark.asyncio
@respx.mock
async def test_search_includes_user_id_in_http_body() -> None:
    route = respx.post("https://memory.example.test/memories/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "memories": [
                    {
                        "id": "memory_1",
                        "content": "Accepted bounded Scout query strategy.",
                        "title": "Scout decision",
                        "labels": ["autoessay", "approved_decision"],
                        "metadata": {"phase": "scout"},
                        "created_at": "2026-04-27T00:00:00Z",
                    },
                ],
            },
        ),
    )
    client = MemoryClient("https://memory.example.test", token="secret")

    result = await client.search("phase=scout", user_id="user_1", limit=3)

    assert len(result) == 1
    assert result[0].id == "memory_1"
    assert route.called
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "query": "phase=scout",
        "user_id": "user_1",
        "limit": 3,
        "enhanced": False,
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer secret"
