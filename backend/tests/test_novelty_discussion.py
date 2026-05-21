import json
from pathlib import Path
from typing import Any

from conftest import seed_project
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from autoessay.config import get_settings
from autoessay.llm_client import LLMClient
from autoessay.main import app
from autoessay.models import NoveltyDiscussion, Run
from autoessay.run_writer import create_run_directory


async def test_novelty_discussion_regenerates_cards_and_saves_messages(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AUTOESSAY_IDEATOR_STUB", raising=False)
    get_settings.cache_clear()
    run_id = "run_novelty_discussion"
    run_dir = _seed_novelty_run(app_session, tmp_path, run_id)
    captured_prompts: list[str] = []

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
        captured_prompts.append(messages[-1]["content"])
        content = json.dumps({"angle_cards": _angle_cards("Regenerated")})
        return {"content": content, "reasoning_text": "", "usage": {}, "raw_content": content}

    monkeypatch.setattr(LLMClient, "chat_completion", fake_chat_completion)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/novelty/discuss",
            json={
                "user_message": "Current angles are too weak; try an institutional history angle.",
            },
        )
        discussion_response = await client.get(f"/api/runs/{run_id}/novelty/discussion")

    with app_session() as session:
        messages = list(
            session.scalars(
                select(NoveltyDiscussion).where(NoveltyDiscussion.run_id == run_id),
            ),
        )

    assert response.status_code == 201
    assert response.json()["angle_cards"][0]["working_title"].startswith("Regenerated")
    assert discussion_response.status_code == 200
    assert len(messages) == 2
    assert messages[1].role == "assistant"
    assert (run_dir / "novelty" / "angle_cards_v001.json").exists()
    assert (run_dir / "novelty" / "angle_cards_v002.json").exists()
    assert "Previous discussion" in captured_prompts[0]
    assert "institutional history angle" in captured_prompts[0]


def _seed_novelty_run(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    run_id: str,
    *,
    state: str = "USER_NOVELTY_REVIEW",
) -> Path:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state=state,
        domain_id="financial_history",
    )
    _write_synthesis_inputs(run_dir)
    novelty_dir = run_dir / "novelty"
    novelty_dir.mkdir(parents=True)
    (novelty_dir / "angle_cards.json").write_text(
        json.dumps({"angle_cards": _angle_cards("Initial")}),
        encoding="utf-8",
    )
    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises"
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state=state,
                baseline_hash="test",
            ),
        )
        session.commit()
    return run_dir


async def test_novelty_discussion_rejects_running_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    # Round-1 audit #24: discuss_novelty must reject mutation requests
    # while another phase is mid-flight. Without this guard, a user
    # could rewrite angle cards under DRAFTER_RUNNING and corrupt the
    # downstream artifacts.
    run_id = "run_novelty_discuss_running_guard"
    _seed_novelty_run(app_session, tmp_path, run_id, state="DRAFTER_RUNNING")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/novelty/discuss",
            json={"user_message": "Try a different angle."},
        )
    assert response.status_code == 409
    assert "currently running" in response.json()["detail"]


async def test_novelty_discussion_rejects_non_review_state(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    # Round-1 audit #24: discussion is only meaningful at
    # USER_NOVELTY_REVIEW. Earlier states have no angle cards yet;
    # later states (USER_DRAFT_REVIEW, etc.) shouldn't rewrite the
    # locked-in angle.
    run_id = "run_novelty_discuss_state_guard"
    _seed_novelty_run(
        app_session,
        tmp_path,
        run_id,
        state="USER_DRAFT_REVIEW",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/runs/{run_id}/novelty/discuss",
            json={"user_message": "Try a different angle."},
        )
    assert response.status_code == 409
    assert "USER_NOVELTY_REVIEW" in response.json()["detail"]


def _write_synthesis_inputs(run_dir: Path) -> None:
    synthesis_dir = run_dir / "synthesis"
    source_notes_dir = synthesis_dir / "source_notes"
    source_notes_dir.mkdir(parents=True)
    (synthesis_dir / "claims.jsonl").write_text(
        json.dumps(
            {
                "source_id": "source_001",
                "claim_id": "claim_001",
                "text": "Credit shocks shaped local banking outcomes.",
                "claim_type": "finding",
            },
        )
        + "\n",
        encoding="utf-8",
    )
    (source_notes_dir / "source_001.json").write_text(
        json.dumps(
            {
                "source_id": "source_001",
                "thesis": "A source-bound thesis.",
                "evidence": "A source-bound evidence note.",
            },
        ),
        encoding="utf-8",
    )


def _angle_cards(prefix: str) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for index in range(1, 5):
        cards.append(
            {
                "angle_id": f"angle_{index:03d}",
                "working_title": f"{prefix} angle {index}",
                "thesis_one_sentence": f"{prefix} thesis {index}.",
                "key_claim_ids": ["claim_001"],
                "why_novel": "It reframes the source pack.",
                "evidence_so_far": "One claim supports the angle.",
                "missing_evidence": "More archival evidence.",
                "journal_fit_note": "Fits if tightened.",
                "risks": ["thin evidence"],
            },
        )
    return cards
