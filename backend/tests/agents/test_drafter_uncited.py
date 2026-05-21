import json
import re
from pathlib import Path
from typing import Any

from conftest import seed_project

from autoessay.agents.curator import run_curator
from autoessay.agents.drafter import run_drafter
from autoessay.agents.ideator import run_ideator, select_thesis_for_run
from autoessay.agents.scout import run_scout
from autoessay.agents.synthesizer import run_synthesizer
from autoessay.config import get_settings
from autoessay.llm_client import LLMClient
from autoessay.models import Run
from autoessay.run_writer import create_run_directory


def test_run_drafter_rejects_empty_source_ids_through_harness(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = _seed_ready_for_live_drafter(app_session, tmp_path, monkeypatch)

    async def fake_chat_completion(
        self: LLMClient,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        retries: int = 2,
        response_format: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> dict[str, Any]:
        section = _section_from_prompt(messages[-1]["content"])
        content = json.dumps(
            {
                "section_id": section["section_id"],
                "section_title": section["section_title"],
                "prose": f"{section['section_title']} makes a source-hungry claim.",
                "claim_map": [
                    {
                        "paragraph_id": f"{section['section_id']}-p001",
                        "claim_text": "This claim lacks source IDs.",
                        "source_ids": [],
                    },
                ],
            },
        )
        return {
            "content": content,
            "reasoning_text": "",
            "usage": {},
            "raw_content": content,
        }

    monkeypatch.setattr(LLMClient, "chat_completion", fake_chat_completion)

    with app_session() as session:
        summary = run_drafter(run_id, session)
        run = session.get(Run, run_id)

    draft_dir = run_dir / "drafts" / "v001"
    claim_map = _read_jsonl(draft_dir / "claim_map.jsonl")
    rationale = (draft_dir / "draft_rationale.md").read_text(encoding="utf-8")

    assert run is not None
    assert run.state == "FAILED_FIXABLE"
    assert summary["state"] == "FAILED_FIXABLE"
    fallback_claims = [
        claim
        for claim in claim_map
        if claim.get("claim_text") == "LLM JSON did not parse after one retry."
    ]
    assert summary["stubbed_sections"] == len(fallback_claims)
    assert len(claim_map) >= summary["stubbed_sections"]
    assert "All sections fell back" in str(summary["guidance"])
    assert "[UNCITED] claims:" in rationale
    assert "LLM JSON did not parse after one retry." in rationale


def _seed_ready_for_live_drafter(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> tuple[str, Path]:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "1")
    monkeypatch.delenv("AUTOESSAY_DRAFTER_STUB", raising=False)
    get_settings.cache_clear()
    run_id = "run_drafter_uncited"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
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

        run_scout(run_id, session)
        run_curator(run_id, session)
        run_synthesizer(run_id, session)
        run_ideator(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        select_thesis_for_run(run, "angle_001")
        session.commit()

    return run_id, run_dir


def _section_from_prompt(prompt: str) -> dict[str, str]:
    match = re.search(r"Outline: (\{.*?\})\. Approved sources:", prompt)
    assert match is not None
    decoded = json.loads(match.group(1))
    assert isinstance(decoded, dict)
    return {
        "section_id": str(decoded["section_id"]),
        "section_title": str(decoded["section_title"]),
    }


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
