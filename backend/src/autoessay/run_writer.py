import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def create_run_directory(
    runs_root: str | Path,
    run_id: str,
    project_id: str,
    *,
    state: str = "CREATED",
    domain_id: str | None = None,
) -> Path:
    run_dir = Path(runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    timestamp = _utc_timestamp()
    run_payload = {
        "run_id": run_id,
        "project_id": project_id,
        "state": state,
        "domain_id": domain_id,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _write_json(run_dir / "run.json", run_payload)
    _write_text(run_dir / "baseline.md", _baseline_text(run_payload))
    _write_text(run_dir / "CURRENT_STATUS.md", f"# Current Status\n\nState: `{state}`\n")
    append_ledger_event(
        run_dir,
        {
            "event": "run_directory_created",
            "run_id": run_id,
            "project_id": project_id,
            "state": state,
        },
    )
    return run_dir


def append_ledger_event(run_dir: str | Path, event: dict[str, Any]) -> None:
    payload = {"ts": _utc_timestamp(), **event}
    with (Path(run_dir) / "ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n",
        )


def record_run_event_payload(run_dir: str | Path, payload: dict[str, Any]) -> None:
    append_ledger_event(run_dir, {"event": "run_event", "payload": payload})


def ensure_phase_checkpoint(run_dir: str | Path, state: str) -> Path | None:
    if not state.endswith("_RUNNING"):
        return None
    phase = state.removesuffix("_RUNNING").lower()
    phase_dir = Path(run_dir) / phase
    phase_dir.mkdir(parents=True, exist_ok=True)
    marker = phase_dir / "checkpoint.json"
    _write_json(
        marker,
        {
            "phase": phase,
            "state": state,
            "created_at": _utc_timestamp(),
        },
    )
    return marker


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _baseline_text(payload: dict[str, Any]) -> str:
    lines = [
        "# Baseline",
        "",
        f"Run ID: `{payload['run_id']}`",
        f"Project ID: `{payload['project_id']}`",
        f"State: `{payload['state']}`",
    ]
    if payload.get("domain_id"):
        lines.append(f"Domain: `{payload['domain_id']}`")
    return "\n".join(lines) + "\n"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
