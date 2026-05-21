import json
import re
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.drafter import DrafterSection, run_drafter
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.models import Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory


class UncitedDrafterLLM:
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
        content = json.dumps(
            {
                "section_id": section["section_id"],
                "section_title": section["section_title"],
                "prose": f"{section['section_title']} makes a source-hungry claim.",
                "claim_map": [
                    {
                        "paragraph_id": f"{section['section_id']}-p001",
                        "claim_text": "This claim lacks source IDs.",
                    },
                ],
            },
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 55}}

    async def aclose(self) -> None:
        return None


def test_drafter_section_schema_accepts_uncited_literal() -> None:
    parsed = DrafterSection.parse_obj(
        {
            "section_id": "introduction",
            "section_title": "Introduction",
            "prose": "A claim needs later evidence.",
            "claim_map": [
                {
                    "paragraph_id": "introduction-p001",
                    "claim_text": "This claim needs evidence.",
                    "source_ids": "[UNCITED]",
                },
            ],
        },
    )

    assert parsed.claim_map[0].source_ids == "[UNCITED]"


def test_drafter_harness_uncited_exhausts_budget_and_fails_fixable(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """When the LLM keeps emitting [UNCITED] after the corrective retry budget,
    Drafter must transition the run to FAILED_FIXABLE with a retry guidance —
    not silently accept the uncited draft."""
    run_dir = _seed_drafter_run(app_session, tmp_path, run_id="run_drafter_harness_uncited")
    monkeypatch.setenv("AUTOESSAY_DRAFTER_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", UncitedDrafterLLM)

    with app_session() as session:
        summary = run_drafter("run_drafter_harness_uncited", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_drafter_harness_uncited"),
            ),
        )

    assert summary["state"] == "FAILED_FIXABLE"
    assert summary["failure_class"] == "failed_fixable" if "failure_class" in summary else True
    assert "exhausted" in summary["guidance"].lower() or "manually" in summary["guidance"].lower()
    assert summary["resume_options"] == ["retry", "edit_section", "mark_unverified"]
    # corrective budget = 2, so each section gets 3 attempts (initial + 2 retries)
    # before emitting the SchemaViolationError; multiple failed sections accumulate.
    assert all(
        call.status in {"accepted", "retrying", "failed_schema_violation"}
        for call in provider_calls
    )
    assert any(call.status == "failed_schema_violation" for call in provider_calls), (
        "expected at least one section to record failed_schema_violation after budget exhaustion"
    )
    # Stub fallback files are still produced (best-effort) but the run state must be FAILED_FIXABLE.
    draft_dir = run_dir / "drafts" / "v001"
    assert (draft_dir / "manuscript.md").exists()


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
    novelty_dir = run_dir / "novelty"
    sources_dir.mkdir(parents=True, exist_ok=True)
    novelty_dir.mkdir(parents=True, exist_ok=True)
    source = NormalizedSource(
        source_id="source_001",
        title="Paper source_001",
        authors=["Author source_001"],
        year=2024,
        venue="Journal source_001",
        doi=None,
        url="https://example.test/source_001",
        pdf_url=None,
        abstract="Abstract evidence for banking crises.",
        source_client="semantic_scholar",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )
    (sources_dir / "shortlist.json").write_text(
        json.dumps([source.dict()], indent=2, sort_keys=True) + "\n",
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
