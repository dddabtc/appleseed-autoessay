from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from autoessay.config import DEFAULT_EXPRESS_ARS_SKILL_PATH, LLMProviderSpec, Settings, get_settings
from autoessay.experiments.abc_generator import _load_ars_full_mode_prompt
from autoessay.experiments.abc_prompts import PromptBundle
from autoessay.express_runner import (
    EXPRESS_FAILURE_BUDGET,
    EXPRESS_FAILURE_CANCELLED,
    EXPRESS_FAILURE_TIMEOUT,
    EXPRESS_FAILURE_TRUNCATED,
    ExpressCompletion,
    ExpressTimeout,
    ExpressTransportError,
    ExpressTruncated,
    HttpExpressClient,
    _extract_json_object,
    run_express,
)
from autoessay.llm_client import LLMClient
from autoessay.models import Domain, Project, Run, RunEvent, User, utcnow
from autoessay.run_writer import create_run_directory


@dataclass
class FakeExpressClient:
    responses: list[ExpressCompletion | Exception]
    calls: int = 0

    def complete(
        self,
        prompt: PromptBundle,
        *,
        timeout_seconds: int,
        max_tokens: int,
        expect_json: bool = False,
    ) -> ExpressCompletion:
        del prompt, timeout_seconds, max_tokens, expect_json
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _usage(total: int) -> dict[str, int]:
    return {
        "prompt_tokens": total // 2,
        "completion_tokens": total - total // 2,
        "total_tokens": total,
    }


def _complete_manuscript() -> str:
    body = "\n\n".join(
        [
            "# 金融危机中的地方银行治理",
            "## 摘要\n本文讨论地方银行治理与危机应对之间的关系，强调制度安排的解释力。",
            "## 关键词\n金融史；银行治理；危机",
            "## 一、引言\n" + "地方银行在危机中的行为体现了监管结构与资产负债约束。 " * 80,
            "## 二、分析\n" + "本文以历史制度主义的角度分析地方银行的风险暴露。 " * 80,
            "## 三、结论\n研究表明，治理结构会改变危机期间的流动性选择。",
            "## 参考文献\n[1] Sample, 2020.",
        ],
    )
    assert len(body) > 1000
    return body


def _audit_json() -> str:
    return json.dumps(
        {
            "status": "pass",
            "summary": "audit-only pass",
            "citation_traceability": {"status": "soft"},
            "word_count": {"status": "ok"},
            "style_compliance": {"status": "ok"},
            "issues": [],
        },
    )


def _seed_express_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
    *,
    state: str = "DOMAIN_LOADED",
) -> str:
    skill_path = tmp_path / "ars" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        "# academic-paper\n\n"
        "## Quick Start\nWrite a complete academic paper.\n\n"
        "## Agent Team (12 Agents)\nWriter.\n\n"
        "## Orchestration Workflow (8 Phases)\nSingle call for this test.\n\n"
        "## Operational Modes (10 Modes)\nFull mode.\n\n"
        "## Anti-Patterns\nNo process notes.\n\n"
        "## Quality Standards\nComplete manuscript.\n\n"
        "## Output Language\nMatch user language.\n",
        encoding="utf-8",
    )
    agents_dir = skill_path.parent / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "intake_agent.md").write_text(
        "## Interview Protocol\nUse defaults.\n\n## Output Format\nMarkdown.\n",
        encoding="utf-8",
    )
    (agents_dir / "structure_architect_agent.md").write_text(
        "## Role Definition\nArchitect.\n\n"
        "## Core Principles\nCoherence.\n\n"
        "## Structure Selection\nAcademic.\n\n"
        "## Outline Construction Process\nPlan.\n\n"
        "## Output Format\nMarkdown.\n",
        encoding="utf-8",
    )
    (agents_dir / "draft_writer_agent.md").write_text(
        "## Role Definition\nWriter.\n\n"
        "## Core Principles\nEvidence.\n\n"
        "## Writing Process\nDraft.\n\n"
        "## Writing Style Guidelines\nClear.\n\n"
        "## Output Format\nMarkdown.\n\n"
        "## Quality Gates\nComplete.\n\n"
        "### Phase 4b — Writer paper-visible drafting + self-scoring\nNo hidden notes.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOESSAY_EXPRESS_ARS_SKILL_PATH", str(skill_path))
    monkeypatch.setenv("AUTOESSAY_EXPRESS_MANUSCRIPT_MAX_TOKENS", "2000")
    monkeypatch.setenv("AUTOESSAY_EXPRESS_AUDIT_MAX_TOKENS", "1000")
    monkeypatch.setenv("AUTOESSAY_EXPRESS_TOKEN_CAP", "100000")
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "0")
    get_settings.cache_clear()
    run_id = f"run_{uuid4().hex}"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_express",
        state=state,
        domain_id="financial_history",
    )
    with app_session() as session:
        session.add(User(id="single-user", display_name="Single User"))
        session.add(
            Domain(
                id="financial_history",
                display_name="Financial history",
                version="0.1.0",
            ),
        )
        session.flush()
        session.add(
            Project(
                id="proj_express",
                user_id="single-user",
                title="地方银行治理与金融危机",
                domain_id="financial_history",
                domain_version="0.1.0",
                language="zh",
                status="ACTIVE",
            ),
        )
        session.flush()
        session.add(
            Run(
                id=run_id,
                project_id="proj_express",
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state=state,
                baseline_hash="test",
                generation_mode="express",
            ),
        )
        session.commit()
    return run_id


