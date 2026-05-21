"""PR-D4 evaluator — score a frozen run-artifact bundle on the
acceptance vector.

Reads the artifacts produced by a real-paper / baseline walk and emits
``evaluator.json`` with structured score vector + diagnostics. Used by
the acceptance gate skeleton (`backend/scripts/run_baseline_suite.py`
generates a bundle, this script scores it; `diff_baselines.py`
compares vectors against a frozen baseline).

Codex round-1 amendments folded:
  * #1 — manuscript path: prefer ``exports/manuscript.md``, fallback
    ``drafts/v*/style/paper_styled.md``, then ``drafts/v*/manuscript.md``.
    ``evidence_ledger.jsonl`` / ``claim_map.jsonl`` are conditional —
    record ``artifact_present=false`` rather than failing.
  * #2 — ``stop_slop.score_text_static`` (no LLM) with real
    ``load_stop_slop_rules()`` phrases + structures, NOT empty.
  * #3 — ``claim_density`` from ``claim_map.jsonl`` claim count, NOT
    period heuristic. Vector uses ``manuscript_bytes`` (no naive word
    count for CN). Each field has ``direction`` metadata.
  * #4 — vector contributors per-field ``direction``: ``exact-zero``
    (P0 / fabricated_citations / fallback_events), ``higher-is-better``
    (manuscript_bytes / claim_density / cited_sources / stop_slop).
    Tolerance applied at diff time, not here.
  * #5 — J9b ``rerank_quality`` block: ``fallback_events`` in vector;
    ``scope_fit_top10_avg`` / ``verified_by_openalex_count`` /
    ``rerank_axes_coverage`` as diagnostics.
  * #11 — status enum is ``baseline_candidate`` / ``baseline_confirmed``
    (precise), not ``"candidate"`` / ``"confirmed"``.

Usage:

    python backend/scripts/evaluate_paper.py <bundle_dir> \
        [--output evaluator.json]

When ``--output`` is omitted, writes to ``<bundle_dir>/evaluator.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The evaluator MUST be runnable without booting the FastAPI app — it
# only imports two helpers from the autoessay package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "backend" / "src"))

from autoessay.stop_slop import (  # noqa: E402  isort:skip
    load_stop_slop_rules,
    score_text_static,
)


# ----------------------------------------------------------------------
# Acceptance-vector field definitions (codex round-1 A4).
# ``direction`` values:
#   exact-zero         — must be 0; any positive integer = regression
#   higher-is-better   — drop below baseline beyond tolerance = regression
# ----------------------------------------------------------------------
VECTOR_FIELDS: list[dict[str, Any]] = [
    {"name": "integrity_p0", "direction": "exact-zero"},
    {"name": "fabricated_citations", "direction": "exact-zero"},
    {"name": "fallback_events", "direction": "exact-zero"},
    {"name": "manuscript_bytes", "direction": "higher-is-better"},
    {"name": "claim_density", "direction": "higher-is-better"},
    {"name": "stop_slop_total", "direction": "higher-is-better"},
    {"name": "manuscript_citations", "direction": "higher-is-better"},
]


SCHEMA_VERSION = 1
BASELINE_STATUS_CANDIDATE = "baseline_candidate"
BASELINE_STATUS_CONFIRMED = "baseline_confirmed"


def evaluate_run_bundle(
    bundle_dir: Path,
) -> dict[str, Any]:
    """Score a frozen run-artifact bundle. Returns the full evaluator
    payload (does not write to disk; the CLI wrapper handles that)."""
    bundle_dir = Path(bundle_dir)
    if not bundle_dir.exists():
        raise FileNotFoundError(f"bundle dir not found: {bundle_dir}")

    manuscript_text, manuscript_path, manuscript_source = _read_manuscript(bundle_dir)
    integrity = _read_integrity_summary(bundle_dir)
    claim_map = _read_claim_map(bundle_dir)
    ledger = _read_ledger_events(bundle_dir)
    shortlist = _read_shortlist(bundle_dir)
    run_meta = _read_run_meta(bundle_dir)

    integrity_counts = _integrity_counts(integrity)
    citation_diff = _citation_diff(manuscript_text, claim_map, shortlist)
    rerank = _rerank_quality(shortlist, ledger)
    stop_slop_score = _stop_slop_static(manuscript_text)
    claim_density = _claim_density(manuscript_text, claim_map)

    scores: dict[str, Any] = {
        "integrity_p0": integrity_counts["p0"],
        "integrity_p1": integrity_counts["p1"],
        "integrity_p2": integrity_counts["p2"],
        "claim_density": claim_density,
        "manuscript_bytes": len(manuscript_text.encode("utf-8")),
        "manuscript_chars": len(manuscript_text),
        "manuscript_source": manuscript_source,
        "citation_diff": citation_diff,
        "stop_slop": stop_slop_score,
        "stop_slop_total": stop_slop_score["total"],
        "rerank_quality": rerank,
        "fallback_events": rerank["fallback_events"],
        "fabricated_citations": citation_diff["fabricated_citations"],
        "manuscript_citations": citation_diff["manuscript_citations"],
    }

    vector = [scores[field["name"]] for field in VECTOR_FIELDS]

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluated_at": _utc_now(),
        "run_id": run_meta.get("run_id"),
        "paper_mode": run_meta.get("paper_mode"),
        "domain_id": run_meta.get("domain_id"),
        "scores": scores,
        "vector": vector,
        "vector_fields": [field["name"] for field in VECTOR_FIELDS],
        "vector_directions": {field["name"]: field["direction"] for field in VECTOR_FIELDS},
        "baseline_status": BASELINE_STATUS_CANDIDATE,
        "baseline_label": run_meta.get("baseline_label")
        or run_meta.get("project_title")
        or bundle_dir.name,
        "artifacts": {
            "manuscript_path": (
                str(manuscript_path.relative_to(bundle_dir)) if manuscript_path else None
            ),
            "manuscript_present": manuscript_path is not None,
            "claim_map_present": claim_map["artifact_present"],
            "ledger_present": ledger["artifact_present"],
            "shortlist_present": shortlist["artifact_present"],
            "integrity_summary_present": integrity["artifact_present"],
        },
    }
    return payload


# ----------------------------------------------------------------------
# Artifact readers
# ----------------------------------------------------------------------


def _read_manuscript(bundle_dir: Path) -> tuple[str, Path | None, str]:
    """Codex round-1 A1: prefer exports/manuscript.md (final exporter
    output), fall back to the latest drafts/v*/style/paper_styled.md
    (post-stylist), then drafts/v*/manuscript.md (pre-stylist drafter
    output)."""
    candidates: list[tuple[Path, str]] = []
    exports_md = bundle_dir / "exports" / "manuscript.md"
    if exports_md.exists():
        candidates.append((exports_md, "exports"))
    drafts_dir = bundle_dir / "drafts"
    if drafts_dir.exists():
        for version in sorted(drafts_dir.glob("v*"), reverse=True):
            styled = version / "style" / "paper_styled.md"
            if styled.exists():
                candidates.append((styled, "drafter_styled"))
                break
        for version in sorted(drafts_dir.glob("v*"), reverse=True):
            raw = version / "manuscript.md"
            if raw.exists():
                candidates.append((raw, "drafter_raw"))
                break
    if not candidates:
        return "", None, "missing"
    path, source = candidates[0]
    return path.read_text(encoding="utf-8"), path, source


def _read_integrity_summary(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / "integrity" / "integrity_summary.json"
    if not path.exists():
        return {"artifact_present": False, "data": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"artifact_present": False, "data": {}}
    return {"artifact_present": True, "data": data}


def _integrity_counts(summary: dict[str, Any]) -> dict[str, int]:
    """Map the integrity summary into priority bucket counts.

    Current integrity_summary shape:
      ``scans.{ai_style,plagiarism}.span_count``

    Codex round-1 A1: artifact may be absent (skipped scans). Treat
    missing scan as 0 spans, not as P0. Mapping rule (kept simple for
    the skeleton — D4.1 will refine after live runs):
      * plagiarism spans → P0  (originality risk = blocker)
      * ai_style spans → P1   (reviewer reads as quality issue)
      * P2 reserved for future expansion (currently 0)
    """
    if not summary.get("artifact_present"):
        return {"p0": 0, "p1": 0, "p2": 0}
    data = summary.get("data") or {}
    scans = data.get("scans") or {}
    plag = scans.get("plagiarism") or {}
    ai_style = scans.get("ai_style") or {}
    p0 = int(plag.get("span_count", 0) or 0)
    p1 = int(ai_style.get("span_count", 0) or 0)
    return {"p0": p0, "p1": p1, "p2": 0}


def _read_claim_map(bundle_dir: Path) -> dict[str, Any]:
    """Find latest drafts/v*/claim_map.jsonl. Codex A1 + A3:
    artifact may be missing (drafter never ran) → record absence;
    don't fail."""
    drafts_dir = bundle_dir / "drafts"
    if not drafts_dir.exists():
        return {"artifact_present": False, "claims": []}
    for version in sorted(drafts_dir.glob("v*"), reverse=True):
        path = version / "claim_map.jsonl"
        if path.exists():
            claims = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            return {"artifact_present": True, "claims": claims, "path": path}
    return {"artifact_present": False, "claims": []}


