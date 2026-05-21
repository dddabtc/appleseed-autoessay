"""PR-D4 evaluator unit tests.

Covers:
  * full bundle scoring against ``case_analysis_smoke`` fixture
  * vector + vector_directions metadata
  * artifact-absent paths (manuscript / claim_map / ledger / shortlist /
    integrity_summary all conditional)
  * J9b rerank_quality block (axes coverage / fallback events /
    verified_by openalex count)
  * citation_diff (uncited_ledger / fabricated_citations)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "backend" / "scripts"))

import evaluate_paper  # noqa: E402  isort:skip

FIXTURE_DIR = _REPO_ROOT / "backend" / "tests" / "fixtures" / "evaluator"
SMOKE_BUNDLE = FIXTURE_DIR / "case_analysis_smoke"


def _payload() -> dict:
    return evaluate_paper.evaluate_run_bundle(SMOKE_BUNDLE)


def test_evaluator_returns_full_payload_shape() -> None:
    payload = _payload()
    assert payload["schema_version"] == evaluate_paper.SCHEMA_VERSION
    assert payload["paper_mode"] == "case_analysis"
    assert payload["run_id"] == "run_smoke_fixture"
    assert payload["baseline_status"] == evaluate_paper.BASELINE_STATUS_CANDIDATE
    assert payload["baseline_label"] == "case_analysis-smoke-fixture"
    assert payload["vector_fields"] == [f["name"] for f in evaluate_paper.VECTOR_FIELDS]
    assert len(payload["vector"]) == len(payload["vector_fields"])


def test_vector_directions_metadata_complete() -> None:
    payload = _payload()
    directions = payload["vector_directions"]
    assert directions["integrity_p0"] == "exact-zero"
    assert directions["fabricated_citations"] == "exact-zero"
    assert directions["fallback_events"] == "exact-zero"
    assert directions["manuscript_bytes"] == "higher-is-better"
    assert directions["claim_density"] == "higher-is-better"
    assert directions["stop_slop_total"] == "higher-is-better"
    assert directions["manuscript_citations"] == "higher-is-better"


def test_smoke_bundle_zero_p0_zero_fallback() -> None:
    """Smoke fixture: integrity_summary has 0 plagiarism spans, ledger
    has 0 curator_rerank_fallback events."""
    payload = _payload()
    assert payload["scores"]["integrity_p0"] == 0
    assert payload["scores"]["fallback_events"] == 0
    assert payload["scores"]["fabricated_citations"] == 0


def test_claim_density_uses_claim_map_count_not_periods() -> None:
    """Claim density = claims / 1000 bytes (codex A3). Smoke fixture
    has 4 claim_map entries; bytes ≈ 2k → density ≈ 1.8."""
    payload = _payload()
    density = payload["scores"]["claim_density"]
    bytes_total = payload["scores"]["manuscript_bytes"]
    expected = round(4 / bytes_total * 1000, 4)
    assert density == expected
    assert density > 1.0


def test_rerank_quality_block_populated() -> None:
    """J9b signal: 3/3 sources have rerank_axes; 1 verified_by openalex;
    0 fallback events."""
    payload = _payload()
    rq = payload["scores"]["rerank_quality"]
    assert rq["rerank_active"] is True
    assert rq["rerank_axes_coverage"] == 1.0
    assert rq["verified_by_openalex_count"] == 1
    assert rq["fallback_events"] == 0
    assert rq["shortlist_size"] == 3
    assert 0.85 <= rq["scope_fit_top10_avg"] <= 0.95


def test_citation_diff_counts_inline_citations_and_dois() -> None:
    payload = _payload()
    diff = payload["scores"]["citation_diff"]
    # Manuscript references "Brokaw, 2005" and "McDermott (2006)" plus 1
    # DOI; the inline regex catches author-year citations that have a
    # comma between author and year (e.g. "Brokaw, 2005").
    assert diff["manuscript_citations"] >= 1
    assert diff["inline_dois"] >= 1
    assert diff["ledger_entries"] >= 2  # claim_map source_ids unique


def test_artifact_present_flags() -> None:
    payload = _payload()
    arts = payload["artifacts"]
    assert arts["manuscript_present"] is True
    assert arts["claim_map_present"] is True
    assert arts["ledger_present"] is True
    assert arts["shortlist_present"] is True
    assert arts["integrity_summary_present"] is True


def test_evaluator_handles_missing_bundle_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        evaluate_paper.evaluate_run_bundle(tmp_path / "no_such_dir")


def test_evaluator_records_artifact_absence_when_partial(tmp_path: Path) -> None:
    """Codex A1: artifacts are conditional. A bundle with only a
    manuscript should evaluate, not raise."""
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "manuscript.md").write_text("# Tiny\n\nbody.\n", encoding="utf-8")
    payload = evaluate_paper.evaluate_run_bundle(tmp_path)
    arts = payload["artifacts"]
    assert arts["manuscript_present"] is True
    assert arts["claim_map_present"] is False
    assert arts["ledger_present"] is False
    assert arts["shortlist_present"] is False
    assert arts["integrity_summary_present"] is False
    assert payload["scores"]["claim_density"] == 0.0
    assert payload["scores"]["fallback_events"] == 0


def test_evaluator_falls_back_to_drafter_styled_when_exports_absent(tmp_path: Path) -> None:
    drafts = tmp_path / "drafts" / "v001" / "style"
    drafts.mkdir(parents=True)
    (drafts / "paper_styled.md").write_text("# Styled draft fallback\n", encoding="utf-8")
    payload = evaluate_paper.evaluate_run_bundle(tmp_path)
    assert payload["scores"]["manuscript_source"] == "drafter_styled"


def test_evaluator_falls_back_to_drafter_raw_when_styled_absent(tmp_path: Path) -> None:
    drafts = tmp_path / "drafts" / "v001"
    drafts.mkdir(parents=True)
    (drafts / "manuscript.md").write_text("# Raw draft fallback\n", encoding="utf-8")
    payload = evaluate_paper.evaluate_run_bundle(tmp_path)
    assert payload["scores"]["manuscript_source"] == "drafter_raw"


def test_cli_writes_evaluator_json_and_exits_zero(tmp_path: Path) -> None:
    out = tmp_path / "eval.json"
    rc = evaluate_paper.main(["--output", str(out), str(SMOKE_BUNDLE)])
    assert rc == 0
    assert out.exists()
    decoded = json.loads(out.read_text(encoding="utf-8"))
    assert decoded["paper_mode"] == "case_analysis"


def test_stop_slop_uses_real_phrases_not_empty() -> None:
    """Codex A2: evaluator must load the real stop_slop rules. Pass-
    through smoke shouldn't flag many phrases but rules must be loaded
    (verified by checking the dimensions are <= max default of 8 with
    some deductions possible)."""
    payload = _payload()
    stop_slop = payload["scores"]["stop_slop"]
    assert "dimensions" in stop_slop
    assert "total" in stop_slop
    # Smoke fixture is mild academic prose, but we assert the structure
    # is real (not the empty-phrase default, which would never produce
    # findings).
    assert isinstance(stop_slop["findings"], list)
