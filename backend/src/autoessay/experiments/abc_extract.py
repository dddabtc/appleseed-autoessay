"""Extract A's front-half artifacts into the ABC evidence package."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from autoessay.experiments.abc_architecture import EXPERIMENT_ID

FRONT_HALF_PACKAGE_SCHEMA = "abc_front_half_package_v1"

ALLOWED_FRONT_HALF_ARTIFACTS: tuple[str, ...] = (
    "discovery/scout_report.md",
    "sources/shortlist.json",
    "synthesis/claims.jsonl",
    "synthesis/synthesizer.json",
    "synthesis/tension_extraction.json",
    "synthesis/framework_lens.json",
)

PROHIBITED_PATH_PARTS: frozenset[str] = frozenset(
    {
        "ideator",
        "drafter",
        "drafts",
        "stylist",
        "style",
        "final_rewrite",
        "rewrite",
        "critic",
        "reviews",
        "integrity",
        "exports",
    }
)


@dataclass(frozen=True)
class KernelMetadata:
    """Kernel-level inputs shared by all generated arms."""

    title: str
    research_kernel: Mapping[str, object]
    target_journal: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "title": self.title,
            "research_kernel": dict(self.research_kernel),
            "target_journal": self.target_journal,
        }


@dataclass(frozen=True)
class FrontHalfPackagePaths:
    package_json: Path
    package_md: Path
    package_sha256: Path
    kernel_json: Path


def dump_front_half_package(
    *,
    run_dir: str | Path,
    results_dir: str | Path,
    kernel_id: str,
    a_run_id: str | None = None,
    metadata: KernelMetadata | None = None,
) -> FrontHalfPackagePaths:
    """Write package.json/package.md/package.sha256 for one A run_dir.

    Only the protocol-approved front-half artifact paths are read. Missing
    approved artifacts are represented in the structured package instead of
    raising, because tension/lens can legitimately be skipped in some A runs.
    """
    run_root = Path(run_dir)
    if not run_root.is_dir():
        raise FileNotFoundError(f"A run_dir does not exist: {run_root}")

    kernel_root = Path(results_dir) / kernel_id
    front_half_dir = kernel_root / "front_half"
    front_half_dir.mkdir(parents=True, exist_ok=True)

    artifacts = [
        _read_allowed_artifact(run_root, relative_path)
        for relative_path in ALLOWED_FRONT_HALF_ARTIFACTS
    ]
    metadata_payload = metadata.to_json() if metadata is not None else _empty_metadata()
    package_payload: dict[str, object] = {
        "schema_version": FRONT_HALF_PACKAGE_SCHEMA,
        "experiment_id": EXPERIMENT_ID,
        "kernel_id": kernel_id,
        "a_run_id": a_run_id,
        "a_run_dir": str(run_root),
        "metadata": metadata_payload,
        "allowed_artifacts": list(ALLOWED_FRONT_HALF_ARTIFACTS),
        "artifacts": artifacts,
        "created_at": _utc_now(),
    }
    package_md = _render_package_markdown(
        kernel_id=kernel_id,
        metadata=metadata_payload,
        artifacts=artifacts,
    )
    package_hash = _sha256_text(package_md)
    package_payload["package_md_sha256"] = package_hash

    package_json_path = front_half_dir / "package.json"
    package_md_path = front_half_dir / "package.md"
    package_sha_path = front_half_dir / "package.sha256"
    kernel_json_path = kernel_root / "kernel.json"

    _write_json(package_json_path, package_payload)
    _write_text(package_md_path, package_md)
    _write_text(package_sha_path, package_hash + "\n")
    _write_json(
        kernel_json_path,
        {
            "schema_version": "abc_kernel_metadata_v1",
            "experiment_id": EXPERIMENT_ID,
            "kernel_id": kernel_id,
            "a_run_id": a_run_id,
            "a_run_dir": str(run_root),
            **metadata_payload,
        },
    )
    return FrontHalfPackagePaths(
        package_json=package_json_path,
        package_md=package_md_path,
        package_sha256=package_sha_path,
        kernel_json=kernel_json_path,
    )


def load_kernel_metadata(results_dir: str | Path, kernel_id: str) -> KernelMetadata:
    """Load kernel metadata written by dump-front-half."""
    kernel_path = Path(results_dir) / kernel_id / "kernel.json"
    if kernel_path.exists():
        payload = _load_json_object(kernel_path)
    else:
        package_path = Path(results_dir) / kernel_id / "front_half" / "package.json"
        payload = _load_json_object(package_path).get("metadata", {})
        if not isinstance(payload, dict):
            payload = {}
    title = str(payload.get("title") or "").strip()
    research_kernel = payload.get("research_kernel")
    target_journal = payload.get("target_journal")
    if not title:
        raise ValueError(f"Missing kernel title for {kernel_id}; run dump-front-half first.")
    if not isinstance(research_kernel, dict):
        raise ValueError(f"Missing research_kernel for {kernel_id}; run dump-front-half first.")
    return KernelMetadata(
        title=title,
        research_kernel=dict(research_kernel),
        target_journal=str(target_journal).strip() if target_journal else None,
    )


def package_sha256(results_dir: str | Path, kernel_id: str) -> str:
    """Return package.md sha256 from package.sha256 or by hashing package.md."""
    front_half_dir = Path(results_dir) / kernel_id / "front_half"
    sha_path = front_half_dir / "package.sha256"
    if sha_path.exists():
        value = sha_path.read_text(encoding="utf-8").strip()
        if value:
            return value
    package_md = (front_half_dir / "package.md").read_text(encoding="utf-8")
    return _sha256_text(package_md)


def _read_allowed_artifact(run_root: Path, relative_path: str) -> dict[str, object]:
    _assert_allowed_relative_path(relative_path)
    path = run_root / relative_path
    if not path.exists():
        return {
            "path": relative_path,
            "present": False,
            "reason": "missing_or_skipped",
        }
    raw = path.read_text(encoding="utf-8")
    artifact: dict[str, object] = {
        "path": relative_path,
        "present": True,
        "sha256": _sha256_text(raw),
    }
    if relative_path.endswith(".json"):
        artifact["content_type"] = "json"
        artifact["data"] = _loads_json(raw)
    elif relative_path.endswith(".jsonl"):
        rows, errors = _loads_jsonl(raw)
        artifact["content_type"] = "jsonl"
        artifact["data"] = rows
        if errors:
            artifact["parse_errors"] = errors
    else:
        artifact["content_type"] = "markdown"
        artifact["text"] = raw
    return artifact


def _assert_allowed_relative_path(relative_path: str) -> None:
    parts = set(Path(relative_path).parts)
    if parts & PROHIBITED_PATH_PARTS:
        raise ValueError(f"Prohibited A artifact path requested: {relative_path}")
    if relative_path not in ALLOWED_FRONT_HALF_ARTIFACTS:
        raise ValueError(f"Path is not in the ABC front-half allowlist: {relative_path}")


def _render_package_markdown(
    *,
    kernel_id: str,
    metadata: Mapping[str, object],
    artifacts: list[dict[str, object]],
) -> str:
    lines: list[str] = [
        "# ABC Front-Half Evidence Package",
        "",
        f"Kernel ID: `{kernel_id}`",
        f"Title: {metadata.get('title') or ''}",
        f"Target journal: {metadata.get('target_journal') or ''}",
        "",
        "## Research Kernel",
        "",
        "```json",
        json.dumps(
            metadata.get("research_kernel") or {},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## Approved Front-Half Artifacts",
        "",
    ]
    for artifact in artifacts:
        artifact_path = str(artifact["path"])
        lines.extend([f"### {artifact_path}", ""])
        if not artifact.get("present"):
            lines.extend(["Artifact missing or skipped.", ""])
            continue
        content_type = artifact.get("content_type")
        if content_type == "markdown":
            text = str(artifact.get("text") or "").strip()
            lines.extend([text, ""])
        else:
            lines.extend(
                [
                    "```json",
                    json.dumps(
                        artifact.get("data"),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _loads_json(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"parse_error": str(exc), "raw_text": raw}


def _loads_jsonl(raw: str) -> tuple[list[object], list[dict[str, object]]]:
    rows: list[object] = []
    errors: list[dict[str, object]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except json.JSONDecodeError as exc:
            errors.append(
                {
                    "line": line_number,
                    "error": str(exc),
                    "raw_text": line,
                }
            )
    return rows, errors


def _load_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return data


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _empty_metadata() -> dict[str, object]:
    return {"title": "", "research_kernel": {}, "target_journal": None}
