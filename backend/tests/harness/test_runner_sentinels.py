"""Runner-level test for the harness sentinel mechanism.

Confirms that the harness automatically rejects an LLM response whose
parsed body contains a forbidden pattern (e.g. literal "[UNCITED]"),
exactly as if Pydantic validation failed.
"""

from __future__ import annotations

import json

from harness.helpers import harness_run_context
from pydantic import BaseModel
from sqlalchemy import select

from autoessay.harness import (
    AuditWriter,
    HookContext,
    HookRegistry,
    LLMCallRequest,
    run_llm_step,
)
from autoessay.models import ProviderCall


class Section(BaseModel):
    section_id: str
    prose: str


class SentinelFakeLLM:
    instances: list[SentinelFakeLLM] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        SentinelFakeLLM.instances.append(self)

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
            content = json.dumps(
                {
                    "section_id": "introduction",
                    "prose": "本节包含 [UNCITED] 字面，不应通过 sanity gate。",
                },
            )
        else:
            content = json.dumps(
                {
                    "section_id": "introduction",
                    "prose": "本节是合规正文，没有 sanity 违规。",
                },
            )
        return {
            "content": content,
            "raw_content": content,
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            "reasoning_text": "",
        }

    async def aclose(self) -> None:
        return None


async def test_sentinel_violation_triggers_corrective_retry(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
    monkeypatch,
) -> None:
    SentinelFakeLLM.instances = []
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", SentinelFakeLLM)
    with harness_run_context(app_session, tmp_path) as (session, run_dir, run_id):
        response = await run_llm_step(
            request=_request(),
            hooks=HookRegistry(),
            context=_context(run_id),
            output_schema=Section,
            audit=AuditWriter(session=session, run_dir=run_dir, agent_name="Drafter"),
            max_corrective_retries=2,
        )
        provider_calls = list(session.scalars(select(ProviderCall)))
        fake = SentinelFakeLLM.instances[0]

    # First attempt had "[UNCITED]" — must have been rejected.
    # Second attempt was clean and accepted.
    assert response.parsed.prose == "本节是合规正文，没有 sanity 违规。"
    assert len(fake.messages) == 2
    # The corrective suffix passed back to the LLM should mention the
    # sanity gate, so the model knows what to fix.
    second_prompt = "\n".join(m["content"] for m in fake.messages[1])
    assert "output sanity gate" in second_prompt
    assert "UNCITED" in second_prompt or "[UNCITED]" in second_prompt
    # ProviderCall rows: first retrying, second accepted.
    assert len(provider_calls) == 2
    assert any(call.status == "retrying" for call in provider_calls)
    assert any(call.status == "accepted" for call in provider_calls)


def _request() -> LLMCallRequest:
    return LLMCallRequest(
        messages=[{"role": "user", "content": "draft introduction"}],
        model="test-model",
        temperature=0.2,
        max_tokens=400,
        response_format={"type": "json_object"},
        request_id="sentinel_request",
        prompt_template_id="drafter.section.v1",
    )


def _context(run_id: str) -> HookContext:
    return HookContext(
        run_id=run_id,
        phase="discovery",
        step_id="step",
        user_id="single-user",
        attempt=1,
        prompt_template_id="drafter.section.v1",
        prompt_filled="prompt",
        prompt_hash="hash",
        project_title="Project",
        run_metadata={},
    )
