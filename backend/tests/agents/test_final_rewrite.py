from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import seed_project
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from autoessay.agents import critic as critic_module
from autoessay.agents import critic_loop as critic_loop_module
from autoessay.agents import final_rewrite as final_rewrite_module
from autoessay.agents._critic_polish_loop import (
    ExpertCritiqueOutput,
    QualityScoreSet,
    RevisionItem,
)
from autoessay.agents.critic import run_critic
from autoessay.agents.final_rewrite import (
    load_latest_rewrite_artifact,
    rewrite_summary_for_run,
    run_final_rewrite,
    run_final_rewrite_then_critic,
)
from autoessay.agents.integrity import run_integrity
from autoessay.config import get_settings
from autoessay.main import start_critic
from autoessay.models import Checkpoint, Run, RunEvent, utcnow
from autoessay.run_writer import create_run_directory
from autoessay.state_machine import ALLOWED_TRANSITIONS


def test_stub_mode_passes_through_and_writes_empty_audit(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_STUB", "1")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_stub")

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    assert artifact is not None
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact.manuscript == _STYLED_MANUSCRIPT
    assert artifact.claim_map == [_CLAIM]
    assert artifact.audit == {}
    assert (run_dir / "rewrite" / "v001" / "diff.txt").read_text(encoding="utf-8") == ""


