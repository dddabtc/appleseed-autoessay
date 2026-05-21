import json
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_project
from pydantic import ValidationError
from sqlalchemy import select

from autoessay.agents import synthesizer
from autoessay.agents.synthesizer import SynthesizerSourceNote, run_synthesizer
from autoessay.clients import pdf_text
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.memory import Memory
from autoessay.models import AgentInvocation, Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory


class LegacySynthesizerLLM:
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
        content = json.dumps(_source_note("source_001"))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 40}}

    async def aclose(self) -> None:
        return None


class HarnessSynthesizerLLM:
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
        content = json.dumps(_source_note("source_001"))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 41}}

    async def aclose(self) -> None:
        return None


class RetrySynthesizerLLM:
    instances: list["RetrySynthesizerLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        RetrySynthesizerLLM.instances.append(self)

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
        content = json.dumps(_source_note("source_001"))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 42}}

    async def aclose(self) -> None:
        return None


class MemorySynthesizerLLM:
    instances: list["MemorySynthesizerLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        MemorySynthesizerLLM.instances.append(self)

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
        content = json.dumps(_source_note("source_001"))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 43}}

    async def aclose(self) -> None:
        return None


class PoorExtractionSynthesizerLLM:
    instances: list["PoorExtractionSynthesizerLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        PoorExtractionSynthesizerLLM.instances.append(self)

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
        content = json.dumps(_source_note("source_002"))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 44}}

    async def aclose(self) -> None:
        return None


class FakeSynthesizerMemoryClient:
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
        FakeSynthesizerMemoryClient.calls.append(
            {"query": query, "user_id": user_id, "limit": limit, "enhanced": enhanced},
        )
        return [
            Memory(
                id="memory_synthesizer_1",
                title="Synthesizer decision",
                content="Prefer source notes with explicit method and limits fields.",
                labels=["synthesizer"],
            ),
        ]


def test_synthesizer_source_note_schema_accepts_structured_note() -> None:
    parsed = SynthesizerSourceNote.parse_obj(_source_note("source_001"))

    assert parsed.source_id == "source_001"
    assert parsed.claims[0].claim_type == "finding"


def test_synthesizer_source_note_schema_rejects_unknown_claim_type() -> None:
    payload = _source_note("source_001")
    claims = payload["claims"]
    assert isinstance(claims, list)
    first_claim = claims[0]
    assert isinstance(first_claim, dict)
    first_claim["claim_type"] = "novelty"

    with pytest.raises(ValidationError):
        SynthesizerSourceNote.parse_obj(payload)


def test_synthesizer_harness_writes_audited_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "0")
    harness_run_dir = _seed_synthesizer_run(
        app_session,
        tmp_path,
        run_id="run_synthesizer_harness",
        source_ids=["source_001"],
        with_manifest=False,
    )

    with app_session() as session:
        get_settings.cache_clear()
        monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessSynthesizerLLM)
        harness_summary = run_synthesizer("run_synthesizer_harness", session)

        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_synthesizer_harness"),
            ),
        )
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(
                    AgentInvocation.run_id == "run_synthesizer_harness",
                ),
            ),
        )

    harness_note = harness_run_dir / "synthesis" / "source_notes" / "source_001.json"
    note_payload = json.loads(harness_note.read_text(encoding="utf-8"))

    assert note_payload == _source_note("source_001")
    assert harness_summary["state"] == "USER_FIELD_REVIEW"
    assert len(provider_calls) == 1
    assert provider_calls[0].status == "accepted"
    assert len(invocations) == 1
    assert invocations[0].agent_name == "Synthesizer"
    assert invocations[0].status == "accepted"
    assert (harness_run_dir / "synthesis" / "llm_calls.jsonl").is_file()
    assert (
        harness_run_dir / "synthesis" / "prompts" / "synthesizer_source_note_source_001.txt"
    ).is_file()
    assert (
        harness_run_dir / "synthesis" / "responses" / "synthesizer_source_note_source_001.txt"
    ).is_file()


def test_synthesizer_harness_retries_after_invalid_json(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    RetrySynthesizerLLM.instances = []
    run_dir = _seed_synthesizer_run(
        app_session,
        tmp_path,
        run_id="run_synthesizer_retry",
        source_ids=["source_001"],
        with_manifest=False,
    )
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", RetrySynthesizerLLM)

    with app_session() as session:
        summary = run_synthesizer("run_synthesizer_retry", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_synthesizer_retry"),
            ),
        )

    jsonl = _read_jsonl(run_dir / "synthesis" / "llm_calls.jsonl")
    fake = RetrySynthesizerLLM.instances[0]

    assert summary["state"] == "USER_FIELD_REVIEW"
    assert len(fake.messages) == 2
    assert any("Schema errors" in message["content"] for message in fake.messages[1])
    assert len(provider_calls) == 2
    assert jsonl[0]["status"] == "retrying"
    assert jsonl[1]["status"] == "accepted"


