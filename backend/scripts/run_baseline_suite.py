"""PR-D4 baseline runner — drive a single paper-mode walk against a
local-mirror API and snapshot the artifact bundle for the acceptance
gate.

Skeleton scope (codex round-1 #8): the runner is an httpx-based
Python driver — no Playwright dependency, CI-friendly. It walks the
11-state pipeline by POSTing the dedicated phase-start endpoints
(`/api/runs/{id}/{phase}`) and advancing user-review states via
`/api/runs/{id}/transitions`. Specific care for:

  * checkpoint endpoints — DRAFTER_RUNNING transitions immediately on
    angle-select but the phase only completes when ``phase_done(drafter)``
    fires; the runner waits on the event stream, not the run state.
  * optional framework_lens — `should_run_framework_lens` decides skip;
    the runner respects whichever next-state the API advertises.
  * research_kernel PUT — the put requires a base etag from the
    kernel-version snapshot to avoid stale-write 409s.

The runner does NOT decide which kernel to run; the CLI takes a JSON
template at ``--kernel-template`` (see
``backend/baselines/_kernels/case_analysis/_smoke_example.json``).

Testing: see ``backend/tests/scripts/test_run_baseline_suite.py`` —
all live-API behavior is covered via ``httpx.MockTransport`` so the
tests run without booting the FastAPI app.

Usage:

    bash frontend/scripts/run-e2e-server.sh &  # or run-local-mirror.sh
    python backend/scripts/run_baseline_suite.py \
        --paper-mode case_analysis \
        --kernel-template backend/baselines/_kernels/case_analysis/_smoke.json \
        --output-dir /tmp/baseline_bundle \
        --api-base http://127.0.0.1:8017

D4 skeleton stage: this script is exercised via unit tests but is NOT
yet invoked from `.github/workflows/acceptance.yml`. The CI smoke job
that calls it lives in PR-D4.1, gated on the first
`baseline_confirmed` landing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

DEFAULT_TIMEOUT = 120.0  # per-request timeout (s); LLM-stub responses are fast
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_PHASE_BUDGET_SECS = 600.0  # 10 min per phase


# Sequence of (current_state, action_kind, target). action_kind:
#   "phase_post" → POST /api/runs/{id}/{target}
#   "transition" → POST /api/runs/{id}/transitions {to_state: target}
# The list mirrors real-paper.spec.ts PHASE_STEPS but in API form.
PHASE_STEPS: list[dict[str, Any]] = [
    {
        "from": "DOMAIN_LOADED",
        "kind": "phase_post",
        "phase": "proposal",
        "wait": "USER_PROPOSAL_REVIEW",
    },
    {
        "from": "USER_PROPOSAL_REVIEW",
        "kind": "transition",
        "to_state": "USER_SEARCH_REVIEW",
        "wait": "USER_SEARCH_REVIEW",
    },
    {
        "from": "USER_SEARCH_REVIEW",
        "kind": "phase_post",
        "phase": "scout",
        "wait": "USER_SEARCH_REVIEW",
    },
    {
        "from": "USER_SEARCH_REVIEW",
        "kind": "phase_post",
        "phase": "curator",
        "wait": "USER_DEEP_DIVE_REVIEW",
    },
    {
        "from": "USER_DEEP_DIVE_REVIEW",
        "kind": "phase_post",
        "phase": "synthesizer",
        "wait": "USER_FIELD_REVIEW",
    },
    {
        "from": "USER_FIELD_REVIEW",
        "kind": "phase_post",
        "phase": "ideator",
        "wait_any": ["USER_NOVELTY_REVIEW", "USER_LENS_REVIEW"],
    },
    {
        "from": "USER_NOVELTY_REVIEW",
        "kind": "transition",
        "to_state": "DRAFTER_RUNNING",
        "wait_event": ("phase_done", "drafter"),
    },
    {
        "from": "DRAFTER_RUNNING",
        "kind": "phase_post",
        "phase": "stylist",
        "wait": "USER_REVISION_REVIEW",
    },
    {
        "from": "USER_REVISION_REVIEW",
        "kind": "phase_post",
        "phase": "critic",
        "wait": "USER_EXTERNAL_SCAN_APPROVAL",
    },
    {
        "from": "USER_EXTERNAL_SCAN_APPROVAL",
        "kind": "transition",
        "to_state": "USER_INTEGRITY_REVIEW",
        "wait": "USER_INTEGRITY_REVIEW",
    },
    {
        "from": "USER_INTEGRITY_REVIEW",
        "kind": "transition",
        "to_state": "USER_FINAL_ACCEPTANCE",
        "wait": "USER_FINAL_ACCEPTANCE",
    },
    {
        "from": "USER_FINAL_ACCEPTANCE",
        "kind": "transition",
        "to_state": "EXPORTS_DONE",
        "wait": "EXPORTS_DONE",
    },
]


def run_baseline(
    *,
    paper_mode: str,
    kernel_template: dict[str, Any],
    output_dir: Path,
    api_base: str = "http://127.0.0.1:8017",
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    phase_budget_secs: float = DEFAULT_PHASE_BUDGET_SECS,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Drive one full pipeline walk + snapshot the artifact bundle to
    ``output_dir``. Returns the run metadata (run_id / paper_mode /
    project_title / kernel_label) used by the evaluator."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    if client is None:
        client = httpx.Client(base_url=api_base, timeout=DEFAULT_TIMEOUT)
    try:
        project = _create_project(client, kernel_template, paper_mode)
        run = _create_run(client, project["id"])
        _set_kernel(client, run["id"], kernel_template)
        for step in PHASE_STEPS:
            _execute_step(
                client=client,
                run_id=run["id"],
                step=step,
                poll_interval=poll_interval,
                budget_secs=phase_budget_secs,
            )
        run_dir = _resolve_run_dir(client, run["id"])
        meta = _snapshot_bundle(
            run_dir=run_dir,
            output_dir=output_dir,
            run_id=run["id"],
            paper_mode=paper_mode,
            project_title=project.get("title"),
            kernel_template=kernel_template,
        )
        return meta
    finally:
        if owns_client:
            client.close()


# ----------------------------------------------------------------------
# API helpers
# ----------------------------------------------------------------------


def _create_project(
    client: httpx.Client,
    kernel: Mapping[str, Any],
    paper_mode: str,
) -> dict[str, Any]:
    body = {
        "title": kernel.get("title") or kernel.get("project_title") or "[D4 baseline]",
        "domain_id": kernel.get("domain_id", "general_academic"),
        "language": kernel.get("language", "en"),
        "paper_mode": paper_mode,
    }
    response = client.post("/api/projects", json=body)
    response.raise_for_status()
    return response.json()


def _create_run(client: httpx.Client, project_id: str) -> dict[str, Any]:
    response = client.post(f"/api/projects/{project_id}/runs", json={})
    response.raise_for_status()
    return response.json()


def _set_kernel(
    client: httpx.Client,
    run_id: str,
    kernel: Mapping[str, Any],
) -> None:
    """Codex round-1 #8: kernel PUT requires the current
    research_kernel etag to avoid stale-write 409s. Pull it from the
    run snapshot before PUT."""
    snapshot = client.get(f"/api/runs/{run_id}").raise_for_status().json()
    base_etag = snapshot.get("research_kernel_etag")
    body = {
        "research_kernel": kernel.get("research_kernel") or {},
        "kernel_schema_version": kernel.get("kernel_schema_version", "v1"),
        "base_etag": base_etag,
    }
    response = client.put(f"/api/runs/{run_id}/research_kernel", json=body)
    response.raise_for_status()


def _execute_step(
    *,
    client: httpx.Client,
    run_id: str,
    step: Mapping[str, Any],
    poll_interval: float,
    budget_secs: float,
) -> None:
    _wait_for_state(client, run_id, step["from"], poll_interval, budget_secs)
    if step["kind"] == "phase_post":
        client.post(f"/api/runs/{run_id}/{step['phase']}").raise_for_status()
    elif step["kind"] == "transition":
        client.post(
            f"/api/runs/{run_id}/transitions",
            json={"to_state": step["to_state"]},
        ).raise_for_status()
    else:
        raise ValueError(f"unknown step kind: {step['kind']}")
    if "wait_event" in step:
        event_type, phase = step["wait_event"]
        _wait_for_event(client, run_id, event_type, phase, poll_interval, budget_secs)
    elif "wait_any" in step:
        _wait_for_any_state(client, run_id, list(step["wait_any"]), poll_interval, budget_secs)
    else:
        _wait_for_state(client, run_id, step["wait"], poll_interval, budget_secs)


def _wait_for_state(
    client: httpx.Client,
    run_id: str,
    state: str,
    poll_interval: float,
    budget_secs: float,
) -> None:
    _wait_for_any_state(client, run_id, [state], poll_interval, budget_secs)


def _wait_for_any_state(
    client: httpx.Client,
    run_id: str,
    states: list[str],
    poll_interval: float,
    budget_secs: float,
) -> None:
    deadline = time.monotonic() + budget_secs
    while time.monotonic() < deadline:
        snap = client.get(f"/api/runs/{run_id}").json()
        if snap.get("state") in states:
            return
        time.sleep(poll_interval)
    raise TimeoutError(f"run {run_id} never reached any of {states} within {budget_secs}s")


def _wait_for_event(
    client: httpx.Client,
    run_id: str,
    event_type: str,
    phase: str,
    poll_interval: float,
    budget_secs: float,
) -> None:
    """Codex round-1 #8: DRAFTER_RUNNING is a special case — backend
    transitions into DRAFTER_RUNNING immediately on angle-select but
    drafter writes 8 sections sequentially over 5-10 min. We poll the
    event stream for ``phase_done(drafter)`` rather than the state."""
    deadline = time.monotonic() + budget_secs
    seen = 0
    while time.monotonic() < deadline:
        events = client.get(f"/api/runs/{run_id}/events", params={"after": seen}).json()
        for ev in events:
            seen = max(seen, int(ev.get("id", 0)))
            payload = ev.get("payload") or {}
            if ev.get("event_type") == event_type and payload.get("phase") == phase:
                return
        time.sleep(poll_interval)
    raise TimeoutError(
        f"run {run_id} never emitted event {event_type}({phase}) within {budget_secs}s"
    )


def _resolve_run_dir(client: httpx.Client, run_id: str) -> Path:
    """The API doesn't expose run_dir directly. We rely on the
    ``AUTOESSAY_DATA_DIR`` env var used by run-e2e-server.sh and
    run-local-mirror.sh (both write under ``$DATA_DIR/runs/<run_id>``).
    """
    import os

    data_dir = os.environ.get("AUTOESSAY_DATA_DIR")
    if not data_dir:
        raise RuntimeError("AUTOESSAY_DATA_DIR not set; cannot locate frozen run artifacts")
    return Path(data_dir) / "runs" / run_id


def _snapshot_bundle(
    *,
    run_dir: Path,
    output_dir: Path,
    run_id: str,
    paper_mode: str,
    project_title: str | None,
    kernel_template: Mapping[str, Any],
) -> dict[str, Any]:
    """Copy the artifact files the evaluator needs into ``output_dir``
    (keeps the bundle stable + git-committable while the source
    run_dir keeps churning)."""
    targets = [
        "exports/manuscript.md",
        "exports/manifest.json",
        "drafts",
        "integrity/integrity_summary.json",
        "synthesis/evidence_ledger.jsonl",
        "synthesis/synthesizer.json",
        "synthesis/framework_lens.json",
        "sources/shortlist.json",
        "ledger.jsonl",
        "run.json",
        "CURRENT_STATUS.md",
    ]
    for relpath in targets:
        src = run_dir / relpath
        if not src.exists():
            continue
        dest = output_dir / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "paper_mode": paper_mode,
        "project_title": project_title,
        "kernel_template_label": kernel_template.get("baseline_label")
        or kernel_template.get("title")
        or "(unlabeled)",
        "domain_id": kernel_template.get("domain_id"),
        "snapshotted_at": _utc_now(),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-mode", required=True, help="paper_mode (e.g. case_analysis)")
    parser.add_argument(
        "--kernel-template",
        type=Path,
        required=True,
        help="path to a JSON kernel template (title + research_kernel + domain_id)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="snapshot bundle dir (will be created)",
    )
    parser.add_argument(
        "--api-base",
        default="http://127.0.0.1:8017",
        help="API base URL of the local mirror / e2e server",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="state-poll interval in seconds (default 2)",
    )
    parser.add_argument(
        "--phase-budget-secs",
        type=float,
        default=DEFAULT_PHASE_BUDGET_SECS,
        help="per-phase budget in seconds (default 600 / 10 min)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    template = json.loads(args.kernel_template.read_text(encoding="utf-8"))
    meta = run_baseline(
        paper_mode=args.paper_mode,
        kernel_template=template,
        output_dir=args.output_dir,
        api_base=args.api_base,
        poll_interval=args.poll_interval,
        phase_budget_secs=args.phase_budget_secs,
    )
    print(json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
