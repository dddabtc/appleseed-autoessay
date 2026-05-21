import json
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_project
from pydantic import ValidationError
from sqlalchemy import select

from autoessay.agents import proposal
from autoessay.agents.proposal import ProposalOutput, run_proposal_draft
from autoessay.config import get_settings
from autoessay.memory import Memory
from autoessay.models import AgentInvocation, ProviderCall, Run
from autoessay.run_writer import create_run_directory

PROPOSAL_PAYLOAD = {
    "research_question": (
        "How did interwar banking stress reshape lender-of-last-resort practice in regional "
        "credit markets?"
    ),
    "significance": (
        "The project connects financial-history debates about institutional response to "
        "evidence on local banking outcomes."
    ),
    "preliminary_approach": (
        "Map recent scholarship, separate consensus claims from open disputes, and ground "
        "the literature search in banking, credit, and institutional-response sources."
    ),
    "expected_contribution": (
        "A focused field map that can later support a source-bound novelty angle."
    ),
    "scope": (
        "The first pass covers interwar banking stress and regional credit markets without "
        "asserting a final causal thesis."
    ),
    "preliminary_keywords": [
        "interwar banking",
        "lender of last resort",
        "regional credit markets",
    ],
}


class LegacyProposalLLM:
    async def chat_completion(
        self,
        messages,  # type: ignore[no-untyped-def]
        model: str,
        temperature: float,
        max_tokens: int = 4000,
        retries: int = 2,
        response_format: dict[str, object] | None = None,
        force_no_reasoning: bool = False,
        **_kwargs: object,
    ) -> dict[str, object]:
        del messages, model, temperature, max_tokens, retries, response_format, force_no_reasoning
        content = json.dumps(PROPOSAL_PAYLOAD)
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 33}}

    async def aclose(self) -> None:
        return None


class HarnessProposalLLM:
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
        content = json.dumps(PROPOSAL_PAYLOAD)
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 34}}

    async def aclose(self) -> None:
        return None


class RetryProposalLLM:
    instances: list["RetryProposalLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        RetryProposalLLM.instances.append(self)

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
        content = json.dumps(PROPOSAL_PAYLOAD)
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 35}}

    async def aclose(self) -> None:
        return None


class InvalidProposalLLM:
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
        InvalidProposalLLM.calls += 1
        return {"content": "not-json", "raw_content": "not-json", "usage": {"total_tokens": 1}}

    async def aclose(self) -> None:
        return None


class MemoryProposalLLM:
    instances: list["MemoryProposalLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        MemoryProposalLLM.instances.append(self)

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
        content = json.dumps(PROPOSAL_PAYLOAD)
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 36}}

    async def aclose(self) -> None:
        return None


class FakeProposalMemoryClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token

    async def search(
        self,
        query: str,
        user_id: str | None,
        limit: int = 5,
        enhanced: bool = False,
    ) -> list[Memory]:
        FakeProposalMemoryClient.calls.append(
            {"query": query, "user_id": user_id, "limit": limit, "enhanced": enhanced},
        )
        return [
            Memory(
                id="memory_proposal_1",
                title="Proposal decision",
                content="Keep the proposal anchored to banking institutions.",
                labels=["proposal"],
            ),
        ]


def test_proposal_output_schema_normalizes_text_and_keywords() -> None:
    parsed = ProposalOutput.parse_obj(
        {
            **PROPOSAL_PAYLOAD,
            "research_question": "  How should banking stress be studied?  ",
            "preliminary_keywords": [" banking stress ", "Banking Stress", "credit markets"],
        },
    )

    assert parsed.research_question == "How should banking stress be studied?"
    assert parsed.preliminary_keywords == ["banking stress", "credit markets"]


def test_proposal_output_schema_rejects_empty_required_field() -> None:
    with pytest.raises(ValidationError):
        ProposalOutput.parse_obj({**PROPOSAL_PAYLOAD, "research_question": "   "})


