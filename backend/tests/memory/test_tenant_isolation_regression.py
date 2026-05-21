import json

import httpx
import pytest
import respx

from autoessay.memory import MemoryClient


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_only_documents_for_requested_user() -> None:
    documents = {
        "user_a": [
            {
                "id": "a_1",
                "content": "User A accepted source weighting decision.",
                "title": "A decision",
                "labels": ["autoessay"],
                "metadata": {"owner": "user_a"},
            },
        ],
        "user_b": [
            {
                "id": "b_1",
                "content": "User B accepted a different domain decision.",
                "title": "B decision",
                "labels": ["autoessay"],
                "metadata": {"owner": "user_b"},
            },
        ],
    }

    def filtered_response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["enhanced"] is False
        user_id = payload["user_id"]
        return httpx.Response(200, json={"memories": documents.get(user_id, [])})

    respx.post("https://memory.example.test/memories/search").mock(side_effect=filtered_response)
    client = MemoryClient("https://memory.example.test", token="")

    user_a_results = await client.search("phase=scout", user_id="user_a")
    user_b_results = await client.search("phase=scout", user_id="user_b")

    assert [memory.id for memory in user_a_results] == ["a_1"]
    assert all(memory.metadata["owner"] == "user_a" for memory in user_a_results)
    assert [memory.id for memory in user_b_results] == ["b_1"]
    assert all(memory.metadata["owner"] == "user_b" for memory in user_b_results)
