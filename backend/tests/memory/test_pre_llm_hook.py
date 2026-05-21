import logging

import pytest

from autoessay.harness import HookContext
from autoessay.memory import Memory, make_memory_pre_llm_hook


class FakeMemoryClient:
    def __init__(self, memories: list[Memory], error: Exception | None = None) -> None:
        self.memories = memories
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def search(
        self,
        query: str,
        user_id: str | None,
        limit: int = 5,
        enhanced: bool = False,
    ) -> list[Memory]:
        self.calls.append(
            {
                "query": query,
                "user_id": user_id,
                "limit": limit,
                "enhanced": enhanced,
            },
        )
        if self.error is not None:
            raise self.error
        return self.memories


@pytest.mark.asyncio
async def test_pre_llm_hook_injects_memories_into_prompt_context() -> None:
    client = FakeMemoryClient(
        [
            Memory(
                id="memory_1",
                title="Accepted source policy",
                content="Use source weighting before deduplication.",
                labels=["autoessay", "decision"],
                metadata={},
                created_at=None,
            ),
        ],
    )
    hook = make_memory_pre_llm_hook(client, max_memories=1)

    result = await hook(_context())

    assert client.calls == [
        {
            "query": "phase=scout topic=Banking crises domain=financial_history",
            "user_id": "user_1",
            "limit": 1,
            "enhanced": False,
        },
    ]
    assert result.prompt_context["previous_related_decisions"] == [
        {
            "id": "memory_1",
            "title": "Accepted source policy",
            "content": "Use source weighting before deduplication.",
            "labels": ["autoessay", "decision"],
        },
    ]


@pytest.mark.asyncio
async def test_pre_llm_hook_returns_context_unchanged_on_memory_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    ctx = _context()
    client = FakeMemoryClient([], error=RuntimeError("memory unavailable"))
    hook = make_memory_pre_llm_hook(client)

    result = await hook(ctx)

    assert result == ctx
    assert "Memory read failed" in caplog.text


@pytest.mark.asyncio
async def test_pre_llm_hook_returns_context_unchanged_when_user_id_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    ctx = _context(user_id=None)
    client = FakeMemoryClient([])
    hook = make_memory_pre_llm_hook(client)

    result = await hook(ctx)

    assert result == ctx
    assert client.calls == []
    assert "user_id is missing" in caplog.text


def _context(user_id: str | None = "user_1") -> HookContext:
    return HookContext(
        run_id="run_1",
        phase="discovery",
        step_id="scout.query_expansion",
        user_id=user_id,
        attempt=1,
        prompt_template_id="scout.query_expansion.v1",
        prompt_filled="Create queries.",
        prompt_hash="hash",
        project_title="Banking crises",
        run_metadata={
            "domain_id": "financial_history",
            "memory_query": "phase=scout topic=Banking crises domain=financial_history",
        },
    )
