"""Per-version diff for phase artifacts (codex-AGREEd #2 stage 2.D).

Compares two ``phase_version`` rows of the same (run, phase) and
returns a per-file diff. Supported diff types:

- ``text_unified``: unified-diff line array. For .md, .txt, .bib,
  .rst, and any text artifact the agents produce. Whitespace is
  lightly normalized (trailing whitespace stripped, line endings
  unified) so trivial reformat noise doesn't drown the real changes.
- ``jsonl_records``: per-record diff. Each record is keyed by a
  phase-specific primary key (e.g. ``claim_id`` for
  ``synthesis/claims.jsonl``); records that fail key matching fall
  back to a content-hash equality check. ``match_basis`` tells the
  frontend whether the match was id-based or content-based.
- ``json_structural``: recursive add/remove/change list. Lists of
  objects with a stable per-element id key (e.g. ``angle_id``) are
  matched by id; otherwise list elements are matched by index.
- ``binary``: just sha256 + size_bytes side-by-side. The frontend
  shows "binary differs" without trying to render content.

Pairing: every artifact under either pv's logical_path participates.
The output ``file_status`` is one of ``added`` (only in B),
``removed`` (only in A), ``changed`` (both, content differs),
``unchanged`` (both, content equal).

Causality context: the response also returns ``same_upstream_inputs``
and ``prompt_hash_changed`` so the user can tell whether a content
change is plausibly attributable to the prompt or to upstream drift.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from autoessay.models import (
    PhaseArtifact,
    PhaseVersion,
    PhaseVersionInput,
    Run,
)

#: Per-phase + per-logical-path JSONL primary key, used to pair
#: records in ``jsonl_records`` diffs. Falls back to content-hash
#: matching when the key is missing or yields ambiguous matches.
JSONL_KEYS: dict[tuple[str, str], str] = {
    ("synthesizer", "synthesis/claims.jsonl"): "claim_id",
}

#: Per-phase + per-key list-id field for ``json_structural`` diffs.
#: Lists of objects under these paths are matched by the named id
#: instead of by index. Index fallback applies to everything else.
JSON_LIST_ID_KEYS: dict[tuple[str, str], str] = {
    ("ideator", "novelty/angle_cards.json"): "angle_id",
}


@dataclass
class FileDiff:
    logical_path: str
    file_status: str  # added | removed | changed | unchanged
    diff_type: str  # text_unified | jsonl_records | json_structural | binary | unchanged
    body: dict[str, Any] = field(default_factory=dict)
    match_basis: str | None = None  # for jsonl_records


@dataclass
class DiffResponse:
    run_id: str
    phase: str
    from_version: dict[str, Any]
    to_version: dict[str, Any]
    context: dict[str, Any]
    summary: dict[str, int]
    files: list[FileDiff]


_TEXT_EXTENSIONS = {".md", ".txt", ".bib", ".rst", ".csv"}


def _classify(logical_path: str) -> str:
    """Return ``text``, ``jsonl``, ``json``, or ``binary``."""
    suffix = Path(logical_path).suffix.lower()
    if suffix == ".jsonl":
        return "jsonl"
    if suffix == ".json":
        return "json"
    if suffix in _TEXT_EXTENSIONS:
        return "text"
    return "binary"


def _diff_type_for_kind(kind: str) -> str:
    """Map the file-classification kind to the wire-level diff_type
    string the frontend renders. Codex round-1 review #2 stage 2.D
    flagged that added/removed files used to leak the raw kind
    (``text``/``jsonl``/``json``) which the UI doesn't recognize."""
    return {
        "text": "text_unified",
        "jsonl": "jsonl_records",
        "json": "json_structural",
        "binary": "binary",
    }.get(kind, "binary")


def _normalize_text(content: str) -> list[str]:
    """Strip trailing whitespace per line, unify line endings."""
    return [line.rstrip() for line in content.replace("\r\n", "\n").split("\n")]


def _read_blob(run_dir: Path, blob_path: str) -> bytes | None:
    full = run_dir / blob_path
    if not full.exists():
        return None
    return full.read_bytes()


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _diff_text(a_bytes: bytes, b_bytes: bytes) -> dict[str, Any]:
    a_lines = _normalize_text(a_bytes.decode("utf-8", errors="replace"))
    b_lines = _normalize_text(b_bytes.decode("utf-8", errors="replace"))
    return {
        "lines": list(difflib.unified_diff(a_lines, b_lines, lineterm="", n=3)),
        "a_line_count": len(a_lines),
        "b_line_count": len(b_lines),
    }


