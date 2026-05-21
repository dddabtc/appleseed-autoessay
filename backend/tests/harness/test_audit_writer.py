import json

from harness.helpers import harness_run_context
from sqlalchemy import select

from autoessay.harness import (
    AuditVerdict,
    AuditWriter,
    HookContext,
    LLMCallRequest,
    LLMCallResponse,
)
from autoessay.harness.types import ValidationResult
from autoessay.models import AgentInvocation, ProviderCall


def test_audit_writer_records_db_rows_jsonl_and_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    with harness_run_context(app_session, tmp_path) as (session, run_dir, run_id):
        _assert_audit_writer_records_db_rows_jsonl_and_artifacts(session, run_dir, run_id)


def _assert_audit_writer_records_db_rows_jsonl_and_artifacts(
    session,  # type: ignore[no-untyped-def]
    run_dir,
    run_id: str,
) -> None:
    context = HookContext(
        run_id=run_id,
        phase="discovery",
        step_id="scout.query_expansion",
        user_id="single-user",
        attempt=1,
        prompt_template_id="scout.query_expansion.v1",
        prompt_filled="prompt",
        prompt_hash="hash",
        project_title="Project",
        run_metadata={},
    )
    request = LLMCallRequest(
        messages=[{"role": "user", "content": "prompt"}],
        model="test-model",
        temperature=0.2,
        max_tokens=100,
        response_format={"type": "json_object"},
        request_id="request_1",
        prompt_template_id="scout.query_expansion.v1",
    )
    writer = AuditWriter(session=session, run_dir=run_dir, agent_name="Scout")

    attempt = writer.record_pending(
        request=request, ctx=context, messages=request.messages, attempt=1
    )
    writer.finish_attempt(
        attempt=attempt,
        request=request,
        ctx=context,
        response=LLMCallResponse(
            content='{"ok": true}',
            parsed={"ok": True},
            raw_content='{"ok": true}',
            reasoning_text="",
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            latency_ms=11,
            attempt=1,
            validation_result=ValidationResult(valid=True, parsed={"ok": True}, errors=[]),
        ),
        status="accepted",
        verdict=AuditVerdict.ACCEPTED,
    )
    writer.finish_invocation(status="accepted", retry_count=0)

    invocations = list(session.scalars(select(AgentInvocation)))
    calls = list(session.scalars(select(ProviderCall)))
    jsonl_lines = (
        (run_dir / "discovery" / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()
    )
    jsonl_payload = json.loads(jsonl_lines[0])

    assert len(invocations) == 1
    assert invocations[0].run_id == run_id
    assert invocations[0].agent_name == "Scout"
    assert invocations[0].status == "accepted"
    assert invocations[0].metadata_json["phase"] == "discovery"
    assert len(calls) == 1
    assert calls[0].status == "accepted"
    assert calls[0].request_hash
    assert calls[0].response_ref == "discovery/responses/request_1.attempt1.txt"
    assert jsonl_payload["provider_call_id"] == calls[0].id
    assert jsonl_payload["prompt_artifact"] == "discovery/prompts/request_1.attempt1.txt"
    assert (run_dir / "discovery" / "prompts" / "request_1.txt").is_file()
    assert (run_dir / "discovery" / "responses" / "request_1.txt").is_file()
