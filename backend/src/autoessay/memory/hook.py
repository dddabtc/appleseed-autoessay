from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol

from autoessay.harness.types import HookContext
from autoessay.memory.client import Memory

logger = logging.getLogger(__name__)


class MemorySearcher(Protocol):
    async def search(
        self,
        query: str,
        user_id: str | None,
        limit: int = 5,
        enhanced: bool = False,
    ) -> list[Memory]: ...


def make_memory_pre_llm_hook(
    memory_client: MemorySearcher,
    max_memories: int = 5,
) -> Callable[[HookContext], Awaitable[HookContext]]:
    async def pre_llm(ctx: HookContext) -> HookContext:
        if ctx.user_id is None or not ctx.user_id.strip():
            logger.warning("Skipping memory read for %s because user_id is missing", ctx.step_id)
            return ctx
        query = _memory_query(ctx)
        try:
            memories = await memory_client.search(
                query=query,
                user_id=ctx.user_id,
                limit=max_memories,
                enhanced=False,
            )
        except Exception as exc:  # noqa: BLE001 - memory is best-effort enrichment.
            logger.warning("Memory read failed for %s: %s", ctx.step_id, exc)
            return ctx
        compact = _compact_memories(memories, max_memories)
        if not compact:
            return ctx
        prompt_context = dict(ctx.prompt_context)
        prompt_context["previous_related_decisions"] = compact
        return replace(ctx, prompt_context=prompt_context)

    return pre_llm


def _memory_query(ctx: HookContext) -> str:
    explicit_query = ctx.run_metadata.get("memory_query")
    if isinstance(explicit_query, str) and explicit_query.strip():
        return _bounded_text(explicit_query, 500)

    parts = [
        f"phase={_bounded_text(ctx.phase, 80)}",
        f"topic={_bounded_text(ctx.project_title, 200)}",
    ]
    domain_id = ctx.run_metadata.get("domain_id")
    if isinstance(domain_id, str) and domain_id.strip():
        parts.append(f"domain={_bounded_text(domain_id, 80)}")
    accepted = _accepted_decisions_summary(ctx.run_metadata)
    if accepted:
        parts.append(f"accepted_decisions={accepted}")
    return _bounded_text(" ".join(parts), 500)


def _accepted_decisions_summary(run_metadata: dict[str, Any]) -> str:
    for key in ("accepted_decisions_summary", "accepted_decisions"):
        value = run_metadata.get(key)
        if isinstance(value, str) and value.strip():
            return _bounded_text(value, 200)
        if isinstance(value, (dict, list)):
            return _bounded_text(json.dumps(value, sort_keys=True), 200)
    return ""


def _compact_memories(memories: list[Memory], max_memories: int) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    for memory in memories[: max(0, max_memories)]:
        content = _bounded_text(" ".join(memory.content.split()), 320)
        title = _bounded_text(" ".join(memory.title.split()), 120)
        if not content and not title:
            continue
        compact.append(
            {
                "id": memory.id,
                "title": title,
                "content": content,
                "labels": memory.labels[:4],
            },
        )
    return compact


def _bounded_text(value: str, limit: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."
