import json
from pathlib import Path

from conftest import seed_styled_run
from sqlalchemy import select

from autoessay.agents.critic import _normalize_raw_source_id_markers_for_review, run_critic
from autoessay.clients.common import AccessStatus, NormalizedSource
from autoessay.config import get_settings
from autoessay.models import Run, RunEvent


class BlockingCriticLLM:
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
                "issues": [
                    {
                        "issue_id": "critic_blocker_001",
                        "severity": "BLOCKER",
                        "dimension": "evidence",
                        "paragraph_id": "introduction-p001",
                        "source_ids": ["crossref:diamond_1983"],
                        "description": "A load-bearing claim needs citation verification.",
                        "suggested_action": "VERIFY_CITATION",
                    },
                ],
            },
        )
        return {"content": content, "raw_content": content, "usage": {"total_tokens": 33}}

    async def aclose(self) -> None:
        return None


def test_run_critic_stub_writes_review_artifacts_and_blocks_llm_blockers(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_id, run_dir = seed_styled_run(app_session, tmp_path, monkeypatch, "run_critic_success")
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "0")
    get_settings.cache_clear()
    monkeypatch.setattr("autoessay.harness.runner.LLMClient", BlockingCriticLLM)
    with app_session() as session:
        summary = run_critic(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    reviews_dir = run_dir / "reviews"
    shortlist = json.loads((run_dir / "sources" / "shortlist.json").read_text(encoding="utf-8"))
    shortlist_ids = {source["source_id"] for source in shortlist}
    audit_rows = _read_jsonl(reviews_dir / "claim_audit.jsonl")
    blocking = json.loads((reviews_dir / "blocking_issues.json").read_text(encoding="utf-8"))

    assert run is not None
    assert run.state == "USER_EXTERNAL_SCAN_APPROVAL"
    assert summary["state"] == "USER_EXTERNAL_SCAN_APPROVAL"
    assert (reviews_dir / "critic_v001.md").exists()
    assert (reviews_dir / "revision_plan.md").exists()
    assert audit_rows
    for row in audit_rows:
        for source_id in row["source_ids"]:
            assert source_id in shortlist_ids
    assert blocking["issues"]
    assert blocking["issues"][0]["severity"] == "BLOCKER"
    gate = json.loads((reviews_dir / "north_star_gate.json").read_text(encoding="utf-8"))
    assert gate["status"] in {"skipped_no_baseline", "skipped_stub_baseline"}
    assert summary["north_star_gate"]["status"] == gate["status"]
    assert events[-1].event_type == "phase_done"


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_normalize_raw_source_id_markers_for_review() -> None:
    sources = [
        _source("official:fraser:bog-minutes-1968-03-20"),
        _source("official:imf:annual-report-1968"),
        _source("shadow_baseline_v001"),
    ]
    claim_map = [
        {"source_ids": ["shadow_baseline_v001"]},
        {"source_ids": ["official:fraser:bog-minutes-1968-03-20"]},
        {"source_ids": ["official:imf:annual-report-1968"]},
    ]
    manuscript = (
        "基线判断[shadow_baseline_v001]。\n\n"
        "联储纪要（official:fraser:bog-minutes-1968-03-20）。\n\n"
        "IMF年报(official:imf:annual-report-1968)。\n\n"
        "复合标记[official:fraser:bog-minutes-1968-03-20；official:imf:annual-report-1968]。"
    )

    normalized = _normalize_raw_source_id_markers_for_review(manuscript, claim_map, sources)

    assert "基线判断[3]。" in normalized
    assert "联储纪要[1]。" in normalized
    assert "IMF年报[2]。" in normalized
    assert "复合标记[1][2]。" in normalized
    assert "shadow_baseline_v001" not in normalized
    assert "official:" not in normalized


def _source(source_id: str) -> NormalizedSource:
    return NormalizedSource(
        source_id=source_id,
        title=source_id,
        authors=["A"],
        year=2024,
        venue="J",
        doi=None,
        url=None,
        pdf_url=None,
        abstract=None,
        source_client="test",
        access_status=AccessStatus.METADATA_ONLY,
        license=None,
        rank_score=1.0,
        risk_flags=[],
    )
