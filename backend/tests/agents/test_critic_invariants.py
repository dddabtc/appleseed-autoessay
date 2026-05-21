import json
from pathlib import Path

import pytest
from conftest import seed_project
from pydantic import ValidationError
from sqlalchemy import select

from autoessay.agents.critic import CriticReport, run_critic
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.models import Project, ProviderCall, Run
from autoessay.run_writer import create_run_directory


class InvariantCriticLLM:
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
        content = json.dumps({"issues": [_issue_payload()]})
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 30}}

    async def aclose(self) -> None:
        return None


def test_critic_report_schema_accepts_expected_issue_shape() -> None:
    parsed = CriticReport.parse_obj({"issues": [_issue_payload()]})

    assert parsed.issues[0].severity == "HIGH"
    assert parsed.issues[0].dimension == "evidence"


def test_critic_report_schema_rejects_invalid_severity() -> None:
    payload = {**_issue_payload(), "severity": "CRITICAL"}

    with pytest.raises(ValidationError):
        CriticReport.parse_obj({"issues": [payload]})


def test_critic_harness_path_satisfies_review_invariants(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness_run_dir = _seed_critic_run(app_session, tmp_path, run_id="run_critic_harness_invariant")

    with app_session() as session:
        monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "0")
        get_settings.cache_clear()
        monkeypatch.setattr("autoessay.harness.runner.LLMClient", InvariantCriticLLM)
        harness_summary = run_critic("run_critic_harness_invariant", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(
                    ProviderCall.run_id == "run_critic_harness_invariant",
                ),
            ),
        )

    reviews_dir = harness_run_dir / "reviews"
    audit_rows = _read_jsonl(reviews_dir / "claim_audit.jsonl")
    blocking = json.loads((reviews_dir / "blocking_issues.json").read_text(encoding="utf-8"))
    assert (reviews_dir / "critic_v001.md").exists()
    assert (reviews_dir / "revision_plan.md").exists()
    assert audit_rows[0]["status"] == "PASS"
    assert blocking["issues"] == []

    assert harness_summary["state"] == "USER_EXTERNAL_SCAN_APPROVAL"
    assert harness_summary["issues"] == 1
    assert len(provider_calls) == 1
    assert provider_calls[0].status == "accepted"


def _seed_critic_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    *,
    run_id: str,
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
    source = NormalizedSource(
        source_id="source_1",
        title="Paper source_1",
        authors=["Author source_1"],
        year=2024,
        venue="Journal source_1",
        doi=None,
        url="https://example.test/source_1",
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


def _issue_payload() -> dict[str, object]:
    return {
        "issue_id": "critic_high_001",
        "severity": "HIGH",
        "dimension": "evidence",
        "paragraph_id": "introduction-p001",
        "source_ids": ["source_1"],
        "description": "The evidence paragraph needs a clearer citation explanation.",
        "suggested_action": "VERIFY_CITATION",
    }


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
