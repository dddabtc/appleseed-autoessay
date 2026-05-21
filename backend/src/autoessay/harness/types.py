from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AuditVerdict(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_SCHEMA_VIOLATION = "rejected_schema_violation"
    REJECTED_FALLBACK_USED = "rejected_fallback_used"
    RETRYING = "retrying"


@dataclass
class ValidationResult:
    valid: bool
    parsed: Any
    errors: list[str] = field(default_factory=list)


@dataclass
class LLMCallRequest:
    messages: list[dict[str, str]]
    model: str
    temperature: float
    max_tokens: int
    response_format: dict[str, object] | None
    request_id: str
    prompt_template_id: str


@dataclass
class LLMCallResponse:
    content: str
    parsed: Any
    raw_content: str
    reasoning_text: str
    usage: dict[str, Any]
    latency_ms: int
    attempt: int
    validation_result: ValidationResult
    # Identity of the provider that actually served this response.
    # Empty string for transport-failure responses (no provider
    # answered) or for unit tests that don't go through LLMClient.
    provider_used: str = ""
    # Real per-provider model name sent on the wire (may differ
    # from the caller's logical ``request.model`` when providers
    # disagree on naming, e.g. minimax serving "MiniMax-M2.7" while
    # the caller asked for "gpt-5.4-mini").
    provider_model: str = ""


@dataclass
class ToolCallRequest:
    provider: str
    endpoint: str
    payload: dict[str, Any]
    request_id: str
    prompt_template_id: str


@dataclass
class ToolCallResponse:
    content: str
    parsed: Any
    raw_content: str
    latency_ms: int
    attempt: int
    validation_result: ValidationResult
    error: str | None = None


@dataclass
class HookContext:
    run_id: str
    phase: str
    step_id: str
    user_id: str | None
    attempt: int
    prompt_template_id: str
    prompt_filled: str
    prompt_hash: str
    project_title: str
    run_metadata: dict[str, Any] = field(default_factory=dict)
    prompt_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    context: HookContext | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    verdict: AuditVerdict | None = None
