import json
import re
from pathlib import Path
from typing import Any

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents import drafter
from autoessay.agents.drafter import DEFAULT_SECTION_TITLES, run_drafter
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.memory import Memory
from autoessay.models import AgentInvocation, Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory


class LegacyDrafterLLM:
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
        del model, temperature, max_tokens, retries, response_format
        section = _section_from_messages(messages)
        content = json.dumps(_section_payload(section, ["source_001"]))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 50}}

    async def aclose(self) -> None:
        return None


class HarnessDrafterLLM:
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
        section = _section_from_messages(messages)
        content = json.dumps(_section_payload(section, ["source_001"]))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 51}}

    async def aclose(self) -> None:
        return None


class WhitelistRetryDrafterLLM:
    instances: list["WhitelistRetryDrafterLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        WhitelistRetryDrafterLLM.instances.append(self)

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
        section = _section_from_messages(messages)
        source_ids = ["fabricated_source"] if len(self.messages) == 1 else ["source_001"]
        content = json.dumps(_section_payload(section, source_ids))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 52}}

    async def aclose(self) -> None:
        return None


class MemoryDrafterLLM:
    instances: list["MemoryDrafterLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        MemoryDrafterLLM.instances.append(self)

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
        section = _section_from_messages(messages)
        content = json.dumps(_section_payload(section, ["source_001"]))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 53}}

    async def aclose(self) -> None:
        return None


class FakeDrafterMemoryClient:
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
        FakeDrafterMemoryClient.calls.append(
            {"query": query, "user_id": user_id, "limit": limit, "enhanced": enhanced},
        )
        return [
            Memory(
                id="memory_drafter_1",
                title="Drafter decision",
                content="Keep section claims tied to the shortlist source identifiers.",
                labels=["drafter"],
            ),
        ]


def test_drafter_harness_writes_audited_section_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "0")
    harness_run_dir = _seed_drafter_run(app_session, tmp_path, run_id="run_drafter_harness")

    with app_session() as session:
        get_settings.cache_clear()
        monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessDrafterLLM)
        harness_summary = run_drafter("run_drafter_harness", session)

        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_drafter_harness"),
            ),
        )
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(AgentInvocation.run_id == "run_drafter_harness"),
            ),
        )

    harness_draft_dir = harness_run_dir / "drafts" / "v001"
    for name in ("manuscript.md", "claim_map.jsonl", "citations.bib", "draft_rationale.md"):
        assert (harness_draft_dir / name).is_file()

    claim_rows = _read_jsonl(harness_draft_dir / "claim_map.jsonl")
    jsonl = _read_jsonl(harness_run_dir / "drafter" / "llm_calls.jsonl")

    assert harness_summary["state"] == "DRAFTER_RUNNING"
    assert len(claim_rows) == len(DEFAULT_SECTION_TITLES)
    assert len(provider_calls) == len(DEFAULT_SECTION_TITLES)
    assert all(call.status == "accepted" for call in provider_calls)
    assert len(invocations) == 1
    assert invocations[0].agent_name == "Drafter"
    assert invocations[0].status == "accepted"
    assert len(jsonl) == len(DEFAULT_SECTION_TITLES)
    assert {row["agent_invocation_id"] for row in jsonl} == {invocations[0].id}


def test_drafter_harness_retries_fabricated_source_id(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    WhitelistRetryDrafterLLM.instances = []
    run_dir = _seed_drafter_run(app_session, tmp_path, run_id="run_drafter_whitelist_retry")
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", WhitelistRetryDrafterLLM)

    with app_session() as session:
        summary = run_drafter("run_drafter_whitelist_retry", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_drafter_whitelist_retry"),
            ),
        )

    jsonl = _read_jsonl(run_dir / "drafter" / "llm_calls.jsonl")
    second_attempt_prompt = WhitelistRetryDrafterLLM.instances[0].messages[1]

    assert summary["state"] == "DRAFTER_RUNNING"
    assert len(provider_calls) == len(DEFAULT_SECTION_TITLES) * 2
    assert [row["status"] for row in jsonl].count("retrying") == len(DEFAULT_SECTION_TITLES)
    assert [row["status"] for row in jsonl].count("accepted") == len(DEFAULT_SECTION_TITLES)
    assert any("not in shortlist" in error for error in jsonl[0]["validation_errors"])
    assert any("not in shortlist" in message["content"] for message in second_attempt_prompt)


