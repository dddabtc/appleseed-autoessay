"""PR-G-CriticScores skeleton (codex round-3 AGREE on v4):
unit tests for the polish-loop helper module.

Focus on the deterministic helpers that don't need LLM mocking:
- ``QualityScoreSet`` schema validation
- ``_alias`` deterministic A/B label assignment
- ``compute_anti_plagiarism_jaccard`` — CJK char 5-gram + en word
  5-gram with body-only stripping
- ``evaluate_pass_margin`` — ``>= baseline + margin`` with saturated
  10.0 baselines passing on equality
- ``_strip_for_anti_plagiarism`` removes 摘要 / 关键词 /
  参考文献 / CNKI 标题 / [N] / punctuation

The actual LLM blind-eval call + targeted rewrite are exercised
in real-paper acceptance walks; the helper module ships behind
``Settings.polish_loop_enabled`` (default False) until joint
PR-G-Sources + PR-G-Coherence validation completes.
"""

from __future__ import annotations

from autoessay.agents._critic_polish_loop import (
    ANTI_PLAGIARISM_JACCARD_THRESHOLD,
    CRITIC_LOOP_ACTIVE_DIMS,
    DEFAULT_PASS_MARGIN,
    POLISH_BLIND_EVAL_SYSTEM_PROMPT,
    PolishLoopResult,
    QualityScoreSet,
    _alias,
    _PolishCritiqueOutput,
    _scores_from_letter,
    _strip_for_anti_plagiarism,
    compute_anti_plagiarism_jaccard,
    evaluate_pass_margin,
    is_anti_plagiarism_violation,
    manuscript_eval_metadata,
    quality_scores_dict,
)

# ----- QualityScoreSet schema -------------------------------------


def test_quality_score_set_clamps_to_0_10_range() -> None:
    """Impossible score values are clipped and audited instead of
    dropping the candidate response."""
    scores = QualityScoreSet(compliance=11.0, novelty=-1.0, completeness=5.0)
    assert scores.compliance == 10.0
    assert scores.novelty == 0.0
    assert scores.completeness == 5.0
    assert scores.score_clipped is True
    # Boundaries OK and do not mark clipping.
    bounded = QualityScoreSet(compliance=0.0, novelty=10.0, completeness=5.5)
    assert bounded.score_clipped is False


def test_quality_score_set_get_returns_dim_value() -> None:
    """``get(dim)`` returns the numeric score; ``justification(dim)``
    returns the per-dim text. Used by polish loop to thread one
    dim through prompt building."""
    scores = QualityScoreSet(
        compliance=8.5,
        novelty=4.0,
        completeness=7.0,
        evidence_strength=6.5,
        compliance_justification="cnki + N citations align",
        novelty_justification="hits 2 of 5 categories",
        completeness_justification="missing references list",
        evidence_strength_justification="claim strength exceeds archive coverage",
    )
    assert scores.get("compliance") == 8.5
    assert scores.get("novelty") == 4.0
    assert scores.get("evidence_strength") == 6.5
    assert scores.justification("novelty") == "hits 2 of 5 categories"
    assert scores.justification("evidence_strength") == "claim strength exceeds archive coverage"


# ----- _alias deterministic A/B mapping ---------------------------


def test_alias_is_deterministic_for_same_seed() -> None:
    """Codex R4 amendment: same seed must always produce the same
    label mapping so a re-run from the same run_id maps the same
    way (audit reproducibility)."""
    seed = "run_abc_polish_critic_seed_v1"
    a1, b1, m1 = _alias("pipeline-md", "baseline-md", seed)
    a2, b2, m2 = _alias("pipeline-md", "baseline-md", seed)
    assert (a1, b1, m1) == (a2, b2, m2)


def test_alias_assigns_both_orderings_across_seeds() -> None:
    """Across many seeds the helper should produce both orderings —
    sanity check that randomization isn't constant."""
    orderings = {_alias("pipeline-md", "baseline-md", f"run_{i}_seed")[2]["A"] for i in range(50)}
    assert orderings == {"pipeline", "baseline"}, f"randomization broken — only saw {orderings}"


# ----- _strip_for_anti_plagiarism ---------------------------------