def test_proposal_harness_writes_audited_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "0")
    harness_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_proposal_harness",
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "interwar banking stress"
        session.add_all(
            [
                Run(
                    id="run_proposal_harness",
                    project_id=project.id,
                    domain_version="0.1.0",
                    run_dir=str(harness_run_dir),
                    state="DOMAIN_LOADED",
                    baseline_hash="test",
                ),
            ],
        )
        session.commit()
        get_settings.cache_clear()
        monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessProposalLLM)
        harness_summary = run_proposal_draft("run_proposal_harness", session)

        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_proposal_harness"),
            ),
        )
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(
                    AgentInvocation.run_id == "run_proposal_harness",
                ),
            ),
        )

    proposal_payload = json.loads(
        (harness_run_dir / "proposal" / "proposal_v001.json").read_text(encoding="utf-8"),
    )

    assert harness_summary["state"] == "USER_PROPOSAL_REVIEW"
    assert proposal_payload == PROPOSAL_PAYLOAD
    assert len(provider_calls) == 1
    assert provider_calls[0].status == "accepted"
    assert len(invocations) == 1
    assert invocations[0].agent_name == "Proposal"
    assert invocations[0].status == "accepted"
    assert (harness_run_dir / "proposal" / "llm_calls.jsonl").is_file()
    assert (harness_run_dir / "proposal" / "prompts" / "proposal_draft.txt").is_file()
    assert (harness_run_dir / "proposal" / "responses" / "proposal_draft.txt").is_file()


def test_proposal_harness_retries_after_invalid_json(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    RetryProposalLLM.instances = []
    run_dir = _seed_loaded_run(app_session, tmp_path, "run_proposal_retry")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", RetryProposalLLM)

    with app_session() as session:
        summary = run_proposal_draft("run_proposal_retry", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_proposal_retry"),
            ),
        )

    jsonl = _read_jsonl(run_dir / "proposal" / "llm_calls.jsonl")
    fake = RetryProposalLLM.instances[0]

    assert summary["state"] == "USER_PROPOSAL_REVIEW"
    assert len(fake.messages) == 2
    assert any("Schema errors" in message["content"] for message in fake.messages[1])
    assert len(provider_calls) == 2
    assert jsonl[0]["status"] == "retrying"
    assert jsonl[1]["status"] == "accepted"


def test_proposal_harness_fails_fixable_after_second_invalid_json(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    InvalidProposalLLM.calls = 0
    run_dir = _seed_loaded_run(app_session, tmp_path, "run_proposal_schema_failure")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", InvalidProposalLLM)

    with app_session() as session:
        summary = run_proposal_draft("run_proposal_schema_failure", session)
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(
                    AgentInvocation.run_id == "run_proposal_schema_failure",
                ),
            ),
        )

    jsonl = _read_jsonl(run_dir / "proposal" / "llm_calls.jsonl")

    assert summary["state"] == "FAILED_FIXABLE"
    # max_corrective_retries=2 → initial + 2 retries = 3 total LLM calls.
    assert InvalidProposalLLM.calls == 3
    assert len(invocations) == 1
    assert invocations[0].status == "failed_schema_violation"
    assert jsonl[0]["status"] == "retrying"
    assert jsonl[1]["status"] == "retrying"
    assert jsonl[2]["status"] == "failed_schema_violation"


def test_proposal_harness_memory_hook_uses_bounded_query(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    MemoryProposalLLM.instances = []
    FakeProposalMemoryClient.calls = []
    run_dir = _seed_loaded_run(app_session, tmp_path, "run_proposal_memory")
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_MEMORY_READ", "1")
    monkeypatch.setenv("APPLESEED_MEMORY_URL", "https://memory.example.test")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", MemoryProposalLLM)
    monkeypatch.setattr(proposal, "MemoryClient", FakeProposalMemoryClient)

    with app_session() as session:
        run_proposal_draft("run_proposal_memory", session)

    user_prompt = MemoryProposalLLM.instances[0].messages[0][-1]["content"]
    prompt_artifact = (run_dir / "proposal" / "prompts" / "proposal_draft.txt").read_text(
        encoding="utf-8",
    )

    assert FakeProposalMemoryClient.calls == [
        {
            "query": "phase=proposal topic=interwar banking stress domain=financial_history",
            "user_id": "single-user",
            "limit": 5,
            "enhanced": False,
        },
    ]
    assert "Keep the proposal anchored to banking institutions." in user_prompt
    assert "Keep the proposal anchored to banking institutions." in prompt_artifact


def _seed_loaded_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    run_id: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )
    with app_session() as session:
        project = seed_project(session)
        project.title = "interwar banking stress"
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
