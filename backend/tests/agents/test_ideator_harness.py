import json
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_project
from pydantic import ValidationError
from sqlalchemy import select

from autoessay.agents import ideator
from autoessay.agents.ideator import MIN_ANGLE_CARDS, IdeatorOutput, run_ideator
from autoessay.config import get_settings
from autoessay.memory import Memory
from autoessay.models import AgentInvocation, ProviderCall, Run
from autoessay.run_writer import create_run_directory


class LegacyIdeatorLLM:
    async def chat_completion(
        self,
        messages,  # type: ignore[no-untyped-def]
        model: str,
        temperature: float,
        max_tokens: int = 4000,
        retries: int = 2,
        response_format: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        del messages, model, temperature, max_tokens, retries, response_format
        content = json.dumps({"angle_cards": _angle_cards()})
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 44}}

    async def aclose(self) -> None:
        return None


class HarnessIdeatorLLM:
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
        content = json.dumps({"angle_cards": _angle_cards()})
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 45}}

    async def aclose(self) -> None:
        return None


class RetryIdeatorLLM:
    instances: list["RetryIdeatorLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        RetryIdeatorLLM.instances.append(self)

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
        content = json.dumps({"angle_cards": _angle_cards()})
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 46}}

    async def aclose(self) -> None:
        return None


class InvalidIdeatorLLM:
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
        InvalidIdeatorLLM.calls += 1
        return {"content": "not-json", "raw_content": "not-json", "usage": {"total_tokens": 1}}

    async def aclose(self) -> None:
        return None


class MemoryIdeatorLLM:
    instances: list["MemoryIdeatorLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        MemoryIdeatorLLM.instances.append(self)

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
        content = json.dumps({"angle_cards": _angle_cards()})
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 47}}

    async def aclose(self) -> None:
        return None


class FakeIdeatorMemoryClient:
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
        FakeIdeatorMemoryClient.calls.append(
            {"query": query, "user_id": user_id, "limit": limit, "enhanced": enhanced},
        )
        return [
            Memory(
                id="memory_ideator_1",
                title="Ideator decision",
                content="Prefer angles with explicit claim IDs and evidence gaps.",
                labels=["ideator"],
            ),
        ]


def test_ideator_output_schema_rejects_too_few_cards() -> None:
    with pytest.raises(ValidationError):
        IdeatorOutput.parse_obj({"angle_cards": [_angle_cards()[0]]})


def test_ideator_output_schema_normalizes_duplicate_list_values() -> None:
    cards = _angle_cards()
    cards[0]["key_claim_ids"] = ["claim_001", " claim_001 ", "claim_002"]
    cards[0]["risks"] = [" thin evidence ", "thin evidence", "scope drift"]

    parsed = IdeatorOutput.parse_obj({"angle_cards": cards})

    assert parsed.angle_cards[0].key_claim_ids == ["claim_001", "claim_002"]
    assert parsed.angle_cards[0].risks == ["thin evidence", "scope drift"]


def test_ideator_harness_writes_audited_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "0")
    harness_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_ideator_harness",
        "proj_test",
        state="USER_FIELD_REVIEW",
        domain_id="financial_history",
    )
    _write_synthesis_inputs(harness_run_dir)

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add_all(
            [
                Run(
                    id="run_ideator_harness",
                    project_id=project.id,
                    domain_version="0.1.0",
                    run_dir=str(harness_run_dir),
                    state="USER_FIELD_REVIEW",
                    baseline_hash="test",
                ),
            ],
        )
        session.commit()
        get_settings.cache_clear()
        monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessIdeatorLLM)
        harness_summary = run_ideator("run_ideator_harness", session)

        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_ideator_harness"),
            ),
        )
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(AgentInvocation.run_id == "run_ideator_harness"),
            ),
        )

    payload = json.loads(
        (harness_run_dir / "novelty" / "angle_cards.json").read_text(encoding="utf-8"),
    )

    IdeatorOutput.parse_obj(payload)
    assert harness_summary["state"] == "USER_NOVELTY_REVIEW"
    assert len(provider_calls) == 1
    assert provider_calls[0].status == "accepted"
    assert len(invocations) == 1
    assert invocations[0].agent_name == "Ideator"
    assert invocations[0].status == "accepted"
    assert (harness_run_dir / "novelty" / "llm_calls.jsonl").is_file()
    assert (harness_run_dir / "novelty" / "prompts" / "ideator_angle_cards.txt").is_file()
    assert (harness_run_dir / "novelty" / "responses" / "ideator_angle_cards.txt").is_file()


