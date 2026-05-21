"""PR-C1.a: evidence ledger.

Append-only ``synthesis/evidence_ledger.jsonl`` carrying one row
per (primary_source, claim) pair the synthesizer's primary track
will cite. User attribution overrides land as separate rows so
the ledger remains an audit trail.

## Path

``{run_dir}/synthesis/evidence_ledger.jsonl`` — sits inside the
synthesis phase directory so phase versioning + rerun cascade
already own it. Codex C1 round-1 amendment: NOT at run root —
phase versioning needs to manage it.

## Schema (per row)

Three row kinds, distinguished by ``kind``:

  ``claim``         — extracted by the LLM evidence-extraction
                       agent on a primary_source. Has ``source_id``,
                       ``claim_id``, ``claim_text``,
                       ``citation_target``, ``confidence``.
  ``override``      — user marks a primary_source's claim (or all
                       its claims if ``claim_id`` is ``null``) as
                       ``attribute_to_user`` instead of citing
                       directly. Has ``source_id``, ``claim_id``
                       (nullable), ``action``, ``user``,
                       ``recorded_at``.
  ``ledger_event``  — bookkeeping entries (e.g. extraction-failed
                       for source X). Has ``event_type``, ``payload``.

## Idempotency

``claim_id`` is the SHA-256 hex of normalized
``(source_id || "\\n" || claim_text || "\\n" || citation_target)``,
truncated to 16 chars. Re-running the extractor against the same
source+text+target yields the same id, so an idempotent
re-append simply skips writing already-present rows.

## Atomic append semantics

The writer reads existing rows once, computes the id-set, then
appends only NEW rows in a single ``open(mode="a")`` write under
``fsync``. Concurrent appends from the same process are
serialized by an in-process lock; cross-process concurrency is
not expected (one synthesizer phase per run at a time).

User-override rows are NEVER deduped against earlier override
rows — folding by (source_id, claim_id, latest-recorded_at) is
the reader's job.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# In-process lock so two synthesizer threads in the same worker
# (e.g. dual-track concurrent execution in a future variant) don't
# corrupt the ledger.
_WRITE_LOCK = threading.Lock()


_LEDGER_FILENAME = "evidence_ledger.jsonl"
_CLAIM_ID_LENGTH = 16


def ledger_path_for_run(run_dir: Path) -> Path:
    """Returns the on-disk path for the ledger.

    Caller MUST ensure the parent ``synthesis`` directory exists
    via ``ensure_synthesis_dir(run_dir)`` before writing — we don't
    create it implicitly because the synthesizer phase is
    responsible for owning the directory layout.
    """
    return run_dir / "synthesis" / _LEDGER_FILENAME


def ensure_synthesis_dir(run_dir: Path) -> Path:
    out = run_dir / "synthesis"
    out.mkdir(parents=True, exist_ok=True)
    return out


def compute_claim_id(
    source_id: str,
    claim_text: str,
    citation_target: str,
) -> str:
    """Deterministic 16-hex-char id; same input always same output."""
    payload = "\n".join(
        [source_id.strip(), claim_text.strip(), citation_target.strip()],
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:_CLAIM_ID_LENGTH]


def claim_row(
    *,
    source_id: str,
    claim_text: str,
    citation_target: str,
    confidence: float,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a ``kind=claim`` row. Caller passes through ``append_rows``."""
    cid = compute_claim_id(source_id, claim_text, citation_target)
    row: dict[str, object] = {
        "kind": "claim",
        "source_id": source_id,
        "claim_id": cid,
        "claim_text": claim_text,
        "citation_target": citation_target,
        "confidence": float(confidence),
    }
    if extra:
        row["extra"] = dict(extra)
    return row


def override_row(
    *,
    source_id: str,
    claim_id: str | None,
    action: str,
    user: str,
    recorded_at: str | None = None,
) -> dict[str, object]:
    """Build a ``kind=override`` row.

    ``claim_id=None`` means the override applies source-wide
    (every claim for this source is marked ``action``). Reader
    folds by (source_id, claim_id, latest-recorded_at).
    """
    return {
        "kind": "override",
        "source_id": source_id,
        "claim_id": claim_id,
        "action": action,
        "user": user,
        "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
    }


def event_row(
    *,
    event_type: str,
    payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "kind": "ledger_event",
        "event_type": event_type,
        "payload": dict(payload or {}),
    }


def read_rows(run_dir: Path) -> list[dict[str, object]]:
    """Returns all ledger rows, oldest-first. Empty list when the
    file does not exist."""
    p = ledger_path_for_run(run_dir)
    if not p.exists():
        return []
    out: list[dict[str, object]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError:
                # Skip malformed rows rather than crashing the
                # synthesizer phase; the writer always emits
                # valid JSON, so a malformed line is corruption
                # we want to surface separately.
                continue
    return out


def existing_claim_ids(run_dir: Path) -> set[str]:
    """For idempotent claim re-append. Only counts ``kind=claim`` rows."""
    out: set[str] = set()
    for row in read_rows(run_dir):
        if row.get("kind") == "claim" and isinstance(row.get("claim_id"), str):
            out.add(str(row["claim_id"]))
    return out


def append_rows(
    run_dir: Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    skip_existing_claims: bool = True,
) -> int:
    """Append rows atomically. Returns the count actually written
    (after dedup).

    When ``skip_existing_claims`` is True (default) we filter out
    ``kind=claim`` rows whose ``claim_id`` is already present in
    the ledger. Other row kinds (override, ledger_event) are
    always appended regardless — overrides intentionally accumulate
    so ``recorded_at`` history is preserved.
    """
    rows_list = [dict(r) for r in rows]
    if not rows_list:
        return 0

    ensure_synthesis_dir(run_dir)
    p = ledger_path_for_run(run_dir)

    with _WRITE_LOCK:
        existing = existing_claim_ids(run_dir) if skip_existing_claims else set()
        to_write: list[dict[str, Any]] = []
        for row in rows_list:
            if (
                skip_existing_claims
                and row.get("kind") == "claim"
                and isinstance(row.get("claim_id"), str)
                and row["claim_id"] in existing
            ):
                continue
            to_write.append(row)

        if not to_write:
            return 0

        with p.open("a", encoding="utf-8") as fh:
            for row in to_write:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        return len(to_write)


def fold_overrides(
    rows: Iterable[Mapping[str, object]],
) -> dict[tuple[str, str | None], dict[str, object]]:
    """Returns the latest ``kind=override`` row per ``(source_id,
    claim_id)``. Caller uses this to render the user-attribution
    state without scanning the full ledger every time.

    A source-wide override (``claim_id=None``) appears under
    ``(source_id, None)`` and applies to every claim for that
    source UNLESS a per-claim override has been recorded with a
    later ``recorded_at``.
    """
    latest: dict[tuple[str, str | None], dict[str, object]] = {}
    for row in rows:
        if row.get("kind") != "override":
            continue
        sid = row.get("source_id")
        cid = row.get("claim_id")
        if not isinstance(sid, str):
            continue
        key: tuple[str, str | None] = (
            sid,
            str(cid) if isinstance(cid, str) else None,
        )
        prev = latest.get(key)
        if prev is None:
            latest[key] = dict(row)
            continue
        prev_at = str(prev.get("recorded_at") or "")
        new_at = str(row.get("recorded_at") or "")
        if new_at >= prev_at:
            latest[key] = dict(row)
    return latest