def test_happy_path_writes_rewrite_and_transitions_to_critic_running(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_happy")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        events = _events(session, run_id)

    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert artifact is not None
    assert artifact.manuscript == _REWRITTEN_MANUSCRIPT
    assert artifact.claim_map == [_CLAIM]
    assert audit["rewrite_mode"] == "holistic"
    assert result["state"] == "CRITIC_RUNNING"
    assert run.state == "CRITIC_RUNNING"
    assert any(event.event_type == "rewrite_completed" for event in events)


def test_controlled_polish_skips_without_real_shadow_baseline(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_polish_no_baseline")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    def fail_if_called(**_kwargs: object) -> None:
        raise AssertionError("polish critique should not run without a real baseline")

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        fail_if_called,
    )

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert result["state"] == "CRITIC_RUNNING"
    assert audit["controlled_polish_loop"]["status"] == "skipped_no_real_baseline"
    assert load_latest_rewrite_artifact(run_dir).manuscript == _REWRITTEN_MANUSCRIPT  # type: ignore[union-attr]


def test_controlled_polish_accepts_monotonic_candidate(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_polish_accept")
    polished = "# Paper\n\nTherefore, argument about banking stress remains precise [1].\n"
    critiques = [
        _polish_critique(
            compliance=7.0,
            novelty=7.0,
            completeness=7.0,
            needs_revision=True,
            items=[
                RevisionItem(
                    severity="HIGH",
                    scope="body-p001",
                    issue="The paragraph needs a clearer argumentative connector.",
                    suggestion="Tighten the transition without adding facts.",
                )
            ],
        ),
        _polish_critique(
            compliance=9.5,
            novelty=9.5,
            completeness=9.5,
            needs_revision=False,
            items=[],
        ),
    ]

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    def fake_baseline(_run_dir: Path) -> tuple[str, str]:
        return (
            "A separate manuscript about agricultural credit and rural institutions.",
            "real",
        )

    def fake_critique(**_kwargs: object) -> ExpertCritiqueOutput:
        return critiques.pop(0)

    def fake_polish_rewrite(**_kwargs: object) -> str:
        return polished

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(final_rewrite_module, "_controlled_polish_baseline_text", fake_baseline)
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        fake_critique,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fake_polish_rewrite,
    )

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == polished
    assert audit["controlled_polish_loop"]["status"] == "approved_targets_cleared"
    assert audit["controlled_polish_loop"]["accepted_rewrites"] == 1
    assert audit["controlled_polish_loop"]["approved_blocker_high_all_cleared"] is True
    assert audit["controlled_polish_loop"]["attempts"][0]["accept_conditions"] == {
        "score_monotonic": True,
        "accepted": True,
    }
    assert audit["controlled_polish_loop"]["final_scores"]["completeness"] == 9.5
    assert "final_validation" not in audit["controlled_polish_loop"]
    assert (run_dir / "drafts" / "v001" / "polish" / "paper_polished.md").read_text(
        encoding="utf-8",
    ) == polished


def test_final_rewrite_runs_production_critic_loop_after_polish(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_critic_loop")
    selected = "# Paper\n\nCritic loop selected a better final manuscript [1].\n"

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    def fake_loop(**kwargs: object) -> critic_loop_module.CriticLoopRunResult:
        assert kwargs["manuscript"] == _REWRITTEN_MANUSCRIPT
        return critic_loop_module.CriticLoopRunResult(
            manuscript=selected,
            audit={
                "status": "selected",
                "selected_iter": 1,
                "selected_metrics": {"max_loss": 0.0},
            },
        )

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(
        critic_loop_module,
        "run_production_critic_loop",
        fake_loop,
    )

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == selected
    assert audit["critic_loop"]["status"] == "selected"
    assert audit["critic_loop"]["selected_replaced_final_rewrite_manuscript"] is True


def test_controlled_polish_attempts_repair_without_incumbent_hard_gate(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_polish_entry")
    polished = "# Paper\n\nTherefore, argument about banking stress is clearer [1].\n"
    critiques = [
        _polish_critique(
            compliance=6.0,
            novelty=7.0,
            completeness=6.0,
            needs_revision=True,
            items=[
                RevisionItem(
                    severity="HIGH",
                    scope="body-p001",
                    issue="The argument needs a sharper compliance repair.",
                    suggestion="Clarify the paragraph without changing citations.",
                )
            ],
        ),
        _polish_critique(
            compliance=9.5,
            novelty=9.5,
            completeness=9.5,
            needs_revision=False,
            items=[],
        ),
    ]

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    def fake_baseline(_run_dir: Path) -> tuple[str, str]:
        return (
            "A separate manuscript about agricultural credit and rural institutions.",
            "real",
        )

    def fake_critique(**_kwargs: object) -> ExpertCritiqueOutput:
        return critiques.pop(0)

    def fake_polish_rewrite(**_kwargs: object) -> str:
        return polished

    def fail_if_validate_called(**_kwargs: object) -> None:
        raise AssertionError("literal polish loop must not hard-validate candidates")

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(final_rewrite_module, "_controlled_polish_baseline_text", fake_baseline)
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        fake_critique,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fake_polish_rewrite,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_validate_controlled_polish_candidate",
        fail_if_validate_called,
    )

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == polished
    assert audit["controlled_polish_loop"]["status"] == "approved_targets_cleared"
    assert audit["controlled_polish_loop"]["improvement_found"] is True
    assert "final_validation" not in audit["controlled_polish_loop"]


def test_literal_polish_accepts_score_monotonic_candidate_with_high_items(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_polish_literal")
    bad_polished = "# Paper\n\nTherefore, argument about banking stress without marker.\n"
    critique_stages: list[str] = []

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    def fake_baseline(_run_dir: Path) -> tuple[str, str]:
        return (
            "A separate manuscript about agricultural credit and rural institutions.",
            "real",
        )

    def fake_critique(**kwargs: object) -> ExpertCritiqueOutput:
        critique_stages.append(str(kwargs.get("stage") or "unknown"))
        if kwargs.get("stage") == "candidate":
            return _polish_critique(
                compliance=10.0,
                novelty=10.0,
                completeness=10.0,
                needs_revision=True,
                items=[
                    RevisionItem(
                        severity="HIGH",
                        scope="body-p001",
                        issue="The paragraph still needs a stronger compliance repair.",
                        suggestion="Tighten the sentence again.",
                    )
                ],
            )
        return _polish_critique(
            compliance=7.0,
            novelty=7.0,
            completeness=7.0,
            needs_revision=True,
            items=[
                RevisionItem(
                    severity="HIGH",
                    scope="body-p001",
                    issue="The paragraph needs a clearer argumentative connector.",
                    suggestion="Tighten the transition without adding facts.",
                )
            ],
        )

    def fake_polish_rewrite(**_kwargs: object) -> str:
        return bad_polished

    def fail_if_validate_called(**_kwargs: object) -> None:
        raise AssertionError("literal polish loop must not hard-validate candidates")

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(final_rewrite_module, "_controlled_polish_baseline_text", fake_baseline)
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        fake_critique,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fake_polish_rewrite,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_validate_controlled_polish_candidate",
        fail_if_validate_called,
    )

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == bad_polished
    assert audit["controlled_polish_loop"]["status"] == "approved_targets_cleared"
    assert audit["controlled_polish_loop"]["accepted_rewrites"] == 1
    assert audit["controlled_polish_loop"]["critic_error_count"] == 1
    assert audit["controlled_polish_loop"]["approved_blocker_high_all_cleared"] is True
    assert audit["controlled_polish_loop"]["attempts"][0]["candidate_high_blocker_count"] == 1
    assert audit["controlled_polish_loop"]["attempts"][0]["critic_error_count"] == 1
    assert audit["controlled_polish_loop"]["attempts"][0]["accept_conditions"] == {
        "score_monotonic": True,
        "accepted": True,
    }
    assert "candidate" in critique_stages
    assert "final_validation" not in audit["controlled_polish_loop"]


def test_controlled_polish_records_exit_after_final_rewrite_policy_fallback(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_polish_fallback")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        claim = _rewrite_claim()
        claim["source_ids"] = ["missing_source"]
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [claim]}

    def fake_baseline(_run_dir: Path) -> tuple[str, str]:
        return (
            "A separate manuscript about agricultural credit and rural institutions.",
            "real",
        )

    critiques = [
        _polish_critique(
            compliance=8.0,
            novelty=8.0,
            completeness=8.0,
            needs_revision=False,
            items=[],
        ),
        _polish_critique(
            compliance=8.0,
            novelty=8.0,
            completeness=8.0,
            needs_revision=False,
            items=[],
        ),
        _polish_critique(
            compliance=8.0,
            novelty=8.0,
            completeness=8.0,
            needs_revision=False,
            items=[],
        ),
    ]
    polish_rewrite_calls: list[int] = []

    def fake_critique(**_kwargs: object) -> ExpertCritiqueOutput:
        return critiques.pop(0)

    def fake_polish_rewrite(**_kwargs: object) -> str:
        polish_rewrite_calls.append(1)
        return _STYLED_MANUSCRIPT

    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_HOLISTIC", "0")
    get_settings.cache_clear()
    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(final_rewrite_module, "_controlled_polish_baseline_text", fake_baseline)
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        fake_critique,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fake_polish_rewrite,
    )

    with app_session() as session:
        result = run_final_rewrite(run_id, session)
        events = _events(session, run_id)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    artifact = load_latest_rewrite_artifact(run_dir)
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == _STYLED_MANUSCRIPT
    assert audit["fallback_to_original"] is True
    assert audit["controlled_polish_loop"]["input_source"] == (
        "fallback_original_after_failed_final_rewrite"
    )
    assert audit["controlled_polish_loop"]["status"] == "approved_targets_cleared"
    assert audit["controlled_polish_loop"]["exit_reason"] == "approved_targets_cleared"
    assert audit["controlled_polish_loop"]["accepted_rewrites"] == 0
    assert polish_rewrite_calls == []
    assert audit["controlled_polish_loop"]["ran"] is True
    assert "final_validation" not in audit["controlled_polish_loop"]
    assert any(event.event_type == "controlled_polish_loop_exit" for event in events)


def test_controlled_polish_stops_after_two_no_score_gain_attempts_on_approved_target(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_polish_no_gain")
    target_issue = "The paragraph needs a clearer argumentative connector."
    approved_item = RevisionItem(
        severity="HIGH",
        scope="body-p001",
        issue=target_issue,
        suggestion="Tighten the transition without adding facts.",
    )
    critiques = [
        _polish_critique(
            compliance=8.0,
            novelty=8.0,
            completeness=8.0,
            needs_revision=True,
            items=[approved_item],
        ),
        _polish_critique(
            compliance=8.0,
            novelty=8.0,
            completeness=8.0,
            needs_revision=True,
            items=[approved_item],
        ),
        _polish_critique(
            compliance=8.0,
            novelty=8.0,
            completeness=8.0,
            needs_revision=True,
            items=[approved_item],
        ),
    ]
    rewrite_target_issues: list[list[str]] = []

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    def fake_baseline(_run_dir: Path) -> tuple[str, str]:
        return (
            "A separate manuscript about agricultural credit and rural institutions.",
            "real",
        )

    def fake_critique(**_kwargs: object) -> ExpertCritiqueOutput:
        return critiques.pop(0)

    def fake_polish_rewrite(**kwargs: object) -> str:
        target_items = kwargs["target_items"]
        assert isinstance(target_items, list)
        rewrite_target_issues.append([str(item.get("issue")) for item in target_items])
        return "# Paper\n\nTherefore, argument about banking stress is unchanged [1].\n"

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(final_rewrite_module, "_controlled_polish_baseline_text", fake_baseline)
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        fake_critique,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fake_polish_rewrite,
    )

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert result["state"] == "CRITIC_RUNNING"
    assert audit["controlled_polish_loop"]["status"] == "stopped_no_score_gain"
    assert audit["controlled_polish_loop"]["accepted_rewrites"] == 2
    assert audit["controlled_polish_loop"]["approved_blocker_high_remaining_count"] == 1
    assert rewrite_target_issues == [[target_issue], [target_issue]]
    no_gain_counts = [
        attempt["consecutive_no_score_gain"]
        for attempt in audit["controlled_polish_loop"]["attempts"]
    ]
    assert no_gain_counts == [1, 2]


def test_controlled_polish_does_not_target_exit_with_clipped_scores_and_open_high(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_polish_clipped")
    target_issue = "The conclusion overstates the evidence."
    approved_item = RevisionItem(
        severity="HIGH",
        scope="conclusion-p001",
        issue=target_issue,
        suggestion="Downgrade the claim until the evidence supports it.",
    )
    critiques = [
        _polish_critique(
            compliance=10.0,
            novelty=10.0,
            completeness=10.0,
            needs_revision=True,
            items=[approved_item],
            score_clipped=True,
        ),
        _polish_critique(
            compliance=10.0,
            novelty=10.0,
            completeness=10.0,
            needs_revision=True,
            items=[approved_item],
            score_clipped=True,
        ),
        _polish_critique(
            compliance=10.0,
            novelty=10.0,
            completeness=10.0,
            needs_revision=True,
            items=[approved_item],
            score_clipped=True,
        ),
    ]
    rewrite_calls: list[int] = []

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    def fake_baseline(_run_dir: Path) -> tuple[str, str]:
        return (
            "A separate manuscript about agricultural credit and rural institutions.",
            "real",
        )

    def fake_critique(**_kwargs: object) -> ExpertCritiqueOutput:
        return critiques.pop(0)

    def fake_polish_rewrite(**_kwargs: object) -> str:
        rewrite_calls.append(1)
        return "# Paper\n\nTherefore, argument about banking stress remains overstrong [1].\n"

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(final_rewrite_module, "_controlled_polish_baseline_text", fake_baseline)
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        fake_critique,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fake_polish_rewrite,
    )

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    loop = audit["controlled_polish_loop"]
    assert result["state"] == "CRITIC_RUNNING"
    assert loop["status"] == "stopped_no_score_gain"
    assert loop["exit_reason"] == "stopped_no_score_gain"
    assert loop["accepted_rewrites"] == 2
    assert loop["approved_blocker_high_remaining_count"] == 1
    assert loop["final_score_clipped"] is True
    assert rewrite_calls == [1, 1]
    target_decisions = [
        decision
        for attempt in loop["attempts"]
        for decision in attempt["exit_decisions"]
        if decision["condition"] == "target_score_reached"
    ]
    assert target_decisions
    assert all(decision["scores_all_at_least"] is True for decision in target_decisions)
    assert all(decision["score_clipped"] is True for decision in target_decisions)
    assert all(decision["allowed"] is False for decision in target_decisions)


def test_controlled_polish_validator_uses_root_original_citation_multiset(
    app_session: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_polish_root_original",
    )
    drifted = "# Paper\n\nArgument about banking stress [1][1].\n"
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        project = session.get(final_rewrite_module.Project, run.project_id)
        assert project is not None
        settings = get_settings()
        validation = final_rewrite_module._validate_controlled_polish_candidate(
            candidate={"manuscript": drifted, "claim_map": [_rewrite_claim()]},
            incumbent={"manuscript": drifted, "claim_map": [_rewrite_claim()]},
            root_original={"manuscript": _STYLED_MANUSCRIPT, "claim_map": [_rewrite_claim()]},
            settings=settings,
            run_dir=run_dir,
            project=project,
            session=session,
            baseline_md=("A separate manuscript about agricultural credit and rural institutions."),
            policies=final_rewrite_module.EvidencePolicies.from_settings("final", settings),
        )

    assert not validation.passed
    assert "citation_multiset_mismatch" in validation.reasons
    assert any(detail.get("basis") == "root_original" for detail in validation.details)


def test_controlled_polish_validator_uses_incumbent_paragraph_shape(
    app_session: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_polish_incumbent_shape",
    )
    candidate = "# Paper\n\nArgument about banking stress [1].\n\nExtra paragraph.\n"
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        project = session.get(final_rewrite_module.Project, run.project_id)
        assert project is not None
        settings = get_settings()
        validation = final_rewrite_module._validate_controlled_polish_candidate(
            candidate={"manuscript": candidate, "claim_map": [_rewrite_claim()]},
            incumbent={"manuscript": _STYLED_MANUSCRIPT, "claim_map": [_rewrite_claim()]},
            root_original={"manuscript": _STYLED_MANUSCRIPT, "claim_map": [_rewrite_claim()]},
            settings=settings,
            run_dir=run_dir,
            project=project,
            session=session,
            baseline_md=("A separate manuscript about agricultural credit and rural institutions."),
            policies=final_rewrite_module.EvidencePolicies.from_settings("final", settings),
        )

    assert not validation.passed
    assert "paragraph_count_changed" in validation.reasons
    assert any(detail.get("basis") == "incumbent" for detail in validation.details)


def test_controlled_polish_strict_better_requires_quality_gain() -> None:
    incumbent = QualityScoreSet(compliance=7.0, novelty=7.0, completeness=7.0)
    equal = QualityScoreSet(compliance=7.0, novelty=7.0, completeness=7.0)
    novelty_only = QualityScoreSet(compliance=7.0, novelty=8.0, completeness=7.0)
    completeness_gain = QualityScoreSet(compliance=7.0, novelty=7.0, completeness=8.0)
    compliance_regression = QualityScoreSet(compliance=6.0, novelty=8.0, completeness=8.0)

    assert not final_rewrite_module._scores_strictly_better(equal, incumbent)
    assert not final_rewrite_module._scores_strictly_better(novelty_only, incumbent)
    assert final_rewrite_module._scores_strictly_better(completeness_gain, incumbent)
    assert not final_rewrite_module._scores_strictly_better(compliance_regression, incumbent)


def test_controlled_polish_critic_prompt_requires_one_shot_review(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_polish_critic_prompt",
    )
    captured: dict[str, object] = {}

    async def fake_run_llm_step(**kwargs: object) -> object:
        request = kwargs["request"]
        captured["system"] = request.messages[0]["content"]  # type: ignore[attr-defined,index]
        captured["user"] = request.messages[1]["content"]  # type: ignore[attr-defined,index]

        class Response:
            parsed = _polish_critique(
                compliance=8.0,
                novelty=8.0,
                completeness=8.0,
                needs_revision=False,
                items=[],
            )

        return Response()

    monkeypatch.setattr(final_rewrite_module, "run_llm_step", fake_run_llm_step)
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        project = session.get(final_rewrite_module.Project, run.project_id)
        assert project is not None
        result = final_rewrite_module._controlled_polish_critique_via_harness(
            manuscript=_STYLED_MANUSCRIPT,
            run=run,
            project=project,
            session=session,
            rewrite_version="v001",
            attempt=0,
            stage="incumbent",
        )

    assert result is not None
    system = str(captured["system"])
    user = str(captured["user"])
    assert "顶级期刊处理编辑、资深审稿人和方法论专家" in system
    assert "本次审查是唯一一次正式提出修改意见的机会" in system
    assert "initial_review_omission" in system
    assert "anchor_map" in system
    assert "frozen_issue_registry" in system
    assert "BLOCKER：不修改则无法作为严肃学术论文投稿" in system
    assert "review_goal: 一次性、完整、不可追加" in user
    assert "{{project_title}}" not in user
    assert "{{manuscript}}" not in user


def test_v2_expert_critique_tolerates_partial_optional_schema() -> None:
    parsed = ExpertCritiqueOutput.parse_obj(
        {
            "needs_revision": True,
            "scores": {
                "compliance": 6.0,
                "novelty": 7.0,
                "completeness": 6.5,
                "top_journal_fit": 4.0,
            },
            "revision_items": [
                {
                    "severity": "HIGH",
                    "scope": "S01_P01",
                    "issue": "研究问题未收束。",
                    "acceptance_test": "引言明确给出可检验问题。",
                }
            ],
        }
    )

    assert parsed.needs_revision is True
    assert parsed.scores.top_journal_fit == 4.0
    assert parsed.revision_items[0]["acceptance_test"] == "引言明确给出可检验问题。"
    assert "top_journal_readiness" in parsed.schema_partial_fields
    assert parsed.missing_evidence_map == []
    assert parsed.deletion_or_compression_plan == []


def test_controlled_polish_handoff_packages_v2_compliance_fields(
    app_session: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    run_id, _run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_polish_handoff",
    )
    critique = ExpertCritiqueOutput.parse_obj(
        {
            "needs_revision": True,
            "top_journal_readiness": "NOT_READY",
            "editorial_decision_if_submitted_now": "DESK_REJECT",
            "scores": {"compliance": 5.0, "novelty": 6.0, "completeness": 5.5},
            "revision_items": [],
            "missing_evidence_map": [{"claim": "核心断言", "scope": "S02_P01"}],
            "required_analyses_or_materials": [{"analysis_name": "材料互证"}],
            "required_tables_figures_formulas": [{"item_type": "TABLE"}],
            "literature_revision_plan": [{"scope": "MISSING_IN:文献综述"}],
        }
    )

    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        project = session.get(final_rewrite_module.Project, run.project_id)
        assert project is not None
        payload = final_rewrite_module._controlled_polish_handoff_payload(
            critique,
            project=project,
            rewrite_version="v001",
        )

    assert payload["source"] == "initial_v2_polish_critic"
    assert payload["top_journal_readiness"] == "NOT_READY"
    assert payload["missing_evidence_map"] == [{"claim": "核心断言", "scope": "S02_P01"}]
    assert payload["required_analyses_or_materials"] == [{"analysis_name": "材料互证"}]
    assert payload["required_tables_figures_formulas"] == [{"item_type": "TABLE"}]
    assert payload["literature_revision_plan"] == [{"scope": "MISSING_IN:文献综述"}]


def test_literal_polish_rewrite_prompt_uses_critic_acceptance_contract(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_polish_prompt")
    captured: dict[str, object] = {}

    async def fake_run_llm_step(**kwargs: object) -> object:
        request = kwargs["request"]
        captured["system"] = request.messages[0]["content"]  # type: ignore[attr-defined,index]
        captured["user"] = request.messages[1]["content"]  # type: ignore[attr-defined,index]

        class Response:
            parsed = final_rewrite_module.ControlledPolishRewriteOutput(
                manuscript=_STYLED_MANUSCRIPT,
            )

        return Response()

    monkeypatch.setattr(final_rewrite_module, "run_llm_step", fake_run_llm_step)
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        project = session.get(final_rewrite_module.Project, run.project_id)
        assert project is not None
        settings = get_settings()
        result = final_rewrite_module._controlled_polish_rewrite_via_harness(
            manuscript=_STYLED_MANUSCRIPT,
            critique=_polish_critique(
                compliance=6.0,
                novelty=7.0,
                completeness=6.0,
                needs_revision=True,
                items=[
                    RevisionItem(
                        severity="HIGH",
                        scope="body-p001",
                        issue="Needs sharper transition.",
                        suggestion="Tighten the sentence without adding facts.",
                        acceptance_test="The paragraph uses one transition and adds no facts.",
                    ),
                ],
            ),
            target_items=[
                RevisionItem(
                    severity="HIGH",
                    scope="body-p001",
                    issue="Needs sharper transition.",
                    suggestion="Tighten the sentence without adding facts.",
                    acceptance_test="The paragraph uses one transition and adds no facts.",
                )
            ],
            run=run,
            project=project,
            session=session,
            hooks=final_rewrite_module.HookRegistry(),
            rewrite_version="v001",
            attempt=1,
            policies=final_rewrite_module.EvidencePolicies.from_settings("final", settings),
        )

    assert result == _STYLED_MANUSCRIPT.strip()
    system = str(captured["system"])
    user = str(captured["user"])
    assert "controlled-polish editor" in system
    assert "root_original_95pct_min_chars" not in system
    assert "专家修改执行 prompt" in user
    assert "acceptance_contract" in user
    assert "deletion_or_compression_plan" in user
    assert "acceptance_test" in user
    assert "revision_items_are_audit_only" in user
    assert "scores_must_be_monotonic_vs_incumbent" in user
    assert "revision_items 只进入 audit，不参与接受判定" in user
    assert "连续两轮没有任何维度增长" in user
    assert "incumbent_paragraph_count" in user
    assert "hard_validator" not in user
    assert "出版年份" in user
    assert "因果断言" in user


def test_first_attempt_vendor_error_falls_back_to_original_and_continues(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_vendor_fallback")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("provider returned 503")

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        events = _events(session, run_id)

    rewrite_dir = run_dir / "rewrite" / "v001"
    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((rewrite_dir / "audit.json").read_text(encoding="utf-8"))
    assert result["state"] == "CRITIC_RUNNING"
    assert run.state == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == _STYLED_MANUSCRIPT
    assert artifact.claim_map == [_CLAIM]
    assert audit["compliance"]["failed"] is False
    assert audit["fallback_to_original"] is True
    assert audit["fallback_reason"] == "llm_error:RuntimeError"
    assert audit["llm_error"]["message"] == "provider returned 503"
    assert any(event.event_type == "rewrite_policy_fallback" for event in events)
    assert any(event.event_type == "rewrite_completed" for event in events)
    assert not (rewrite_dir / "rejected_manuscript.md").exists()


def test_holistic_paragraph_count_change_falls_back_to_original(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_holistic_para")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": (
                "# Paper\n\nTherefore, argument about banking stress [1].\n\n"
                "Extra seam paragraph.\n"
            ),
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    _assert_policy_fallback(run_dir, result, "holistic_paragraph_count_changed")


def test_holistic_citation_order_change_falls_back_to_original(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_holistic_order")
    _add_second_source(run_dir)

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": "# Paper\n\nTherefore, argument about banking stress [2][1].\n",
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    _assert_policy_fallback(
        run_dir,
        result,
        "holistic_cite_marker_sequence_changed",
        expected_manuscript="# Paper\n\nArgument about banking stress [1][2].\n",
        expected_claim_map=[
            {
                **_CLAIM,
                "source_ids": ["src1", "src2"],
            }
        ],
    )


def test_cite_marker_multiset_change_falls_back_to_original(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_cites")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": _REWRITTEN_MANUSCRIPT.replace("[1]", "[1] [1]"),
            "claim_map": [_rewrite_claim()],
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    _assert_policy_fallback(run_dir, result, "cite_marker_multiset_change")


def test_cite_marker_multiset_retry_can_recover(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_cites_retry")
    calls = 0

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        manuscript = (
            _REWRITTEN_MANUSCRIPT.replace("[1]", "[1] [1]") if calls == 1 else _REWRITTEN_MANUSCRIPT
        )
        return {
            "manuscript": manuscript,
            "claim_map": [_rewrite_claim()],
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    rewrite_dir = run_dir / "rewrite" / "v001"
    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((rewrite_dir / "audit.json").read_text(encoding="utf-8"))
    assert calls == 2
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == _REWRITTEN_MANUSCRIPT
    assert audit["compliance"]["failed"] is False
    assert audit["citation_multiset_retry"]["attempted"] is True
    assert audit["citation_multiset_retry"]["retry_compliance_failed"] is False
    assert not (rewrite_dir / "rejected_manuscript.md").exists()


def test_cite_marker_multiset_retry_vendor_exception_falls_back(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_cites_retry_vendor")
    calls = 0

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "manuscript": _REWRITTEN_MANUSCRIPT.replace("[1]", "[1] [1]"),
                "claim_map": [_rewrite_claim()],
            }
        raise ValueError("provider rejected corrective retry")

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    rewrite_dir = run_dir / "rewrite" / "v001"
    audit = json.loads((rewrite_dir / "audit.json").read_text(encoding="utf-8"))
    _assert_policy_fallback(run_dir, result, "cite_marker_multiset_change")
    assert calls == 2
    assert audit["citation_multiset_retry"]["attempted"] is True
    assert audit["citation_multiset_retry"]["retry_error"] == "ValueError"


def test_unresolved_numeric_marker_is_pre_repaired_before_compliance(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_cite_pre_repair")
    styled_path = run_dir / "drafts" / "v001" / "style" / "paper_styled.md"
    styled_path.write_text("# Paper\n\nArgument about banking stress [2].\n", encoding="utf-8")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": "# Paper\n\nTherefore, argument about banking stress [2].\n",
            "claim_map": [_rewrite_claim()],
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    rewrite_dir = run_dir / "rewrite" / "v001"
    audit = json.loads((rewrite_dir / "audit.json").read_text(encoding="utf-8"))
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert "Therefore, argument about banking stress [1]." in artifact.manuscript
    assert "[2]" not in artifact.manuscript
    assert audit["compliance"]["failed"] is False
    assert audit["citation_pre_repair"]["original"]["changed"] is True
    assert audit["citation_pre_repair"]["rewritten"]["changed"] is True
    assert not (rewrite_dir / "rejected_manuscript.md").exists()


def test_escaped_markdown_newlines_are_normalized_before_citation_pre_repair(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_escaped_newline_pre_repair",
    )
    styled_path = run_dir / "drafts" / "v001" / "style" / "paper_styled.md"
    styled_path.write_text("# Paper\\n\\nArgument about banking stress [2].\\n", encoding="utf-8")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": "# Paper\\n\\nTherefore, argument about banking stress [2].\\n",
            "claim_map": [_rewrite_claim()],
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    rewrite_dir = run_dir / "rewrite" / "v001"
    audit = json.loads((rewrite_dir / "audit.json").read_text(encoding="utf-8"))
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert "\\n" not in artifact.manuscript
    assert "\n\n" in artifact.manuscript
    assert "Therefore, argument about banking stress [1]." in artifact.manuscript
    assert "[2]" not in artifact.manuscript
    assert audit["compliance"]["failed"] is False
    assert audit["citation_pre_repair"]["original"]["markdown_newlines_normalized"] is True
    assert audit["citation_pre_repair"]["rewritten"]["markdown_newlines_normalized"] is True
    assert audit["citation_pre_repair"]["original"]["changed"] is True
    assert audit["citation_pre_repair"]["rewritten"]["changed"] is True


def test_parenthesized_source_id_is_pre_repaired_before_compliance(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_raw_source_repair")
    source_id = "crossref:10.1525/9780520921474"
    draft_dir = run_dir / "drafts" / "v001"
    (draft_dir / "style" / "paper_styled.md").write_text(
        "# Paper\n\nArgument about banking stress（crossref:10.1525/9780520921474）。\n",
        encoding="utf-8",
    )
    claim = _rewrite_claim()
    claim["source_ids"] = [source_id]
    (draft_dir / "claim_map.jsonl").write_text(
        json.dumps(claim, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (draft_dir / "draft_metadata.json").write_text(
        json.dumps({"cited_sources": [source_id]}, sort_keys=True),
        encoding="utf-8",
    )
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps([_source(source_id)], sort_keys=True),
        encoding="utf-8",
    )

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": (
                "# Paper\n\nTherefore, argument about banking stress"
                "（crossref:10.1525/9780520921474）。\n"
            ),
            "claim_map": [claim],
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    rewrite_dir = run_dir / "rewrite" / "v001"
    audit = json.loads((rewrite_dir / "audit.json").read_text(encoding="utf-8"))
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert "Therefore, argument about banking stress[1]。" in artifact.manuscript
    assert "crossref:10.1525/9780520921474" not in artifact.manuscript
    assert audit["compliance"]["failed"] is False
    assert audit["citation_pre_repair"]["original"]["changed"] is True
    assert audit["citation_pre_repair"]["rewritten"]["changed"] is True


def test_insufficient_material_diagnostic_calibrates_definitive_node_claim(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_material_scope")
    draft_dir = run_dir / "drafts" / "v001"
    styled_path = draft_dir / "style" / "paper_styled.md"
    styled_path.write_text(
        "# Paper\n\n1968年应被视为功能性失效节点[1]。\n",
        encoding="utf-8",
    )
    claim = _rewrite_claim()
    claim["claim_text"] = "1968年应被视为功能性失效节点。"
    (draft_dir / "claim_map.jsonl").write_text(
        json.dumps(claim, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "synthesis" / "material_diagnostic.json").write_text(
        json.dumps(
            {
                "sufficient": False,
                "recommended_action": "iterate",
                "missing_materials": ["IMF 内部备忘录", "伦敦黄金池季度结算记录"],
                "risks": ["当前材料不足以锁定唯一失效节点。"],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": "# Paper\n\n1968年应被视为功能性失效节点[1]。\n",
            "claim_map": [claim],
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    rewrite_dir = run_dir / "rewrite" / "v001"
    audit = json.loads((rewrite_dir / "audit.json").read_text(encoding="utf-8"))
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert "1968年可作为高置信度的功能性失效候选节点[1]" in artifact.manuscript
    assert "应被视为功能性失效节点" not in artifact.manuscript
    assert artifact.claim_map[0]["claim_text"] == "1968年可作为高置信度的功能性失效候选节点。"
    assert audit["material_scope_calibration"]["changed"] is True
    assert audit["compliance"]["failed"] is False


def test_claim_count_explosion_falls_back_to_original(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_HOLISTIC", "0")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_claim_count")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": _REWRITTEN_MANUSCRIPT,
            "claim_map": [
                _rewrite_claim("p001"),
                _rewrite_claim("p002"),
            ],
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    _assert_policy_fallback(run_dir, result, "claim_count_explosion")


def test_policy_fallback_writes_pre_repaired_original_markers(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_HOLISTIC", "0")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_fallback_repaired")
    styled_path = run_dir / "drafts" / "v001" / "style" / "paper_styled.md"
    styled_path.write_text("# Paper\n\nArgument about banking stress [2].\n", encoding="utf-8")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {
            "manuscript": "# Paper\n\nTherefore, argument about banking stress [2].\n",
            "claim_map": [
                _rewrite_claim("p001"),
                _rewrite_claim("p002"),
            ],
        }

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert "Argument about banking stress [1]." in artifact.manuscript
    assert "[2]" not in artifact.manuscript
    assert audit["fallback_to_original"] is True
    assert audit["fallback_reason"] == "claim_count_explosion"
    assert audit["citation_pre_repair"]["original"]["changed"] is True


def test_claim_grounding_failure_source_not_in_shortlist_falls_back_to_original(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_HOLISTIC", "0")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_grounding")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        claim = _rewrite_claim()
        claim["source_ids"] = ["missing_source"]
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [claim]}

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    _assert_policy_fallback(run_dir, result, "claim_grounding_failed")


def test_evidence_whitelist_failure_new_conclusion_year_falls_back_to_original(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_whitelist")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        manuscript = "# Paper\n\nA new 1999 rupture followed [1].\n"
        claim = _rewrite_claim("conclusion-p001")
        claim["section_id"] = "conclusion"
        claim["claim_text"] = "A new 1999 rupture followed."
        return {"manuscript": manuscript, "claim_map": [claim]}

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    _assert_policy_fallback(run_dir, result, "evidence_whitelist_failed")


def test_llm_timeout_falls_back_to_original_and_continues(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_timeout")

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        raise TimeoutError("gateway timed out")

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)

    with app_session() as session:
        result = run_final_rewrite(run_id, session)

    artifact = load_latest_rewrite_artifact(run_dir)
    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == _STYLED_MANUSCRIPT
    assert audit["fallback_to_original"] is True
    assert audit["fallback_reason"] == "llm_error:TimeoutError"


def test_final_rewrite_disabled_start_critic_skips_rewrite(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_ENABLED", "0")
    monkeypatch.setenv("AUTOESSAY_CRITIC_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_POLISH_LOOP_ENABLED", "0")
    monkeypatch.setenv("AUTOESSAY_SYNC_WORKER", "1")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_disabled")

    with app_session() as session:
        response = start_critic(run_id, session)
        run = session.get(Run, run_id)

    assert response.expected_state == "CRITIC_RUNNING"
    assert run is not None
    assert run.state == "USER_EXTERNAL_SCAN_APPROVAL"
    assert not (run_dir / "rewrite").exists()


def test_final_rewrite_then_critic_transfers_lock_to_critic(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_STUB", "1")
    monkeypatch.setenv("AUTOESSAY_NORTH_STAR_GATE_ENABLED", "0")
    monkeypatch.setenv("AUTOESSAY_POLISH_LOOP_ENABLED", "0")
    get_settings.cache_clear()
    run_id, _run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_lock_handoff")
    token = "lock_final_to_critic"
    observed_lock: dict[str, str | None] = {}

    def fake_critic_via_harness(**_kwargs: object) -> list[critic_module.CriticIssue]:
        with app_session() as inspect_session:
            run = inspect_session.get(Run, run_id)
            assert run is not None
            observed_lock["phase"] = run.active_phase_lock
            observed_lock["job_id"] = run.active_phase_lock_job_id
        return []

    monkeypatch.setattr(critic_module, "_critic_via_harness", fake_critic_via_harness)

    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.active_phase_lock = "final_rewrite"
        run.active_phase_lock_job_id = token
        run.active_phase_lock_claimed_at = utcnow()
        session.commit()
        result = run_final_rewrite_then_critic(run_id, session, lock_token=token)
        session.refresh(run)
        final_lock = run.active_phase_lock
        final_lock_job_id = run.active_phase_lock_job_id

    assert observed_lock == {"phase": "critic", "job_id": token}
    assert result["state"] == "USER_EXTERNAL_SCAN_APPROVAL"
    assert final_lock is None
    assert final_lock_job_id is None


def test_versioned_artifacts_increment_on_retry(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_STUB", "1")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_versions")

    with app_session() as session:
        run_final_rewrite(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        run.state = "USER_REVISION_REVIEW"
        session.commit()
        run_final_rewrite(run_id, session)

    for version in ("v001", "v002"):
        version_dir = run_dir / "rewrite" / version
        assert (version_dir / "manuscript.md").is_file()
        assert (version_dir / "claim_map.json").is_file()
        assert (version_dir / "audit.json").is_file()
        assert (version_dir / "diff.txt").is_file()


def test_rewrite_summary_metadata_is_available(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_STUB", "1")
    get_settings.cache_clear()
    run_id, _run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_summary")

    with app_session() as session:
        run_final_rewrite(run_id, session)
        run = session.get(Run, run_id)
        assert run is not None
        summary = rewrite_summary_for_run(run)
        latest = _events(session, run_id)[-1]

    assert summary is not None
    assert summary["rewrite_version"] == "v001"
    assert summary["rewrite_audit_path"] == "rewrite/v001/audit.json"
    assert "rewrite_summary" in json.loads(latest.payload)


def test_critic_reads_rewrite_artifact_instead_of_stylist(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_POLISH_LOOP_ENABLED", "0")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_critic_reads")
    _write_rewrite_artifact(run_dir, manuscript="# Rewrite\n\nRewrite text [1].\n")
    captured: dict[str, str] = {}

    def fake_critic_via_harness(**kwargs: object) -> list[critic_module.CriticIssue]:
        captured["draft"] = str(kwargs["draft"])
        return []

    monkeypatch.setattr(critic_module, "_critic_via_harness", fake_critic_via_harness)

    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        run.state = "CRITIC_RUNNING"
        session.commit()
        result = run_critic(run_id, session)

    assert result["state"] == "USER_EXTERNAL_SCAN_APPROVAL"
    assert captured["draft"].startswith("# Rewrite")


def test_downstream_critic_blocker_falls_back_to_stylist_and_reruns_critic(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_POLISH_LOOP_ENABLED", "0")
    monkeypatch.setenv("AUTOESSAY_FINAL_REWRITE_HOLISTIC", "1")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_critic_fallback")
    rejected_manuscript = "# Paper\n\nBad rewrite text [1].\n"
    seen_drafts: list[str] = []

    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": rejected_manuscript, "claim_map": [_rewrite_claim()]}

    def fake_critic_via_harness(**kwargs: object) -> list[critic_module.CriticIssue]:
        draft = str(kwargs["draft"])
        seen_drafts.append(draft)
        if "Bad rewrite" in draft:
            return [
                critic_module.CriticIssue(
                    issue_id="rewrite_blocker_001",
                    severity="BLOCKER",
                    dimension="evidence",
                    paragraph_id="body-p001",
                    source_ids=["src1"],
                    description="Rewritten manuscript failed downstream review.",
                    suggested_action="REWRITE",
                ),
            ]
        return []

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(critic_module, "_critic_via_harness", fake_critic_via_harness)

    with app_session() as session:
        result = run_final_rewrite_then_critic(run_id, session)
        events = _events(session, run_id)

    latest = load_latest_rewrite_artifact(run_dir)
    first_audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text())
    fallback_audit = json.loads((run_dir / "rewrite" / "v002" / "audit.json").read_text())
    blocking = json.loads((run_dir / "reviews" / "blocking_issues.json").read_text())

    assert result["state"] == "USER_EXTERNAL_SCAN_APPROVAL"
    assert seen_drafts == [rejected_manuscript, _STYLED_MANUSCRIPT]
    assert latest is not None
    assert latest.version == "v002"
    assert latest.manuscript == _STYLED_MANUSCRIPT
    assert first_audit.get("fallback_to_original") is not True
    assert fallback_audit["fallback_to_original"] is True
    assert fallback_audit["fallback_reason"] == "downstream_critic_blockers"
    assert fallback_audit["downstream_rejected_rewrite_version"] == "v001"
    assert fallback_audit["downstream_blockers"][0]["issue_id"] == "rewrite_blocker_001"
    assert (run_dir / "rewrite" / "v002" / "rejected_manuscript.md").read_text(
        encoding="utf-8"
    ) == rejected_manuscript
    assert (run_dir / "reviews" / "critic_v001.md").is_file()
    assert (run_dir / "reviews" / "critic_v002.md").is_file()
    assert blocking["issues"] == []
    assert any(event.event_type == "downstream_rewrite_fallback" for event in events)


def test_integrity_reads_rewrite_artifact_instead_of_stylist(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_INTEGRITY_STUB", "1")
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_integrity_reads",
        state="USER_EXTERNAL_SCAN_APPROVAL",
    )
    _write_rewrite_artifact(run_dir, manuscript="# Rewrite\n\nRewrite text [1].\n")

    with app_session() as session:
        session.add(
            Checkpoint(
                id="checkpoint_integrity_reads",
                run_id=run_id,
                checkpoint_type="USER_EXTERNAL_SCAN_APPROVAL",
                status="ACCEPTED",
                decision_payload=json.dumps({"approve": True, "scan_kinds": ["plagiarism"]}),
                decided_at=utcnow(),
            ),
        )
        session.commit()
        result = run_integrity(run_id, session)

    summary = json.loads((run_dir / "integrity" / "integrity_summary.json").read_text())
    assert result["state"] == "USER_INTEGRITY_REVIEW"
    assert summary["manuscript_source"] == "rewrite"
    assert summary["rewrite_version"] == "v001"


def test_state_machine_allows_rewrite_edges() -> None:
    assert "REWRITE_RUNNING" in ALLOWED_TRANSITIONS["USER_REVISION_REVIEW"]
    assert "CRITIC_RUNNING" in ALLOWED_TRANSITIONS["REWRITE_RUNNING"]
    assert "FAILED_POLICY" in ALLOWED_TRANSITIONS["REWRITE_RUNNING"]


def _assert_policy_fallback(
    run_dir: Path,
    result: dict[str, object],
    reason: str,
    *,
    expected_manuscript: str | None = None,
    expected_claim_map: list[dict[str, object]] | None = None,
) -> None:
    rewrite_dir = run_dir / "rewrite" / "v001"
    audit = json.loads((rewrite_dir / "audit.json").read_text(encoding="utf-8"))
    artifact = load_latest_rewrite_artifact(run_dir)

    assert result["state"] == "CRITIC_RUNNING"
    assert artifact is not None
    assert artifact.manuscript == (expected_manuscript or _STYLED_MANUSCRIPT)
    assert artifact.claim_map == (expected_claim_map or [_CLAIM])
    assert audit["compliance"]["failed"] is True
    assert audit["compliance"]["reason"] == reason
    assert audit["fallback_to_original"] is True
    assert audit["fallback_reason"] == reason
    assert (rewrite_dir / "rejected_manuscript.md").is_file()
    assert (rewrite_dir / "rejected_claim_map.json").is_file()


_STYLED_MANUSCRIPT = "# Paper\n\nArgument about banking stress [1].\n"
_REWRITTEN_MANUSCRIPT = "# Paper\n\nTherefore, argument about banking stress [1].\n"
_CLAIM = {
    "section_id": "body",
    "paragraph_id": "body-p001",
    "claim_text": "Argument about banking stress.",
    "source_ids": ["src1"],
    "uncited": False,
}


def _rewrite_claim(paragraph_id: str = "body-p001") -> dict[str, object]:
    return {
        "section_id": "body",
        "paragraph_id": paragraph_id,
        "claim_text": "Argument about banking stress.",
        "source_ids": ["src1"],
        "evidence_status": "source_bound",
    }


def _seed_rewrite_ready_run(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    run_id: str,
    *,
    state: str = "USER_REVISION_REVIEW",
    mathematical_mode: bool = False,
) -> tuple[str, Path]:
    run_dir = create_run_directory(
        tmp_path / "runs",
        run_id,
        "proj_test",
        state=state,
        domain_id="financial_history",
    )
    draft_dir = run_dir / "drafts" / "v001"
    style_dir = draft_dir / "style"
    style_dir.mkdir(parents=True, exist_ok=True)
    (style_dir / "paper_styled.md").write_text(_STYLED_MANUSCRIPT, encoding="utf-8")
    (draft_dir / "claim_map.jsonl").write_text(
        json.dumps(_CLAIM, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (draft_dir / "draft_metadata.json").write_text(
        json.dumps({"cited_sources": ["src1"]}, sort_keys=True),
        encoding="utf-8",
    )
    (draft_dir / "citations.bib").write_text("@article{src1,title={Source}}\n", encoding="utf-8")
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    (sources_dir / "shortlist.json").write_text(
        json.dumps([_source("src1")], sort_keys=True),
        encoding="utf-8",
    )
    (run_dir / "synthesis" / "source_notes").mkdir(parents=True, exist_ok=True)
    (run_dir / "synthesis" / "claims.jsonl").write_text("", encoding="utf-8")
    (run_dir / "novelty").mkdir(parents=True, exist_ok=True)
    (run_dir / "novelty" / "selected_thesis.json").write_text("{}", encoding="utf-8")
    with app_session() as session:
        project = seed_project(session)
        project.title = "Banking stress"
        project.language = "en"
        session.add(project)
        session.add(
            Run(
                id=run_id,
                project_id=project.id,
                domain_version="0.1.0",
                run_dir=str(run_dir),
                state=state,
                baseline_hash="test",
                mathematical_mode=mathematical_mode,
            ),
        )
        session.commit()
    return run_id, run_dir


def _source(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "title": "Banking Stress Source",
        "authors": ["Smith"],
        "year": 1933,
        "venue": "Journal",
        "doi": "10.1234/test",
        "url": "https://example.com/source",
        "pdf_url": None,
        "abstract": "Banking stress evidence.",
        "source_client": "test",
        "access_status": "open",
        "license": None,
        "risk_flags": [],
    }


def _add_second_source(run_dir: Path) -> None:
    draft_dir = run_dir / "drafts" / "v001"
    style_dir = draft_dir / "style"
    (style_dir / "paper_styled.md").write_text(
        "# Paper\n\nArgument about banking stress [1][2].\n",
        encoding="utf-8",
    )
    claim = dict(_CLAIM)
    claim["source_ids"] = ["src1", "src2"]
    (draft_dir / "claim_map.jsonl").write_text(
        json.dumps(claim, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (draft_dir / "draft_metadata.json").write_text(
        json.dumps({"cited_sources": ["src1", "src2"]}, sort_keys=True),
        encoding="utf-8",
    )
    (run_dir / "sources" / "shortlist.json").write_text(
        json.dumps([_source("src1"), _source("src2")], sort_keys=True),
        encoding="utf-8",
    )


def _write_rewrite_artifact(run_dir: Path, *, manuscript: str) -> None:
    rewrite_dir = run_dir / "rewrite" / "v001"
    rewrite_dir.mkdir(parents=True, exist_ok=True)
    (rewrite_dir / "manuscript.md").write_text(manuscript, encoding="utf-8")
    (rewrite_dir / "claim_map.json").write_text(
        json.dumps([_rewrite_claim()], sort_keys=True),
        encoding="utf-8",
    )
    (rewrite_dir / "audit.json").write_text(
        json.dumps(
            {
                "rewrite_diff_summary": {
                    "paragraphs_reordered": 0,
                    "transitions_added": 0,
                    "claims_consolidated": 0,
                    "claim_map_count": 1,
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (rewrite_dir / "diff.txt").write_text("", encoding="utf-8")


def _polish_critique(
    *,
    compliance: float,
    novelty: float,
    completeness: float,
    needs_revision: bool,
    items: list[RevisionItem],
    score_clipped: bool = False,
) -> ExpertCritiqueOutput:
    return ExpertCritiqueOutput(
        scores=QualityScoreSet(
            compliance=compliance,
            novelty=novelty,
            completeness=completeness,
            score_clipped=score_clipped,
        ),
        value_assessment="Test assessment.",
        revision_items=items,
        needs_revision=needs_revision,
    )


def _events(session: Session, run_id: str) -> list[RunEvent]:
    return list(
        session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id)
            .order_by(RunEvent.created_at, RunEvent.id),
        )
    )