def test_default_express_ars_skill_path_is_vendored_and_manifest_pinned(monkeypatch) -> None:
    monkeypatch.delenv("AUTOESSAY_EXPRESS_ARS_SKILL_PATH", raising=False)
    settings = Settings()

    assert settings.express_ars_skill_path == DEFAULT_EXPRESS_ARS_SKILL_PATH
    assert settings.express_ars_skill_path.is_file()

    context = _load_ars_full_mode_prompt(settings.express_ars_skill_path)
    assert context["ars_skill_sha"] == "be49a42940332e75a11287ac89767d0df6d02019"
    labels = {item["label"] for item in context["manifest"]}  # type: ignore[index]
    assert labels == {
        "SKILL.md mode/workflow excerpt",
        "intake_agent defaults excerpt",
        "structure_architect_agent excerpt",
        "draft_writer_agent excerpt",
        "writer_full contract JSON",
    }


def test_express_ars_skill_path_env_override_still_wins(monkeypatch) -> None:
    override = Path("/tmp/ars-experiment/academic-research-skills/academic-paper/SKILL.md")
    monkeypatch.setenv("AUTOESSAY_EXPRESS_ARS_SKILL_PATH", str(override))

    assert Settings().express_ars_skill_path == override


def test_http_express_client_pins_provider_models_without_codex_binary(monkeypatch) -> None:
    monkeypatch.setattr("autoessay.express_runner.shutil.which", lambda _: None)
    captured: dict[str, object] = {}

    async def fake_chat_completion(
        self: LLMClient,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int = 4000,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        force_no_reasoning: bool = False,
        validate_json_content: bool = False,
        stream: bool = False,
    ) -> dict[str, object]:
        del messages, max_tokens, retries, response_format, validate_json_content
        provider = self._providers[0]  # type: ignore[attr-defined]
        captured["model_arg"] = model
        captured["temperature"] = temperature
        captured["force_no_reasoning"] = force_no_reasoning
        captured["stream"] = stream
        captured["provider_model"] = provider.model
        return {
            "content": "# 完整论文\n\n这是测试输出。",
            "usage": _usage(123),
            "provider_used": provider.name,
            "provider_model": provider.model,
            "finish_reason": "stop",
        }

    monkeypatch.setattr(LLMClient, "chat_completion", fake_chat_completion)
    client = HttpExpressClient(
        model="gpt-5.4",
        providers=[
            LLMProviderSpec(
                name="apiport",
                base_url="https://apiport.example",
                api_key="token",
                model="gpt-4o-mini",
            ),
        ],
    )

    response = client.complete(
        PromptBundle(system="system", user="user"),
        timeout_seconds=30,
        max_tokens=1000,
    )

    assert response.provider == "apiport"
    assert response.provider_model == "gpt-5.4"
    assert response.finish_reason == "stop"
    assert captured == {
        "model_arg": "gpt-5.4",
        "temperature": 0.7,
        "force_no_reasoning": True,
        "stream": True,
        "provider_model": "gpt-5.4",
    }


def test_express_runner_success_path_writes_artifacts_and_express_states(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    client = FakeExpressClient(
        [
            ExpressCompletion(_complete_manuscript(), "fake", "gpt-5.4", _usage(5000)),
            ExpressCompletion(_audit_json(), "fake", "gpt-5.4", _usage(500)),
        ],
    )
    with app_session() as session:
        result = run_express(run_id, session, completion_client=client)
        assert result["state"] == "EXPRESS_DONE"

    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run_dir = Path(run.run_dir)
        assert run.state == "EXPRESS_DONE"
        assert (run_dir / "express" / "ars_manuscript_raw.md").is_file()
        assert (run_dir / "express" / "audit_critic.json").is_file()
        assert (run_dir / "integrity" / "integrity_summary.json").is_file()
        assert (run_dir / "express" / "humanizer.json").is_file()
        assert (run_dir / "exports" / "manifest.json").is_file()
        transitions = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "state_transition",
            ),
        ).all()
        payloads = [json.loads(event.payload) for event in transitions]
        assert [p["to_state"] for p in payloads] == ["EXPRESS_RUNNING", "EXPRESS_DONE"]


def test_express_runner_accepts_api_pre_enqueued_running_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.state = "EXPRESS_RUNNING"
        session.commit()

    client = FakeExpressClient(
        [
            ExpressCompletion(_complete_manuscript(), "fake", "gpt-5.4", _usage(5000)),
            ExpressCompletion(_audit_json(), "fake", "gpt-5.4", _usage(500)),
        ],
    )
    with app_session() as session:
        result = run_express(run_id, session, completion_client=client)
        assert result["state"] == "EXPRESS_DONE"

    with app_session() as session:
        transitions = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "state_transition",
            ),
        ).all()
        payloads = [json.loads(event.payload) for event in transitions]
        assert [p["to_state"] for p in payloads] == ["EXPRESS_DONE"]
        started = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "express_generation_started",
            ),
        ).first()
        assert started is not None