def _read_ledger_events(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / "ledger.jsonl"
    if not path.exists():
        return {"artifact_present": False, "events": []}
    events = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return {"artifact_present": True, "events": events, "path": path}


def _read_shortlist(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / "sources" / "shortlist.json"
    if not path.exists():
        return {"artifact_present": False, "sources": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"artifact_present": False, "sources": []}
    if isinstance(data, list):
        sources = data
    elif isinstance(data, dict):
        sources = data.get("sources") or data.get("shortlist") or []
    else:
        sources = []
    return {"artifact_present": True, "sources": sources, "path": path}


def _read_run_meta(bundle_dir: Path) -> dict[str, Any]:
    """Pull paper_mode + domain_id + run_id from run.json + manifest if
    present. ``manifest.json`` (baseline-suite output) takes precedence
    over the raw ``run.json`` because it carries the curated baseline
    label."""
    out: dict[str, Any] = {}
    run_json = bundle_dir / "run.json"
    if run_json.exists():
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        out.update(
            {
                "run_id": data.get("run_id"),
                "domain_id": data.get("domain_id"),
            }
        )
    manifest = bundle_dir / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        out.update(
            {
                "paper_mode": data.get("paper_mode"),
                "project_title": data.get("project_title"),
                "baseline_label": data.get("baseline_label"),
                "domain_id": data.get("domain_id") or out.get("domain_id"),
                "run_id": data.get("run_id") or out.get("run_id"),
            }
        )
    return out


# ----------------------------------------------------------------------
# Score helpers
# ----------------------------------------------------------------------


def _claim_density(manuscript_text: str, claim_map: dict[str, Any]) -> float:
    """Codex A3: prefer claim_map count over period-heuristic. Density
    = claims per 1000 manuscript bytes (UTF-8). Bytes (not words) is
    safe for CN/EN mixed prose; words = English-centric."""
    if not claim_map.get("artifact_present"):
        return 0.0
    claim_count = sum(1 for _ in claim_map.get("claims", []))
    bytes_total = len(manuscript_text.encode("utf-8"))
    if bytes_total == 0:
        return 0.0
    return round(claim_count / bytes_total * 1000, 4)


# (Author Year) / (Author, Year) / [Author, Year] in either CJK or Latin
_INLINE_CITATION_RE = re.compile(r"[\(\[]\s*([\w·' .-]+?[A-Za-z一-鿿])\s*,?\s+(\d{4})\s*[\)\]]")
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")


def _citation_diff(
    manuscript_text: str,
    claim_map: dict[str, Any],
    shortlist: dict[str, Any],
) -> dict[str, Any]:
    """Build the citation-diff sub-vector.

    ``ledger_entries`` = unique source_ids referenced from claim_map
    (claim_map is the run's record of *which* sources the drafter
    actually used). When claim_map is absent, fall back to 0.
    ``manuscript_citations`` = unique inline citations (Author Year)
    + DOIs found by regex in the manuscript text.
    ``uncited_ledger`` = ledger sources never name-dropped by the
    manuscript (proxy: doesn't appear by source_id substring).
    ``fabricated_citations`` = inline citations or DOIs in manuscript
    that don't map to any source in ledger ∪ shortlist.
    """
    ledger_source_ids = _claim_map_source_ids(claim_map)
    shortlist_source_ids = {
        item.get("source_id")
        for item in shortlist.get("sources", [])
        if isinstance(item, dict) and item.get("source_id")
    }
    known_source_ids = ledger_source_ids | {sid for sid in shortlist_source_ids if sid}

    inline_citations = {match.group(0) for match in _INLINE_CITATION_RE.finditer(manuscript_text)}
    inline_dois = {match.group(0).rstrip(".,;)") for match in _DOI_RE.finditer(manuscript_text)}
    manuscript_citations = inline_citations | inline_dois

    uncited_ledger = sum(
        1
        for sid in ledger_source_ids
        if sid not in manuscript_text and not _doi_in(sid, manuscript_text)
    )

    # Fabricated = inline DOIs not matching any known source DOI (DOIs
    # in our shortlist are normalized via NormalizedSource.doi). For
    # author-year citations we cannot trivially attribute, so we don't
    # flag them here (D4.1 will add a stricter author-name match).
    known_dois = {_extract_doi(sid) for sid in known_source_ids}
    known_dois |= {
        item.get("doi")
        for item in shortlist.get("sources", [])
        if isinstance(item, dict) and item.get("doi")
    }
    known_dois -= {None, ""}
    fabricated = sum(1 for doi in inline_dois if doi not in known_dois)

    return {
        "ledger_entries": len(ledger_source_ids),
        "manuscript_citations": len(manuscript_citations),
        "inline_dois": len(inline_dois),
        "uncited_ledger": uncited_ledger,
        "fabricated_citations": fabricated,
    }


def _claim_map_source_ids(claim_map: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for claim in claim_map.get("claims", []):
        for sid in claim.get("source_ids") or []:
            if isinstance(sid, str) and sid:
                out.add(sid)
    return out


def _doi_in(source_id: str, text: str) -> bool:
    doi = _extract_doi(source_id)
    return bool(doi) and doi in text


def _extract_doi(source_id: str | None) -> str:
    """Pull the DOI out of a source_id like ``crossref:10.1177/...``
    or return empty when the source_id has no DOI shape."""
    if not source_id:
        return ""
    match = _DOI_RE.search(source_id)
    return match.group(0) if match else ""


def _rerank_quality(
    shortlist: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Codex A5: J9b signal block. ``fallback_events`` enters the
    vector (exact-zero); rest are diagnostics."""
    sources = shortlist.get("sources", [])
    rerank_axes_count = 0
    scope_fit_values: list[float] = []
    verified_openalex = 0
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            continue
        axes = item.get("rerank_axes") or {}
        if axes:
            rerank_axes_count += 1
            if index < 10 and isinstance(axes.get("scope_fit"), (int, float)):
                scope_fit_values.append(float(axes["scope_fit"]))
        if item.get("verified_by") == "openalex":
            verified_openalex += 1
    coverage = (rerank_axes_count / len(sources)) if sources else 0.0
    fallback_events = sum(
        1
        for ev in ledger.get("events", [])
        if isinstance(ev, dict)
        and (
            ev.get("event_type") == "curator_rerank_fallback"
            or ev.get("event") == "curator_rerank_fallback"
        )
    )
    return {
        "rerank_active": coverage > 0.0,
        "rerank_axes_coverage": round(coverage, 4),
        "scope_fit_top10_avg": round(sum(scope_fit_values) / len(scope_fit_values), 4)
        if scope_fit_values
        else None,
        "verified_by_openalex_count": verified_openalex,
        "fallback_events": fallback_events,
        "shortlist_size": len(sources),
    }


def _stop_slop_static(manuscript_text: str) -> dict[str, Any]:
    """Codex A2: load real phrases + structures, NOT empty. LLM
    grader force-disabled."""
    rules = load_stop_slop_rules()
    return score_text_static(
        manuscript_text,
        rules.phrases,
        rules.structures,
    )  # type: ignore[return-value]


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle_dir", type=Path, help="path to a frozen run-artifact bundle")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output evaluator.json path (default: <bundle_dir>/evaluator.json)",
    )
    parser.add_argument(
        "--print",
        action="store_true",
        help="print the evaluator JSON to stdout in addition to writing the file",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    payload = evaluate_run_bundle(args.bundle_dir)
    out = args.output or (args.bundle_dir / "evaluator.json")
    _write_json(out, payload)
    if args.print:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
