"""PR-G-CriticScores Q3.3 (codex round-2 amendment): shared
final-manuscript resolver tests.

Validates the 3-tier resolution chain:
1. ``drafts/v*/polish/paper_polished.md`` (PR-G-CriticScores)
2. ``drafts/v*/style/paper_styled.md`` (stylist)
3. ``drafts/v*/manuscript.md`` (drafter)
"""

from __future__ import annotations

from pathlib import Path

from autoessay.agents._final_manuscript_resolver import (
    read_final_manuscript,
    resolve_final_manuscript_path,
)


def _make_drafts(tmp_path: Path, version: str) -> Path:
    drafts = tmp_path / "drafts" / version
    drafts.mkdir(parents=True, exist_ok=True)
    return drafts


# ----- resolve_final_manuscript_path ------------------------------


def test_returns_none_when_no_drafts_dir(tmp_path: Path) -> None:
    """Pre-drafter run state — nothing on disk → None."""
    assert resolve_final_manuscript_path(tmp_path) is None


def test_resolves_drafter_only_when_only_manuscript_exists(tmp_path: Path) -> None:
    """drafter has run but stylist/polish haven't — fall back to
    raw ``manuscript.md``."""
    version = _make_drafts(tmp_path, "v001")
    (version / "manuscript.md").write_text("# raw drafter", encoding="utf-8")
    path = resolve_final_manuscript_path(tmp_path)
    assert path is not None
    assert path.name == "manuscript.md"


def test_resolves_stylist_when_styled_present(tmp_path: Path) -> None:
    """stylist completed → ``paper_styled.md`` wins over raw."""
    version = _make_drafts(tmp_path, "v001")
    (version / "manuscript.md").write_text("# raw drafter", encoding="utf-8")
    (version / "style").mkdir()
    (version / "style" / "paper_styled.md").write_text("# styled", encoding="utf-8")
    path = resolve_final_manuscript_path(tmp_path)
    assert path is not None
    assert path.name == "paper_styled.md"


def test_resolves_polish_when_polished_present(tmp_path: Path) -> None:
    """PR-G-CriticScores: polish path takes precedence over both
    stylist and raw."""
    version = _make_drafts(tmp_path, "v001")
    (version / "manuscript.md").write_text("# raw", encoding="utf-8")
    (version / "style").mkdir()
    (version / "style" / "paper_styled.md").write_text("# styled", encoding="utf-8")
    (version / "polish").mkdir()
    (version / "polish" / "paper_polished.md").write_text("# polished", encoding="utf-8")
    path = resolve_final_manuscript_path(tmp_path)
    assert path is not None
    assert path.name == "paper_polished.md"


def test_picks_latest_version_when_multiple_drafts(tmp_path: Path) -> None:
    """v001 + v002 + v003 on disk → latest (v003) wins."""
    for version in ("v001", "v002", "v003"):
        d = _make_drafts(tmp_path, version)
        (d / "manuscript.md").write_text(f"# version {version}", encoding="utf-8")
    path = resolve_final_manuscript_path(tmp_path)
    assert path is not None
    assert "v003" in str(path)


def test_explicit_version_overrides_latest(tmp_path: Path) -> None:
    """Operator can pin a specific version (e.g. for evaluator
    re-scoring of a frozen baseline bundle)."""
    for version in ("v001", "v002"):
        d = _make_drafts(tmp_path, version)
        (d / "manuscript.md").write_text(f"# {version}", encoding="utf-8")
    path = resolve_final_manuscript_path(tmp_path, draft_version="v001")
    assert path is not None
    assert "v001" in str(path)


def test_explicit_version_returns_none_when_missing(tmp_path: Path) -> None:
    """Wrong version name → None (no fall-through to latest)."""
    _make_drafts(tmp_path, "v001")
    path = resolve_final_manuscript_path(tmp_path, draft_version="v999")
    assert path is None


def test_empty_manuscript_falls_through_to_next_tier(tmp_path: Path) -> None:
    """If polish file exists but is empty (whitespace only),
    skip to the next tier rather than returning a useless path."""
    version = _make_drafts(tmp_path, "v001")
    (version / "polish").mkdir()
    (version / "polish" / "paper_polished.md").write_text("   \n  ", encoding="utf-8")
    (version / "manuscript.md").write_text("# raw", encoding="utf-8")
    path = resolve_final_manuscript_path(tmp_path)
    assert path is not None
    assert path.name == "manuscript.md"


# ----- read_final_manuscript --------------------------------------


def test_read_returns_text_path_source_for_polish(tmp_path: Path) -> None:
    version = _make_drafts(tmp_path, "v001")
    (version / "polish").mkdir()
    (version / "polish" / "paper_polished.md").write_text("# polished body", encoding="utf-8")
    text, path, source = read_final_manuscript(tmp_path)
    assert text == "# polished body"
    assert path is not None and path.name == "paper_polished.md"
    assert source == "polish"


def test_read_returns_missing_when_no_artifacts(tmp_path: Path) -> None:
    text, path, source = read_final_manuscript(tmp_path)
    assert text == ""
    assert path is None
    assert source == "missing"


def test_read_classifies_stylist_and_drafter_sources(tmp_path: Path) -> None:
    """Source classification mirrors the pre-PR ``_read_manuscript``
    enum so callers that already track tier don't break."""
    # Drafter only
    v1 = _make_drafts(tmp_path / "run1", "v001")
    (v1 / "manuscript.md").write_text("raw", encoding="utf-8")
    _, _, src1 = read_final_manuscript(tmp_path / "run1")
    assert src1 == "drafter"

    # Stylist
    v2 = _make_drafts(tmp_path / "run2", "v001")
    (v2 / "manuscript.md").write_text("raw", encoding="utf-8")
    (v2 / "style").mkdir()
    (v2 / "style" / "paper_styled.md").write_text("styled", encoding="utf-8")
    _, _, src2 = read_final_manuscript(tmp_path / "run2")
    assert src2 == "stylist"
