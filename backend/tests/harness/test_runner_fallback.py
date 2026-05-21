import json

from harness.helpers import harness_run_context
from pydantic import BaseModel
from sqlalchemy import select

from autoessay.harness import AuditWriter, HookContext, HookRegistry, LLMCallRequest, run_llm_step
from autoessay.models import ProviderCall


class Answer(BaseModel):
    value: str


class InvalidFakeLLM:
    calls = 0

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
        InvalidFakeLLM.calls += 1
        return {"content": "not-json", "raw_content": "not-json", "usage": {"total_tokens": 1}}

    async def aclose(self) -> None:
        return None


async def test_runner_uses_fallback_for_optional_schema_failure(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    InvalidFakeLLM.calls = 0
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", InvalidFakeLLM)
    fallback_called = False

    def fallback() -> dict[str, str]:
        nonlocal fallback_called
        fallback_called = True
        return {"value": "fallback"}

    with harness_run_context(app_session, tmp_path) as (session, run_dir, run_id):
        response = await run_llm_step(
            request=_request(),
            hooks=HookRegistry(),
            context=_context(run_id),
            output_schema=Answer,
            audit=AuditWriter(session=session, run_dir=run_dir, agent_name="Scout"),
            max_corrective_retries=1,
            llm_optional=True,
            fallback=fallback,
        )

        provider_calls = list(session.scalars(select(ProviderCall)))
        jsonl = _read_jsonl(run_dir / "discovery" / "llm_calls.jsonl")

        assert fallback_called is True
        assert InvalidFakeLLM.calls == 2
        assert response.parsed.value == "fallback"
        assert len(provider_calls) == 2
        assert jsonl[-1]["audit_verdict"] == "rejected_fallback_used"
        assert any(call.status == "rejected_fallback_used" for call in provider_calls)


def _request() -> LLMCallRequest:
    return LLMCallRequest(
        messages=[{"role": "user", "content": "prompt"}],
        model="test-model",
        temperature=0.2,
        max_tokens=100,
        response_format={"type": "json_object"},
        request_id="fallback_request",
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