def test_ideator_harness_retries_after_invalid_json(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    RetryIdeatorLLM.instances = []
    run_dir = _seed_field_review_run(app_session, tmp_path, "run_ideator_retry")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", RetryIdeatorLLM)

    with app_session() as session:
        summary = run_ideator("run_ideator_retry", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_ideator_retry"),
            ),
        )

    jsonl = _read_jsonl(run_dir / "novelty" / "llm_calls.jsonl")
    fake = RetryIdeatorLLM.instances[0]

    assert summary["state"] == "USER_NOVELTY_REVIEW"
    assert len(fake.messages) == 2
    assert any("Schema errors" in message["content"] for message in fake.messages[1])
    assert len(provider_calls) == 2
    assert jsonl[0]["status"] == "retrying"
    assert jsonl[1]["status"] == "accepted"


def test_ideator_harness_fails_fixable_after_second_invalid_json(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    InvalidIdeatorLLM.calls = 0
    run_dir = _seed_field_review_run(app_session, tmp_path, "run_ideator_schema_failure")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", InvalidIdeatorLLM)

    with app_session() as session:
        summary = run_ideator("run_ideator_schema_failure", session)
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(
                    AgentInvocation.run_id == "run_ideator_schema_failure",
                ),
            ),
        )

    jsonl = _read_jsonl(run_dir / "novelty" / "llm_calls.jsonl")
    empty_payload = json.loads((run_dir / "novelty" / "angle_cards.json").read_text())

    assert summary["state"] == "FAILED_FIXABLE"
    # max_corrective_retries=2 → initial + 2 retries = 3 total LLM calls.
    assert InvalidIdeatorLLM.calls == 3
    assert len(invocations) == 1
    assert invocations[0].status == "failed_schema_violation"
    assert jsonl[0]["status"] == "retrying"
    assert jsonl[1]["status"] == "retrying"
    assert jsonl[2]["status"] == "failed_schema_violation"
    assert empty_payload == {"angle_cards": []}


def test_ideator_harness_memory_hook_uses_bounded_query(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    MemoryIdeatorLLM.instances = []
    FakeIdeatorMemoryClient.calls = []
    run_dir = _seed_field_review_run(app_session, tmp_path, "run_ideator_memory")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_MEMORY_READ", "1")
    monkeypatch.setenv("APPLESEED_MEMORY_URL", "https://memory.example.test")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", MemoryIdeatorLLM)
    monkeypatch.setattr(ideator, "MemoryClient", FakeIdeatorMemoryClient)

    with app_session() as session:
        run_ideator("run_ideator_memory", session)

    user_prompt = MemoryIdeatorLLM.instances[0].messages[0][-1]["content"]
    prompt_artifact = (run_dir / "novelty" / "prompts" / "ideator_angle_cards.txt").read_text(
        encoding="utf-8"
    )

    assert FakeIdeatorMemoryClient.calls == [
        {
            "query": (
                "phase=ideator topic=banking crises in the Great Depression "
                f"angle_count={MIN_ANGLE_CARDS} domain=financial_history"
            ),
            "user_id": "single-user",
            "limit": 5,
            "enhanced": False,
        },
    ]
    assert "Prefer angles with explicit claim IDs and evidence gaps." in user_prompt
    assert "Prefer angles with explicit claim IDs and evidence gaps." in prompt_artifact


def _seed_field_review_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    run_id: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_FIELD_REVIEW",
        domain_id="financial_history",
    )
    _write_synthesis_inputs(run_dir)
    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_FIELD_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _write_synthesis_inputs(run_dir: Path) -> None:
    synthesis_dir = run_dir / "synthesis"
    source_notes_dir = synthesis_dir / "source_notes"
    source_notes_dir.mkdir(parents=True, exist_ok=True)
    (synthesis_dir / "claims.jsonl").write_text(
        json.dumps(
            {
                "source_id": "source_001",
                "claim_id": "claim_001",
                "text": "Credit shocks shaped local banking outcomes.",
                "claim_type": "finding",
                "n_sources_supporting": 1,
                "page_anchor": None,
            },
        )
        + "\n",
        encoding="utf-8",
    )
    (source_notes_dir / "source_001.json").write_text(
        json.dumps(
            {
                "source_id": "source_001",
                "thesis": "A source-bound thesis.",
                "evidence": "A source-bound evidence note.",
            },
        ),
        encoding="utf-8",
    )


def _angle_cards() -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for index in range(1, 5):
        cards.append(
            {
                "angle_id": f"angle_{index:03d}",
                "working_title": f"Angle {index}",
                "thesis_one_sentence": f"Thesis {index}.",
                "key_claim_ids": ["claim_001"],
                "why_novel": "Novel because it reframes the source pack.",
                "evidence_so_far": "One claim supports the angle.",
                "missing_evidence": "More archival evidence.",
                "journal_fit_note": "Fits the target journal if tightened.",
                "risks": ["thin evidence"],
            },
        )
    return cards


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
