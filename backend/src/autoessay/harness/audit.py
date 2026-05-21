from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from autoessay.harness.types import (
    AuditVerdict,
    HookContext,
    LLMCallRequest,
    LLMCallResponse,
    ToolCallRequest,
    ToolCallResponse,
)
from autoessay.models import AgentInvocation, ProviderCall, utcnow


@dataclass
class AuditAttempt:
    provider_call_id: str
    request_hash: str
    prompt_artifact: Path
    response_artifact: Path
    primary_prompt_artifact: Path
    primary_response_artifact: Path
    attempt: int


class AuditWriter:
    def __init__(
        self,
        *,
        session: Session,
        run_dir: str | Path,
        agent_name: str,
        provider: str = "one_api",
        auto_commit: bool = True,
    ) -> None:
        self._session = session
        self._run_dir = Path(run_dir)
        self._agent_name = agent_name
        self._provider = provider
        self._auto_commit = auto_commit
        self._invocation: AgentInvocation | None = None

    @property
    def agent_invocation_id(self) -> str | None:
        if self._invocation is None:
            return None
        return self._invocation.id

    def start_invocation(self, ctx: HookContext) -> AgentInvocation:
        if self._invocation is not None:
            return self._invocation
        invocation = AgentInvocation(
            id=f"agent_invocation_{uuid4().hex}",
            run_id=ctx.run_id,
            agent_name=self._agent_name,
            status="started",
            failure_class=None,
            retry_count=0,
            started_at=utcnow(),
            finished_at=None,
            metadata_json={
                "phase": ctx.phase,
                "step_id": ctx.step_id,
                "user_id": ctx.user_id,
                "prompt_template_id": ctx.prompt_template_id,
                "prompt_hash": ctx.prompt_hash,
                "project_title": ctx.project_title,
                "run_metadata": ctx.run_metadata,
                "attempts": [],
            },
        )
        self._session.add(invocation)
        self._invocation = invocation
        self._save()
        return invocation

    def record_pending(
        self,
        *,
        request: LLMCallRequest,
        ctx: HookContext,
        messages: list[dict[str, str]],
        attempt: int,
    ) -> AuditAttempt:
        invocation = self.start_invocation(ctx)
        phase_dir = self._phase_dir(ctx.phase)
        prompt_artifact = phase_dir / "prompts" / f"{request.request_id}.attempt{attempt}.txt"
        response_artifact = phase_dir / "responses" / f"{request.request_id}.attempt{attempt}.txt"
        primary_prompt = phase_dir / "prompts" / f"{request.request_id}.txt"
        primary_response = phase_dir / "responses" / f"{request.request_id}.txt"
        prompt_text = _messages_to_prompt_artifact(messages)
        _write_text(prompt_artifact, prompt_text)
        if attempt == 1:
            _write_text(primary_prompt, prompt_text)

        request_hash = hash_request(
            {
                "request_id": request.request_id,
                "attempt": attempt,
                "messages": messages,
                "model": request.model,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "response_format": request.response_format,
            },
        )
        provider_call = ProviderCall(
            id=f"provider_call_{uuid4().hex}",
            run_id=ctx.run_id,
            provider=self._provider,
            call_type="llm",
            status="pending",
            units=None,
            estimated_cost=None,
            request_hash=request_hash,
            response_ref=None,
        )
        self._session.add(provider_call)
        self._append_attempt_metadata(
            invocation,
            {
                "attempt": attempt,
                "provider_call_id": provider_call.id,
                "request_hash": request_hash,
                "status": "pending",
                "model": request.model,
                "prompt_artifact": self._relative(prompt_artifact),
                "primary_prompt_artifact": self._relative(primary_prompt),
                "prompt_template_id": request.prompt_template_id,
            },
        )
        self._save()
        return AuditAttempt(
            provider_call_id=provider_call.id,
            request_hash=request_hash,
            prompt_artifact=prompt_artifact,
            response_artifact=response_artifact,
            primary_prompt_artifact=primary_prompt,
            primary_response_artifact=primary_response,
            attempt=attempt,
        )

    def record_tool_pending(
        self,
        *,
        request: ToolCallRequest,
        ctx: HookContext,
        attempt: int,
    ) -> AuditAttempt:
        invocation = self.start_invocation(ctx)
        phase_dir = self._phase_dir(ctx.phase)
        request_artifact = (
            phase_dir / "tool_requests" / f"{request.request_id}.attempt{attempt}.json"
        )
        response_artifact = (
            phase_dir / "tool_responses" / f"{request.request_id}.attempt{attempt}.json"
        )
        primary_request = phase_dir / "tool_requests" / f"{request.request_id}.json"
        primary_response = phase_dir / "tool_responses" / f"{request.request_id}.json"
        request_text = _tool_request_artifact(request, attempt)
        _write_text(request_artifact, request_text)
        if attempt == 1:
            _write_text(primary_request, request_text)

        request_hash = hash_request(
            {
                "request_id": request.request_id,
                "attempt": attempt,
                "provider": request.provider,
                "endpoint": request.endpoint,
                "payload": request.payload,
            },
        )
        provider_call = ProviderCall(
            id=f"provider_call_{uuid4().hex}",
            run_id=ctx.run_id,
            provider=request.provider,
            call_type="tool",
            status="pending",
            units=None,
            estimated_cost=None,
            request_hash=request_hash,
            response_ref=None,
        )
        self._session.add(provider_call)
        self._append_attempt_metadata(
            invocation,
            {
                "attempt": attempt,
                "provider_call_id": provider_call.id,
                "request_hash": request_hash,
                "status": "pending",
                "provider": request.provider,
                "endpoint": request.endpoint,
                "request_artifact": self._relative(request_artifact),
                "primary_request_artifact": self._relative(primary_request),
                "prompt_template_id": request.prompt_template_id,
            },
        )
        self._save()
        return AuditAttempt(
            provider_call_id=provider_call.id,
            request_hash=request_hash,
            prompt_artifact=request_artifact,
            response_artifact=response_artifact,
            primary_prompt_artifact=primary_request,
            primary_response_artifact=primary_response,
            attempt=attempt,
        )

    def finish_attempt(
        self,
        *,
        attempt: AuditAttempt,
        request: LLMCallRequest,
        ctx: HookContext,
        response: LLMCallResponse,
        status: str,
        verdict: AuditVerdict,
        error_kind: str | None = None,
        hook_annotations: dict[str, Any] | None = None,
    ) -> None:
        _write_text(attempt.response_artifact, response.raw_content or response.content)
        if status in {"accepted", "rejected_fallback_used"}:
            _write_text(attempt.primary_response_artifact, response.raw_content or response.content)

        # Provider that actually served this response wins over the
        # AuditWriter constructor default. Default ("one_api")
        # remains the fallback for transport-failure responses where
        # no provider answered, and for unit tests that synthesize a
        # response without going through LLMClient.
        provider_for_audit = response.provider_used or self._provider
        provider_model = response.provider_model

        provider_call = self._session.get(ProviderCall, attempt.provider_call_id)
        if provider_call is not None:
            provider_call.provider = provider_for_audit
            provider_call.status = status
            provider_call.units = _token_count(response.usage)
            provider_call.response_ref = self._relative(attempt.response_artifact)

        invocation = self.start_invocation(ctx)
        attempt_metadata = {
            "attempt": attempt.attempt,
            "provider_call_id": attempt.provider_call_id,
            "request_hash": attempt.request_hash,
            "response_ref": self._relative(attempt.response_artifact),
            "status": status,
            "audit_verdict": verdict.value,
            # ``model`` carries the caller's logical model name
            # (kept for back-compat with existing log readers);
            # ``requested_model`` is the canonical replacement;
            # ``provider_model`` is the real per-provider name on
            # the wire, which can differ across providers.
            "model": request.model,
            "requested_model": request.model,
            "provider_used": provider_for_audit,
            "provider_model": provider_model,
            "prompt_tokens": _usage_int(response.usage, "prompt_tokens"),
            "completion_tokens": _usage_int(response.usage, "completion_tokens"),
            "total_tokens": _usage_int(response.usage, "total_tokens"),
            "latency_ms": response.latency_ms,
            "error_kind": error_kind,
            "validation_valid": response.validation_result.valid,
            "validation_errors": response.validation_result.errors,
            "prompt_artifact": self._relative(attempt.prompt_artifact),
            "primary_prompt_artifact": self._relative(attempt.primary_prompt_artifact),
            "response_artifact": self._relative(attempt.response_artifact),
            "primary_response_artifact": self._relative(attempt.primary_response_artifact),
            "hook_annotations": hook_annotations or {},
            "prompt_template_id": request.prompt_template_id,
        }
        self._replace_attempt_metadata(invocation, attempt_metadata)
        self._append_jsonl(
            ctx.phase,
            invocation.id,
            attempt_metadata,
            provider=provider_for_audit,
            call_type="llm",
        )
        self._save()

    def finish_tool_attempt(
        self,
        *,
        attempt: AuditAttempt,
        request: ToolCallRequest,
        ctx: HookContext,
        response: ToolCallResponse,
        status: str,
        verdict: AuditVerdict,
        error_kind: str | None = None,
        hook_annotations: dict[str, Any] | None = None,
    ) -> None:
        _write_text(attempt.response_artifact, response.raw_content or response.content)
        if status == "accepted":
            _write_text(attempt.primary_response_artifact, response.raw_content or response.content)

        provider_call = self._session.get(ProviderCall, attempt.provider_call_id)
        if provider_call is not None:
            provider_call.status = status
            provider_call.units = None
            provider_call.response_ref = self._relative(attempt.response_artifact)

        invocation = self.start_invocation(ctx)
        attempt_metadata = {
            "attempt": attempt.attempt,
            "provider_call_id": attempt.provider_call_id,
            "request_hash": attempt.request_hash,
            "response_ref": self._relative(attempt.response_artifact),
            "status": status,
            "audit_verdict": verdict.value,
            "provider": request.provider,
            "endpoint": request.endpoint,
            "latency_ms": response.latency_ms,
            "error_kind": error_kind,
            "validation_valid": response.validation_result.valid,
            "validation_errors": response.validation_result.errors,
            "request_artifact": self._relative(attempt.prompt_artifact),
            "primary_request_artifact": self._relative(attempt.primary_prompt_artifact),
            "response_artifact": self._relative(attempt.response_artifact),
            "primary_response_artifact": self._relative(attempt.primary_response_artifact),
            "hook_annotations": hook_annotations or {},
            "prompt_template_id": request.prompt_template_id,
        }
        self._replace_attempt_metadata(invocation, attempt_metadata)
        self._append_jsonl(
            ctx.phase,
            invocation.id,
            attempt_metadata,
            provider=request.provider,
            call_type="tool",
        )
        self._save()

    def finish_invocation(
        self,
        *,
        status: str,
        retry_count: int,
        failure_class: str | None = None,
    ) -> None:
        if self._invocation is None:
            return
        self._invocation.status = status
        self._invocation.retry_count = retry_count
        self._invocation.failure_class = failure_class
        self._invocation.finished_at = utcnow()
        self._save()

    def _append_jsonl(
        self,
        phase: str,
        agent_invocation_id: str,
        attempt_metadata: dict[str, Any],
        *,
        provider: str,
        call_type: str,
    ) -> None:
        filename = "tool_calls.jsonl" if call_type == "tool" else "llm_calls.jsonl"
        path = self._phase_dir(phase) / filename
        line = {
            "agent_invocation_id": agent_invocation_id,
            "run_id": self._invocation.run_id if self._invocation is not None else None,
            "agent_name": self._agent_name,
            "provider": provider,
            "call_type": call_type,
            **attempt_metadata,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n",
            )

    def _append_attempt_metadata(
        self,
        invocation: AgentInvocation,
        attempt_metadata: dict[str, Any],
    ) -> None:
        metadata = dict(invocation.metadata_json or {})
        attempts = list(metadata.get("attempts", []))
        attempts.append(attempt_metadata)
        metadata["attempts"] = attempts
        invocation.metadata_json = metadata

    def _replace_attempt_metadata(
        self,
        invocation: AgentInvocation,
        attempt_metadata: dict[str, Any],
    ) -> None:
        metadata = dict(invocation.metadata_json or {})
        attempts = [
            item
            for item in list(metadata.get("attempts", []))
            if not (
                isinstance(item, dict)
                and item.get("provider_call_id") == attempt_metadata["provider_call_id"]
            )
        ]
        attempts.append(attempt_metadata)
        metadata["attempts"] = attempts
        invocation.metadata_json = metadata

    def _phase_dir(self, phase: str) -> Path:
        path = self._run_dir / phase
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._run_dir))
        except ValueError:
            return str(path)

    def _save(self) -> None:
        self._session.flush()
        if self._auto_commit:
            self._session.commit()


def hash_request(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _messages_to_prompt_artifact(messages: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "unknown")
        content = message.get("content", "")
        lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines).rstrip() + "\n"


def _tool_request_artifact(request: ToolCallRequest, attempt: int) -> str:
    return (
        json.dumps(
            {
                "attempt": attempt,
                "provider": request.provider,
                "endpoint": request.endpoint,
                "payload": request.payload,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _token_count(usage: dict[str, Any]) -> int | None:
    total = _usage_int(usage, "total_tokens")
    if total is not None:
        return total
    prompt_tokens = _usage_int(usage, "prompt_tokens") or 0
    completion_tokens = _usage_int(usage, "completion_tokens") or 0
    total = prompt_tokens + completion_tokens
    return total if total > 0 else None


def _usage_int(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    if isinstance(value, int):
        return value
    return None
