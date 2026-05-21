#!/usr/bin/env python3
"""Build the blinded ABC experiment submission set."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from autoessay.experiments.abc_architecture import RESULTS_DIR_ENV
from autoessay.experiments.abc_blinder import build_blindset

DEFAULT_RESULTS_DIR = REPO_ROOT / "docs" / "experiments" / "abc-architecture-comparison" / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=_default_results_dir())
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild existing blinded folders and blind_map.json.",
    )
    args = parser.parse_args(argv)

    result = build_blindset(results_dir=args.results_dir, force=args.force)
    print(f"blind_map: {result.blind_map_path}")
    print(f"submissions: {len(result.submissions)}")
    return 0


def _default_results_dir() -> Path:
    import os

    override = os.getenv(RESULTS_DIR_ENV, "").strip()
    return Path(override) if override else DEFAULT_RESULTS_DIR


if __name__ == "__main__":
    raise SystemExit(main())
