import json
import os
from pathlib import Path

import pytest
from conftest import seed_project
from sqlalchemy import select

from autoessay.agents.proposal import ProposalOutput, run_proposal_draft
from autoessay.config import get_settings
from autoessay.models import ProviderCall, Run
from autoessay.run_writer import create_run_directory

pytestmark = pytest.mark.skipif(
    os.getenv("AUTOESSAY_LIVE_PROPOSAL") != "1",
    reason="live Proposal invariant test is opt-in via AUTOESSAY_LIVE_PROPOSAL=1",
)


@pytest.mark.live
def test_live_proposal_legacy_and_harness_paths_satisfy_invariants(
    app_session,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_PROPOSAL_STUB", "0")
    legacy_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_live_proposal_legacy",
        "proj_test",
        state="DOMAIN_LOADED",
        domain_id="financial_history",
    )
    harness_run_dir = create_run_directory(
        tmp_path / "runs",
        "run_live_proposal_harness",
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
                    id="run_live_proposal_legacy",
                    project_id=project.id,
                    domain_version="0.1.0",
                    run_dir=str(legacy_run_dir),
                    state="DOMAIN_LOADED",
                    baseline_hash="test",
                ),
                Run(
                    id="run_live_proposal_harness",
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
        legacy_summary = run_proposal_draft("run_live_proposal_legacy", session)
        get_settings.cache_clear()
        harness_summary = run_proposal_draft("run_live_proposal_harness", session)
        provider_calls = list(
            session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == "run_live_proposal_harness"),
            ),
        )

    legacy_proposal = _read_json(legacy_run_dir / "proposal" / "proposal_v001.json")
    harness_proposal = _read_json(harness_run_dir / "proposal" / "proposal_v001.json")

    assert legacy_summary["state"] == harness_summary["state"] == "USER_PROPOSAL_REVIEW"
    ProposalOutput.parse_obj(legacy_proposal)
    ProposalOutput.parse_obj(harness_proposal)
    assert harness_proposal["research_question"]
    assert harness_proposal["preliminary_keywords"]
    assert len(provider_calls) >= 1
    assert len(provider_calls) <= 2


def _read_json(path: Path) -> dict[str, object]:
    decoded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return decoded
