#!/usr/bin/env python3
"""Generate per-FR test-report.md files from a Playwright JSON
reporter dump.

Inputs:
  --results PATH       playwright JSON reporter output (default:
                       tmp/qa-artifacts/<sha>/<run>/results.json)
  --commit SHA         commit sha (default: `git rev-parse HEAD`)
  --branch NAME        branch (default: `git symbolic-ref --short HEAD`)
  --env LABEL          environment label, e.g. ``ci`` / ``local``
                       (default: env `GITHUB_ACTIONS` → ``ci`` else
                       ``local``)
  --features-dir PATH  features directory (default: docs/qa/features)
  --verdicts-dir PATH  per-case codex critic verdict dir (optional)
  --dry-run            print diffs to stdout, don't write

Output:
  Writes / overwrites ``test-report.md`` under each FR directory it
  finds matching test rows for.

Conventions:
  - test name format: ``FR-NN.SS.CC <free-form title>`` —
    qa-id is the leading token, FR-NN identifies the feature dir.
  - test names without an FR-NN prefix are ignored.
  - rows are sorted by (qa-id, run-start) so reports diff cleanly.

Codex round-1 verdict (2026-05-06): Q2=B, generator owns the
report. Stable ordering is required so successive CI runs produce
clean diffs.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


QA_ID_RE = re.compile(r"^(FR-(\d{2}))\.(\d{2})\.(\d{2})\b")


def _git(args: list[str], default: str = "") -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except subprocess.CalledProcessError:
        return default


def _detect_env() -> str:
    return "ci" if os.getenv("GITHUB_ACTIONS") else "local"


def _parse_qa_id(test_title: str) -> tuple[str, str] | None:
    """Return (qa-id, fr-dir-prefix) or None if title isn't tagged."""
    m = QA_ID_RE.match(test_title.strip())
    if not m:
        return None
    qa_id = f"{m.group(1)}.{m.group(3)}.{m.group(4)}"
    fr_prefix = m.group(1)  # e.g. "FR-01"
    return qa_id, fr_prefix


