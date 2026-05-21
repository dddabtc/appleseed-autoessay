from __future__ import annotations

import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from autoessay.harness.audit import AuditWriter, hash_text
from autoessay.harness.hooks import HookRegistry
from autoessay.harness.sentinels import (
    check_value as check_sentinels,
)
from autoessay.harness.sentinels import (
    format_violations as format_sentinel_violations,
)
from autoessay.harness.types import (
    AuditVerdict,
    HookContext,
    HookResult,
    LLMCallRequest,
    LLMCallResponse,
    ToolCallRequest,
    ToolCallResponse,
    ValidationResult,
)
from autoessay.harness.validator import validate_response
from autoessay.llm_client import JSONStrictRetryable, LLMClient


class SchemaViolationError(ValueError):
    def __init__(self, message: str, attempts: list[LLMCallResponse]) -> None:
        super().__init__(message)
        self.attempts = attempts


class ToolInvocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        attempts: list[ToolCallResponse],
        failure_class: str,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.failure_class = failure_class


async def run_llm_step(
    request: LLMCallRequest,
    hooks: HookRegistry,
    context: HookContext,
    output_schema: dict[str, Any] | type[BaseModel],
    audit: AuditWriter,
    max_corrective_retries: int = 1,
    llm_optional: bool = False,
    fallback: Callable[[], Any] | None = None,
    extra_sentinel_substrings: tuple[str, ...] = (),
    extra_sentinel_regexes: tuple[Any, ...] = (),
) -> LLMCallResponse:
    context = await hooks.run_pre_llm_async(context)
    prompt_filled = _prompt_with_context(context.prompt_filled, context.prompt_context)
    messages = _messages_with_prompt(request.messages, prompt_filled)
    context = _context_for_attempt(context, messages, 1)
    client = LLMClient()
    attempts: list[LLMCallResponse] = []
    try:
        max_attempts = max(1, max_corrective_retries + 1)
        for attempt_number in range(1, max_attempts + 1):
            context = _context_for_attempt(context, messages, attempt_number)
            audit_attempt = audit.record_pending(
                request=request,
                ctx=context,
                messages=messages,
                attempt=attempt_number,
            )
            try:
                response = await _call_llm(
                    client=client,
                    request=request,
                    messages=messages,
                    attempt_number=attempt_number,
                    output_schema=output_schema,
                )
            except Exception as exc:
                failure_response = _transport_failure_response(attempt_number, exc)
                audit.finish_attempt(
                    attempt=audit_attempt,
                    request=request,
                    ctx=context,
                    response=failure_response,
                    status="failed_transport",
                    verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
                    error_kind=type(exc).__name__,
                )
                audit.finish_invocation(
                    status="failed_transport",
                    retry_count=max(0, attempt_number - 1),
                    failure_class="transport",
                )
                raise

            attempts.append(response)
            # Run sentinel check on the parsed output before any post_llm
            # hooks. If any forbidden pattern is found, downgrade the
            # response to a schema violation so the existing retry loop
            # picks it up. This is the long-term mechanism: every LLM
            # call goes through sentinels automatically; agents do not
            # need to remember to wire it.
            if response.validation_result.valid:
                violations = check_sentinels(
                    response.parsed,
                    extra_substrings=extra_sentinel_substrings,
                    extra_regexes=extra_sentinel_regexes,
                )
                if violations:
                    sentinel_errors = format_sentinel_violations(violations)
                    response = _response_with_sentinel_violations(response, sentinel_errors)
                    attempts[-1] = response
            hook_result = None
            if response.validation_result.valid:
                hook_result = hooks.run_post_llm(context, response)
                if hook_result.verdict == AuditVerdict.REJECTED_FALLBACK_USED:
                    if fallback is not None:
                        fallback_response = _fallback_response(
                            fallback=fallback,
                            output_schema=output_schema,
                            attempt_number=attempt_number,
                            latency_ms=response.latency_ms,
                        )
                        audit.finish_attempt(
                            attempt=audit_attempt,
                            request=request,
                            ctx=context,
                            response=fallback_response,
                            status="rejected_fallback_used",
                            verdict=AuditVerdict.REJECTED_FALLBACK_USED,
                            error_kind="post_llm_rejected",
                            hook_annotations=hook_result.annotations,
                        )
                        audit.finish_invocation(
                            status="rejected_fallback_used",
                            retry_count=max(0, attempt_number - 1),
                            failure_class="post_llm_rejected",
                        )
                        return fallback_response
                    response = _response_with_hook_errors(
                        response,
                        _hook_validation_errors(hook_result),
                    )
                    attempts[-1] = response
                elif hook_result.verdict == AuditVerdict.REJECTED_SCHEMA_VIOLATION:
                    response = _response_with_hook_errors(
                        response,
                        _hook_validation_errors(hook_result),
                    )
                    attempts[-1] = response

            if response.validation_result.valid:
                audit.finish_attempt(
                    attempt=audit_attempt,
                    request=request,
                    ctx=context,
                    response=response,
                    status="accepted",
                    verdict=AuditVerdict.ACCEPTED,
                    hook_annotations=hook_result.annotations if hook_result is not None else None,
                )
                audit.finish_invocation(status="accepted", retry_count=max(0, attempt_number - 1))
                return response

            if attempt_number < max_attempts:
                audit.finish_attempt(
                    attempt=audit_attempt,
                    request=request,
                    ctx=context,
                    response=response,
                    status="retrying",
                    verdict=AuditVerdict.RETRYING,
                    error_kind="schema_violation",
                    hook_annotations=hook_result.annotations if hook_result is not None else None,
                )
                messages = _append_corrective_suffix(messages, response.validation_result.errors)
                continue

            if llm_optional and fallback is not None:
                fallback_response = _fallback_response(
                    fallback=fallback,
                    output_schema=output_schema,
                    attempt_number=attempt_number,
                    latency_ms=0,
                )
                audit.finish_attempt(
                    attempt=audit_attempt,
                    request=request,
                    ctx=context,
                    response=fallback_response,
                    status="rejected_fallback_used",
                    verdict=AuditVerdict.REJECTED_FALLBACK_USED,
                    error_kind="schema_violation",
                    hook_annotations=hook_result.annotations if hook_result is not None else None,
                )
                audit.finish_invocation(
                    status="rejected_fallback_used",
                    retry_count=max(0, attempt_number - 1),
                    failure_class="schema_violation",
                )
                return fallback_response

            audit.finish_attempt(
                attempt=audit_attempt,
                request=request,
                ctx=context,
                response=response,
                status="failed_schema_violation",
                verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
                error_kind="schema_violation",
                hook_annotations=hook_result.annotations if hook_result is not None else None,
            )
            audit.finish_invocation(
                status="failed_schema_violation",
                retry_count=max(0, attempt_number - 1),
                failure_class="schema_violation",
            )
            raise SchemaViolationError("LLM response failed schema validation", attempts)
        raise RuntimeError("LLM runner exited without a terminal attempt")
    finally:
        await _close_client(client)


