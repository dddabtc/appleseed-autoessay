import json
import os
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.ideator import IdeatorOutput, run_ideator
from autoessay.config import get_settings
from autoessay.models import ProviderCall, Run
from autoessay.run_writer import create_run_directory

pytestmark = pytest.mark.skipif(
    os.getenv("AUTOESSAY_LIVE_IDEATOR") != "1",
    reason="live Ideator invariant test is opt-in via AUTOESSAY_LIVE_IDEATOR=1",
)


@pytest.mark.live
def test_live_ideator_legacy_and_harness_paths_satisfy_invariants(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_IDEATOR_STUB", "0")
    legacy_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_live_ideator_legacy",
        "proj_test",
        state="USER_FIELD_REVIEW",
        domain_id="financial_history",
    )
    harness_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_live_ideator_harness",
        "proj_test",
        state="USER_FIELD_REVIEW",
        domain_id="financial_history",
    )
    _write_synthesis_inputs(legacy_run_dir)
    _write_synthesis_inputs(harness_run_dir)

    with app_session() as session:
        project = seed_project(session)
        project.title = "banking crises in the Great Depression"
        session.add_all(
            [
                Run(
                    id="run_live_ideator_legacy",
                    project_id=project.id,
                    domain_version="0.1.0",
                    run_dir=str(legacy_run_dir),
                    state="USER_FIELD_REVIEW",
                    baseline_hash="test",
                ),
                Run(
                    id="run_live_ideator_harness",
                    project_id=project.id,
                    domain_version="0.1.0",
                    run_dir=str(harness_run_dir),
                    state="USER_FIELD_REVIEW",
                    baseline_hash="test",
                ),
            ],
        )
        session.commit()
        get_settings.cache_clear()
        legacy_summary = run_ideator("run_live_ideator_legacy", session)
        get_settings.cache_clear()
        harness_summary = run_ideator("run_live_ideator_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_live_ideator_harness"),
            ),
        )

    legacy_payload = _read_json(legacy_run_dir / "novelty" / "angle_cards.json")
    harness_payload = _read_json(harness_run_dir / "novelty" / "angle_cards.json")

    assert legacy_summary["state"] == harness_summary["state"] == "USER_NOVELTY_REVIEW"
    IdeatorOutput.parse_obj(legacy_payload)
    parsed = IdeatorOutput.parse_obj(harness_payload)
    assert 4 <= len(parsed.angle_cards) <= 6
    assert all(card.thesis_one_sentence for card in parsed.angle_cards)
    assert len(provider_calls) >= 1
    assert len(provider_calls) <= 2


def _write_synthesis_inputs(run_dir: Path) -> None:
    synthesis_dir = run_dir / "synthesis"
    source_notes_dir = synthesis_dir / "source_notes"
    source_notes_dir.mkdir(parents=True, exist_ok=True)
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


def _read_json(path: Path) -> dict[str, object]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return decoded
