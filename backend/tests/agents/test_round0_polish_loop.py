"""Integration tests for polish-loop round-0 A→B→C flow.

Round 0 v2 (2026-05-12 redesign after canary):
- Stage A = incumbent manuscript entering polish loop
- Stage B = open-prompt foundation-model rewrite via gpt-5.5 (no V2 system,
  no JSON, no citation hard constraint). Implemented in
  ``_controlled_polish_holistic_round0_open_prompt``.
- Stage C = pipeline's existing ``_controlled_polish_rewrite_via_harness``
  applied to stage B with the V2 incumbent critique as scope. Re-anchors
  stage B in pipeline's source pool / claim_map / citation rules so the
  structured iter 1+ can keep running.
- Unconditional accept of stage C (user's "整体轮无条件接受"), then re-critique
  on stage C so iter 1 sees a fresh V2 critique.

Failure paths exercised here:
- stage B returns None → ``stage_b_open_prompt_failed_skipped``
- stage C returns None → ``stage_c_rewrite_failed_reverted`` (incumbent stays
  at stage A; for loop runs on original)
- stage C succeeds + re-critique fails →
  ``succeeded_but_recritique_failed_using_pre_critique``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from test_final_rewrite import (
    _REWRITTEN_MANUSCRIPT,
    _polish_critique,
    _rewrite_claim,
    _seed_rewrite_ready_run,
)

from autoessay.agents import final_rewrite as final_rewrite_module
from autoessay.agents._critic_polish_loop import ExpertCritiqueOutput, RevisionItem
from autoessay.agents.final_rewrite import run_final_rewrite
from autoessay.config import get_settings

_STAGE_B_OUTPUT = (
    "# Paper\n\n"
    "（round-0 stage B — open-prompt foundation model rewrite）\n"
    "Therefore, argument about banking stress remains precise [1].\n\n"
    "本节由 gpt-5.5 整体改写产生，作为 stage C 输入。\n"
)

_STAGE_C_OUTPUT = (
    "# Paper\n\n"
    "（round-0 stage C — pipeline rewriter applied to stage B）\n"
    "Argument about banking stress, now polished by pipeline V2 rewriter [1].\n"
)


def _baseline_returns_real(_run_dir: Path) -> tuple[str, str]:
    return (
        "A separate manuscript about agricultural credit and rural institutions.",
        "real",
    )


def _setup_common_monkeypatches(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_rewrite(**_kwargs: object) -> dict[str, object]:
        return {"manuscript": _REWRITTEN_MANUSCRIPT, "claim_map": [_rewrite_claim()]}

    monkeypatch.setattr(final_rewrite_module, "_final_rewrite_via_harness", fake_rewrite)
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_baseline_text",
        _baseline_returns_real,
    )


def test_round0_flag_off_records_skipped_disabled(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTOESSAY_POLISH_HOLISTIC_ROUND0", raising=False)
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(app_session, tmp_path, "run_r0v2_off")
    _setup_common_monkeypatches(monkeypatch)

    def fail_if_called(**_kwargs: object) -> None:
        raise AssertionError("open-prompt helper must not run when flag is OFF")

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_holistic_round0_open_prompt",
        fail_if_called,
    )

    critiques = [
        _polish_critique(
            compliance=9.5,
            novelty=9.5,
            completeness=9.5,
            needs_revision=False,
            items=[],
        ),
    ]
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        lambda **_kwargs: critiques.pop(0),
    )

    with app_session() as session:
        run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text(encoding="utf-8"))
    round0 = audit["controlled_polish_loop"]["round0_holistic"]
    assert round0["status"] == "skipped_disabled"
    assert round0["enabled"] is False


def test_round0_stage_b_failure_marks_open_prompt_skipped(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PR-366: agent reads run.mathematical_mode rather than env flag.
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_r0v2_b_fail",
        mathematical_mode=True,
    )
    _setup_common_monkeypatches(monkeypatch)

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_holistic_round0_open_prompt",
        lambda **_kwargs: None,
    )

    def fail_if_rewrite_called(**_kwargs: object) -> None:
        raise AssertionError("pipeline rewriter (stage C) must not run when stage B failed")

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fail_if_rewrite_called,
    )

    critiques = [
        _polish_critique(
            compliance=9.5,
            novelty=9.5,
            completeness=9.5,
            needs_revision=False,
            items=[],
        ),
    ]
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        lambda **_kwargs: critiques.pop(0),
    )

    with app_session() as session:
        run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text(encoding="utf-8"))
    round0 = audit["controlled_polish_loop"]["round0_holistic"]
    assert round0["status"] == "stage_b_open_prompt_failed_skipped"
    assert round0["enabled"] is True


def test_round0_stage_c_failure_reverts_to_stage_a(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_r0v2_c_fail",
        mathematical_mode=True,
    )
    _setup_common_monkeypatches(monkeypatch)

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_holistic_round0_open_prompt",
        lambda **_kwargs: _STAGE_B_OUTPUT,
    )

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        lambda **_kwargs: None,
    )

    critiques = [
        _polish_critique(
            compliance=9.5,
            novelty=9.5,
            completeness=9.5,
            needs_revision=False,
            items=[],
        ),
    ]
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        lambda **_kwargs: critiques.pop(0),
    )

    with app_session() as session:
        run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text(encoding="utf-8"))
    round0 = audit["controlled_polish_loop"]["round0_holistic"]
    assert round0["status"] == "stage_c_rewrite_failed_reverted"
    # Stage B output landed on disk so we can inspect it after a failed C.
    assert (run_dir / "drafts" / "v001" / "polish" / "round0_stage_b_open_prompt.md").is_file()


def test_round0_happy_path_lands_stage_c_as_incumbent(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_r0v2_happy",
        mathematical_mode=True,
    )
    _setup_common_monkeypatches(monkeypatch)

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_holistic_round0_open_prompt",
        lambda **_kwargs: _STAGE_B_OUTPUT,
    )

    rewriter_calls: list[str] = []

    def fake_rewriter(**kwargs: object) -> str:
        # Stage C call inside round 0 — incoming manuscript should be stage B.
        rewriter_calls.append(str(kwargs.get("manuscript") or "")[:80])
        return _STAGE_C_OUTPUT

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fake_rewriter,
    )

    critiques = [
        # initial critique on the v3 rewrite output
        _polish_critique(
            compliance=7.0,
            novelty=7.0,
            completeness=7.0,
            needs_revision=True,
            items=[
                RevisionItem(
                    severity="HIGH",
                    scope="body-p001",
                    issue="Sharpen transition.",
                    suggestion="Tighten.",
                ),
            ],
        ),
        # re-critique after round 0 sees stage C — scores rise; structured for
        # loop exits via approved_targets_cleared without firing rewriter again
        _polish_critique(
            compliance=9.5,
            novelty=9.5,
            completeness=9.5,
            needs_revision=False,
            items=[],
        ),
    ]
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        lambda **_kwargs: critiques.pop(0),
    )

    with app_session() as session:
        run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text(encoding="utf-8"))
    round0 = audit["controlled_polish_loop"]["round0_holistic"]
    assert round0["status"] == "succeeded"
    assert round0["pre_round0_scores"]["compliance"] == 7.0
    assert round0["post_round0_scores"]["compliance"] == 9.5
    assert audit["controlled_polish_loop"]["pre_loop_scores"]["compliance"] == 9.5

    # Stage C rewriter received stage B as input
    assert rewriter_calls
    assert "round-0 stage B" in rewriter_calls[0]

    polish_dir = run_dir / "drafts" / "v001" / "polish"
    assert (polish_dir / "round0_stage_b_open_prompt.md").is_file()
    assert (polish_dir / "round0_stage_c_pipeline_rewrite.md").is_file()


def test_round0_recritique_failure_keeps_stage_c_with_pre_critique(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_r0v2_recrit_fail",
        mathematical_mode=True,
    )
    _setup_common_monkeypatches(monkeypatch)

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_holistic_round0_open_prompt",
        lambda **_kwargs: _STAGE_B_OUTPUT,
    )
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        lambda **_kwargs: _STAGE_C_OUTPUT,
    )

    critiques: list[ExpertCritiqueOutput | None] = [
        _polish_critique(
            compliance=9.5,
            novelty=9.5,
            completeness=9.5,
            needs_revision=False,
            items=[],
        ),
        None,  # re-critique on stage C fails
    ]
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        lambda **_kwargs: critiques.pop(0),
    )

    with app_session() as session:
        run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text(encoding="utf-8"))
    round0 = audit["controlled_polish_loop"]["round0_holistic"]
    assert round0["status"] == "succeeded_but_recritique_failed_using_pre_critique"


def test_round0_only_lands_when_structured_rounds_accept_none(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When round 0 succeeds but the structured polish loop rejects every
    candidate, ``paper_polished.md`` must still contain the round-0 output
    (stage C). The pre-existing ``round0_applied`` fallback covers this.
    """
    get_settings.cache_clear()
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        "run_r0v2_only",
        mathematical_mode=True,
    )
    _setup_common_monkeypatches(monkeypatch)

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_holistic_round0_open_prompt",
        lambda **_kwargs: _STAGE_B_OUTPUT,
    )

    # First rewriter call is stage C inside round 0 (returns stage C). All
    # subsequent rewriter calls (inside the structured for loop) return None
    # to force exit with accepted_rewrites=0 + round0_applied=True.
    rewrite_responses: list[str | None] = [_STAGE_C_OUTPUT, None, None, None, None, None]

    def fake_rewriter(**_kwargs: object) -> str | None:
        return rewrite_responses.pop(0)

    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_rewrite_via_harness",
        fake_rewriter,
    )

    initial_revision_items = [
        RevisionItem(
            severity="HIGH",
            scope="body-p001",
            issue="Sharpen transition.",
            suggestion="Tighten.",
        ),
    ]
    critiques = [
        _polish_critique(
            compliance=6.0,
            novelty=6.0,
            completeness=6.0,
            needs_revision=True,
            items=list(initial_revision_items),
        ),
        # re-critique on stage C: mid-score with HIGH item → for-loop runs
        _polish_critique(
            compliance=7.5,
            novelty=7.5,
            completeness=7.5,
            needs_revision=True,
            items=list(initial_revision_items),
        ),
    ]
    monkeypatch.setattr(
        final_rewrite_module,
        "_controlled_polish_critique_via_harness",
        lambda **_kwargs: critiques.pop(0),
    )

    with app_session() as session:
        run_final_rewrite(run_id, session)

    audit = json.loads((run_dir / "rewrite" / "v001" / "audit.json").read_text(encoding="utf-8"))
    polish_audit = audit["controlled_polish_loop"]
    assert polish_audit["round0_holistic"]["status"] == "succeeded"
    assert polish_audit["accepted_rewrites"] == 0
    assert polish_audit.get("round0_only_applied") is True

    polished = (run_dir / "drafts" / "v001" / "polish" / "paper_polished.md").read_text(
        encoding="utf-8"
    )
    assert polished == _STAGE_C_OUTPUT
