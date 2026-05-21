#!/usr/bin/env python3
"""Audit the A/E/G independent replication result set."""

from __future__ import annotations

import argparse
import json
import sys
from hashlib import md5, sha256
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from autoessay.experiments.abc_driver import parse_kernels  # noqa: E402
from autoessay.experiments.abc_judge_schema import JUDGE_IDS, validate_judge_output  # noqa: E402

DEFAULT_RESULTS_DIR = (
    REPO_ROOT / "docs" / "experiments" / "abc-architecture-comparison" / "results-replication"
)
DEFAULT_KERNELS_PATH = REPO_ROOT / ".codex-replication-design.md"
REQUIRED_ARMS = ("A", "E", "G")
REQUIRED_EG_PROVIDER = ("codex-cli", "gpt-5.4")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--kernels-path", type=Path, default=DEFAULT_KERNELS_PATH)
    args = parser.parse_args(argv)

    report = audit_replication(results_dir=args.results_dir, kernels_path=args.kernels_path)
    _write_json(args.results_dir / "replication-audit.json", report)
    _write_text(args.results_dir / "replication-audit.md", render_markdown(report))
    print(args.results_dir / "replication-audit.json")
    print(args.results_dir / "replication-audit.md")
    return 0 if report["passed"] else 1


def audit_replication(*, results_dir: Path, kernels_path: Path) -> dict[str, Any]:
    kernels = parse_kernels(kernels_path)
    expected_kernel_ids = [kernel.kernel_id for kernel in kernels]
    checks: list[dict[str, Any]] = []
    manuscripts: dict[str, dict[str, str]] = {}

    _check(checks, "kernel_count", len(expected_kernel_ids) == 10, {"kernel_ids": expected_kernel_ids})

    for kernel_id in expected_kernel_ids:
        for arm in REQUIRED_ARMS:
            arm_dir = results_dir / kernel_id / arm
            manuscript_path = arm_dir / "manuscript.md"
            provenance_path = arm_dir / "provenance.json"
            _check(checks, f"{kernel_id}/{arm}/files", manuscript_path.is_file() and provenance_path.is_file())
            if not manuscript_path.is_file() or not provenance_path.is_file():
                continue
            manuscript = manuscript_path.read_text(encoding="utf-8")
            provenance = _load_json(provenance_path)
            manuscripts[f"{kernel_id}/{arm}"] = {
                "md5": md5(manuscript.encode("utf-8")).hexdigest(),
                "sha256": sha256(manuscript.encode("utf-8")).hexdigest(),
            }
            usage = _dict(provenance.get("token_usage"))
            _check(
                checks,
                f"{kernel_id}/{arm}/token_cap",
                int(usage.get("total_tokens") or 0) > 0
                and not bool(usage.get("budget_exceeded")),
                usage,
            )
            if arm in {"E", "G"}:
                _check_eg(checks, kernel_id, arm, provenance, arm_dir)
            if arm == "A":
                _check_a(checks, kernel_id, provenance)

    md5_values = [value["md5"] for value in manuscripts.values()]
    sha_values = [value["sha256"] for value in manuscripts.values()]
    _check(
        checks,
        "manuscript_hash_uniqueness",
        len(md5_values) == len(set(md5_values)) and len(sha_values) == len(set(sha_values)),
        manuscripts,
    )
    _check_blinding_and_judges(checks, results_dir, expected_kernel_ids)

    return {
        "schema_version": "abc_replication_audit_v1",
        "results_dir": str(results_dir),
        "kernels_path": str(kernels_path),
        "required_arms": list(REQUIRED_ARMS),
        "required_judges": list(JUDGE_IDS),
        "required_eg_provider": {
            "provider": REQUIRED_EG_PROVIDER[0],
            "provider_model": REQUIRED_EG_PROVIDER[1],
        },
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def _check_eg(
    checks: list[dict[str, Any]],
    kernel_id: str,
    arm: str,
    provenance: dict[str, Any],
    arm_dir: Path,
) -> None:
    provider = str(provenance.get("provider") or "")
    provider_model = str(provenance.get("provider_model") or "")
    _check(
        checks,
        f"{kernel_id}/{arm}/provider",
        (provider, provider_model) == REQUIRED_EG_PROVIDER,
        {"provider": provider, "provider_model": provider_model},
    )
    ars = _dict(provenance.get("ars_single_call"))
    stage_calls = ars.get("stage_calls")
    _check(
        checks,
        f"{kernel_id}/{arm}/llm_call_count",
        ars.get("phase_count") == 1
        and ars.get("llm_call_count") == 1
        and isinstance(stage_calls, list)
        and len(stage_calls) == 1,
        ars,
    )
    claim_audit = _dict(ars.get("claim_audit"))
    _check(checks, f"{kernel_id}/{arm}/claim_audit", claim_audit.get("enabled") is True, claim_audit)
    state_path = arm_dir / "state.json"
    _check(checks, f"{kernel_id}/{arm}/state_tracking", state_path.is_file(), {"state_path": str(state_path)})


def _check_a(checks: list[dict[str, Any]], kernel_id: str, provenance: dict[str, Any]) -> None:
    pipeline = _dict(provenance.get("appleseed_pipeline"))
    _check(
        checks,
        f"{kernel_id}/A/phase_count",
        pipeline.get("phase_count_expected") == 13
        and len(pipeline.get("phase_order") or []) == 13,
        pipeline,
    )
    _check(
        checks,
        f"{kernel_id}/A/llm_usage_audit",
        int(pipeline.get("llm_call_count") or 0) > 0
        and isinstance(pipeline.get("per_stage_token_usage"), list)
        and bool(pipeline.get("per_stage_token_usage")),
        pipeline,
    )
    _check(
        checks,
        f"{kernel_id}/A/state_tracking",
        bool(_dict(pipeline.get("state_tracking")).get("run_dir")),
        _dict(pipeline.get("state_tracking")),
    )


def _check_blinding_and_judges(
    checks: list[dict[str, Any]], results_dir: Path, expected_kernel_ids: list[str]
) -> None:
    blind_map_path = results_dir / "blind_map.json"
    _check(checks, "blind_map_exists", blind_map_path.is_file(), {"path": str(blind_map_path)})
    if not blind_map_path.is_file():
        return
    blind_map = _load_json(blind_map_path)
    submissions = blind_map.get("submissions")
    if not isinstance(submissions, list):
        _check(checks, "blind_map_shape", False)
        return
    expected_count = len(expected_kernel_ids) * len(REQUIRED_ARMS)
    _check(checks, "blind_submission_count", len(submissions) == expected_count, {"count": len(submissions)})
    seen_pairs: set[tuple[str, str]] = set()
    for entry in submissions:
        if not isinstance(entry, dict):
            continue
        kernel_id = str(entry.get("kernel_id") or "")
        arm = str(entry.get("arm") or "")
        submission_uuid = str(entry.get("submission_uuid") or "")
        seen_pairs.add((kernel_id, arm))
        submission_dir = results_dir / kernel_id / "blinded" / submission_uuid
        _check(
            checks,
            f"blind/{submission_uuid}/manuscript",
            (submission_dir / "manuscript.md").is_file(),
            {"kernel_id": kernel_id, "arm": arm},
        )
        for judge_id in JUDGE_IDS:
            judge_path = submission_dir / f"judge-{judge_id}.json"
            ok = False
            detail: Any = {"path": str(judge_path)}
            if judge_path.is_file():
                payload = _load_json(judge_path)
                ok, errors = validate_judge_output(payload)
                ok = ok and payload.get("judge_id") == judge_id and payload.get("submission_uuid") == submission_uuid
                detail = errors if errors else {"judge_id": judge_id}
            _check(checks, f"judge/{submission_uuid}/{judge_id}", ok, detail)
    expected_pairs = {(kernel_id, arm) for kernel_id in expected_kernel_ids for arm in REQUIRED_ARMS}
    _check(checks, "blind_kernel_arm_coverage", seen_pairs == expected_pairs, {"seen_pairs": sorted(seen_pairs)})
    forbidden = list(results_dir.glob("*/blinded/*/judge-*mini*.json")) + list(
        results_dir.glob("*/blinded/*/judge-minimax*.json")
    )
    manual_inputs = list(results_dir.glob("*/blinded/*/judge-input-*.md"))
    _check(checks, "no_mini_or_minimax_judges", not forbidden, {"forbidden": [str(path) for path in forbidden]})
    _check(checks, "no_manual_fallback_inputs", not manual_inputs, {"manual_inputs": [str(path) for path in manual_inputs]})


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ABC Replication Audit",
        "",
        f"- Passed: {report['passed']}",
        f"- Results dir: `{report['results_dir']}`",
        f"- Required arms: {', '.join(report['required_arms'])}",
        f"- Required judges: {', '.join(report['required_judges'])}",
        "",
        "| Check | Passed |",
        "|---|---:|",
    ]
    for check in report["checks"]:
        lines.append(f"| `{check['id']}` | {check['passed']} |")
    lines.append("")
    return "\n".join(lines)


def _check(
    checks: list[dict[str, Any]],
    check_id: str,
    passed: bool,
    details: Any | None = None,
) -> None:
    payload = {"id": check_id, "passed": bool(passed)}
    if details is not None:
        payload["details"] = details
    checks.append(payload)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return payload


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