def test_express_runner_retries_one_transport_error(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    client = FakeExpressClient(
        [
            ExpressTransportError("temporary gateway failure"),
            ExpressCompletion(_complete_manuscript(), "fake", "gpt-5.4", _usage(5000)),
            ExpressCompletion(_audit_json(), "fake", "gpt-5.4", _usage(500)),
        ],
    )
    with app_session() as session:
        result = run_express(run_id, session, completion_client=client)
    assert result["state"] == "EXPRESS_DONE"
    assert client.calls == 3


def test_express_runner_budget_failure_does_not_call_llm(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOESSAY_EXPRESS_TOKEN_CAP", "1000")
    get_settings.cache_clear()
    client = FakeExpressClient([])
    with app_session() as session:
        result = run_express(run_id, session, completion_client=client)
    assert result["state"] == "EXPRESS_FAILED"
    assert result["failure_code"] == EXPRESS_FAILURE_BUDGET
    assert client.calls == 0


def test_express_runner_cancel_failure_is_not_retried(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    client = FakeExpressClient(
        [ExpressCompletion(_complete_manuscript(), "fake", "gpt-5.4", _usage(5000))],
    )
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.cancel_requested_at = utcnow()
        session.commit()
        result = run_express(run_id, session, completion_client=client)
    assert result["state"] == "EXPRESS_FAILED"
    assert result["failure_code"] == EXPRESS_FAILURE_CANCELLED
    assert client.calls == 0


def test_express_runner_timeout_failure(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    client = FakeExpressClient([ExpressTimeout("timeout")])
    with app_session() as session:
        result = run_express(run_id, session, completion_client=client)
    assert result["state"] == "EXPRESS_FAILED"
    assert result["failure_code"] == EXPRESS_FAILURE_TIMEOUT
    assert client.calls == 1


def test_express_runner_audit_timeout_failure(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    client = FakeExpressClient(
        [
            ExpressCompletion(_complete_manuscript(), "fake", "gpt-5.4", _usage(5000)),
            ExpressTimeout("audit timeout"),
        ],
    )
    with app_session() as session:
        result = run_express(run_id, session, completion_client=client)
    assert result["state"] == "EXPRESS_FAILED"
    assert result["failure_code"] == EXPRESS_FAILURE_TIMEOUT
    assert client.calls == 2


def test_express_runner_truncated_failure(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    client = FakeExpressClient(
        [ExpressCompletion("too short", "fake", "gpt-5.4", _usage(200), finish_reason="length")],
    )
    with app_session() as session:
        result = run_express(run_id, session, completion_client=client)
    assert result["state"] == "EXPRESS_FAILED"
    assert result["failure_code"] == EXPRESS_FAILURE_TRUNCATED
    assert client.calls == 1


def test_express_runner_audit_invalid_json_failure_is_not_retried(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id = _seed_express_run(app_session, tmp_path, monkeypatch)
    client = FakeExpressClient(
        [
            ExpressCompletion(_complete_manuscript(), "fake", "gpt-5.4", _usage(5000)),
            ExpressCompletion("not json", "fake", "gpt-5.4", _usage(500)),
        ],
    )
    with app_session() as session:
        result = run_express(run_id, session, completion_client=client)
    assert result["state"] == "EXPRESS_FAILED"
    assert result["failure_code"] == EXPRESS_FAILURE_TRUNCATED
    assert client.calls == 2


def test_extract_json_object_passes_through_clean_json() -> None:
    assert _extract_json_object('{"audit": "needs_revision"}') == ('{"audit": "needs_revision"}')


def test_extract_json_object_strips_markdown_fence() -> None:
    fenced = '```json\n{"audit": "ok"}\n```'
    assert json.loads(_extract_json_object(fenced)) == {"audit": "ok"}


def test_extract_json_object_strips_unlabeled_fence() -> None:
    fenced = '```\n{"audit": "ok"}\n```'
    assert json.loads(_extract_json_object(fenced)) == {"audit": "ok"}


def test_extract_json_object_recovers_from_prose_wrapper() -> None:
    body = 'Here is my audit:\n{"audit": "needs_revision", "score": 3}\nThank you.'
    assert json.loads(_extract_json_object(body)) == {
        "audit": "needs_revision",
        "score": 3,
    }


def test_extract_json_object_raises_truncated_when_no_object() -> None:
    try:
        _extract_json_object("totally not json")
    except ExpressTruncated as exc:
        assert "no object boundary" in str(exc)
    else:
        raise AssertionError("expected ExpressTruncated for non-JSON content")


def test_extract_json_object_raises_truncated_when_braces_invalid() -> None:
    try:
        _extract_json_object("garbage {not: valid json} trail")
    except ExpressTruncated as exc:
        assert "non-JSON audit" in str(exc)
    else:
        raise AssertionError("expected ExpressTruncated for invalid JSON")
