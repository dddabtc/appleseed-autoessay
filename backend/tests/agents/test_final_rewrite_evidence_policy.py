from __future__ import annotations

import json
from pathlib import Path

from conftest import seed_project
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from autoessay.agents._evidence_policy import EvidencePolicies
from autoessay.agents.final_rewrite import (
    _final_rewrite_system_prompt,
    _run_post_rewrite_compliance,
)
from autoessay.config import get_settings
from autoessay.models import Run, RunEvent
from autoessay.run_writer import create_run_directory


def test_final_rewrite_system_prompt_includes_strict_directives() -> None:
    policies = EvidencePolicies(
        phase="final",
        verify_source_bound="strict",
        verify_analytic="strict",
        whitelist="strict",
    )

    prompt = _final_rewrite_system_prompt(policies)

    assert "source_bound" in prompt
    assert "analytic" in prompt
    assert "不得首次引入新的年份" in prompt
    assert "唯一可援引" in prompt


def test_post_rewrite_compliance_strict_keeps_existing_failure() -> None:
    result = _run_post_rewrite_compliance(
        rewritten=_rewritten_payload(),
        original=_original_payload(),
        settings=get_settings(),
        policies=EvidencePolicies(
            phase="final",
            verify_source_bound="strict",
            verify_analytic="strict",
            whitelist="strict",
        ),
    )

    assert result.failed is True
    assert result.reason == "evidence_whitelist_failed"


def test_post_rewrite_compliance_soft_emits_warning_event_without_failure(
    app_session: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    get_settings.cache_clear()
    run_dir = create_run_directory(
        tmp_path / "runs",
        "run_soft_policy",
        "proj_test",
        state="REWRITE_RUNNING",
        domain_id="financial_history",
    )
    with app_session() as session:
        project = seed_project(session)
        run = Run(
            id="run_soft_policy",
            project_id=project.id,
            domain_version="0.1.0",
            run_dir=str(run_dir),
            state="REWRITE_RUNNING",
            baseline_hash="test",
        )
        session.add(run)
        session.commit()

        result = _run_post_rewrite_compliance(
            rewritten=_rewritten_payload(),
            original=_original_payload(),
            settings=get_settings(),
            policies=EvidencePolicies(
                phase="final",
                verify_source_bound="strict",
                verify_analytic="strict",
                whitelist="soft",
            ),
            run=run,
            session=session,
        )
        session.commit()
        events = list(
            session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run.id)
                .order_by(RunEvent.created_at, RunEvent.id),
            ),
        )

    assert result.failed is False
    warning = [event for event in events if event.event_type == "evidence_whitelist_warning"]
    assert warning
    payload = json.loads(warning[-1].payload)
    assert payload["phase_mode"] == "final"
    assert payload["details"][0]["reason"] == "new_protected_terms"


def _original_payload() -> dict[str, object]:
    return {
        "manuscript": "# Paper\n\n## Conclusion\n\nOriginal supported claim [1].\n",
        "claim_map": [
            {
                "section_id": "body",
                "paragraph_id": "body-p001",
                "claim_text": "Original supported claim.",
                "source_ids": ["src1"],
                "evidence_status": "source_bound",
            },
        ],
    }


def _rewritten_payload() -> dict[str, object]:
    return {
        "manuscript": "# Paper\n\n## Conclusion\n\nA new 1999 rupture followed [1].\n",
        "claim_map": [
            {
                "section_id": "conclusion",
                "paragraph_id": "conclusion-p001",
                "claim_text": "A new 1999 rupture followed.",
                "source_ids": ["src1"],
                "evidence_status": "source_bound",
            },
        ],
    }
