import json
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_project
from pydantic import ValidationError
from sqlalchemy import select

from autoessay.agents import curator
from autoessay.agents.curator import CuratorRanking, run_curator
from autoessay.clients.common import AccessStatus, NormalizedSource, VerificationStatus
from autoessay.config import get_settings
from autoessay.memory import Memory
from autoessay.models import AgentInvocation, Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory

SOURCE_IDS = ["source_001", "source_002"]
RELEVANCE = {"source_001": 0.92, "source_002": 0.51}


class LegacyCuratorLLM:
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
        content = json.dumps(
            {
                "scores": [
                    {"source_id": source_id, "relevance_score": RELEVANCE[source_id]}
                    for source_id in SOURCE_IDS
                ],
            },
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 30}}

    async def aclose(self) -> None:
        return None


class HarnessCuratorLLM:
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
        content = json.dumps(_ranking_payload(SOURCE_IDS))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 31}}

    async def aclose(self) -> None:
        return None


class RetryCuratorLLM:
    instances: list["RetryCuratorLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        RetryCuratorLLM.instances.append(self)

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
            return {"content": "not-json", "raw_content": "not-json", "usage": {"total_tokens": 1}}
        content = json.dumps(_ranking_payload(["source_001"]))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 32}}

    async def aclose(self) -> None:
        return None


class MemoryCuratorLLM:
    instances: list["MemoryCuratorLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        MemoryCuratorLLM.instances.append(self)

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
        content = json.dumps(_ranking_payload(["source_001"]))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 33}}

    async def aclose(self) -> None:
        return None


class FakeCuratorMemoryClient:
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
        FakeCuratorMemoryClient.calls.append(
            {"query": query, "user_id": user_id, "limit": limit, "enhanced": enhanced},
        )
        return [
            Memory(
                id="memory_curator_1",
                title="Curator decision",
                content="Retain sources with explicit lender-of-last-resort evidence.",
                labels=["curator"],
            ),
        ]


def test_curator_ranking_schema_supports_root_list_shape() -> None:
    parsed = CuratorRanking.parse_raw(json.dumps(_ranking_payload(["source_001"])))

    assert parsed.__root__[0].source_id == "source_001"
    assert parsed.__root__[0].retain_decision is True


def test_curator_ranking_schema_rejects_out_of_range_scores() -> None:
    payload = _ranking_payload(["source_001"])
    payload[0]["relevance"] = 1.5

    with pytest.raises(ValidationError):
        CuratorRanking.parse_obj(payload)


def test_curator_harness_writes_audited_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_CURATOR_RERANK_STUB", "0")
    harness_run_dir = _seed_curator_run(
        app_session,
        tmp_path,
        run_id="run_curator_harness",
        source_ids=SOURCE_IDS,
    )

    with app_session() as session:
        get_settings.cache_clear()
        monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessCuratorLLM)
        harness_summary = run_curator("run_curator_harness", session)

        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_curator_harness"),
            ),
        )
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(AgentInvocation.run_id == "run_curator_harness"),
            ),
        )

    shortlist = json.loads(
        (harness_run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"),
    )

    assert [source["source_id"] for source in shortlist] == SOURCE_IDS
    assert harness_summary["state"] == "USER_DEEP_DIVE_REVIEW"
    assert len(provider_calls) == 1
    assert provider_calls[0].status == "accepted"
    assert len(invocations) == 1
    assert invocations[0].agent_name == "Curator"
    assert invocations[0].status == "accepted"
    assert (harness_run_dir / "sources" / "llm_calls.jsonl").is_file()
    assert (harness_run_dir / "sources" / "prompts" / "curator_ranking_batch_001.txt").is_file()
    assert (harness_run_dir / "sources" / "responses" / "curator_ranking_batch_001.txt").is_file()


def test_curator_harness_retries_once_per_batch_after_invalid_json(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    RetryCuratorLLM.instances = []
    run_dir = _seed_curator_run(
        app_session,
        tmp_path,
        run_id="run_curator_retry",
        source_ids=["source_001"],
    )
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_CURATOR_RERANK_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", RetryCuratorLLM)

    with app_session() as session:
        summary = run_curator("run_curator_retry", session)
        provider_calls = list(
            session.scalars(select(ProviderCall).where(ProviderCall.run_id == "run_curator_retry")),
        )

    jsonl = _read_jsonl(run_dir / "sources" / "llm_calls.jsonl")
    fake = RetryCuratorLLM.instances[0]

    assert summary["state"] == "USER_DEEP_DIVE_REVIEW"
    assert len(fake.messages) == 2
    assert any("Schema errors" in message["content"] for message in fake.messages[1])
    assert len(provider_calls) == 2
    assert jsonl[0]["status"] == "retrying"
    assert jsonl[1]["status"] == "accepted"


def test_curator_harness_memory_hook_uses_bounded_query(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    MemoryCuratorLLM.instances = []
    FakeCuratorMemoryClient.calls = []
    run_dir = _seed_curator_run(
        app_session,
        tmp_path,
        run_id="run_curator_memory",
        source_ids=["source_001"],
    )
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_CURATOR_RERANK_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_MEMORY_READ", "1")
    monkeypatch.setenv("APPLESEED_MEMORY_URL", "https://memory.example.test")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", MemoryCuratorLLM)
    monkeypatch.setattr(curator, "MemoryClient", FakeCuratorMemoryClient)

    with app_session() as session:
        run_curator("run_curator_memory", session)

    user_prompt = MemoryCuratorLLM.instances[0].messages[0][-1]["content"]
    prompt_artifact = (run_dir / "sources" / "prompts" / "curator_ranking_batch_001.txt").read_text(
        encoding="utf-8",
    )

    assert FakeCuratorMemoryClient.calls == [
        {
            "query": (
                "phase=curator topic=banking crises in the Great Depression "
                "domain=financial_history candidate_count=1"
            ),
            "user_id": "single-user",
            "limit": 5,
            "enhanced": False,
        },
    ]
    assert "Retain sources with explicit lender-of-last-resort evidence." in user_prompt
    assert "Retain sources with explicit lender-of-last-resort evidence." in prompt_artifact


def _seed_curator_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
    source_ids: list[str],
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_SEARCH_REVIEW",
        domain_id="financial_history",
    )
    discovery_dir = run_dir / "discovery"
    discovery_dir.mkdir(parents=True, exist_ok=True)
    _write_sources_jsonl(
        discovery_dir / "skim_candidates.jsonl",
        [_source(source_id, index) for index, source_id in enumerate(source_ids)],
    )
    with app_session() as session:
        project = session.get(Project, "proj_test")
        if project is None:
            project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add(project)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_SEARCH_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _source(source_id: str, index: int) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024 - index,
        venue=f"Journal {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=None,
        abstract="A source about banking crises and lender-of-last-resort practice.",
        source_client="semantic_scholar",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=0.0,
        risk_flags=[],
        verified_by="crossref",
        verification_status=VerificationStatus.VERIFIED,
        confidence=0.7,
    )


def _ranking_payload(source_ids: list[str]) -> list[dict[str, object]]:
    return [
        {
            "source_id": source_id,
            "rank_score": RELEVANCE.get(source_id, 0.72),
            "relevance": RELEVANCE.get(source_id, 0.72),
            "recency": 0.8,
            "venue_authority": 0.6,
            "diversity_bonus": 0.5,
            "retain_decision": True,
            "risk_flags": [],
        }
        for source_id in source_ids
    ]


def _write_sources_jsonl(path: Path, sources: list[NormalizedSource]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for source in sources:
            handle.write(source.json(sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