def test_strip_removes_cnki_blocks_and_citations() -> None:
    """The Jaccard check sees only body prose: 摘要 / 关键词 /
    参考文献 / CNKI body section titles / [N] / punctuation
    are all stripped before n-gram extraction."""
    text = (
        "摘要：研究问题 [1].\n\n"
        "关键词：A；B；C\n\n"
        "一、引言\n\n"
        "本节论证 [2][3] 重要性。\n\n"
        "二、文献综述\n\n"
        "综述段落 [4]。\n\n"
        "## 参考文献\n\n"
        "[1] 张三\n[2] 李四\n"
    )
    stripped = _strip_for_anti_plagiarism(text)
    # Front matter gone
    assert "摘要" not in stripped
    assert "关键词" not in stripped
    # Back matter gone
    assert "参考文献" not in stripped
    assert "张三" not in stripped
    assert "李四" not in stripped
    # CNKI body headings gone
    assert "一、引言" not in stripped
    assert "二、文献综述" not in stripped
    # [N] markers gone
    assert "[1]" not in stripped
    assert "[2]" not in stripped
    # Body prose preserved
    assert "本节论证" in stripped
    assert "综述段落" in stripped


# ----- compute_anti_plagiarism_jaccard ----------------------------


def test_jaccard_low_for_independent_text() -> None:
    """Two independently written CJK manuscripts should have a
    Jaccard well under 0.08 — the cap is calibrated for academic
    Chinese where some 5-gram overlap is unavoidable (常见虚词)."""
    pipeline = (
        "一、引言\n\n本研究讨论历史问题 [1]。\n\n"
        "二、研究方法\n\n采用档案研究方法 [2]。\n\n"
        "## 参考文献\n\n[1] 张三\n[2] 李四"
    )
    baseline = (
        "一、引言\n\n本文聚焦另一个领域 [1]。\n\n"
        "二、文献综述\n\n综述既有研究的不同方向 [2]。\n\n"
        "## 参考文献\n\n[1] 王五\n[2] 赵六"
    )
    score = compute_anti_plagiarism_jaccard(pipeline, baseline, "zh")
    assert score < ANTI_PLAGIARISM_JACCARD_THRESHOLD


def test_jaccard_high_for_near_verbatim_copy() -> None:
    """If pipeline is ≥80% baseline body text the Jaccard fires."""
    body = (
        "一、引言\n\n"
        "本研究通过档案考察晚清江南刊本的断代依据，结合序跋纪年与"
        "刻工题记两类材料，重新梳理传统版本学的判断框架。"
        "本节阐述问题源起与方法选择。"
    )
    pipeline = body
    baseline = body
    score = compute_anti_plagiarism_jaccard(pipeline, baseline, "zh")
    assert score > 0.5  # near-identical body → very high overlap


def test_jaccard_violation_detector() -> None:
    """``is_anti_plagiarism_violation`` is True iff Jaccard >
    threshold."""
    body = "一、引言\n\n" + "通过档案考察晚清江南刊本的断代依据 " * 10
    different = "一、引言\n\n" + "本文论述其他主题，与原稿完全不同 " * 10
    # Identical bodies → Jaccard ≈ 1.0 → violates 0.5 threshold
    assert is_anti_plagiarism_violation(body, body, "zh", threshold=0.5)
    # Independent bodies → Jaccard low → does NOT violate 0.5
    assert not is_anti_plagiarism_violation(body, different, "zh", threshold=0.5)


def test_jaccard_uses_word_5gram_for_english() -> None:
    """English / non-CJK paths use word 5-grams not char 5-grams
    (avoids spurious overlap on common 5-letter substrings)."""
    pipeline = (
        "Section one introduction. The study examines historical evidence. Method section follows."
    )
    baseline = (
        "Introduction first. Our analysis examines unrelated material. We use different methods."
    )
    score = compute_anti_plagiarism_jaccard(pipeline, baseline, "en")
    assert score < 0.5


# ----- evaluate_pass_margin ---------------------------------------


def test_pass_when_all_dims_clear_baseline_plus_margin() -> None:
    """Pipeline must be at least baseline + 0.5 on every non-saturated dim."""
    pipeline = QualityScoreSet(compliance=9.0, novelty=8.0, completeness=8.5)
    baseline = QualityScoreSet(compliance=8.0, novelty=7.0, completeness=7.5)
    passed, failed = evaluate_pass_margin(pipeline, baseline, margin=0.5)
    assert passed is True
    assert failed == []