def test_synthesizer_harness_memory_hook_uses_bounded_query(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    MemorySynthesizerLLM.instances = []
    FakeSynthesizerMemoryClient.calls = []
    run_dir = _seed_synthesizer_run(
        app_session,
        tmp_path,
        run_id="run_synthesizer_memory",
        source_ids=["source_001"],
        with_manifest=False,
    )
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_MEMORY_READ", "1")
    monkeypatch.setenv("APPLESEED_MEMORY_URL", "https://memory.example.test")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", MemorySynthesizerLLM)
    monkeypatch.setattr(synthesizer, "MemoryClient", FakeSynthesizerMemoryClient)

    with app_session() as session:
        run_synthesizer("run_synthesizer_memory", session)

    user_prompt = MemorySynthesizerLLM.instances[0].messages[0][-1]["content"]
    prompt_artifact = (
        run_dir / "synthesis" / "prompts" / "synthesizer_source_note_source_001.txt"
    ).read_text(encoding="utf-8")

    assert FakeSynthesizerMemoryClient.calls == [
        {
            "query": (
                "phase=synthesizer topic=banking crises in the Great Depression source_count=1"
            ),
            "user_id": "single-user",
            "limit": 5,
            "enhanced": False,
        },
    ]
    assert "Prefer source notes with explicit method and limits fields." in user_prompt
    assert "Prefer source notes with explicit method and limits fields." in prompt_artifact


def test_synthesizer_harness_skips_poor_extraction_before_llm_and_records_skip_count(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    PoorExtractionSynthesizerLLM.instances = []
    run_dir = _seed_synthesizer_run(
        app_session,
        tmp_path,
        run_id="run_synthesizer_poor_harness",
        source_ids=["source_001", "source_002"],
        with_manifest=True,
    )

    def fake_extract_text(pdf_bytes: bytes, source_id: str | None = None) -> str:
        del pdf_bytes
        if source_id == "source_001":
            raise pdf_text.PoorExtraction("synthetic poor extraction")
        return "usable extracted text for source_002 " * 10

    monkeypatch.setattr(pdf_text, "extract_text", fake_extract_text)
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", PoorExtractionSynthesizerLLM)

    with app_session() as session:
        summary = run_synthesizer("run_synthesizer_poor_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(
                    ProviderCall.run_id == "run_synthesizer_poor_harness",
                ),
            ),
        )
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(
                    AgentInvocation.run_id == "run_synthesizer_poor_harness",
                ),
            ),
        )

    warnings_report = (run_dir / "synthesis" / "synthesizer_report.md").read_text(
        encoding="utf-8",
    )
    fake = PoorExtractionSynthesizerLLM.instances[0]

    assert summary["state"] == "USER_FIELD_REVIEW"
    assert summary["sources_processed"] == 1
    assert len(provider_calls) == 1
    assert len(fake.messages) == 1
    assert "source_001" not in fake.messages[0][-1]["content"]
    assert "source_002" in fake.messages[0][-1]["content"]
    assert len(invocations) == 1
    assert invocations[0].metadata_json["run_metadata"]["skip_count"] == 1
    assert "source_001" in warnings_report


def _seed_synthesizer_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
    source_ids: list[str],
    with_manifest: bool,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_DEEP_DIVE_REVIEW",
        domain_id="financial_history",
    )
    _write_source_pack(run_dir, source_ids, with_manifest=with_manifest)
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
                state="USER_DEEP_DIVE_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _write_source_pack(run_dir: Path, source_ids: list[str], *, with_manifest: bool) -> None:
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    sources = [_source(source_id, index) for index, source_id in enumerate(source_ids)]
    (sources_dir / "shortlist.json").write_text(
        json.dumps([source.dict() for source in sources], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not with_manifest:
        return
    fulltext_dir = sources_dir / "fulltext"
    fulltext_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, object]] = {}
    for source in sources:
        pdf_path = fulltext_dir / f"{source.source_id}.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 synthetic")
        manifest[source.source_id] = {
            "pdf_path": str(pdf_path.relative_to(run_dir)),
            "sha256": "test",
            "size_bytes": 18,
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "license": "CC-BY",
        }
    (sources_dir / "fulltext_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _source(source_id: str, index: int) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024 - index,
        venue=f"Journal {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}",
        pdf_url=f"https://example.test/{source_id}.pdf",
        abstract=f"Abstract evidence for {source_id} on banking crises.",
        source_client="semantic_scholar",
        access_status=AccessStatus.OPEN,
        license="CC-BY",
        rank_score=1.0 - (index * 0.1),
        risk_flags=[],
    )


def _source_note(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "thesis": f"{source_id} links banking stress to institutional response.",
        "method": "Qualitative financial-history source reading.",
        "evidence": "The source uses archival and secondary evidence about banking crises.",
        "limits": "The source does not establish field-wide consensus by itself.",
        "claims": [
            {
                "claim_id": f"claim_{source_id}",
                "text": f"{source_id} reports a source-bound finding relevant to banking crises.",
                "claim_type": "finding",
                "n_sources_supporting": 1,
                "page_anchor": None,
            },
        ],
    }


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