async def run_tool_step(
    *,
    request: ToolCallRequest,
    hooks: HookRegistry,
    context: HookContext,
    tool: Callable[[], Any | Awaitable[Any]],
    output_schema: dict[str, Any] | type[BaseModel],
    audit: AuditWriter,
    max_transient_retries: int = 1,
) -> ToolCallResponse:
    attempts: list[ToolCallResponse] = []
    max_attempts = max(1, max_transient_retries + 1)
    for attempt_number in range(1, max_attempts + 1):
        attempt_context = _tool_context_for_attempt(context, request, attempt_number)
        attempt_context = await hooks.run_pre_tool_async(attempt_context)
        audit_attempt = audit.record_tool_pending(
            request=request,
            ctx=attempt_context,
            attempt=attempt_number,
        )
        try:
            response = await _call_tool(
                tool=tool,
                output_schema=output_schema,
                attempt_number=attempt_number,
            )
        except Exception as exc:
            failure_class = _classify_tool_exception(exc)
            response = _tool_failure_response(attempt_number, exc, failure_class)
            attempts.append(response)
            retryable = failure_class == "transient_transport" and attempt_number < max_attempts
            failure_status = (
                "retrying"
                if retryable
                else "failed_transport"
                if failure_class == "transient_transport"
                else "failed_vendor"
            )
            audit.finish_tool_attempt(
                attempt=audit_attempt,
                request=request,
                ctx=attempt_context,
                response=response,
                status=failure_status,
                verdict=AuditVerdict.RETRYING
                if retryable
                else AuditVerdict.REJECTED_SCHEMA_VIOLATION,
                error_kind=type(exc).__name__,
            )
            if retryable:
                continue
            audit.finish_invocation(
                status=failure_status,
                retry_count=max(0, attempt_number - 1),
                failure_class=failure_class,
            )
            raise ToolInvocationError(
                str(exc),
                attempts=attempts,
                failure_class=failure_class,
            ) from exc

        attempts.append(response)
        hook_result = None
        if response.validation_result.valid:
            hook_result = hooks.run_post_tool(attempt_context, response)
            if hook_result.verdict in {
                AuditVerdict.REJECTED_SCHEMA_VIOLATION,
                AuditVerdict.REJECTED_FALLBACK_USED,
            }:
                response = _tool_response_with_hook_errors(
                    response,
                    _hook_validation_errors(hook_result),
                )
                attempts[-1] = response

        if response.validation_result.valid:
            audit.finish_tool_attempt(
                attempt=audit_attempt,
                request=request,
                ctx=attempt_context,
                response=response,
                status="accepted",
                verdict=AuditVerdict.ACCEPTED,
                hook_annotations=hook_result.annotations if hook_result is not None else None,
            )
            audit.finish_invocation(status="accepted", retry_count=max(0, attempt_number - 1))
            return response

        audit.finish_tool_attempt(
            attempt=audit_attempt,
            request=request,
            ctx=attempt_context,
            response=response,
            status="failed_schema_violation",
            verdict=AuditVerdict.REJECTED_SCHEMA_VIOLATION,
            error_kind="schema_violation",
            hook_annotations=hook_result.annotations if hook_result is not None else None,
        )
        audit.finish_invocation(
            status="failed_schema_violation",
            retry_count=max(0, attempt_number - 1),
            failure_class="schema_violation",
        )
        raise ToolInvocationError(
            "tool response failed schema validation",
            attempts=attempts,
            failure_class="schema_violation",
        )
    raise RuntimeError("Tool runner exited without a terminal attempt")


