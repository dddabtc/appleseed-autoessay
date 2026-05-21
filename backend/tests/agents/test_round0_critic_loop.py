"""Integration tests for critic-loop round-0 holistic revision.

Codex AGREE-WITH-AMENDMENTS 2026-05-12 coverage:
- Flag default OFF → skipped_disabled, iterations remain at 3 and
  ``selected_iter`` semantics are unchanged (so the existing critic-loop
  take-best invariant survives the wiring change).
- Flag ON + ``_score_manuscript`` fails → ``precritique_failed_skipped``,
  loop continues on the original input manuscript.
- Flag ON + holistic LLM returns ``None`` → ``failed_skipped``.
- Flag ON + sanity gate fails → ``sanity_failed_skipped``.
- Flag ON + happy path → ``succeeded``, ``current_md`` replaced, and the
  first structured iteration sees the round-0 manuscript.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from test_final_rewrite import _seed_rewrite_ready_run

from autoessay.agents import critic_loop as critic_loop_module
from autoessay.agents.critic_loop import run_production_critic_loop
from autoessay.agents.shadow_baseline import (
    ShadowBaselineOutput,
    persist_shadow_baseline,
)
from autoessay.config import get_settings
from autoessay.harness import HookRegistry
from autoessay.models import Project, Run

_BASELINE_MARKDOWN = (
    "# 影子基线\n\n"
    "这是 critic loop round 0 测试用的 shadow baseline 稿件 [1]。"
    "完整段落以满足 critic 评分上下文。" * 30
)

_INPUT_MANUSCRIPT = (
    "# Paper\n\nPipeline 输出的 candidate manuscript，引用 [1]，content padding。" * 30
)

_ROUND0_MANUSCRIPT = (
    "# Paper\n\nRound 0 整体改写后的稿子，仍保留单一引用 [1]，content padding。" * 30
)


def _seed_critic_loop_run(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    run_id: str,
    *,
    mathematical_mode: bool = False,
) -> tuple[Run, Project, Path]:
    run_id, run_dir = _seed_rewrite_ready_run(
        app_session,
        tmp_path,
        run_id,
        mathematical_mode=mathematical_mode,
    )
    # critic_loop reads shadow_baseline from disk via load_shadow_baseline.
    baseline = ShadowBaselineOutput(manuscript_markdown=_BASELINE_MARKDOWN)
    persist_shadow_baseline(run_dir, baseline)
    rewrite_dir = run_dir / "rewrite" / "v001"
    rewrite_dir.mkdir(parents=True, exist_ok=True)
    return run_id, run_dir, rewrite_dir  # type: ignore[return-value]


def _scored_result(
    *, compliance: float = 7.0, repair_plan: list[dict[str, object]] | None = None
) -> dict[str, object]:
    """Mimic ``_score_manuscript`` return shape for the ``scored`` branch."""
    return {
        "status": "scored",
        "scores": {
            "compliance": compliance,
            "novelty": compliance,
            "completeness": compliance,
            "evidence_strength": compliance,
        },
        "deduction_ledger": [],
        "repair_plan_to_full_score": repair_plan or [],
        "candidate_report": {"scores": {"compliance": compliance}},
        "critic_review": {"scores": {"compliance": compliance}},
    }


def _scored_candidate_result(
    *, max_loss: float = 0.0, repair_plan: list[dict[str, object]] | None = None
) -> dict[str, object]:
    """Mimic ``_score_candidate`` return shape (a superset of _score_manuscript
    that the inner for-loop relies on for max-loss take-best selection)."""
    scores = {
        "compliance": 8.0,
        "novelty": 8.0,
        "completeness": 8.0,
        "evidence_strength": 8.0,
    }
    return {
        "status": "scored",
        "pipeline_quality_scores": scores,
        "baseline_quality_scores": scores,
        "candidate_scores": scores,
        "baseline_scores": scores,
        "score_deltas": {dim: 0.0 for dim in scores},
        "max_loss": max_loss,
        "sum_delta": 0.0,
        "repair_plan_to_full_score": repair_plan or [],
        "deduction_ledger": [],
        "pipeline_report": None,
        "baseline_report": None,
        "paired_review": None,
    }


def _call_loop(
    app_session: sessionmaker[Session],
    run_id: str,
    rewrite_dir: Path,
    manuscript: str = _INPUT_MANUSCRIPT,
) -> critic_loop_module.CriticLoopRunResult:
    with app_session() as session:
        run = session.get(Run, run_id)
        assert run is not None
        project = session.get(Project, run.project_id)
        assert project is not None
        return run_production_critic_loop(
            run=run,
            project=project,
            session=session,
            hooks=HookRegistry(),
            manuscript=manuscript,
            rewrite_dir=rewrite_dir,
            iterations=3,
        )


def test_critic_loop_round0_flag_off_records_skipped_disabled(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTOESSAY_CRITIC_LOOP_HOLISTIC_ROUND0", raising=False)
    get_settings.cache_clear()
    run_id, _run_dir, rewrite_dir = _seed_critic_loop_run(app_session, tmp_path, "run_clr0_off")

    def fail_if_called(**_kwargs: object) -> None:
        raise AssertionError("critic loop round-0 helper must not run when flag is OFF")

    monkeypatch.setattr(critic_loop_module, "_holistic_round0_rewrite", fail_if_called)
    monkeypatch.setattr(
        critic_loop_module,
        "_score_manuscript",
        lambda **_kwargs: _scored_result(),
    )
    monkeypatch.setattr(
        critic_loop_module,
        "_score_candidate",
        lambda **_kwargs: _scored_candidate_result(),
    )

    result = _call_loop(app_session, run_id, rewrite_dir)

    audit = result.audit
    assert audit["round0_holistic"]["status"] == "skipped_disabled"
    # iterations parameter is the budget for the structured loop, not the
    # holistic round; staying at 3 means selected_iter semantics survive.
    assert audit["critic_loop_iterations"] == 3
    assert audit["selected_iter"] in (0, 1, 2)


def test_critic_loop_round0_precritique_failure_skips(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PR-366: per-run mathematical_mode supersedes env flag.
    get_settings.cache_clear()
    run_id, _run_dir, rewrite_dir = _seed_critic_loop_run(
        app_session,
        tmp_path,
        "run_clr0_preflight_fail",
        mathematical_mode=True,
    )

    # First _score_manuscript call is the baseline score (label="baseline"),
    # the second is the round-0 pre-critique (label="round0_input"). Have the
    # second fail so we exercise the precritique_failed_skipped branch.
    score_calls: list[str] = []

    def fake_score(**kwargs: object) -> dict[str, object]:
        label = str(kwargs.get("label") or "")
        score_calls.append(label)
        if label == "round0_input":
            return {"status": "critic_failed", "fail_reason": "simulated"}
        return _scored_result()

    monkeypatch.setattr(critic_loop_module, "_score_manuscript", fake_score)
    monkeypatch.setattr(
        critic_loop_module,
        "_score_candidate",
        lambda **_kwargs: _scored_candidate_result(),
    )

    def fail_if_called(**_kwargs: object) -> None:
        raise AssertionError("round-0 holistic rewrite must not run when precritique fails")

    monkeypatch.setattr(critic_loop_module, "_holistic_round0_rewrite", fail_if_called)

    result = _call_loop(app_session, run_id, rewrite_dir)
    audit = result.audit

    assert audit["round0_holistic"]["status"] == "precritique_failed_skipped"
    assert "round0_input" in score_calls


def test_critic_loop_round0_llm_failure_marks_failed_skipped(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    run_id, _run_dir, rewrite_dir = _seed_critic_loop_run(
        app_session,
        tmp_path,
        "run_clr0_llm_fail",
        mathematical_mode=True,
    )

    monkeypatch.setattr(
        critic_loop_module,
        "_score_manuscript",
        lambda **_kwargs: _scored_result(),
    )
    monkeypatch.setattr(
        critic_loop_module,
        "_score_candidate",
        lambda **_kwargs: _scored_candidate_result(),
    )
    monkeypatch.setattr(
        critic_loop_module,
        "_holistic_round0_rewrite",
        lambda **_kwargs: None,
    )

    result = _call_loop(app_session, run_id, rewrite_dir)
    assert result.audit["round0_holistic"]["status"] == "failed_skipped"


def test_critic_loop_round0_sanity_fail_marks_sanity_failed_skipped(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    run_id, _run_dir, rewrite_dir = _seed_critic_loop_run(
        app_session,
        tmp_path,
        "run_clr0_sanity_fail",
        mathematical_mode=True,
    )

    monkeypatch.setattr(
        critic_loop_module,
        "_score_manuscript",
        lambda **_kwargs: _scored_result(),
    )
    monkeypatch.setattr(
        critic_loop_module,
        "_score_candidate",
        lambda **_kwargs: _scored_candidate_result(),
    )
    # Holistic returns a too-short stub with no citation → fails sanity gate.
    monkeypatch.setattr(
        critic_loop_module,
        "_holistic_round0_rewrite",
        lambda **_kwargs: "# Paper",
    )

    result = _call_loop(app_session, run_id, rewrite_dir)
    audit = result.audit
    assert audit["round0_holistic"]["status"] == "sanity_failed_skipped"
    assert audit["round0_holistic"]["sanity"]["ok"] is False


def test_critic_loop_round0_happy_path_replaces_current_md(
    app_session: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    run_id, run_dir, rewrite_dir = _seed_critic_loop_run(
        app_session,
        tmp_path,
        "run_clr0_happy",
        mathematical_mode=True,
    )

    monkeypatch.setattr(
        critic_loop_module,
        "_score_manuscript",
        lambda **_kwargs: _scored_result(),
    )

    # Capture the manuscript that lands in iter-0 score_candidate so we can
    # verify the round-0 output replaced the input before the structured
    # for-loop started.
    candidates_seen: list[str] = []

    def fake_score_candidate(**kwargs: object) -> dict[str, object]:
        candidates_seen.append(str(kwargs.get("candidate_md") or ""))
        return _scored_candidate_result()

    monkeypatch.setattr(critic_loop_module, "_score_candidate", fake_score_candidate)
    monkeypatch.setattr(
        critic_loop_module,
        "_holistic_round0_rewrite",
        lambda **_kwargs: _ROUND0_MANUSCRIPT,
    )

    result = _call_loop(app_session, run_id, rewrite_dir)
    audit = result.audit
    assert audit["round0_holistic"]["status"] == "succeeded"
    assert candidates_seen, "structured loop must have scored at least one candidate"
    assert candidates_seen[0] == _ROUND0_MANUSCRIPT.strip()

    # iteration budget unchanged → selected_iter still in {0,1,2}
    assert audit["critic_loop_iterations"] == 3
    assert audit["selected_iter"] in (0, 1, 2)

    # Round-0 manuscript artifact written under the loop dir.
    round0_path = run_dir / "rewrite" / "v001" / "critic_loop" / "round0_holistic_manuscript.md"
    assert round0_path.is_file()
    assert round0_path.read_text(encoding="utf-8").strip() == _ROUND0_MANUSCRIPT.strip()
