import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents import stylist
from autoessay.agents.stylist import run_stylist
from autoessay.config import get_settings
from autoessay.memory import Memory
from autoessay.models import AgentInvocation, Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory


class HarnessStylistLLM:
    instances: list["HarnessStylistLLM"] = []

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []
        HarnessStylistLLM.instances.append(self)

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
        user_prompt = messages[-1]["content"]
        content = json.dumps(_stylist_payload(user_prompt))
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 41}}

    async def aclose(self) -> None:
        return None


class MemoryStylistLLM(HarnessStylistLLM):
    instances: list["MemoryStylistLLM"] = []

    def __init__(self) -> None:
        self.messages = []
        MemoryStylistLLM.instances.append(self)


class FakeStylistMemoryClient:
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
        FakeStylistMemoryClient.calls.append(
            {"query": query, "user_id": user_id, "limit": limit, "enhanced": enhanced},
        )
        return [
            Memory(
                id="memory_stylist_1",
                title="Stylist decision",
                content="Keep prose revisions citation-preserving and plain.",
                labels=["stylist"],
            ),
        ]


def test_stylist_harness_writes_per_section_provider_calls(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    HarnessStylistLLM.instances = []
    run_dir = _seed_stylist_run(app_session, tmp_path, run_id="run_stylist_harness")
    monkeypatch.setenv("AUTOESSAY_STYLIST_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessStylistLLM)

    with app_session() as session:
        summary = run_stylist("run_stylist_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_stylist_harness")
            ),
        )
        invocations = list(
            session.scalars(
                select(AgentInvocation).where(AgentInvocation.run_id == "run_stylist_harness"),
            ),
        )

    style_dir = run_dir / "drafts" / "v001" / "style"
    jsonl = _read_jsonl(run_dir / "stylist" / "llm_calls.jsonl")
    styled = (style_dir / "paper_styled.md").read_text(encoding="utf-8")

    assert summary["state"] == "USER_REVISION_REVIEW"
    assert len(provider_calls) == 2
    assert all(call.status == "accepted" for call in provider_calls)
    assert len(invocations) == 1
    assert invocations[0].agent_name == "Stylist"
    assert len(jsonl) == 2
    assert {row["agent_invocation_id"] for row in jsonl} == {invocations[0].id}
    assert "source_1" in styled
    assert "source_2" in styled


def test_stylist_harness_triggers_one_repolish_from_stop_slop_hook_score(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = _seed_stylist_run(app_session, tmp_path, run_id="run_stylist_repolish")
    monkeypatch.setenv("AUTOESSAY_STYLIST_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessStylistLLM)

    def fake_score(
        text: str,
        _phrases: set[str],
        _structures: Sequence[object],
    ) -> dict[str, object]:
        total = 44 if "Polished full manuscript" in text else 20
        return {
            "dimensions": {
                "directness": total // 5,
                "rhythm": total // 5,
                "trust": total // 5,
                "authenticity": total // 5,
                "density": total - 4 * (total // 5),
            },
            "total": total,
            "findings": [],
        }

    monkeypatch.setattr(stylist, "score_text", fake_score)

    with app_session() as session:
        run_stylist("run_stylist_repolish", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_stylist_repolish")
            ),
        )

    score = json.loads((run_dir / "drafts" / "v001" / "style" / "stop_slop_score.json").read_text())
    delta = (run_dir / "drafts" / "v001" / "style" / "style_delta.md").read_text(encoding="utf-8")

    assert len(provider_calls) == 3
    assert score["initial"]["total"] == 20
    assert score["final"]["total"] == 44
    assert score["repolish_attempted"] is True
    assert "Full manuscript re-polish" in delta


def test_stylist_harness_memory_hook_uses_per_section_query(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    MemoryStylistLLM.instances = []
    FakeStylistMemoryClient.calls = []
    run_dir = _seed_stylist_run(app_session, tmp_path, run_id="run_stylist_memory")
    monkeypatch.setenv("AUTOESSAY_STYLIST_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_MEMORY_READ", "1")
    monkeypatch.setenv("AUTOESSAY_STOP_SLOP_LLM_ENABLED", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", MemoryStylistLLM)
    monkeypatch.setattr(stylist, "MemoryClient", FakeStylistMemoryClient)

    with app_session() as session:
        run_stylist("run_stylist_memory", session)

    prompt = MemoryStylistLLM.instances[0].messages[0][-1]["content"]
    prompt_artifact = (
        run_dir / "stylist" / "prompts" / "stylist_section_introduction.txt"
    ).read_text(encoding="utf-8")

    assert len(FakeStylistMemoryClient.calls) == 2
    assert FakeStylistMemoryClient.calls[0] == {
        "query": (
            "phase=stylist section_id=introduction topic=banking crises in the Great Depression"
        ),
        "user_id": "single-user",
        "limit": 5,
        "enhanced": False,
    }
    assert "Keep prose revisions citation-preserving and plain." in prompt
    assert "Keep prose revisions citation-preserving and plain." in prompt_artifact


def _seed_stylist_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DRAFTER_RUNNING",
        domain_id="financial_history",
    )
    draft_dir = run_dir / "drafts" / "v001"
    draft_dir.mkdir(parents=True)
    (draft_dir / "manuscript.md").write_text(
        '<a id="introduction"></a>\n'
        "## Introduction\n\n"
        "The first claim links deposit insurance to bank behavior through `source_1`.\n\n"
        '<a id="discussion"></a>\n'
        "## Discussion\n\n"
        "The second claim extends the argument through `source_2`.\n",
        encoding="utf-8",
    )
    _write_claim_map(draft_dir / "claim_map.jsonl")
    (draft_dir / "citations.bib").write_text(
        "@article{source_1,\n  title={Source One},\n}\n"
        "@article{source_2,\n  title={Source Two},\n}\n",
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
                state="DRAFTER_RUNNING",
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


def _write_claim_map(path: Path) -> None:
    rows = [
        {
            "draft_version": "v001",
            "section_id": "introduction",
            "section_title": "Introduction",
            "claim_id": "claim_1",
            "paragraph_id": "introduction-p001",
            "claim_text": "Deposit insurance changed bank behavior.",
            "source_ids": ["source_1"],
            "uncited": False,
        },
        {
            "draft_version": "v001",
            "section_id": "discussion",
            "section_title": "Discussion",
            "claim_id": "claim_2",
            "paragraph_id": "discussion-p001",
            "claim_text": "The banking behavior claim carries into the discussion.",
            "source_ids": ["source_2"],
            "uncited": False,
        },
    ]
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


def _stylist_payload(prompt: str) -> dict[str, object]:
    if "Perform one prose-only re-polish" in prompt:
        return {
            "revised_prose": (
                '<a id="introduction"></a>\n'
                "## Introduction\n\n"
                "Polished full manuscript keeps the first claim tied to `source_1`.\n\n"
                '<a id="discussion"></a>\n'
                "## Discussion\n\n"
                "Polished full manuscript keeps the second claim tied to `source_2`.\n"
            ),
            "edit_summary": ["Re-polished the full manuscript."],
            "preserved_claim_ids": ["claim_1", "claim_2"],
        }
    if "Section name: Discussion" in prompt:
        return {
            "revised_prose": "The second claim now reads more directly while citing `source_2`.",
            "edit_summary": ["Tightened the discussion section."],
            "preserved_claim_ids": ["claim_2"],
        }
    return {
        "revised_prose": "The first claim now reads more directly while citing `source_1`.",
        "edit_summary": ["Tightened the introduction section."],
        "preserved_claim_ids": ["claim_1"],
    }


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
