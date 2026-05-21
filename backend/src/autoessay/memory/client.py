from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class Memory:
    id: str
    content: str
    title: str
    labels: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None


class MemoryClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._timeout = timeout

    async def search(
        self,
        query: str,
        user_id: str | None,
        limit: int = 5,
        enhanced: bool = False,
    ) -> list[Memory]:
        cleaned_user_id = _required_user_id(user_id)
        body = {
            "query": query,
            "user_id": cleaned_user_id,
            "limit": limit,
            "enhanced": enhanced,
        }
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers(),
            timeout=self._timeout,
        ) as client:
            response = await client.post("/memories/search", json=body)
        response.raise_for_status()
        return _parse_memories(response.json())

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}


def _required_user_id(user_id: str | None) -> str:
    if user_id is None:
        raise ValueError("memory search requires user_id")
    cleaned = user_id.strip()
    if not cleaned:
        raise ValueError("memory search requires user_id")
    return cleaned


def _parse_memories(payload: Any) -> list[Memory]:
    raw_items = _memory_items(payload)
    memories: list[Memory] = []
    for item in raw_items:
        if isinstance(item, dict):
            memories.append(_parse_memory(item))
    return memories


def _memory_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("memories", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _parse_memory(item: dict[str, Any]) -> Memory:
    return Memory(
        id=str(item.get("id", "")),
        content=str(item.get("content", "")),
        title=str(item.get("title", "")),
        labels=_string_list(item.get("labels")),
        metadata=_dict_value(item.get("metadata")),
        created_at=_optional_string(item.get("created_at")),
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