def _walk_specs(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Playwright JSON reporter into per-test rows.

    Playwright JSON reporter shape:
      {"suites": [{"specs": [{"title": "...", "tests": [{"results":
      [{"status":"passed","duration":1234,...}]}]}]}]}
    """
    out: list[dict[str, Any]] = []

    def visit(suite: dict[str, Any], parent_titles: list[str]) -> None:
        for spec in suite.get("specs", []):
            titles = parent_titles + [spec.get("title", "")]
            full_title = " > ".join(t for t in titles if t)
            for test in spec.get("tests", []):
                results_arr = test.get("results", [])
                if not results_arr:
                    continue
                # Take last attempt (after retries)
                r = results_arr[-1]
                out.append(
                    {
                        "title": spec.get("title", ""),
                        "full_title": full_title,
                        "status": r.get("status", "unknown"),
                        "duration_ms": r.get("duration", 0),
                        "start": r.get("startTime"),
                        "trace": next(
                            (
                                a.get("path", "")
                                for a in r.get("attachments", [])
                                if a.get("name") == "trace"
                            ),
                            "",
                        ),
                        "screenshot": next(
                            (
                                a.get("path", "")
                                for a in r.get("attachments", [])
                                if a.get("name") == "screenshot"
                            ),
                            "",
                        ),
                    }
                )
        for child in suite.get("suites", []):
            visit(child, parent_titles + [suite.get("title", "")])

    for s in results.get("suites", []):
        visit(s, [])
    return out


def _status_emoji(status: str) -> str:
    return {
        "passed": "✅ pass",
        "failed": "❌ fail",
        "skipped": "⏭ skip",
        "timedOut": "⏱ timeout",
        "interrupted": "🛑 interrupt",
        "quarantine": "🟡 quarantine",
    }.get(status, status)


def _short(path: str, max_len: int = 60) -> str:
    if not path or len(path) <= max_len:
        return path
    return f"…{path[-max_len:]}"


def _load_verdict(verdicts_dir: Path | None, qa_id: str) -> dict[str, Any] | None:
    if not verdicts_dir:
        return None
    f = verdicts_dir / f"{qa_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except json.JSONDecodeError:
        return None


def _render_report(
    fr_prefix: str,
    rows: list[dict[str, Any]],
    metadata: dict[str, str],
    verdicts_dir: Path | None,
) -> str:
    rows_sorted = sorted(rows, key=lambda r: (r["qa_id"], r.get("start") or ""))
    pass_n = sum(1 for r in rows if r["status"] == "passed")
    fail_n = sum(
        1 for r in rows if r["status"] in ("failed", "timedOut", "interrupted")
    )
    skip_n = sum(1 for r in rows if r["status"] == "skipped")
    quar_n = sum(1 for r in rows if r["status"] == "quarantine")

    lines: list[str] = [
        f"# {fr_prefix} — test report",
        "",
        "> **Auto-generated** by `scripts/qa-report-gen.py`. Hand-edits will be overwritten.",
        "",
        "## Run metadata",
        "",
        "| field | value |",
        "|-------|-------|",
        f"| Commit | `{metadata['commit']}` |",
        f"| Branch | `{metadata['branch']}` |",
        f"| Environment | {metadata['env']} |",
        f"| Generated at | {metadata['generated_at']} |",
        f"| Run id | {metadata.get('run_id', '—')} |",
        "",
        "## Per-case results",
        "",
        "| qa-id | title | status | duration | trace | verdict |",
        "|-------|-------|--------|---------:|-------|---------|",
    ]
    for row in rows_sorted:
        verdict = _load_verdict(verdicts_dir, row["qa_id"])
        verdict_text = "—"
        if verdict:
            verdict_text = (
                f"{verdict.get('verdict', '—')} "
                f"(score {verdict.get('score_0_5', '?')}/5, "
                f"rubric {verdict.get('rubric_version', '?')})"
            )
        duration_s = f"{(row['duration_ms'] or 0) / 1000:.1f}s"
        trace_text = _short(row["trace"]) or "—"
        lines.append(
            f"| `{row['qa_id']}` | {row['title']} | {_status_emoji(row['status'])} "
            f"| {duration_s} | {trace_text} | {verdict_text} |"
        )

    p0_failed = any(
        r["status"] in ("failed", "timedOut") and r["qa_id"].startswith(fr_prefix)
        for r in rows
    )
    p0_gate = "🔴 RED" if p0_failed else "🟢 GREEN"
    lines += [
        "",
        "## Aggregate",
        "",
        "| status | count |",
        "|--------|------:|",
        f"| pass | {pass_n} |",
        f"| fail | {fail_n} |",
        f"| skip | {skip_n} |",
        f"| quarantine | {quar_n} |",
        "",
        f"## P0 gate: {p0_gate}",
        "",
        "Green when all P0 cases pass; red on any P0 fail.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--commit", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--env", default=None)
    parser.add_argument(
        "--features-dir",
        default=Path("docs/qa/features"),
        type=Path,
    )
    parser.add_argument("--verdicts-dir", default=None, type=Path)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.results.exists():
        print(f"results file not found: {args.results}", file=sys.stderr)
        return 2

    raw = json.loads(args.results.read_text())
    flat = _walk_specs(raw)

    by_fr: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in flat:
        parsed = _parse_qa_id(row["title"])
        if not parsed:
            continue
        qa_id, fr_prefix = parsed
        row["qa_id"] = qa_id
        by_fr[fr_prefix].append(row)

    metadata = {
        "commit": args.commit or _git(["rev-parse", "HEAD"], "unknown")[:12],
        "branch": args.branch
        or _git(["symbolic-ref", "--short", "HEAD"], "unknown"),
        "env": args.env or _detect_env(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "run_id": args.run_id or "—",
    }

    if not by_fr:
        print(
            "no qa-id-tagged tests found in results; nothing to write",
            file=sys.stderr,
        )
        return 0

    written = 0
    for fr_prefix, rows in sorted(by_fr.items()):
        # Find the FR dir matching this prefix (case-insensitive).
        candidates = list(
            args.features_dir.glob(f"{fr_prefix}-*")
        )
        if not candidates:
            print(
                f"warn: no features dir for {fr_prefix} under "
                f"{args.features_dir}; skipping",
                file=sys.stderr,
            )
            continue
        fr_dir = candidates[0]
        report = _render_report(
            fr_prefix, rows, metadata, args.verdicts_dir
        )
        target = fr_dir / "test-report.md"
        if args.dry_run:
            print(f"--- would write {target} ---")
            print(report)
            print(f"--- end {target} ---\n")
        else:
            target.write_text(report)
            print(f"wrote {target} ({len(rows)} cases)")
            written += 1

    if not args.dry_run:
        print(f"\ndone: {written} report(s) written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
