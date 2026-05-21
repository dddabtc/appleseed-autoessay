import json
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy.orm import Session, sessionmaker

from autoessay.agents import scout
from autoessay.agents.scout import run_scout
from autoessay.clients._stubs import StubLitClient
from autoessay.config import get_settings
from autoessay.memory import Memory
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


class HarnessMemoryLLM:
    messages: list[dict[str, str]] = []

    async def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        del model, temperature, max_tokens, retries, response_format
        self.__class__.messages = [dict(message) for message in messages]
        content = json.dumps(
            {
                "queries": [
                    "banking crises Great Depression credit markets",
                    "financial history bank failures monetary policy",
                    "Great Depression banking panics institutional response",
                ],
                "rationale": "domain coverage",
            },
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 22}}

    async def aclose(self) -> None:
        return None


class FakeScoutMemoryClient:
    calls: list[dict[str, object]] = []

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        del base_url, token, timeout

    async def search(
        self,
        query: str,
        user_id: str | None,
        limit: int = 5,
        enhanced: bool = False,
    ) -> list[Memory]:
        self.__class__.calls.append(
            {
                "query": query,
                "user_id": user_id,
                "limit": limit,
                "enhanced": enhanced,
            },
        )
        return [
            Memory(
                id="memory_1",
                title="Accepted source policy",
                content="Weight curated sources before deduplication.",
                labels=["autoessay", "decision"],
                metadata={},
                created_at=None,
            ),
            Memory(
                id="memory_2",
                title="Accepted scope decision",
                content="Keep Scout query expansion domain-bound.",
                labels=["autoessay", "decision"],
                metadata={},
                created_at=None,
            ),
        ]


def test_scout_harness_includes_memory_block_in_llm_prompt(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    HarnessMemoryLLM.messages = []
    FakeScoutMemoryClient.calls = []
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_MEMORY_READ", "1")
    monkeypatch.setenv("APPLESEED_MEMORY_URL", "https://memory.example.test")
    monkeypatch.setattr(scout, "get_lit_client", _stub_client)
    monkeypatch.setattr(scout, "MemoryClient", FakeScoutMemoryClient)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessMemoryLLM)
    get_settings.cache_clear()

    run_dir = create_run_directory(
        tmp_path / "runs",
        "run_scout_memory",
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )
    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(
            Run(
                id="run_scout_memory",
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
            ),
        )
        session.commit()

        summary = run_scout("run_scout_memory", session)

    assert summary["state"] == "USER_SEARCH_REVIEW"
    assert FakeScoutMemoryClient.calls == [
        {
            "query": (
                "phase=scout topic=banking crises in the Great Depression domain=financial_history"
            ),
            "user_id": "single-user",
            "limit": 5,
            "enhanced": False,
        },
    ]
    user_messages = [
        message["content"] for message in HarnessMemoryLLM.messages if message["role"] == "user"
    ]
    assert len(user_messages) == 1
    assert "Previous related decisions" in user_messages[0]
    assert "1. Accepted source policy" in user_messages[0]
    assert "2. Accepted scope decision" in user_messages[0]
    assert "Weight curated sources before deduplication." in user_messages[0]
    assert "Keep Scout query expansion domain-bound." in user_messages[0]


def _stub_client(
    source_id: str,
    source_config: object = None,
    domain_config: object = None,
) -> StubLitClient:
    del source_config, domain_config
    return StubLitClient(source_id)