async def _call_llm(
    *,
    client: Any,
    request: LLMCallRequest,
    messages: list[dict[str, str]],
    attempt_number: int,
    output_schema: dict[str, Any] | type[BaseModel],
) -> LLMCallResponse:
    started = time.perf_counter()
    # Every harness call validates the response against ``output_schema``
    # via ``validate_response``, which itself does ``json.loads(content)``
    # first. So every harness path expects parseable JSON, regardless
    # of whether the caller passes ``response_format={"type":"json_object"}``
    # (object schemas) or leaves it None (array schemas like
    # ``CuratorRanking``). Always pass ``validate_json_content=True``
    # so JSON-parse failures trigger the multi-provider chain fallback
    # (see ``llm_client.JSONStrictRetryable``), NOT just object-shaped
    # requests. The ``except JSONStrictRetryable`` below converts a
    # chain-exhausted failure into a schema violation so the harness's
    # own corrective-suffix retry loop runs.
    response_format = request.response_format
    try:
        raw_response = await client.chat_completion(
            messages,
            request.model,
            request.temperature,
            request.max_tokens,
            retries=0,
            response_format=response_format,
            validate_json_content=True,
        )
    except JSONStrictRetryable as exc:
        # Codex round-1 amendment 1: when the provider chain
        # exhausts on JSON-content failure, classify as a schema
        # violation (NOT transport). That keeps the harness's
        # corrective-suffix retry loop in play, so the outer
        # ``max_corrective_retries`` budget actually gets used —
        # next attempt re-runs the chain with a more explicit
        # "return strict JSON only" suffix appended. Without this
        # catch the exception bubbles up to ``run_llm_step``'s
        # generic transport handler at line ~89 and the schema
        # retry block at ~176 never runs.
        latency_ms = int((time.perf_counter() - started) * 1000)
        return LLMCallResponse(
            content=exc.content,
            parsed=None,
            raw_content=exc.content,
            reasoning_text="",
            usage={},
            latency_ms=latency_ms,
            attempt=attempt_number,
            validation_result=ValidationResult(
                valid=False,
                parsed=None,
                errors=[f"JSON-content fallback chain exhausted: {exc}"],
            ),
            provider_used=exc.provider,
            provider_model="",
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    content = str(raw_response.get("content", ""))
    validation = validate_response(content, output_schema)
    return LLMCallResponse(
        content=content,
        parsed=validation.parsed,
        raw_content=str(raw_response.get("raw_content", content)),
        reasoning_text=str(raw_response.get("reasoning_text", "")),
        usage=_response_usage(raw_response),
        latency_ms=latency_ms,
        attempt=attempt_number,
        validation_result=validation,
        provider_used=str(raw_response.get("provider_used", "")),
        provider_model=str(raw_response.get("provider_model", "")),
    )


async def _call_tool(
    *,
    tool: Callable[[], Any | Awaitable[Any]],
    output_schema: dict[str, Any] | type[BaseModel],
    attempt_number: int,
) -> ToolCallResponse:
    started = time.perf_counter()
    raw_result = tool()
    if inspect.isawaitable(raw_result):
        raw_result = await raw_result
    latency_ms = int((time.perf_counter() - started) * 1000)
    content = _tool_content(raw_result)
    validation = validate_response(content, output_schema)
    return ToolCallResponse(
        content=content,
        parsed=validation.parsed,
        raw_content=content,
        latency_ms=latency_ms,
        attempt=attempt_number,
        validation_result=validation,
    )


def _transport_failure_response(attempt_number: int, exc: Exception) -> LLMCallResponse:
    validation = ValidationResult(valid=False, parsed=None, errors=[str(exc)])
    return LLMCallResponse(
        content="",
        parsed=None,
        raw_content="",
        reasoning_text="",
        usage={},
        latency_ms=0,
        attempt=attempt_number,
        validation_result=validation,
    )


def _tool_failure_response(
    attempt_number: int,
    exc: Exception,
    failure_class: str,
) -> ToolCallResponse:
    validation = ValidationResult(valid=False, parsed=None, errors=[str(exc)])
    return ToolCallResponse(
        content="",
        parsed=None,
        raw_content=json.dumps(
            {
                "error": str(exc),
                "error_type": type(exc).__name__,
                "failure_class": failure_class,
            },
            sort_keys=True,
        ),
        latency_ms=0,
        attempt=attempt_number,
        validation_result=validation,
        error=str(exc),
    )


def _fallback_response(
    *,
    fallback: Callable[[], Any],
    output_schema: dict[str, Any] | type[BaseModel],
    attempt_number: int,
    latency_ms: int,
) -> LLMCallResponse:
    fallback_result = fallback()
    content = _fallback_content(fallback_result)
    validation = validate_response(content, output_schema)
    parsed = validation.parsed if validation.valid else fallback_result
    return LLMCallResponse(
        content=content,
        parsed=parsed,
        raw_content=content,
        reasoning_text="",
        usage={},
        latency_ms=latency_ms,
        attempt=attempt_number,
        validation_result=validation,
    )


def _response_with_hook_errors(
    response: LLMCallResponse,
    errors: list[str],
) -> LLMCallResponse:
    return replace(
        response,
        validation_result=ValidationResult(
            valid=False,
            parsed=response.parsed,
            errors=errors or ["post_llm hook rejected response"],
        ),
    )


def _response_with_sentinel_violations(
    response: LLMCallResponse,
    errors: list[str],
) -> LLMCallResponse:
    """Wrap a response so the harness retry loop sees it as a schema
    violation. We prefix each sentinel message so the corrective
    suffix sent back to the LLM clearly reads as a content-quality
    rejection rather than a JSON-shape error.
    """
    prefixed = [f"output sanity gate: {msg}" for msg in errors] or [
        "output sanity gate rejected response (no specific message)",
    ]
    return replace(
        response,
        validation_result=ValidationResult(
            valid=False,
            parsed=response.parsed,
            errors=prefixed,
        ),
    )


def _tool_response_with_hook_errors(
    response: ToolCallResponse,
    errors: list[str],
) -> ToolCallResponse:
    return replace(
        response,
        validation_result=ValidationResult(
            valid=False,
            parsed=response.parsed,
            errors=errors or ["post_tool hook rejected response"],
        ),
    )


def _hook_validation_errors(hook_result: HookResult) -> list[str]:
    errors: list[str] = []
    for name, annotation in hook_result.annotations.items():
        if not isinstance(annotation, dict):
            continue
        raw_errors = annotation.get("errors")
        if isinstance(raw_errors, list):
            for raw_error in raw_errors:
                text = str(raw_error).strip()
                if text:
                    errors.append(f"{name}: {text}")
        raw_message = annotation.get("message")
        if isinstance(raw_message, str) and raw_message.strip():
            errors.append(f"{name}: {raw_message.strip()}")
    return errors


def _fallback_content(fallback_result: Any) -> str:
    if isinstance(fallback_result, BaseModel):
        return fallback_result.json()
    if isinstance(fallback_result, str):
        return fallback_result
    return json.dumps(fallback_result, sort_keys=True)


def _tool_content(raw_result: Any) -> str:
    if isinstance(raw_result, BaseModel):
        return raw_result.json()
    if isinstance(raw_result, str):
        return raw_result
    return json.dumps(raw_result, sort_keys=True)


def _response_usage(raw_response: dict[str, Any]) -> dict[str, Any]:
    usage = raw_response.get("usage", {})
    return dict(usage) if isinstance(usage, dict) else {}


def _context_for_attempt(
    context: HookContext,
    messages: list[dict[str, str]],
    attempt: int,
) -> HookContext:
    prompt_text = _messages_to_hash_text(messages)
    return replace(context, attempt=attempt, prompt_hash=hash_text(prompt_text))


def _tool_context_for_attempt(
    context: HookContext,
    request: ToolCallRequest,
    attempt: int,
) -> HookContext:
    request_text = json.dumps(
        {
            "provider": request.provider,
            "endpoint": request.endpoint,
            "payload": request.payload,
        },
        sort_keys=True,
        default=str,
    )
    return replace(
        context,
        attempt=attempt,
        prompt_filled=request_text,
        prompt_hash=hash_text(request_text),
    )


def _messages_with_prompt(
    messages: list[dict[str, str]],
    prompt_filled: str,
) -> list[dict[str, str]]:
    copied = [dict(message) for message in messages]
    for message in reversed(copied):
        if message.get("role") == "user":
            message["content"] = prompt_filled
            return copied
    copied.append({"role": "user", "content": prompt_filled})
    return copied


def _prompt_with_context(prompt_filled: str, prompt_context: dict[str, Any]) -> str:
    blocks = _render_prompt_context(prompt_context)
    if not blocks:
        return prompt_filled
    return "\n\n".join([*blocks, prompt_filled])


def _render_prompt_context(prompt_context: dict[str, Any]) -> list[str]:
    decisions = prompt_context.get("previous_related_decisions")
    if not isinstance(decisions, list) or not decisions:
        return []
    lines = ["Previous related decisions"]
    for index, decision in enumerate(decisions, start=1):
        line = _decision_context_line(index, decision)
        if line:
            lines.append(line)
    return ["\n".join(lines)] if len(lines) > 1 else []


def _decision_context_line(index: int, decision: object) -> str:
    if isinstance(decision, dict):
        title = str(decision.get("title", "")).strip()
        content = str(decision.get("content", "")).strip()
        labels = decision.get("labels")
        label_text = ""
        if isinstance(labels, list) and labels:
            label_text = " [" + ", ".join(str(label) for label in labels[:4]) + "]"
        if title and content:
            return f"{index}. {title}{label_text}: {content}"
        if content:
            return f"{index}. {content}{label_text}"
        if title:
            return f"{index}. {title}{label_text}"
        return ""
    text = str(decision).strip()
    return f"{index}. {text}" if text else ""


def _append_corrective_suffix(
    messages: list[dict[str, str]],
    errors: list[str],
) -> list[dict[str, str]]:
    error_lines = "\n".join(f"- {error}" for error in errors)
    suffix = (
        "The previous response failed output schema validation. "
        "Return only strict JSON matching the requested schema. "
        "Do not include markdown, prose, comments, or trailing text.\n"
        f"Schema errors:\n{error_lines}"
    )
    return [*messages, {"role": "system", "content": suffix}]


def _classify_tool_exception(exc: Exception) -> str:
    if isinstance(exc, TimeoutError | ConnectionError):
        return "transient_transport"
    text = str(exc).lower()
    transient_markers = (
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "timeout",
        "timed out",
        "connection",
        "network",
        "temporarily unavailable",
        "request failed",
    )
    if any(marker in text for marker in transient_markers):
        return "transient_transport"
    return "vendor_error"


def _messages_to_hash_text(messages: list[dict[str, str]]) -> str:
    return json.dumps(messages, sort_keys=True, separators=(",", ":"))


async def _close_client(client: Any) -> None:
    close = getattr(client, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result