def test_drafter_harness_memory_hook_uses_per_section_query(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    MemoryDrafterLLM.instances = []
    FakeDrafterMemoryClient.calls = []
    run_dir = _seed_drafter_run(app_session, tmp_path, run_id="run_drafter_memory")
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_MEMORY_READ", "1")
    monkeypatch.setenv("APPLESEED_MEMORY_URL", "https://memory.example.test")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", MemoryDrafterLLM)
    monkeypatch.setattr(drafter, "MemoryClient", FakeDrafterMemoryClient)

    with app_session() as session:
        run_drafter("run_drafter_memory", session)

    first_prompt = MemoryDrafterLLM.instances[0].messages[0][-1]["content"]
    prompt_artifact = (
        run_dir / "drafter" / "prompts" / "drafter_section_introduction.txt"
    ).read_text(encoding="utf-8")

    assert len(FakeDrafterMemoryClient.calls) == len(DEFAULT_SECTION_TITLES)
    assert FakeDrafterMemoryClient.calls[0] == {
        "query": (
            "phase=drafter section_id=introduction "
            "topic=banking crises in the Great Depression "
            "thesis_one_sentence=Banking crisis thesis."
        ),
        "user_id": "single-user",
        "limit": 5,
        "enhanced": False,
    }
    assert "section_id=conclusion" in FakeDrafterMemoryClient.calls[-1]["query"]
    assert "Keep section claims tied to the shortlist source identifiers." in first_prompt
    assert "Keep section claims tied to the shortlist source identifiers." in prompt_artifact


def _seed_drafter_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_NOVELTY_REVIEW",
        domain_id="financial_history",
    )
    _write_drafter_inputs(run_dir)
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
                state="USER_NOVELTY_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _write_drafter_inputs(run_dir: Path) -> None:
    sources_dir = run_dir / "sources"
    notes_dir = run_dir / "synthesis" / "source_notes"
    novelty_dir = run_dir / "novelty"
    sources_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    novelty_dir.mkdir(parents=True, exist_ok=True)
    source = _source("source_001")
    (sources_dir / "shortlist.json").write_text(
        json.dumps([source.dict()], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (notes_dir / "source_001.json").write_text(
        json.dumps(
            {
                "source_id": "source_001",
                "thesis": "The source links banking stress to institutional response.",
                "evidence": "Archival and secondary evidence support the section.",
                "method": "Financial-history source reading.",
                "limits": "The source does not settle every causal question.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (novelty_dir / "selected_thesis.json").write_text(
        json.dumps(
            {
                "angle_id": "angle_001",
                "working_title": "Banking crisis angle",
                "thesis_one_sentence": "Banking crisis thesis.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _source(source_id: str) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024,
        venue=f"Journal {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=None,
        abstract="Abstract evidence for banking crises.",
        source_client="semantic_scholar",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )


def _section_payload(section: dict[str, str], source_ids: list[str] | str) -> dict[str, object]:
    return {
        "section_id": section["section_id"],
        "section_title": section["section_title"],
        "prose": (
            f"{section['section_title']} develops the banking crisis thesis with shortlist "
            "evidence."
        ),
        "claim_map": [
            {
                "paragraph_id": f"{section['section_id']}-p001",
                "claim_text": f"{section['section_title']} uses shortlist evidence.",
                "source_ids": source_ids,
            },
        ],
    }


def _section_from_prompt(prompt: str) -> dict[str, str]:
    match = re.search(r"Outline: (\{.*?\})\. Approved sources:", prompt)
    assert match is not None
    decoded = json.loads(match.group(1))
    assert isinstance(decoded, dict)
    return {
        "section_id": str(decoded["section_id"]),
        "section_title": str(decoded["section_title"]),
    }


def _section_from_messages(messages: list[dict[str, str]]) -> dict[str, str]:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _section_from_prompt(message["content"])
    raise AssertionError("missing user prompt")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
