from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from autoessay.harness.types import HookContext, HookResult, LLMCallResponse, ToolCallResponse

PreHookResult: TypeAlias = HookContext | HookResult | None
PreHook = Callable[[HookContext], PreHookResult | Awaitable[PreHookResult]]
PostLLMHook = Callable[[HookContext, LLMCallResponse], HookResult | None]
PostToolHook = Callable[[HookContext, ToolCallResponse], HookResult | None]


class HookRegistry:
    def __init__(self) -> None:
        self._pre_llm: list[tuple[str, PreHook]] = []
        self._post_llm: list[tuple[str, PostLLMHook]] = []
        self._pre_tool: list[tuple[str, PreHook]] = []
        self._post_tool: list[tuple[str, PostToolHook]] = []

    def register_pre_llm(self, name: str, fn: PreHook) -> None:
        self._pre_llm.append((name, fn))

    def register_post_llm(self, name: str, fn: PostLLMHook) -> None:
        self._post_llm.append((name, fn))

    def register_pre_tool(self, name: str, fn: PreHook) -> None:
        self._pre_tool.append((name, fn))

    def register_post_tool(self, name: str, fn: PostToolHook) -> None:
        self._post_tool.append((name, fn))

    def run_pre_llm(self, ctx: HookContext) -> HookContext:
        return self._run_pre_hooks(self._pre_llm, ctx)

    async def run_pre_llm_async(self, ctx: HookContext) -> HookContext:
        return await self._run_pre_hooks_async(self._pre_llm, ctx)

    def run_post_llm(self, ctx: HookContext, response: LLMCallResponse) -> HookResult:
        result = HookResult()
        for name, fn in self._post_llm:
            hook_result = fn(ctx, response)
            result = _merge_hook_result(result, hook_result, name)
        return result

    def run_pre_tool(self, ctx: HookContext) -> HookContext:
        return self._run_pre_hooks(self._pre_tool, ctx)

    async def run_pre_tool_async(self, ctx: HookContext) -> HookContext:
        return await self._run_pre_hooks_async(self._pre_tool, ctx)

    def run_post_tool(self, ctx: HookContext, tool_result: ToolCallResponse) -> HookResult:
        result = HookResult()
        for name, fn in self._post_tool:
            hook_result = fn(ctx, tool_result)
            result = _merge_hook_result(result, hook_result, name)
        return result

    def _run_pre_hooks(
        self,
        hooks: list[tuple[str, PreHook]],
        ctx: HookContext,
    ) -> HookContext:
        current = ctx
        for _name, fn in hooks:
            hook_result = fn(current)
            if inspect.isawaitable(hook_result):
                raise TypeError("async pre hook requires the matching async pre-hook runner")
            current = _apply_pre_hook_result(current, hook_result)
        return current

    async def _run_pre_hooks_async(
        self,
        hooks: list[tuple[str, PreHook]],
        ctx: HookContext,
    ) -> HookContext:
        current = ctx
        for _name, fn in hooks:
            hook_result = fn(current)
            if inspect.isawaitable(hook_result):
                hook_result = await hook_result
            current = _apply_pre_hook_result(current, hook_result)
        return current


def _apply_pre_hook_result(current: HookContext, hook_result: object) -> HookContext:
    if isinstance(hook_result, HookContext):
        return hook_result
    if isinstance(hook_result, HookResult) and hook_result.context is not None:
        return hook_result.context
    return current


def _merge_hook_result(
    current: HookResult,
    hook_result: HookResult | None,
    name: str,
) -> HookResult:
    if hook_result is None:
        return current
    annotations = dict(current.annotations)
    if hook_result.annotations:
        annotations[name] = hook_result.annotations
    return HookResult(
        context=hook_result.context or current.context,
        annotations=annotations,
        verdict=hook_result.verdict or current.verdict,
    )