def _parse_jsonl_records(data: bytes) -> list[dict[str, Any]] | None:
    out: list[dict[str, Any]] = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        out.append(obj)
    return out


def _diff_jsonl(
    phase: str,
    logical_path: str,
    a_bytes: bytes,
    b_bytes: bytes,
) -> dict[str, Any]:
    a_records = _parse_jsonl_records(a_bytes)
    b_records = _parse_jsonl_records(b_bytes)
    # Parse failure → fall back to text diff. ``__fallback`` marks
    # this for the caller, who will relabel diff_type so the UI
    # renders the unified diff body instead of looking for record
    # arrays that are not there (codex round-2 review #2 stage 2.D).
    if a_records is None or b_records is None:
        out = _diff_text(a_bytes, b_bytes)
        out["__fallback"] = "jsonl_parse_failed"
        return out
    key = JSONL_KEYS.get((phase, logical_path))

    def canon_hash(rec: dict[str, Any]) -> str:
        return hashlib.sha1(
            json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def bucket(
        records: list[dict[str, Any]],
    ) -> tuple[
        dict[str, dict[str, list[dict[str, Any]]]],
        int,
    ]:
        """Index records by ``(id_key, content_hash)`` multisets.

        Every record's id_key is ``id:<value>`` when the configured
        primary key is present, or ``hash:<content>`` otherwise. The
        inner level then groups by content hash so identical records
        collapse into the same bucket regardless of position. This
        is what makes the diff order-insensitive (codex round-3 #2
        stage 2.D: the previous index-bearing slot ids reported a
        reorder as ``2 added + 2 removed`` instead of ``unchanged``).
        """
        by_id: dict[str, dict[str, list[dict[str, Any]]]] = {}
        fallbacks = 0
        for rec in records:
            chash = canon_hash(rec)
            if key and isinstance(rec.get(key), str | int):
                id_key = f"id:{rec[key]}"
            else:
                fallbacks += 1
                id_key = f"hash:{chash}"
            by_id.setdefault(id_key, {}).setdefault(chash, []).append(rec)
        return by_id, fallbacks

    a_idx, a_fb = bucket(a_records)
    b_idx, b_fb = bucket(b_records)
    if key is None:
        match_basis = "content_hash"
    elif a_fb + b_fb == 0:
        match_basis = "id"
    else:
        match_basis = "id_with_fallback"
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for id_key in sorted(set(a_idx.keys()) | set(b_idx.keys())):
        a_buckets = a_idx.get(id_key, {})
        b_buckets = b_idx.get(id_key, {})
        if not a_buckets:
            for recs in b_buckets.values():
                added.extend(recs)
            continue
        if not b_buckets:
            for recs in a_buckets.values():
                removed.extend(recs)
            continue
        # Both sides have records under this id. Match same-content
        # records as unchanged; pair leftover differing-content
        # records as "changed" (only meaningful when records share
        # an id but differ — otherwise unkeyed-fallback id buckets
        # contain a single hash, so leftovers there are unmatchable).
        a_remaining: list[dict[str, Any]] = []
        b_remaining: list[dict[str, Any]] = []
        # ``sorted`` is intentional: ``set`` iteration order varies
        # with PYTHONHASHSEED, which would make the leftover-pairing
        # below produce different ``changed`` entries across runs
        # (codex round-4 #2 stage 2.D non-determinism).
        for chash in sorted(set(a_buckets.keys()) | set(b_buckets.keys())):
            a_recs = a_buckets.get(chash, [])
            b_recs = b_buckets.get(chash, [])
            common = min(len(a_recs), len(b_recs))
            # `common` pairs are byte-identical → unchanged.
            a_remaining.extend(a_recs[common:])
            b_remaining.extend(b_recs[common:])
        # For id-keyed buckets, leftover a/b can be paired as
        # "changed" (same id, different content). For hash-keyed
        # buckets there is at most one hash, so ``a_remaining`` and
        # ``b_remaining`` are both empty if matched, or one side is
        # entirely added/removed.
        if id_key.startswith("id:"):
            pair_count = min(len(a_remaining), len(b_remaining))
            for i in range(pair_count):
                changed.append({"before": a_remaining[i], "after": b_remaining[i]})
            removed.extend(a_remaining[pair_count:])
            added.extend(b_remaining[pair_count:])
        else:
            # Hash-keyed bucket: leftovers cannot be paired.
            removed.extend(a_remaining)
            added.extend(b_remaining)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "match_basis": match_basis,
    }


