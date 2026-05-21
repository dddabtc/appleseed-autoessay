#!/usr/bin/env python3
"""CLI for ABC architecture comparison Phase 1 artifacts."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from sqlalchemy.orm import Session  # noqa: E402

from autoessay.db import make_engine  # noqa: E402
from autoessay.experiments.abc_architecture import RESULTS_DIR_ENV  # noqa: E402
from autoessay.experiments.abc_extract import (  # noqa: E402
    KernelMetadata,
    dump_front_half_package,
)
from autoessay.experiments.abc_generator import generate_arm  # noqa: E402
from autoessay.models import Project, Run  # noqa: E402

DEFAULT_RESULTS_DIR = (
    REPO_ROOT / "docs" / "experiments" / "abc-architecture-comparison" / "results"
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "dump-front-half":
        metadata, run_dir = _resolve_run(args.run_id, args.run_dir)
        paths = dump_front_half_package(
            run_dir=run_dir,
            results_dir=args.results_dir,
            kernel_id=args.kernel_id,
            a_run_id=args.run_id,
            metadata=metadata,
        )
        print(paths.package_md)
        return 0
    if args.command == "generate":
        result = asyncio.run(
            generate_arm(
                kernel_id=args.kernel_id,
                arm=args.arm,
                results_dir=args.results_dir,
            )
        )
        print(result.manuscript_path)
        return 0
    parser.error("unknown command")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    dump = subparsers.add_parser("dump-front-half")
    dump.add_argument("--run-id", required=True)
    dump.add_argument("--kernel-id", required=True)
    dump.add_argument("--results-dir", type=Path, default=_default_results_dir())
    dump.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Optional direct A run_dir override; metadata is still resolved from DB when possible.",
    )

    generate = subparsers.add_parser("generate")
    generate.add_argument("--kernel-id", required=True)
    generate.add_argument(
        "--arm",
        choices=("A", "B", "B_prime", "C", "E", "F", "G"),
        required=True,
    )
    generate.add_argument("--results-dir", type=Path, default=_default_results_dir())
    return parser


def _default_results_dir() -> Path:
    import os

    override = os.getenv(RESULTS_DIR_ENV, "").strip()
    return Path(override) if override else DEFAULT_RESULTS_DIR


def _resolve_run(
    run_id: str, run_dir_override: Path | None
) -> tuple[KernelMetadata, Path]:
    engine = make_engine()
    try:
        with Session(engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                if run_dir_override is None:
                    raise SystemExit(
                        f"Run {run_id!r} not found and --run-dir was not provided."
                    )
                return KernelMetadata(
                    title="", research_kernel={}, target_journal=None
                ), run_dir_override
            project = session.get(Project, run.project_id)
            if project is None:
                raise SystemExit(
                    f"Project {run.project_id!r} for run {run_id!r} not found."
                )
            metadata = KernelMetadata(
                title=project.title,
                research_kernel=_research_kernel(run.research_kernel_json),
                target_journal=project.target_journal,
            )
            return metadata, run_dir_override or _host_run_dir(Path(run.run_dir))
    finally:
        engine.dispose()


def _host_run_dir(run_dir: Path) -> Path:
    """Translate container /data paths when the script runs on the host."""
    if run_dir.parts[:2] != ("/", "data"):
        return run_dir
    host_data_dir = os.getenv("AUTOESSAY_HOST_DATA_DIR", "").strip()
    if not host_data_dir:
        configured_data_dir = os.getenv("AUTOESSAY_DATA_DIR", "").strip()
        if configured_data_dir and configured_data_dir != "/data":
            host_data_dir = configured_data_dir
    if not host_data_dir:
        return run_dir
    return Path(host_data_dir).joinpath(*run_dir.parts[2:])


def _research_kernel(raw: Any) -> dict[str, object]:
    return dict(raw) if isinstance(raw, dict) else {"kernel_schema_version": 1}


if __name__ == "__main__":
    raise SystemExit(main())
