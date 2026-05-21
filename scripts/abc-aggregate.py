#!/usr/bin/env python3
"""Aggregate ABC experiment judge scores."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from autoessay.experiments.abc_aggregate import aggregate_results  # noqa: E402
from autoessay.experiments.abc_architecture import RESULTS_DIR_ENV  # noqa: E402

DEFAULT_RESULTS_DIR = REPO_ROOT / "docs" / "experiments" / "abc-architecture-comparison" / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=_default_results_dir())
    parser.add_argument(
        "--threshold-only",
        action="store_true",
        help="Print the triggered threshold without writing aggregate files.",
    )
    args = parser.parse_args(argv)

    report = aggregate_results(results_dir=args.results_dir, write_files=not args.threshold_only)
    threshold = report["threshold_decision"]
    if not isinstance(threshold, dict):
        raise SystemExit("Invalid aggregate threshold payload.")
    if args.threshold_only:
        print(f"order: {threshold['order']}")
        print(f"conclusion: {threshold['conclusion']}")
        print(f"roadmap_action: {threshold['roadmap_action']}")
    else:
        print(args.results_dir / "aggregate.json")
        print(args.results_dir / "aggregate.md")
    return 0


def _default_results_dir() -> Path:
    import os

    override = os.getenv(RESULTS_DIR_ENV, "").strip()
    return Path(override) if override else DEFAULT_RESULTS_DIR


if __name__ == "__main__":
    raise SystemExit(main())
