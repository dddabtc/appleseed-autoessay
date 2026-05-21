#!/usr/bin/env python3
"""CLI driver for ABC architecture comparison Phase 4."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from autoessay.experiments.abc_driver import (  # noqa: E402
    DEFAULT_API_BASE,
    DEFAULT_DOMAIN_ID,
    DEFAULT_KERNELS_PATH,
    DEFAULT_RESULTS_DIR,
    DEFAULT_MAX_CONCURRENCY,
    MAX_ALLOWED_CONCURRENCY,
    driver_options_from_env,
    run_driver,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    kernel_ids = _split_kernel_ids(args.kernels)
    options = driver_options_from_env(
        kernels_path=args.kernels_path,
        results_dir=args.results_dir,
        state_path=args.state_path,
        api_base=args.api_base,
        all_kernels=args.all,
        smoke_kernel_id=args.smoke,
        kernel_ids=kernel_ids,
        dry_run=args.dry_run,
        resume=args.resume,
        force=args.force,
        poll_interval_seconds=args.poll_interval,
        run_timeout_seconds=args.run_timeout_seconds,
        domain_id=args.domain_id,
        username=args.username,
        password=args.password,
        session_cookie=args.session_cookie,
        max_concurrency=args.max_concurrency,
        arms=_split_arms(args.arms),
    )
    result = run_driver(options)
    if result.dry_run:
        print("DRY RUN")
        for action in result.planned_actions:
            print(f"- {action}")
        return 0
    print(f"state: {result.state_path}")
    print(f"manifest: {result.manifest_path}")
    print(f"kernels: {', '.join(result.selected_kernel_ids)}")
    print(
        f"completed: {len(result.completed_kernel_ids)}/{len(result.selected_kernel_ids)}"
    )
    if result.blocked_kernel_ids:
        print(f"blocked: {', '.join(result.blocked_kernel_ids)}")
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--all", action="store_true", help="Run all kernels in kernels.md."
    )
    target.add_argument(
        "--smoke", metavar="KERNEL_ID", help="Run one kernel end to end."
    )
    target.add_argument(
        "--kernels",
        metavar="ID[,ID...]",
        help="Run a comma-separated subset of kernels.",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help=f"Defaults to AUTOESSAY_ABC_API_BASE or {DEFAULT_API_BASE}.",
    )
    parser.add_argument("--kernels-path", type=Path, default=DEFAULT_KERNELS_PATH)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument("--domain-id", default=DEFAULT_DOMAIN_ID)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument(
        "--run-timeout-seconds",
        type=float,
        default=0.0,
        help="0 means no timeout.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        help=(
            "Kernel-level concurrency. "
            f"Defaults to AUTOESSAY_ABC_MAX_CONCURRENCY or {DEFAULT_MAX_CONCURRENCY}; "
            f"maximum {MAX_ALLOWED_CONCURRENCY}."
        ),
    )
    parser.add_argument(
        "--arms",
        default=None,
        help="Comma-separated arms to generate. Defaults to AUTOESSAY_ABC_ARMS or A,B,B_prime,C.",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="API username. Defaults to AUTOESSAY_API_USERNAME when set.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="API password. Defaults to AUTOESSAY_API_PASSWORD when set.",
    )
    parser.add_argument(
        "--session-cookie",
        default=None,
        help="Existing autoessay_session cookie. Defaults to AUTOESSAY_SESSION_COOKIE when set.",
    )
    return parser


def _split_kernel_ids(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _split_arms(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(item.strip() for item in value.split(",") if item.strip())


if __name__ == "__main__":
    raise SystemExit(main())
