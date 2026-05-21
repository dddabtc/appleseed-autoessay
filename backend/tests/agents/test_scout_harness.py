import json
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents import scout
from autoessay.agents.scout import run_scout
from autoessay.clients._stubs import StubLitClient
from autoessay.config import get_settings
from autoessay.models import ProviderCall, Run
from autoessay.run_writer import create_run_directory

QUERIES = [
    "banking crises Great Depression credit markets",
    "financial history bank failures monetary policy",
    "Great Depression banking panics institutional response",
]

JIANGNAN_KERNEL = {
    "kernel_schema_version": 1,
    "observed_puzzle": "既有研究在断代与文体归属上存在反复张力，需要重新检视一手材料以厘清边界。",
    "tentative_question": "此组文献的断代依据如何被重新建立？",
    "scope": "以 19 世纪后期江南刊本为限，仅含序跋与刻工题记。",
    "primary_materials_status": "yes",
}


class LegacyQueryLLM:
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
        content = json.dumps(QUERIES)
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 20}}

    async def aclose(self) -> None:
        return None


class HarnessQueryLLM:
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
        content = json.dumps({"queries": QUERIES, "rationale": "domain coverage"})
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 22}}

    async def aclose(self) -> None:
        return None


class JiangnanHarnessQueryLLM:
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
        content = json.dumps(
            {
                "queries": [
                    "此组文献 断代依据 江南刊本 序跋 刻工题记",
                    "江南刊本 文体归属 断代 一手材料",
                    "19 世纪后期 江南刊本 序跋 题记",
                ],
                "rationale": "jiangnan fixture",
            },
            ensure_ascii=False,
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 22}}

    async def aclose(self) -> None:
        return None


def test_scout_harness_query_expansion_writes_audited_artifacts(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "0")
    monkeypatch.setattr(scout, "get_lit_client", _stub_client)

    harness_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_scout_harness",
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add_all(
            [
                Run(
                    id="run_scout_harness",
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
        monkeypatch.setattr("autoessay.harness.runner.LLMClient", HarnessQueryLLM)
        harness_summary = run_scout("run_scout_harness", session)

        provider_calls = list(
            session.scalars(select(ProviderCall).where(ProviderCall.run_id == "run_scout_harness")),
        )

    queries = json.loads((harness_run_dir / "discovery" / "queries.json").read_text())

    assert queries == QUERIES
    assert harness_summary["state"] == "USER_SEARCH_REVIEW"
    assert len(provider_calls) == 1
    assert provider_calls[0].status == "accepted"
    assert (harness_run_dir / "discovery" / "llm_calls.jsonl").is_file()
    assert (harness_run_dir / "discovery" / "prompts" / "scout_query_expansion.txt").is_file()
    assert (harness_run_dir / "discovery" / "responses" / "scout_query_expansion.txt").is_file()


def test_scout_harness_jiangnan_fixture_keeps_dedup_floor(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "0")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_MIN_PROCESSED_SOURCES", "3")
    monkeypatch.setattr(scout, "get_lit_client", _stub_client)

    run_dir = create_run_directory(
        tmp_path / "runs",
        "run_scout_jiangnan_e2e",
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "[PWTEST-VM] 2026-05-08T00-00-00"
        session.add(
            Run(
                id="run_scout_jiangnan_e2e",
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="DOMAIN_LOADED",
                baseline_hash="test",
                research_kernel_json=JIANGNAN_KERNEL,
            ),
        )
        session.commit()
        get_settings.cache_clear()
        monkeypatch.setattr("autoessay.harness.runner.LLMClient", JiangnanHarnessQueryLLM)

        summary = run_scout("run_scout_jiangnan_e2e", session)

    warnings = _read_jsonl(run_dir / "discovery" / "warnings.jsonl")

    assert summary["state"] == "USER_SEARCH_REVIEW"
    assert summary["sources"] >= 3
    topic_warning = next(
        warning for warning in warnings if warning["source_id"] == "topic_fitness_filter"
    )
    assert topic_warning["query"] == ""
    assert topic_warning["failure_class"] == "source_pool_quality"
    assert "Bypassed candidate drops" in topic_warning["message"]
    assert "audit" not in topic_warning
    assert "warning_type" not in topic_warning


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _stub_client(source_id: str, source_config=None, domain_config=None):  # type: ignore[no-untyped-def]
    del source_config, domain_config
    return StubLitClient(source_id)
