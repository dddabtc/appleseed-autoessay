#!/usr/bin/env python3
"""Append one generated ABC arm to an existing blinded result set."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from autoessay.experiments.abc_architecture import EXPERIMENT_ID, RESULTS_DIR_ENV  # noqa: E402
from autoessay.experiments.abc_blinder import sanitize_blinded_manuscript  # noqa: E402

DEFAULT_RESULTS_DIR = REPO_ROOT / "docs" / "experiments" / "abc-architecture-comparison" / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=_default_results_dir())
    parser.add_argument("--arm", required=True)
    args = parser.parse_args(argv)

    blind_map_path = args.results_dir / "blind_map.json"
    payload = _load_json_object(blind_map_path)
    if payload.get("experiment_id") != EXPERIMENT_ID:
        raise SystemExit(f"{blind_map_path} experiment_id must be {EXPERIMENT_ID}")
    submissions = payload.get("submissions")
    if not isinstance(submissions, list):
        raise SystemExit(f"{blind_map_path} submissions must be an array")

    existing = {
        (entry.get("kernel_id"), entry.get("arm"))
        for entry in submissions
        if isinstance(entry, dict)
    }
    used_uuids = {
        str(entry.get("submission_uuid"))
        for entry in submissions
        if isinstance(entry, dict) and entry.get("submission_uuid")
    }

    added: list[dict[str, str]] = []
    for manuscript_path in sorted(args.results_dir.glob(f"*/{args.arm}/manuscript.md")):
        kernel_id = manuscript_path.parents[1].name
        if (kernel_id, args.arm) in existing:
            continue
        submission_uuid = _new_uuid(used_uuids)
        blinded_path = args.results_dir / kernel_id / "blinded" / submission_uuid / "manuscript.md"
        blinded = sanitize_blinded_manuscript(manuscript_path.read_text(encoding="utf-8"))
        _write_text(blinded_path, blinded)
        entry = {
            "submission_uuid": submission_uuid,
            "kernel_id": kernel_id,
            "arm": args.arm,
        }
        submissions.append(entry)
        added.append(entry)
        existing.add((kernel_id, args.arm))

    payload["updated_at"] = _utc_now()
    _write_json(blind_map_path, payload)
    print(f"blind_map: {blind_map_path}")
    print(f"added: {len(added)}")
    for entry in added:
        print(f"{entry['kernel_id']} {entry['arm']} {entry['submission_uuid']}")
    return 0


def _default_results_dir() -> Path:
    import os

    override = os.getenv(RESULTS_DIR_ENV, "").strip()
    return Path(override) if override else DEFAULT_RESULTS_DIR


def _new_uuid(used_uuids: set[str]) -> str:
    while True:
        value = str(uuid4())
        if value not in used_uuids:
            used_uuids.add(value)
            return value


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return data


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
