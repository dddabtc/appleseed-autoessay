"""Blindset construction for the ABC architecture experiment."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from autoessay.experiments.abc_architecture import EXPERIMENT_ID

ARMS: tuple[str, ...] = ("A", "B", "B_prime", "C")
EXTRA_BLINDABLE_ARMS: tuple[str, ...] = ("E", "F", "G")
BLINDABLE_ARMS: tuple[str, ...] = (*ARMS, *EXTRA_BLINDABLE_ARMS)

PHASE_NAMES: tuple[str, ...] = (
    "proposal",
    "scout",
    "curator",
    "synthesizer",
    "tension_extraction",
    "framework_lens",
    "ideator",
    "drafter",
    "stylist",
    "final_rewrite",
    "critic",
    "integrity",
    "exports",
    "polish_loop",
    "critic_loop",
)

STATE_NAMES: tuple[str, ...] = (
    "EXPORTS_DONE",
    "exports_done",
    "critic_selected",
    "missing_or_skipped",
    "provider_failed",
)

PROMPT_NAMES: tuple[str, ...] = (
    "prompt.redacted.txt",
    "prompt_sha256",
    "self_critique_prompt_sha256",
    "system prompt",
    "user prompt",
    "judge prompt",
)

METADATA_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?"
    r"(?:"
    r"arm|phase|state|prompt|provenance|generated_at|generation timestamp|"
    r"generated at|created_at|run_id|run id|model_id|provider|token_usage|"
    r"source_package_sha256|prompt_sha256|experiment_id|kernel_id"
    r")\s*[:：=].*$",
    re.IGNORECASE,
)
HTML_COMMENT_METADATA_RE = re.compile(
    r"^\s*<!--.*(?:arm|phase|state|prompt|provenance|generated|run id|kernel id).*-->\s*$",
    re.IGNORECASE,
)
FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)
ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b"
)


@dataclass(frozen=True)
class BlindSubmission:
    submission_uuid: str
    kernel_id: str
    arm: str
    manuscript_path: Path
    blinded_path: Path


@dataclass(frozen=True)
class BlindsetBuildResult:
    blind_map_path: Path
    submissions: tuple[BlindSubmission, ...]


def build_blindset(
    *,
    results_dir: str | Path,
    force: bool = False,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> BlindsetBuildResult:
    """Create blinded manuscript copies and a private blind map."""
    root = Path(results_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"Results directory does not exist: {root}")

    blind_map_path = root / "blind_map.json"
    if blind_map_path.exists() and not force:
        raise FileExistsError(f"{blind_map_path} already exists; rerun with force=True to rebuild")

    source_manuscripts = _discover_source_manuscripts(root)
    if not source_manuscripts:
        raise ValueError(f"No ABC arm manuscripts found below {root}")

    if force:
        for kernel_id in sorted({source.kernel_id for source in source_manuscripts}):
            shutil.rmtree(root / kernel_id / "blinded", ignore_errors=True)

    used_uuids: set[str] = set()
    submissions: list[BlindSubmission] = []
    for source in source_manuscripts:
        submission_uuid = _new_submission_uuid(uuid_factory, used_uuids)
        blinded_path = root / source.kernel_id / "blinded" / submission_uuid / "manuscript.md"
        blinded = sanitize_blinded_manuscript(source.manuscript_path.read_text(encoding="utf-8"))
        _write_text(blinded_path, blinded)
        submissions.append(
            BlindSubmission(
                submission_uuid=submission_uuid,
                kernel_id=source.kernel_id,
                arm=source.arm,
                manuscript_path=source.manuscript_path,
                blinded_path=blinded_path,
            )
        )

    _write_json(
        blind_map_path,
        {
            "experiment_id": EXPERIMENT_ID,
            "created_at": _utc_now(),
            "submissions": [
                {
                    "submission_uuid": submission.submission_uuid,
                    "kernel_id": submission.kernel_id,
                    "arm": submission.arm,
                }
                for submission in submissions
            ],
        },
    )
    return BlindsetBuildResult(
        blind_map_path=blind_map_path,
        submissions=tuple(submissions),
    )


def sanitize_blinded_manuscript(manuscript: str) -> str:
    """Remove protocol-disallowed arm and run metadata from a manuscript."""
    text = FRONTMATTER_RE.sub("", manuscript)
    kept_lines = [
        line.rstrip() for line in text.splitlines() if not _looks_like_metadata_line(line)
    ]
    text = "\n".join(kept_lines)
    text = _redact_system_markers(text)
    text = ISO_TIMESTAMP_RE.sub("[redacted-time]", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text + "\n" if text else ""


def _discover_source_manuscripts(root: Path) -> list[BlindSubmission]:
    submissions: list[BlindSubmission] = []
    for kernel_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        if kernel_dir.name == "blinded":
            continue
        for arm in BLINDABLE_ARMS:
            manuscript_path = kernel_dir / arm / "manuscript.md"
            if manuscript_path.is_file():
                submissions.append(
                    BlindSubmission(
                        submission_uuid="",
                        kernel_id=kernel_dir.name,
                        arm=arm,
                        manuscript_path=manuscript_path,
                        blinded_path=Path(),
                    )
                )
    return submissions


def _new_submission_uuid(
    uuid_factory: Callable[[], UUID],
    used_uuids: set[str],
) -> str:
    for _attempt in range(100):
        value = str(uuid_factory())
        if value not in used_uuids:
            used_uuids.add(value)
            return value
    raise RuntimeError("UUID factory produced too many duplicate submission ids")


def _looks_like_metadata_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(METADATA_LINE_RE.match(stripped) or HTML_COMMENT_METADATA_RE.match(stripped))


def _redact_system_markers(text: str) -> str:
    redacted = text
    redactions = {
        r"\bArm\s+(?:A|B(?:['’]|_prime)?|C|E|F|G)\b": "Submission",
        r"\b(?:A|B(?:['’]|_prime)?|C|E|F|G)\s+arm\b": "submission",
        r"\barm\s*(?:=|:|：)\s*(?:A|B(?:['’]|_prime)?|C|E|F|G)\b": "submission",
        r"\bB_prime\b": "submission",
        r"\bB['’]\b": "submission",
        r"\b13-phase\b": "workflow",
        r"\bsingle-shot\b": "workflow",
        r"\bprovenance\.json\b": "[redacted-metadata]",
        r"\bprovenance\b": "[redacted-metadata]",
    }
    for pattern, replacement in redactions.items():
        redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)
    for term in PHASE_NAMES + STATE_NAMES + PROMPT_NAMES:
        redacted = _redact_term(redacted, term)
    return redacted


def _redact_term(text: str, term: str) -> str:
    if re.search(r"^[A-Z0-9_]+$", term):
        return re.sub(re.escape(term), "[redacted-step]", text)
    pattern = re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])", re.IGNORECASE)
    return pattern.sub("[redacted-step]", text)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
