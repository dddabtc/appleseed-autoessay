import json
from pathlib import Path
from typing import Any

from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.curator import run_curator
from autoessay.agents.ideator import run_ideator
from autoessay.agents.scout import run_scout
from autoessay.agents.synthesizer import run_synthesizer
from autoessay.config import get_settings
from autoessay.llm_client import LLMClient
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory


def test_run_ideator_stub_transitions_and_writes_angle_cards(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_SCOUT_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_CURATOR_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL", "1")
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "1")
    get_settings.cache_clear()
    run_id = "run_ideator_success"
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
        summary = run_ideator(run_id, session)
        run = session.get(Run, run_id)
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc()),
            ),
        )

    novelty_dir = run_dir / "novelty"
    cards_payload = json.loads((novelty_dir / "angle_cards.json").read_text(encoding="utf-8"))
    report = (novelty_dir / "ideator_report.md").read_text(encoding="utf-8")

    assert run is not None
    assert run.state == "USER_NOVELTY_REVIEW"
    assert summary["state"] == "USER_NOVELTY_REVIEW"
    assert len(cards_payload["angle_cards"]) == 4
    assert cards_payload["angle_cards"][0]["key_claim_ids"]
    assert "Ideator Report" in report
    assert "phase_started" in [event.event_type for event in events]
    assert events[-1].event_type == "phase_done"


def test_run_ideator_passes_validate_json_content_and_no_outer_retry(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Hub-only ideator uses harness corrective retries and then fails fixable."""
    monkeypatch.delenv("AUTOESSAY_IDEATOR_STUB", raising=False)
    get_settings.cache_clear()
    run_id = "run_ideator_no_outer_retry"
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state="USER_FIELD_REVIEW",
        domain_id="financial_history",
    )
    _write_synthesis_inputs(run_dir)
    calls: list[dict[str, Any]] = []

    async def fake_chat_completion(
        self: LLMClient,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        retries: int = 0,
        response_format: dict[str, object] | None = None,
        force_no_reasoning: bool = False,
        validate_json_content: bool = False,
    ) -> dict[str, Any]:
        calls.append(
            {
                "validate_json_content": validate_json_content,
                "response_format": response_format,
            },
        )
        # Pretend the chain exhausted with malformed JSON. (In
        # production the LLMClient itself would have retried each
        # provider before giving up — here we collapse that to one
        # surface-level return so we can assert the outer agent
        # behavior.)
        return {
            "content": "not-json",
            "reasoning_text": "",
            "usage": {},
            "raw_content": "not-json",
        }

    monkeypatch.setattr(LLMClient, "chat_completion", fake_chat_completion)

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises"
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state="USER_FIELD_REVIEW",
                baseline_hash="test",
            ),
        )
        session.commit()

        run_ideator(run_id, session)
        run = session.get(Run, run_id)

    assert run is not None
    assert len(calls) == 3
    # validate_json_content propagated.
    assert calls[0]["validate_json_content"] is True
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert run.state == "FAILED_FIXABLE"


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
                "n_sources_supporting": 1,
                "page_anchor": None,
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


def _angle_cards() -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for index in range(1, 5):
        cards.append(
            {
                "angle_id": f"angle_{index:03d}",
                "working_title": f"Angle {index}",
                "thesis_one_sentence": f"Thesis {index}.",
                "key_claim_ids": ["claim_001"],
                "why_novel": "Novel because it reframes the source pack.",
                "evidence_so_far": "One claim supports the angle.",
                "missing_evidence": "More archival evidence.",
                "journal_fit_note": "Fits the target journal if tightened.",
                "risks": ["thin evidence"],
            },
        )
    return cards


# ---------------------------------------------------------------------------
# PR-C2.b Tier 4: angle.framework_lens referential integrity
# ---------------------------------------------------------------------------


def test_filter_angle_lens_references_keeps_valid_drops_invalid() -> None:
    from autoessay.agents.ideator import _filter_angle_lens_references

    cards = [
        {"angle_id": "a1", "framework_lens": ["Bourdieu's habitus", "Made-up theory"]},
        {"angle_id": "a2", "framework_lens": ["Polanyi's embeddedness"]},
        {"angle_id": "a3", "framework_lens": []},
    ]
    valid = {"Bourdieu's habitus", "Polanyi's embeddedness"}
    out, dropped = _filter_angle_lens_references(cards, valid)
    assert out[0]["framework_lens"] == ["Bourdieu's habitus"]
    assert out[1]["framework_lens"] == ["Polanyi's embeddedness"]
    assert out[2]["framework_lens"] == []
    assert dropped == ["Made-up theory"]


def test_filter_angle_lens_references_no_op_when_no_valid_set() -> None:
    # When the lens phase hasn't run, valid set is empty — pass
    # everything through unchanged so legacy runs aren't disrupted.
    from autoessay.agents.ideator import _filter_angle_lens_references

    cards = [
        {"angle_id": "a1", "framework_lens": ["Anything", "Whatever"]},
    ]
    out, dropped = _filter_angle_lens_references(cards, set())
    assert out[0]["framework_lens"] == ["Anything", "Whatever"]
    assert dropped == []


def test_filter_angle_lens_references_handles_non_list_field() -> None:
    # Defensive: if upstream serialization left framework_lens as
    # None (some legacy migration), don't crash. None is left
    # untouched (the isinstance(list) early-return). Missing key
    # normalizes to [] via the .get default — that's also fine.
    from autoessay.agents.ideator import _filter_angle_lens_references

    cards = [
        {"angle_id": "a1", "framework_lens": None},
        {"angle_id": "a2"},  # missing entirely
    ]
    out, dropped = _filter_angle_lens_references(cards, {"Foo"})
    assert out[0].get("framework_lens") is None  # non-list left alone
    assert out[1].get("framework_lens") == []  # missing → defaults to []
    assert dropped == []


def test_order_angle_cards_for_kernel_prefers_dollar_gold_core() -> None:
    from autoessay.agents.ideator import _order_angle_cards_for_kernel

    cards = [
        {
            "angle_id": "angle_001",
            "working_title": "以国际储备配置为透镜：重释英镑通道",
            "thesis_one_sentence": "英镑储备地位衰减可作为制度弱化证据。",
            "why_novel": "关注 sterling reserve role.",
            "evidence_so_far": "pound sterling evidence.",
            "missing_evidence": "需要更多财政部档案。",
            "journal_fit_note": "financial history",
            "risks": [],
        },
        {
            "angle_id": "angle_003",
            "working_title": "金池与国际组织档案中的同步崩解",
            "thesis_one_sentence": (
                "London Gold Pool、IMF 备忘录与美联储会议纪要共同锚定美元—黄金兑换失效。"
            ),
            "why_novel": "直接对应 dollar convertibility into gold.",
            "evidence_so_far": "gold pool, Federal Reserve, IMF evidence.",
            "missing_evidence": "需要补足会议纪要与结算记录。",
            "journal_fit_note": "financial history",
            "risks": [],
        },
    ]
    ordered = _order_angle_cards_for_kernel(
        cards,
        {
            "kernel_schema_version": 1,
            "tentative_question": (
                "布雷顿森林金本位承诺的实际约束力如何依据美元—黄金兑换证据重估？"
            ),
            "scope": (
                "1960-1971 年美元—黄金兑换通道、IMF 备忘录、美联储会议纪要"
                "与 London Gold Pool 记录。"
            ),
        },
    )

    assert ordered[0]["angle_id"] == "angle_003"