def test_fail_when_any_dim_within_margin() -> None:
    """compliance equals baseline → <= margin → fail. Other 2
    dims pass but ``passed=False`` because failed_dims is non-empty."""
    pipeline = QualityScoreSet(compliance=8.5, novelty=8.0, completeness=8.5)
    baseline = QualityScoreSet(compliance=8.5, novelty=7.0, completeness=7.5)
    passed, failed = evaluate_pass_margin(pipeline, baseline, margin=0.5)
    assert passed is False
    assert failed == ["compliance"]


def test_fail_lists_all_failing_dims() -> None:
    pipeline = QualityScoreSet(compliance=7.0, novelty=6.0, completeness=8.5)
    baseline = QualityScoreSet(compliance=8.0, novelty=7.0, completeness=7.5)
    passed, failed = evaluate_pass_margin(pipeline, baseline, margin=0.5)
    assert passed is False
    # compliance: 7 - 8 = -1 < 0.5 → fail
    # novelty:    6 - 7 = -1 < 0.5 → fail
    # completeness: 8.5 - 7.5 = 1 > 0.5 → pass
    assert sorted(failed) == ["compliance", "novelty"]


def test_exact_margin_passes() -> None:
    """pipeline-baseline = exactly margin now passes."""
    pipeline = QualityScoreSet(compliance=8.5, novelty=8.5, completeness=8.5)
    baseline = QualityScoreSet(compliance=8.0, novelty=8.0, completeness=8.0)
    passed, failed = evaluate_pass_margin(pipeline, baseline, margin=0.5)
    assert passed is True
    assert failed == []


def test_zero_margin_mode_passes_on_baseline_equality() -> None:
    """Baseline-as-evidence test mode passes when pipeline >= baseline."""
    pipeline = QualityScoreSet(compliance=9.0, novelty=10.0, completeness=8.5)
    baseline = QualityScoreSet(compliance=9.0, novelty=10.0, completeness=8.5)
    passed, failed = evaluate_pass_margin(pipeline, baseline, margin=0.0)
    assert passed is True
    assert failed == []


def test_saturated_baseline_ten_passes_on_equal_score() -> None:
    pipeline = QualityScoreSet(compliance=10.0, novelty=8.5, completeness=10.0)
    baseline = QualityScoreSet(compliance=10.0, novelty=8.0, completeness=10.0)
    passed, failed = evaluate_pass_margin(pipeline, baseline, margin=0.5)
    assert passed is True
    assert failed == []


# ----- PolishLoopResult serialization -----------------------------


def test_polish_loop_result_to_dict_round_trips() -> None:
    """The result must serialize cleanly to JSON for
    ``reviews/polish_quality.json``."""
    result = PolishLoopResult(
        status="failed_to_beat",
        baseline_mode="real",
        pipeline_quality_scores=QualityScoreSet(compliance=7.0, novelty=5.0, completeness=8.0),
        baseline_quality_scores=QualityScoreSet(compliance=8.0, novelty=7.0, completeness=8.5),
        failed_dims=["compliance", "novelty"],
        polish_attempts=2,
        plagiarism_violations=1,
    )
    payload = result.to_dict()
    assert payload["status"] == "failed_to_beat"
    assert payload["baseline_mode"] == "real"
    assert payload["pipeline_quality_scores"]["compliance"] == 7.0
    assert payload["baseline_quality_scores"]["novelty"] == 7.0
    assert payload["failed_dims"] == ["compliance", "novelty"]
    assert payload["polish_attempts"] == 2
    assert payload["plagiarism_violations"] == 1


def test_polish_loop_result_handles_missing_baseline() -> None:
    """``skipped_no_real_baseline`` path must serialize with
    ``baseline_quality_scores=None``."""
    result = PolishLoopResult(
        status="skipped_no_real_baseline",
        baseline_mode="missing",
        pipeline_quality_scores=None,
        baseline_quality_scores=None,
    )
    payload = result.to_dict()
    assert payload["status"] == "skipped_no_real_baseline"
    assert payload["baseline_mode"] == "missing"
    assert payload["pipeline_quality_scores"] is None
    assert payload["baseline_quality_scores"] is None


