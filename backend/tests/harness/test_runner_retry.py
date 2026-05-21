import json

from harness.helpers import harness_run_context
from pydantic import BaseModel
from sqlalchemy import select

from autoessay.harness import AuditWriter, HookContext, HookRegistry, LLMCallRequest, run_llm_step
from autoessay.models import ProviderCall


class Answer(BaseModel):
    value: str


class RetryFakeLLM:
    instances: list["RetryFakeLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        RetryFakeLLM.instances.append(self)

    async def chat_completion(
        self,
        messages,  # type: ignore[no-untyped-def]
        model: str,
        temperature: float,
        max_tokens: int,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        del model, temperature, max_tokens, retries, response_format
        self.messages.append([dict(message) for message in messages])
        if len(self.messages) == 1:
            return {"content": "not-json", "raw_content": "not-json", "usage": {"total_tokens": 2}}
        return {
            "content": '{"value": "accepted"}',
            "raw_content": '{"value": "accepted"}',
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            "reasoning_text": "",
        }

    async def aclose(self) -> None:
        return None


async def test_runner_retries_once_then_accepts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    RetryFakeLLM.instances = []
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", RetryFakeLLM)
    with harness_run_context(app_session, tmp_path) as (session, run_dir, run_id):
        response = await run_llm_step(
            request=_request(),
            hooks=HookRegistry(),
            context=_context(run_id),
            output_schema=Answer,
            audit=AuditWriter(session=session, run_dir=run_dir, agent_name="Scout"),
            max_corrective_retries=1,
        )

        provider_calls = list(session.scalars(select(ProviderCall)))
        jsonl = _read_jsonl(run_dir / "discovery" / "llm_calls.jsonl")
        fake = RetryFakeLLM.instances[0]

        assert response.parsed.value == "accepted"
        assert len(fake.messages) == 2
        assert any("Schema errors" in message["content"] for message in fake.messages[1])
        assert len(provider_calls) == 2
        assert jsonl[0]["status"] == "retrying"
        assert jsonl[1]["status"] == "accepted"
        assert any(call.status == "accepted" for call in provider_calls)


class JSONStrictExhaustingLLM:
    """Fake that simulates LLMClient's chain exhausting on JSON parse
    failure — raises JSONStrictRetryable on every call."""

    instances: list["JSONStrictExhaustingLLM"] = []

    def __init__(self) -> None:
        self.calls = 0
        JSONStrictExhaustingLLM.instances.append(self)

    async def chat_completion(
        self,
        messages,  # type: ignore[no-untyped-def]
        model: str,
        temperature: float,
        max_tokens: int,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        del messages, model, temperature, max_tokens, retries, response_format
        self.calls += 1
        from autoessay.llm_client import JSONStrictRetryable

        raise JSONStrictRetryable(
            provider="fake-tertiary",
            content="not-json across all providers",
            cause=ValueError("simulated"),
        )

    async def aclose(self) -> None:
        return None


async def test_runner_treats_json_chain_exhaustion_as_schema_violation(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    """Codex round-1 amendment 1: when LLMClient.JSONStrictRetryable
    bubbles out of the provider chain, ``_call_llm`` converts it to
    a schema violation so the harness's corrective-suffix retry loop
    runs — instead of treating it as a transport failure that aborts
    the whole call. With max_corrective_retries=2, three full chain
    passes are attempted (3 × ``len(providers)`` LLM calls in prod;
    here the fake collapses each chain pass into one raise). After
    all 3 attempts still fail, the harness raises
    ``SchemaViolationError`` (NOT transport).
    """
    import pytest

    from autoessay.harness.runner import SchemaViolationError

    JSONStrictExhaustingLLM.instances = []
    monkeypatch.setattr(
        "autoessay.harness.runner.LLMClient",
        JSONStrictExhaustingLLM,
    )
    with harness_run_context(app_session, tmp_path) as (session, run_dir, run_id):
        with pytest.raises(SchemaViolationError) as excinfo:
            await run_llm_step(
                request=_request(),
                hooks=HookRegistry(),
                context=_context(run_id),
                output_schema=Answer,
                audit=AuditWriter(session=session, run_dir=run_dir, agent_name="Scout"),
                max_corrective_retries=2,
            )

        fake = JSONStrictExhaustingLLM.instances[0]
        # 3 chain passes (initial + 2 corrective retries) — the
        # corrective-suffix loop ran, NOT the transport-error abort
        # path which would have stopped after 1 call.
        assert fake.calls == 3
        # The error chain is "schema violation" (the fix), not
        # "transport" — confirms _call_llm absorbed JSONStrictRetryable
        # rather than letting it bubble as transport failure.
        last_attempt = excinfo.value.attempts[-1]
        assert last_attempt.validation_result.valid is False
        assert any(
            "JSON-content fallback chain exhausted" in err
            for err in last_attempt.validation_result.errors
        )


def _request() -> LLMCallRequest:
    return LLMCallRequest(
        messages=[{"role": "user", "content": "prompt"}],
        model="test-model",
        temperature=0.2,
        max_tokens=100,
        response_format={"type": "json_object"},
        request_id="retry_request",
        prompt_template_id="template",
    )


def _context(run_id: str) -> HookContext:
    return HookContext(
        run_id=run_id,
        phase="discovery",
        step_id="step",
        user_id="single-user",
        attempt=1,
        prompt_template_id="template",
        prompt_filled="prompt",
        prompt_hash="hash",
        project_title="Project",
        run_metadata={},
    )


def _read_jsonl(path) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
