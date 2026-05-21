import json
from pathlib import Path
from typing import Any

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents import critic
from autoessay.agents.critic import run_critic
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.memory import Memory
from autoessay.models import AgentInvocation, Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory


class HarnessCriticLLM:
    instances: list["HarnessCriticLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        HarnessCriticLLM.instances.append(self)

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
        content = json.dumps(
            {
                "issues": [
                    {
                        "issue_id": "critic_low_001",
                        "severity": "LOW",
                        "dimension": "prose",
                        "paragraph_id": "introduction-p001",
                        "source_ids": ["source_1"],
                        "description": "A transition can be made more direct.",
                        "suggested_action": "REWRITE",
                    },
                ],
            },
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 31}}

    async def aclose(self) -> None:
        return None


class EmptyCriticLLM:
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
        content = json.dumps({"issues": []})
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 20}}

    async def aclose(self) -> None:
        return None


class MemoryCriticLLM(HarnessCriticLLM):
    instances: list["MemoryCriticLLM"] = []

    def __init__(self) -> None:
        self.messages = []
        MemoryCriticLLM.instances.append(self)


class FakeCriticMemoryClient:
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
        FakeCriticMemoryClient.calls.append(
            {"query": query, "user_id": user_id, "limit": limit, "enhanced": enhanced},
        )
        return [
            Memory(
                id="memory_critic_1",
                title="Critic decision",
                content="Prioritize evidence blockers before prose nits.",
                labels=["critic"],
            ),
        ]


def test_critic_harness_writes_review_artifacts_and_provider_call(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = _seed_critic_run(app_session, tmp_path, run_id="run_critic_harness")
    _enable_harness(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessCriticLLM)

    with app_session() as session:
        summary = run_critic("run_critic_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_critic_harness")
            ),
        )
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(AgentInvocation.run_id == "run_critic_harness"),
            ),
        )

    reviews_dir = run_dir / "reviews"
    audit_rows = _read_jsonl(reviews_dir / "claim_audit.jsonl")

    assert summary["state"] == "USER_EXTERNAL_SCAN_APPROVAL"
    assert len(provider_calls) == 1
    assert provider_calls[0].status == "accepted"
    assert len(invocations) == 1
    assert invocations[0].agent_name == "Critic"
    assert (reviews_dir / "critic_v001.md").exists()
    assert (reviews_dir / "revision_plan.md").exists()
    assert audit_rows[0]["status"] == "PASS"
    assert (run_dir / "critic" / "llm_calls.jsonl").is_file()


def test_critic_harness_citation_audit_hook_injects_blocker(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = _seed_critic_run(
        app_session,
        tmp_path,
        run_id="run_critic_harness_audit",
        source_has_reference=False,
    )
    _enable_harness(monkeypatch)
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", EmptyCriticLLM)

    with app_session() as session:
        summary = run_critic("run_critic_harness_audit", session)

    reviews_dir = run_dir / "reviews"
    audit_rows = _read_jsonl(reviews_dir / "claim_audit.jsonl")
    blocking = json.loads((reviews_dir / "blocking_issues.json").read_text(encoding="utf-8"))

    assert summary["blocking_issues"] == 1
    assert audit_rows[0]["status"] == "BLOCKER"
    assert blocking["issues"][0]["severity"] == "BLOCKER"
    assert blocking["issues"][0]["suggested_action"] == "VERIFY_CITATION"


def test_critic_harness_memory_hook_uses_run_query(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    MemoryCriticLLM.instances = []
    FakeCriticMemoryClient.calls = []
    run_dir = _seed_critic_run(app_session, tmp_path, run_id="run_critic_memory")
    _enable_harness(monkeypatch)
    monkeypatch.setenv("AUTOESSAY_MEMORY_READ", "1")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", MemoryCriticLLM)
    monkeypatch.setattr(critic, "MemoryClient", FakeCriticMemoryClient)

    with app_session() as session:
        run_critic("run_critic_memory", session)

    prompt = MemoryCriticLLM.instances[0].messages[0][-1]["content"]
    prompt_artifact = (run_dir / "critic" / "prompts" / "critic_report_v001.txt").read_text(
        encoding="utf-8",
    )

    assert FakeCriticMemoryClient.calls == [
        {
            "query": (
                "phase=critic topic=banking crises in the Great Depression "
                "draft_version=v001 claim_count=1"
            ),
            "user_id": "single-user",
            "limit": 5,
            "enhanced": False,
        },
    ]
    assert "Prioritize evidence blockers before prose nits." in prompt
    assert "Prioritize evidence blockers before prose nits." in prompt_artifact


def _enable_harness(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "0")
    get_settings.cache_clear()


def _seed_critic_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
    source_has_reference: bool = True,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_REVISION_REVIEW",
        domain_id="financial_history",
    )
    draft_dir = run_dir / "drafts" / "v001"
    style_dir = draft_dir / "style"
    sources_dir = run_dir / "sources"
    notes_dir = run_dir / "synthesis" / "source_notes"
    novelty_dir = run_dir / "novelty"
    style_dir.mkdir(parents=True)
    sources_dir.mkdir(parents=True)
    notes_dir.mkdir(parents=True)
    novelty_dir.mkdir(parents=True)
    (style_dir / "paper_styled.md").write_text(
        '<a id="introduction"></a>\n## Introduction\n\nThe styled claim cites `source_1`.\n',
        encoding="utf-8",
    )
    (draft_dir / "claim_map.jsonl").write_text(
        json.dumps(
            {
                "draft_version": "v001",
                "section_id": "introduction",
                "section_title": "Introduction",
                "claim_id": "claim_1",
                "paragraph_id": "introduction-p001",
                "claim_text": "The styled claim needs source verification.",
                "source_ids": ["source_1"],
                "uncited": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    source = _source("source_1", source_has_reference=source_has_reference)
    (sources_dir / "shortlist.json").write_text(
        json.dumps([source.dict()], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "synthesis" / "claims.jsonl").write_text(
        json.dumps({"claim_id": "claim_1", "source_ids": ["source_1"]}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (notes_dir / "source_1.json").write_text(
        json.dumps({"source_id": "source_1", "evidence": "Evidence note."}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (novelty_dir / "selected_thesis.json").write_text(
        json.dumps({"thesis_one_sentence": "Banking crisis thesis."}, sort_keys=True) + "\n",
        encoding="utf-8",
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
                state="USER_REVISION_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _source(source_id: str, *, source_has_reference: bool) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=f"Paper {source_id}",
        authors=[f"Author {source_id}"],
        year=2024,
        venue=f"Journal {source_id}",
        doi=None,
        url=f"https://example.test/{source_id}" if source_has_reference else None,
        pdf_url=None,
        abstract="Abstract evidence for banking crises.",
        source_client="semantic_scholar" if source_has_reference else "crossref",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