def test_quality_scores_dict_handles_none() -> None:
    assert quality_scores_dict(None) is None
    scores = QualityScoreSet(compliance=8.0, novelty=7.0, completeness=8.5)
    payload = quality_scores_dict(scores)
    assert payload is not None
    assert payload["compliance"] == 8.0


def test_v3_paired_prompt_is_compact_single_candidate_schema() -> None:
    assert "deduction_ledger" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert "repair_plan_to_full_score" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert "client_metadata" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert "score_breakdown" not in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert "global_validation" not in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert "full_score_revision_contract" not in POLISH_BLIND_EVAL_SYSTEM_PROMPT


def test_manuscript_eval_metadata_counts_objective_facts() -> None:
    manuscript = (
        "摘要\n摘要内容。\n\n关键词：制度；档案\n\n"
        "一、引言\n\n" + ("甲" * 1201) + "\n\n"
        "二、文献综述\n\n" + ("乙" * 900) + "[1][2]\n\n"
        "参考文献\n[1] 张三。\n[2] 李四。\n"
    )
    metadata = manuscript_eval_metadata(manuscript)
    assert metadata["has_abstract"] is True
    assert metadata["has_keywords"] is True
    assert metadata["has_references"] is True
    assert metadata["body_section_count"] == 2
    assert metadata["min_body_section_chars"] == 906
    assert metadata["inline_citation_count"] == 2
    assert metadata["reference_entry_count"] == 2


def test_v3_paired_schema_tolerates_optional_missing_fields() -> None:
    parsed = _PolishCritiqueOutput.parse_obj(
        {
            "candidate_reports": [
                {
                    "candidate_id": "A",
                    "scores": {
                        "compliance": 12.0,
                        "novelty": 7.0,
                        "completeness": 8.0,
                    },
                },
                {
                    "candidate_id": "B",
                    "scores": {
                        "compliance": 9.0,
                        "novelty": 6.0,
                        "completeness": 7.0,
                    },
                    "deduction_ledger": [{"id": "D-B-01"}],
                },
            ],
        }
    )
    a_scores = _scores_from_letter(parsed, "a")
    b_scores = _scores_from_letter(parsed, "b")
    assert a_scores.compliance == 10.0
    assert a_scores.score_clipped is True
    assert b_scores.compliance == 9.0
    assert parsed.candidate_reports[0].schema_partial_fields


def test_v3_prompt_makes_evidence_strength_active_overclaim_gate() -> None:
    assert CRITIC_LOOP_ACTIVE_DIMS == (
        "compliance",
        "novelty",
        "completeness",
        "evidence_strength",
    )
    assert "四个维度都是 active 维度" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert "evidence_strength 红线" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert "最高 5 分" in POLISH_BLIND_EVAL_SYSTEM_PROMPT
    assert 'target_dimension="evidence_strength"' in POLISH_BLIND_EVAL_SYSTEM_PROMPT


def test_v3_paired_schema_accepts_legacy_flat_scores() -> None:
    parsed = _PolishCritiqueOutput.parse_obj(
        {
            "a_compliance": 8.0,
            "a_novelty": 7.0,
            "a_completeness": 6.0,
            "a_evidence_strength": 5.0,
            "b_compliance": 5.0,
            "b_novelty": 4.0,
            "b_completeness": 3.0,
            "b_evidence_strength": 2.0,
        }
    )
    assert _scores_from_letter(parsed, "a").compliance == 8.0
    assert _scores_from_letter(parsed, "a").evidence_strength == 5.0
    assert _scores_from_letter(parsed, "b").completeness == 3.0
    assert _scores_from_letter(parsed, "b").evidence_strength == 2.0
    assert "candidate_reports:converted_from_flat_schema" in parsed.schema_partial_fields


# ----- defaults pinned -------------------------------------------


def test_default_pass_margin_is_zero_point_five() -> None:
    """Codex Q4 amendment locked margin to 0.5; this test pins it
    so a refactor doesn't silently relax the gate."""
    assert DEFAULT_PASS_MARGIN == 0.5


def test_anti_plagiarism_threshold_is_zero_point_zero_eight() -> None:
    """Codex Q3 amendment: 0.08, NOT the v3-proposed 0.10."""
    assert ANTI_PLAGIARISM_JACCARD_THRESHOLD == 0.08