def _diff_json_structural(
    phase: str,
    logical_path: str,
    a_bytes: bytes,
    b_bytes: bytes,
) -> dict[str, Any]:
    try:
        a_obj = json.loads(a_bytes.decode("utf-8", errors="replace"))
        b_obj = json.loads(b_bytes.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        out = _diff_text(a_bytes, b_bytes)
        out["__fallback"] = "json_parse_failed"
        return out
    list_id_key = JSON_LIST_ID_KEYS.get((phase, logical_path))
    changes: list[dict[str, Any]] = []
    _walk_structural(a_obj, b_obj, "$", changes, list_id_key)
    return {"changes": changes}


def _walk_structural(
    a: Any,
    b: Any,
    path: str,
    out: list[dict[str, Any]],
    list_id_key: str | None,
) -> None:
    if a == b:
        return
    if type(a) is not type(b):
        out.append({"path": path, "op": "type_change", "before": a, "after": b})
        return
    if isinstance(a, dict):
        for k in sorted(set(a.keys()) | set(b.keys())):
            sub_path = f"{path}.{k}"
            if k not in a:
                out.append({"path": sub_path, "op": "added", "after": b[k]})
            elif k not in b:
                out.append({"path": sub_path, "op": "removed", "before": a[k]})
            else:
                _walk_structural(a[k], b[k], sub_path, out, list_id_key)
        return
    if isinstance(a, list):
        # If the list contains dicts with the configured id key, match
        # by id; otherwise fall back to index-by-index walking.
        if list_id_key and a and b and all(isinstance(x, dict) and list_id_key in x for x in a + b):
            a_by_id = {item[list_id_key]: item for item in a}
            b_by_id = {item[list_id_key]: item for item in b}
            for k in sorted(set(a_by_id.keys()) | set(b_by_id.keys()), key=str):
                sub_path = f"{path}[{list_id_key}={k}]"
                if k not in a_by_id:
                    out.append({"path": sub_path, "op": "added", "after": b_by_id[k]})
                elif k not in b_by_id:
                    out.append({"path": sub_path, "op": "removed", "before": a_by_id[k]})
                else:
                    _walk_structural(a_by_id[k], b_by_id[k], sub_path, out, list_id_key)
            return
        # Index-keyed walk.
        max_len = max(len(a), len(b))
        for i in range(max_len):
            sub_path = f"{path}[{i}]"
            if i >= len(a):
                out.append({"path": sub_path, "op": "added", "after": b[i]})
            elif i >= len(b):
                out.append({"path": sub_path, "op": "removed", "before": a[i]})
            else:
                _walk_structural(a[i], b[i], sub_path, out, list_id_key)
        return
    # Primitive scalar mismatch.
    out.append({"path": path, "op": "value_change", "before": a, "after": b})


def _summary_for_pv(pv: PhaseVersion) -> dict[str, Any]:
    return {
        "id": pv.id,
        "version_no": pv.version_no,
        "status": pv.status,
        "prompt_hash": pv.prompt_hash,
        "input_snapshot_hash": pv.input_snapshot_hash,
        "created_on_branch_id": pv.created_on_branch_id,
        "created_at": pv.created_at.isoformat() if pv.created_at else None,
    }


def _upstream_inputs_for(session: Session, pv_id: str) -> dict[str, str]:
    rows = session.scalars(
        select(PhaseVersionInput).where(PhaseVersionInput.phase_version_id == pv_id),
    ).all()
    return {row.upstream_phase: row.upstream_pv_id for row in rows}


def diff_versions(
    session: Session,
    run: Run,
    phase: str,
    from_pv: PhaseVersion,
    to_pv: PhaseVersion,
) -> DiffResponse:
    """Compute a per-file diff between two pvs of the same (run, phase).

    Caller is responsible for the membership/status checks. ``from_pv``
    is the "left side" (red lines, removed records); ``to_pv`` is the
    "right side" (green lines, added records).
    """
    if from_pv.run_id != run.id or to_pv.run_id != run.id:
        raise ValueError("both versions must belong to the run")
    if from_pv.phase != phase or to_pv.phase != phase:
        raise ValueError("both versions must be of the same phase")
    if from_pv.status != "done" or to_pv.status != "done":
        raise ValueError(
            "both versions must have status='done'; failed/cancelled "
            "versions have no trustworthy artifact snapshot"
        )
    run_dir = Path(run.run_dir)
    a_artifacts = {
        a.logical_path: a
        for a in session.scalars(
            select(PhaseArtifact).where(PhaseArtifact.phase_version_id == from_pv.id),
        ).all()
    }
    b_artifacts = {
        a.logical_path: a
        for a in session.scalars(
            select(PhaseArtifact).where(PhaseArtifact.phase_version_id == to_pv.id),
        ).all()
    }
    files: list[FileDiff] = []
    summary = {"files_added": 0, "files_removed": 0, "files_changed": 0, "files_unchanged": 0}
    all_paths = sorted(set(a_artifacts.keys()) | set(b_artifacts.keys()))
    for path in all_paths:
        a_art = a_artifacts.get(path)
        b_art = b_artifacts.get(path)
        if a_art is None and b_art is not None:
            files.append(
                FileDiff(
                    logical_path=path,
                    file_status="added",
                    diff_type=_diff_type_for_kind(_classify(path)),
                    body={"size_bytes": b_art.size_bytes, "sha256": b_art.sha256},
                )
            )
            summary["files_added"] += 1
            continue
        if b_art is None and a_art is not None:
            files.append(
                FileDiff(
                    logical_path=path,
                    file_status="removed",
                    diff_type=_diff_type_for_kind(_classify(path)),
                    body={"size_bytes": a_art.size_bytes, "sha256": a_art.sha256},
                )
            )
            summary["files_removed"] += 1
            continue
        assert a_art is not None and b_art is not None
        if a_art.sha256 == b_art.sha256:
            files.append(
                FileDiff(
                    logical_path=path,
                    file_status="unchanged",
                    diff_type="unchanged",
                    body={},
                )
            )
            summary["files_unchanged"] += 1
            continue
        a_bytes = _read_blob(run_dir, a_art.blob_path) or b""
        b_bytes = _read_blob(run_dir, b_art.blob_path) or b""
        kind = _classify(path)
        body: dict[str, Any]
        match_basis: str | None = None
        if kind == "text":
            body = _diff_text(a_bytes, b_bytes)
            diff_type = "text_unified"
        elif kind == "jsonl":
            body = _diff_jsonl(phase, path, a_bytes, b_bytes)
            # If the JSONL parser failed inside, the body now holds a
            # text-unified diff. Relabel diff_type so the frontend
            # picks the right renderer (codex round-2 #2 stage 2.D).
            if "__fallback" in body:
                body.pop("__fallback")
                diff_type = "text_unified"
            else:
                diff_type = "jsonl_records"
                match_basis = body.pop("match_basis", None)
        elif kind == "json":
            body = _diff_json_structural(phase, path, a_bytes, b_bytes)
            if "__fallback" in body:
                body.pop("__fallback")
                diff_type = "text_unified"
            else:
                diff_type = "json_structural"
        else:
            body = {
                "a_sha256": a_art.sha256,
                "b_sha256": b_art.sha256,
                "a_size": a_art.size_bytes,
                "b_size": b_art.size_bytes,
            }
            diff_type = "binary"
        files.append(
            FileDiff(
                logical_path=path,
                file_status="changed",
                diff_type=diff_type,
                body=body,
                match_basis=match_basis,
            )
        )
        summary["files_changed"] += 1

    a_inputs = _upstream_inputs_for(session, from_pv.id)
    b_inputs = _upstream_inputs_for(session, to_pv.id)
    return DiffResponse(
        run_id=run.id,
        phase=phase,
        from_version=_summary_for_pv(from_pv),
        to_version=_summary_for_pv(to_pv),
        context={
            "same_upstream_inputs": a_inputs == b_inputs,
            "prompt_hash_changed": from_pv.prompt_hash != to_pv.prompt_hash,
        },
        summary=summary,
        files=files,
    )


__all__ = [
    "DiffResponse",
    "FileDiff",
    "JSONL_KEYS",
    "JSON_LIST_ID_KEYS",
    "diff_versions",
]
