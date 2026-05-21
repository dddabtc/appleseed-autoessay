"""PR-371: derive download-friendly filenames from a project title.

The exporter agent writes ``manuscript.{docx,html,md,tex,...}`` on disk
and that is the canonical name in ``manifest.json``. This module
produces a slug suitable for an HTTP ``Content-Disposition`` header so
the user's browser / ``curl -OJ`` saves the file under a title-derived
name instead of yet another ``manuscript.docx``.

Codex AGREE-WITH-AMENDMENTS 2026-05-13 PR-371:
- treat any Unicode whitespace as a separator
- fold runs of separators down to a single ``"-"``
- strip leading / trailing ``"-"`` AFTER length truncation (not before)
- exclude sidecar artifacts (``literature_usage_table``,
  ``self_check_report``, ``manifest``) — the caller is responsible for
  passing only the manuscript files in
- append a run-id short suffix for titles that collapse to the
  ``"Untitled Project"`` default or to an empty slug, so 100 default
  runs don't all download as the same file
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

# 7-bit ASCII characters we never want in a filename. Filesystem-illegal
# on Windows (NTFS / SMB) plus shell-special and control characters.
_ASCII_SEPARATORS = set(r'/\\:*?"<>|')

# ``"Untitled Project"`` is the wizard's default at
# NewRunPage.tsx:37; titles that equal it after stripping are treated
# as "no real title" and trigger the run-id disambiguator.
_DEFAULT_PROJECT_TITLE = "Untitled Project"

_MAX_SLUG_CHARS = 80


def _is_separator(ch: str) -> bool:
    """Treat anything that isn't a Unicode letter or number as a
    filename separator.

    Keep: ``L`` (letters, includes CJK) + ``N`` (numbers) + literal
    ASCII hyphen (handled by the caller). Drop everything else:
    punctuation, marks, symbols (emoji), separators (whitespace),
    control / format chars.
    """
    if ch == "-":
        return False
    if ch in _ASCII_SEPARATORS:
        return True
    if ch.isspace():
        return True
    cat = unicodedata.category(ch)
    # First letter of the category tells the broad class. Keep L and N
    # only; drop P / M / S / Z / C.
    return cat[0] not in {"L", "N"}


def slug_from_title(
    title: str | None,
    *,
    run_id: str | None = None,
    max_chars: int = _MAX_SLUG_CHARS,
) -> str:
    """Produce a Content-Disposition-friendly slug.

    Rules:
    - Unicode NFKC normalise.
    - Every non-letter / non-digit / non-hyphen char becomes ``"-"``.
      That folds ASCII whitespace, CJK punctuation (《》、，。), emoji,
      filesystem-illegal chars, etc.
    - Runs of ``"-"`` collapse to a single ``"-"``.
    - Truncate to ``max_chars`` Unicode code points (CJK title with 50
      chars is fine; a 200-char Latin string gets clipped).
    - Strip leading/trailing ``"-"`` AFTER truncation so we never end
      on a stray separator (codex amendment 2).
    - If the result is empty or matches the wizard default
      ``"Untitled Project"``, fall back to ``"manuscript"`` and (if
      provided) append the run id's short suffix so different default
      runs don't collide.
    """
    base = (title or "").strip()
    if not base or base == _DEFAULT_PROJECT_TITLE:
        return _fallback_with_run_id(run_id)

    normalised = unicodedata.normalize("NFKC", base)
    chars: list[str] = []
    for ch in normalised:
        if ch == "-" or _is_separator(ch):
            chars.append("-")
        else:
            chars.append(ch)
    joined = "".join(chars)
    # Collapse runs of "-" to a single hyphen.
    collapsed = re.sub(r"-+", "-", joined)
    # Truncate, then strip leading/trailing hyphens.
    truncated = collapsed[:max_chars]
    cleaned = truncated.strip("-")
    if not cleaned:
        return _fallback_with_run_id(run_id)
    return cleaned


def _fallback_with_run_id(run_id: str | None) -> str:
    """For empty / default titles, return ``manuscript`` plus a short
    run-id suffix so concurrent default-titled runs don't collide.
    """
    if not run_id:
        return "manuscript"
    suffix = run_id.removeprefix("run_")[:8]
    if not suffix:
        return "manuscript"
    return f"manuscript-{suffix}"


# Sidecar filenames that should NOT be renamed: they encode the file's
# semantic role, not the manuscript content. ``manifest.json`` is the
# export contract, the *_table.md / *_report.{md,json} files describe
# what the run did, not the paper itself.
_SIDECAR_STEMS = frozenset(
    {
        "manifest",
        "literature_usage_table",
        "self_check_report",
    }
)


def is_sidecar_filename(filename: str) -> bool:
    """Return True for files that keep their literal name on download."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem in _SIDECAR_STEMS


def download_filename_for_export(
    *,
    disk_filename: str,
    project_title: str | None,
    run_id: str | None,
) -> str:
    """Return the filename to use in the Content-Disposition header.

    For sidecar files (``literature_usage_table.md``,
    ``self_check_report.{md,json}``, ``manifest.json``) returns the
    original name. For manuscript outputs returns
    ``{slug}.{ext}`` where ``{slug}`` is derived from
    ``project_title`` via :func:`slug_from_title`.
    """
    if is_sidecar_filename(disk_filename):
        return disk_filename
    slug = slug_from_title(project_title, run_id=run_id)
    if "." in disk_filename:
        ext = disk_filename.rsplit(".", 1)[1]
        return f"{slug}.{ext}"
    return slug


def encode_content_disposition(filename: str) -> str:
    """Build a Content-Disposition value that supports non-ASCII
    filenames (RFC 5987 ``filename*=UTF-8''…``) AND has an ASCII
    ``filename=`` fallback so older HTTP clients see something useful.
    """
    ascii_safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    if not ascii_safe.strip("_"):
        ascii_safe = "manuscript"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_safe}\"; filename*=UTF-8''{encoded}"
