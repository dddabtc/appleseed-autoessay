#!/usr/bin/env python3
"""Run or prepare blind judges for ABC experiment submissions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from autoessay.experiments.abc_architecture import RESULTS_DIR_ENV  # noqa: E402
from autoessay.experiments.abc_judge import (  # noqa: E402
    judge_submission,
    list_blinded_submissions,
)
from autoessay.experiments.abc_judge_schema import JUDGE_IDS  # noqa: E402

DEFAULT_RESULTS_DIR = REPO_ROOT / "docs" / "experiments" / "abc-architecture-comparison" / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=_default_results_dir())
    parser.add_argument("--judge", choices=JUDGE_IDS)
    parser.add_argument("--submission")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run or prepare all three judges for all blinded submissions.",
    )
    parser.add_argument(
        "--manual-only",
        action="store_true",
        help="Skip live adapters and write judge-input files only.",
    )
    args = parser.parse_args(argv)

    if args.all:
        submissions = list_blinded_submissions(args.results_dir)
        if not submissions:
            raise SystemExit("No blinded submissions found.")
        for submission_uuid in submissions:
            for judge_id in JUDGE_IDS:
                result = judge_submission(
                    results_dir=args.results_dir,
                    judge_id=judge_id,
                    submission_uuid=submission_uuid,
                    allow_live=not args.manual_only,
                )
                _print_result(result.status, judge_id, submission_uuid, result.output_path)
        return 0

    if not args.judge or not args.submission:
        parser.error("--judge and --submission are required unless --all is used")
    result = judge_submission(
        results_dir=args.results_dir,
        judge_id=args.judge,
        submission_uuid=args.submission,
        allow_live=not args.manual_only,
    )
    _print_result(result.status, args.judge, args.submission, result.output_path)
    if result.manual_input_path is not None:
        print(f"manual_input: {result.manual_input_path}")
    if result.schema_path is not None:
        print(f"schema: {result.schema_path}")
    return 0


def _print_result(status: str, judge_id: str, submission_uuid: str, output_path: Path) -> None:
    print(f"{status}: {judge_id} {submission_uuid} -> {output_path}")


def _default_results_dir() -> Path:
    import os

    override = os.getenv(RESULTS_DIR_ENV, "").strip()
    return Path(override) if override else DEFAULT_RESULTS_DIR


if __name__ == "__main__":
    raise SystemExit(main())
