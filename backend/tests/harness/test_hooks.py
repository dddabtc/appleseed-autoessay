from dataclasses import replace

from autoessay.harness import (
    HookContext,
    HookRegistry,
    HookResult,
    LLMCallResponse,
    ToolCallResponse,
    ValidationResult,
)


def test_registry_registers_and_chains_pre_llm_hooks() -> None:
    registry = HookRegistry()
    calls: list[str] = []

    def first(ctx: HookContext) -> HookContext:
        calls.append("first")
        return replace(ctx, prompt_filled=f"{ctx.prompt_filled} first")

    def second(ctx: HookContext) -> HookResult:
        calls.append("second")
        return HookResult(context=replace(ctx, prompt_filled=f"{ctx.prompt_filled} second"))

    registry.register_pre_llm("first", first)
    registry.register_pre_llm("second", second)

    result = registry.run_pre_llm(_context())

    assert calls == ["first", "second"]
    assert result.prompt_filled == "prompt first second"


def test_registry_post_llm_aggregates_annotations() -> None:
    registry = HookRegistry()
    response = LLMCallResponse(
        content="{}",
        parsed={},
        raw_content="{}",
        reasoning_text="",
        usage={},
        latency_ms=3,
        attempt=1,
        validation_result=ValidationResult(valid=True, parsed={}, errors=[]),
    )

    registry.register_post_llm("a", lambda _ctx, _response: HookResult(annotations={"ok": True}))
    registry.register_post_llm("b", lambda _ctx, _response: HookResult(annotations={"score": 1}))

    result = registry.run_post_llm(_context(), response)

    assert result.annotations == {"a": {"ok": True}, "b": {"score": 1}}


def test_registry_tool_hooks_chain_and_aggregate_annotations() -> None:
    registry = HookRegistry()
    response = ToolCallResponse(
        content="{}",
        parsed={},
        raw_content="{}",
        latency_ms=3,
        attempt=1,
        validation_result=ValidationResult(valid=True, parsed={}, errors=[]),
    )

    registry.register_pre_tool(
        "pre",
        lambda ctx: replace(ctx, run_metadata={"seen": True}),
    )
    registry.register_post_tool(
        "post",
        lambda _ctx, _response: HookResult(annotations={"ok": True}),
    )

    context = registry.run_pre_tool(_context())
    result = registry.run_post_tool(context, response)

    assert context.run_metadata == {"seen": True}
    assert result.annotations == {"post": {"ok": True}}


def test_registry_is_per_instance_not_global() -> None:
    first = HookRegistry()
    second = HookRegistry()
    first.register_pre_llm("only-first", lambda ctx: replace(ctx, prompt_filled="changed"))

    assert first.run_pre_llm(_context()).prompt_filled == "changed"
    assert second.run_pre_llm(_context()).prompt_filled == "prompt"


def _context() -> HookContext:
    return HookContext(
        run_id="run_1",
        phase="discovery",
        step_id="step_1",
        user_id="user_1",
        attempt=1,
        prompt_template_id="template_1",
        prompt_filled="prompt",
        prompt_hash="hash",
        project_title="Project",
        run_metadata={},
    )
