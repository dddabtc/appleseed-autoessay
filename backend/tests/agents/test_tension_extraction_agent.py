"""PR-C3.a — tension_extraction agent tests (stub-only path).

The full real-LLM extraction agent lands in PR-C3.b. C3.a covers:
  * ``should_run_tension_extraction`` operational gate respects
    ``Settings.tension_taxonomy_enabled`` + checks synthesizer claim
    presence (codex round-2 amendment 6 + 7)
  * ``extract_tensions`` stub path produces 2 deterministic tensions
    grounded in the synthesizer claim_id triples
  * Artifact writes to ``synthesis/tension_extraction.json``
  * ``load_tension_extraction`` reader round-trips and returns None
    when artifact absent
  * Real-LLM path raises NotImplementedError until C3.b
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoessay.agents._tension_taxonomy import (
    TensionExtractionOutput,
)
from autoessay.agents.tension_extraction import (
    extract_tensions,
    load_tension_extraction,
    should_run_tension_extraction,
)
from autoessay.config import get_settings


def _synth_payload() -> dict:
    return {
        "schema_version": 1,
        "primary_track": [
            {"source_id": "src_b", "claim_id": "c_b", "text": "primary claim"},
        ],
        "secondary_track": [
            {"source_id": "src_a", "claim_id": "c_a", "text": "secondary claim"},
        ],
        "theoretical_lens_track": [],
        "methodological_track": [],
    }


# ---------- should_run_tension_extraction ---------------------------


def test_should_run_returns_false_when_taxonomy_disabled(monkeypatch) -> None:
    """Default: AUTOESSAY_TENSION_TAXONOMY_ENABLED unset → False."""
    monkeypatch.delenv("AUTOESSAY_TENSION_TAXONOMY_ENABLED", raising=False)
    get_settings.cache_clear()
    assert (
        should_run_tension_extraction(
            paper_mode="case_analysis",
            synthesizer_payload=_synth_payload(),
        )
        is False
    )
    get_settings.cache_clear()


def test_should_run_returns_false_when_synthesizer_payload_none(monkeypatch) -> None:
    monkeypatch.setenv("AUTOESSAY_TENSION_TAXONOMY_ENABLED", "1")
    get_settings.cache_clear()
    assert (
        should_run_tension_extraction(
            paper_mode="case_analysis",
            synthesizer_payload=None,
        )
        is False
    )
    monkeypatch.delenv("AUTOESSAY_TENSION_TAXONOMY_ENABLED", raising=False)
    get_settings.cache_clear()


def test_should_run_returns_false_when_no_claims_in_any_track(monkeypatch) -> None:
    """Codex round-2 amendment 7: legacy run reader fallback — without
    synthesizer claims, tension extraction has nothing to ground in."""
    monkeypatch.setenv("AUTOESSAY_TENSION_TAXONOMY_ENABLED", "1")
    get_settings.cache_clear()
    payload = {
        "primary_track": [],
        "secondary_track": [],
        "theoretical_lens_track": [],
        "methodological_track": [],
    }
    assert (
        should_run_tension_extraction(
            paper_mode="case_analysis",
            synthesizer_payload=payload,
        )
        is False
    )
    monkeypatch.delenv("AUTOESSAY_TENSION_TAXONOMY_ENABLED", raising=False)
    get_settings.cache_clear()


def test_should_run_returns_true_when_enabled_with_claims(monkeypatch) -> None:
    monkeypatch.setenv("AUTOESSAY_TENSION_TAXONOMY_ENABLED", "1")
    get_settings.cache_clear()
    assert (
        should_run_tension_extraction(
            paper_mode="case_analysis",
            synthesizer_payload=_synth_payload(),
        )
        is True
    )
    monkeypatch.delenv("AUTOESSAY_TENSION_TAXONOMY_ENABLED", raising=False)
    get_settings.cache_clear()


# ---------- extract_tensions stub path ------------------------------


def test_extract_tensions_stub_produces_two_grounded_tensions(monkeypatch) -> None:
    monkeypatch.setenv("AUTOESSAY_TENSION_EXTRACTION_STUB", "1")
    get_settings.cache_clear()
    output, drops = extract_tensions(
        paper_mode="case_analysis",
        synthesizer_payload=_synth_payload(),
    )
    assert isinstance(output, TensionExtractionOutput)
    assert len(output.tensions) == 2
    assert output.paper_mode == "case_analysis"
    assert output.tensions[0].tension_id == "t001"
    assert output.tensions[1].tension_id == "t002"
    # All claim_refs must point at real synthesizer triples.
    assert drops == []
    monkeypatch.delenv("AUTOESSAY_TENSION_EXTRACTION_STUB", raising=False)
    get_settings.cache_clear()


def test_extract_tensions_stub_writes_artifact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOESSAY_TENSION_EXTRACTION_STUB", "1")
    get_settings.cache_clear()
    output, _ = extract_tensions(
        paper_mode="case_analysis",
        synthesizer_payload=_synth_payload(),
        run_dir=tmp_path,
    )
    artifact = tmp_path / "synthesis" / "tension_extraction.json"
    assert artifact.exists()
    decoded = json.loads(artifact.read_text(encoding="utf-8"))
    assert decoded["schema_version"] == 1
    assert decoded["paper_mode"] == "case_analysis"
    assert len(decoded["tensions"]) == 2
    monkeypatch.delenv("AUTOESSAY_TENSION_EXTRACTION_STUB", raising=False)
    get_settings.cache_clear()


def test_extract_tensions_stub_with_empty_synthesizer_yields_zero_tensions(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_TENSION_EXTRACTION_STUB", "1")
    get_settings.cache_clear()
    output, drops = extract_tensions(
        paper_mode="case_analysis",
        synthesizer_payload={
            "primary_track": [],
            "secondary_track": [],
            "theoretical_lens_track": [],
            "methodological_track": [],
        },
    )
    assert output.tensions == []
    assert drops == []
    monkeypatch.delenv("AUTOESSAY_TENSION_EXTRACTION_STUB", raising=False)
    get_settings.cache_clear()


def test_extract_tensions_real_llm_requires_run_project_session(monkeypatch) -> None:
    """PR-C3.b: real-LLM branch is wired but requires run + project +
    session args. Calling without them raises ValueError (not
    NotImplementedError as in C3.a). Operational gate is still OFF
    by default in prod (Settings.tension_taxonomy_enabled=False) so
    this branch is unreachable from the start_tension_extraction
    endpoint until prod flips the env var."""
    monkeypatch.delenv("AUTOESSAY_TENSION_EXTRACTION_STUB", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="run \\+ project \\+ session"):
        extract_tensions(
            paper_mode="case_analysis",
            synthesizer_payload=_synth_payload(),
        )
    get_settings.cache_clear()


# ---------- load_tension_extraction reader -------------------------


def test_load_tension_extraction_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_tension_extraction(tmp_path) is None


def test_load_tension_extraction_round_trips_artifact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOESSAY_TENSION_EXTRACTION_STUB", "1")
    get_settings.cache_clear()
    extract_tensions(
        paper_mode="case_analysis",
        synthesizer_payload=_synth_payload(),
        run_dir=tmp_path,
    )
    loaded = load_tension_extraction(tmp_path)
    assert loaded is not None
    assert len(loaded.tensions) == 2
    assert loaded.paper_mode == "case_analysis"
    monkeypatch.delenv("AUTOESSAY_TENSION_EXTRACTION_STUB", raising=False)
    get_settings.cache_clear()


def test_load_tension_extraction_returns_none_for_malformed_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "synthesis" / "tension_extraction.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("not-json", encoding="utf-8")
    assert load_tension_extraction(tmp_path) is None
