"""PR-G-Sources Stage 1 (codex round-2 amendment Q1):
synthesizer deep_dive_limit resolution chain.

Validates the precedence order:

1. ``Settings.synthesizer_deep_dive_limit`` env override
2. Per-domain ``search.telescope.deep_dive_limit`` from yaml
3. ``DEFAULT_DEEP_DIVE_LIMIT`` (raised from 6 to 14 in this PR)

Codex round-2 verdict: synthesizer was the real bottleneck for
``cited_sources_diversity_floor`` — drafter read shortlist (~24)
but only ~6 had source_notes, so claim_map could only cite 6.
Bumping default to 14 lets drafter cite up to floor=12 without
changes to retrieval / curator.
"""

from __future__ import annotations

from autoessay.agents.synthesizer import DEFAULT_DEEP_DIVE_LIMIT, _deep_dive_limit
from autoessay.config import get_settings


def test_default_limit_is_14() -> None:
    """The original default 6 is the documented bottleneck — this
    test pins the new default so a refactor-induced regression to 6
    is caught immediately."""
    assert DEFAULT_DEEP_DIVE_LIMIT == 14


def test_resolution_falls_back_to_default_when_no_overrides() -> None:
    """No env override + no domain telescope.deep_dive_limit →
    DEFAULT_DEEP_DIVE_LIMIT (14)."""
    get_settings.cache_clear()
    domain_data = {"id": "test", "search": {"telescope": {}}}
    assert _deep_dive_limit(domain_data) == DEFAULT_DEEP_DIVE_LIMIT
    assert _deep_dive_limit(domain_data) == 14


def test_resolution_uses_domain_telescope_limit_when_set() -> None:
    """Per-domain yaml ``search.telescope.deep_dive_limit`` wins
    over the default (but still loses to env override; tested
    below)."""
    get_settings.cache_clear()
    domain_data = {
        "id": "test",
        "search": {"telescope": {"deep_dive_limit": 10}},
    }
    assert _deep_dive_limit(domain_data) == 10


def test_env_override_beats_domain_limit(monkeypatch) -> None:
    """``AUTOESSAY_SYNTHESIZER_DEEP_DIVE_LIMIT`` env var wins over
    domain yaml; this is the operator escape hatch when running a
    real-paper acceptance walk that must use the same limit
    everywhere regardless of the seeded domain."""
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_DEEP_DIVE_LIMIT", "20")
    get_settings.cache_clear()
    domain_data = {
        "id": "test",
        "search": {"telescope": {"deep_dive_limit": 10}},
    }
    assert _deep_dive_limit(domain_data) == 20


def test_env_override_zero_is_ignored(monkeypatch) -> None:
    """A non-positive env override is treated as ``unset`` (so a
    typo or stale env=0 doesn't silently disable the synthesizer
    deep-dive)."""
    monkeypatch.setenv("AUTOESSAY_SYNTHESIZER_DEEP_DIVE_LIMIT", "0")
    get_settings.cache_clear()
    domain_data = {"id": "test", "search": {"telescope": {"deep_dive_limit": 10}}}
    # Falls through to domain_data (10), not 0.
    assert _deep_dive_limit(domain_data) == 10


def test_resolution_handles_malformed_domain_data() -> None:
    """``search`` not a dict, ``telescope`` not a dict, or limit not
    a positive int → fall through to default."""
    get_settings.cache_clear()
    cases = [
        {"id": "x"},  # no search
        {"id": "x", "search": "not a dict"},
        {"id": "x", "search": {"telescope": "not a dict"}},
        {"id": "x", "search": {"telescope": {"deep_dive_limit": "abc"}}},
        {"id": "x", "search": {"telescope": {"deep_dive_limit": -5}}},
        {"id": "x", "search": {"telescope": {"deep_dive_limit": 0}}},
    ]
    for case in cases:
        assert _deep_dive_limit(case) == DEFAULT_DEEP_DIVE_LIMIT, case


def test_yaml_files_set_explicit_deep_dive_limit() -> None:
    """Production domain yamls set explicit deep-dive limits."""
    from pathlib import Path

    import yaml

    repo_root = Path(__file__).resolve().parents[3]
    expected_limits = {
        repo_root / "domains" / "economic_history.yaml": 14,
        repo_root / "domains" / "financial_history.yaml": 24,
        repo_root / "domains" / "general_academic.yaml": 14,
    }
    for path, expected_limit in expected_limits.items():
        assert path.exists(), f"missing domain yaml: {path}"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["search"]["telescope"]["deep_dive_limit"] == expected_limit, (
            f"{path}: expected deep_dive_limit={expected_limit}"
        )
