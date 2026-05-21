"""PR-G-CriticScores Q3.3 (codex round-2 amendment): shared
final-manuscript resolver.

Before this module, exporter / evaluator / integrity /
phase_readiness each had their own ad-hoc fallback chain to find
the "current" manuscript on disk. PR-G-CriticScores adds a new
``drafts/v*/polish/paper_polished.md`` artifact written by the
critic's targeted-rewrite loop; without a single resolver, every
caller would have to update its own resolution chain (and they'd
drift over time).

Resolution chain (highest priority → lowest):

1. ``drafts/v{X}/polish/paper_polished.md`` (PR-G-CriticScores —
   critic's polish-loop output, takes precedence when present)
2. ``drafts/v{X}/style/paper_styled.md`` (stylist output)
3. ``drafts/v{X}/manuscript.md`` (drafter raw output)

When ``draft_version`` is omitted the resolver picks the latest
``v*`` directory by name-sort. Returns ``None`` when no manuscript
exists at any tier (e.g. early-cancelled run).

The companion helper ``read_final_manuscript`` returns ``(text,
path, source)`` where ``source`` is one of ``polish`` / ``stylist``
/ ``drafter`` / ``missing`` — kept identical to the pre-PR
``_read_manuscript`` shape from ``evaluate_paper.py`` so callers
that already track which tier they're reading from don't break.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

ManuscriptSource = Literal["polish", "stylist", "drafter", "missing"]


def _latest_draft_version_dir(run_dir: Path) -> Path | None:
    """Return the ``drafts/v*`` directory with the highest sort
    order (string-sort works because the version names are
    zero-padded ``v001`` / ``v002`` etc). ``None`` when no draft
    directory exists yet (drafter hasn't run)."""
    drafts = run_dir / "drafts"
    if not drafts.exists():
        return None
    candidates = sorted(
        (path for path in drafts.glob("v*") if path.is_dir()),
        key=lambda path: path.name,
    )
    if not candidates:
        return None
    return candidates[-1]


def resolve_final_manuscript_path(
    run_dir: str | Path,
    draft_version: str | None = None,
) -> Path | None:
    """Return the path to the highest-priority manuscript artifact
    that exists on disk. ``None`` when nothing matches.

    ``draft_version`` is the explicit ``v{NNN}`` directory name;
    when ``None`` the latest version is used.
    """
    run_dir = Path(run_dir)
    version_dir: Path | None
    if draft_version is not None:
        candidate = run_dir / "drafts" / draft_version
        if not candidate.exists():
            return None
        version_dir = candidate
    else:
        version_dir = _latest_draft_version_dir(run_dir)
    if version_dir is None:
        return None
    polish_path = version_dir / "polish" / "paper_polished.md"
    if polish_path.exists() and polish_path.read_text(encoding="utf-8").strip():
        return polish_path
    style_path = version_dir / "style" / "paper_styled.md"
    if style_path.exists() and style_path.read_text(encoding="utf-8").strip():
        return style_path
    raw_path = version_dir / "manuscript.md"
    if raw_path.exists() and raw_path.read_text(encoding="utf-8").strip():
        return raw_path
    return None


def read_final_manuscript(
    run_dir: str | Path,
    draft_version: str | None = None,
) -> tuple[str, Path | None, ManuscriptSource]:
    """Return ``(text, path, source)`` for the resolved manuscript.

    ``source`` is one of:
    - ``"polish"``   — read from ``polish/paper_polished.md``
    - ``"stylist"``  — read from ``style/paper_styled.md``
    - ``"drafter"``  — read from ``manuscript.md``
    - ``"missing"``  — no manuscript found (path is None, text "")
    """
    path = resolve_final_manuscript_path(run_dir, draft_version)
    if path is None:
        return "", None, "missing"
    source: ManuscriptSource
    if path.name == "paper_polished.md":
        source = "polish"
    elif path.name == "paper_styled.md":
        source = "stylist"
    else:
        source = "drafter"
    return path.read_text(encoding="utf-8"), path, source


__all__ = [
    "ManuscriptSource",
    "read_final_manuscript",
    "resolve_final_manuscript_path",
]
