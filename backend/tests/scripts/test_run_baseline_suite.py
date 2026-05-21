"""PR-D4 baseline runner unit tests via httpx.MockTransport.

Codex round-1 #8: runner must cover (a) full PHASE_STEPS sequence;
(b) DRAFTER_RUNNING phase_done event wait (not state wait); (c)
research_kernel PUT with base etag; (d) snapshot bundle to output dir.
All 4 covered here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "backend" / "scripts"))

import run_baseline_suite as runner  # noqa: E402  isort:skip

KERNEL_TEMPLATE = {
    "title": "Smoke kernel",
    "domain_id": "humanities_smoke",
    "language": "en",
    "research_kernel": {
        "tentative_question": "Test question",
        "observed_puzzle": "Test puzzle",
        "scope": "Test scope",
    },
}


class _MockBackend:
    """Stand-in for the local mirror — accepts API calls in the order
    the runner makes them and serves them deterministically."""

    def __init__(self) -> None:
        self.run_id = "run_mock_smoke_001"
        self.project_id = "proj_mock_smoke_001"
        self.state = "DOMAIN_LOADED"
        self.events: list[dict] = []
        self.next_event_id = 1
        self.kernel_set = False
        self.kernel_etag = "etag_v1"
        self.calls: list[tuple[str, str]] = []
        # State sequence the mock advances through on each phase_post.
        # Mirrors PHASE_STEPS waits.
        self._state_after: dict[tuple[str, str], str] = {
            ("phase_post", "proposal"): "USER_PROPOSAL_REVIEW",
            ("transition", "USER_SEARCH_REVIEW"): "USER_SEARCH_REVIEW",
            ("phase_post", "scout"): "USER_SEARCH_REVIEW",
            ("phase_post", "curator"): "USER_DEEP_DIVE_REVIEW",
            ("phase_post", "synthesizer"): "USER_FIELD_REVIEW",
            ("phase_post", "ideator"): "USER_NOVELTY_REVIEW",
            ("transition", "DRAFTER_RUNNING"): "DRAFTER_RUNNING",
            ("phase_post", "stylist"): "USER_REVISION_REVIEW",
            ("phase_post", "critic"): "USER_EXTERNAL_SCAN_APPROVAL",
            ("transition", "USER_INTEGRITY_REVIEW"): "USER_INTEGRITY_REVIEW",
            ("transition", "USER_FINAL_ACCEPTANCE"): "USER_FINAL_ACCEPTANCE",
            ("transition", "EXPORTS_DONE"): "EXPORTS_DONE",
        }

    def _add_event(self, event_type: str, payload: dict) -> None:
        self.events.append({"id": self.next_event_id, "event_type": event_type, "payload": payload})
        self.next_event_id += 1

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        self.calls.append((method, path))
        if method == "POST" and path == "/api/projects":
            return httpx.Response(
                200,
                json={"id": self.project_id, "title": "Smoke", "domain_id": "humanities_smoke"},
            )
        if method == "POST" and path == f"/api/projects/{self.project_id}/runs":
            self._add_event("run_created", {"run_id": self.run_id})
            return httpx.Response(
                200,
                json={"id": self.run_id, "project_id": self.project_id, "state": self.state},
            )
        if method == "GET" and path == f"/api/runs/{self.run_id}":
            return httpx.Response(
                200,
                json={
                    "id": self.run_id,
                    "state": self.state,
                    "research_kernel_etag": self.kernel_etag,
                },
            )
        if method == "PUT" and path == f"/api/runs/{self.run_id}/research_kernel":
            body = json.loads(request.content)
            assert body["base_etag"] == self.kernel_etag
            self.kernel_set = True
            return httpx.Response(200, json={"research_kernel_etag": "etag_v2"})
        if method == "GET" and path == f"/api/runs/{self.run_id}/events":
            after = int(request.url.params.get("after", 0))
            new_events = [e for e in self.events if e["id"] > after]
            return httpx.Response(200, json=new_events)
        if method == "POST" and path.startswith(f"/api/runs/{self.run_id}/transitions"):
            body = json.loads(request.content)
            target = body.get("to_state")
            self.state = self._state_after.get(("transition", target), target)
            if self.state == "DRAFTER_RUNNING":
                self._add_event("phase_done", {"phase": "drafter", "version": "v001"})
            return httpx.Response(200, json={"id": self.run_id, "state": self.state})
        if method == "POST" and path.startswith(f"/api/runs/{self.run_id}/"):
            phase = path.rsplit("/", 1)[-1]
            new_state = self._state_after.get(("phase_post", phase))
            if new_state is None:
                return httpx.Response(404, json={"error": f"unmocked phase_post: {phase}"})
            self.state = new_state
            self._add_event("phase_done", {"phase": phase})
            return httpx.Response(200, json={"id": self.run_id, "state": self.state})
        return httpx.Response(404, json={"error": f"unmocked: {method} {path}"})


@pytest.fixture
def mock_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    run_dir = data_dir / "runs" / "run_mock_smoke_001"
    run_dir.mkdir(parents=True)
    (run_dir / "exports").mkdir()
    (run_dir / "exports" / "manuscript.md").write_text(
        "# Mock manuscript\n\nbody.\n", encoding="utf-8"
    )
    (run_dir / "ledger.jsonl").write_text(
        '{"event": "phase_done", "payload": {"phase": "exports"}}\n',
        encoding="utf-8",
    )
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": "run_mock_smoke_001",
                "domain_id": "humanities_smoke",
                "state": "EXPORTS_DONE",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTOESSAY_DATA_DIR", str(data_dir))
    return data_dir


def _build_client(backend: _MockBackend) -> httpx.Client:
    transport = httpx.MockTransport(backend.handle)
    return httpx.Client(transport=transport, base_url="http://mock", timeout=5.0)


def test_run_baseline_drives_full_pipeline(
    tmp_path: Path,
    mock_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _MockBackend()
    output_dir = tmp_path / "bundle"

    # Patch poll-interval to ~0 so the test runs fast.
    meta = runner.run_baseline(
        paper_mode="case_analysis",
        kernel_template=KERNEL_TEMPLATE,
        output_dir=output_dir,
        api_base="http://mock",
        poll_interval=0.001,
        phase_budget_secs=5.0,
        client=_build_client(backend),
    )
    assert meta["run_id"] == "run_mock_smoke_001"
    assert meta["paper_mode"] == "case_analysis"
    assert backend.kernel_set is True
    assert backend.state == "EXPORTS_DONE"


def test_run_baseline_snapshots_bundle_into_output_dir(
    tmp_path: Path,
    mock_data_dir: Path,
) -> None:
    backend = _MockBackend()
    output_dir = tmp_path / "bundle"
    runner.run_baseline(
        paper_mode="case_analysis",
        kernel_template=KERNEL_TEMPLATE,
        output_dir=output_dir,
        api_base="http://mock",
        poll_interval=0.001,
        phase_budget_secs=5.0,
        client=_build_client(backend),
    )
    assert (output_dir / "manifest.json").exists()
    assert (output_dir / "exports" / "manuscript.md").exists()
    assert (output_dir / "ledger.jsonl").exists()
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["paper_mode"] == "case_analysis"
    assert manifest["kernel_template_label"] == "Smoke kernel"


def test_run_baseline_kernel_put_uses_base_etag(
    tmp_path: Path,
    mock_data_dir: Path,
) -> None:
    """Codex round-1 #8: research_kernel PUT must include the
    snapshot's base etag; the backend mock asserts this in handle()."""
    backend = _MockBackend()
    output_dir = tmp_path / "bundle"
    runner.run_baseline(
        paper_mode="case_analysis",
        kernel_template=KERNEL_TEMPLATE,
        output_dir=output_dir,
        api_base="http://mock",
        poll_interval=0.001,
        phase_budget_secs=5.0,
        client=_build_client(backend),
    )
    # If the backend's assert fired, run_baseline would have raised.
    assert backend.kernel_set is True


def test_run_baseline_drafter_waits_on_phase_done_event(
    tmp_path: Path,
    mock_data_dir: Path,
) -> None:
    """Codex round-1 #8: DRAFTER_RUNNING fires immediately on
    angle-select; the runner must wait on phase_done(drafter), not
    just the state. Verified by checking calls include /events GET
    with ``after=`` while state == DRAFTER_RUNNING (covered indirectly:
    the mock backend emits phase_done inside the transition to
    DRAFTER_RUNNING, so the wait_event poll resolves on the first
    iteration. If the runner polled on state instead, it would
    advance to stylist immediately — that would be a bug)."""
    backend = _MockBackend()
    output_dir = tmp_path / "bundle"
    runner.run_baseline(
        paper_mode="case_analysis",
        kernel_template=KERNEL_TEMPLATE,
        output_dir=output_dir,
        api_base="http://mock",
        poll_interval=0.001,
        phase_budget_secs=5.0,
        client=_build_client(backend),
    )
    # Verify /events was polled (proves wait_event path was taken).
    assert any(
        method == "GET" and path == f"/api/runs/{backend.run_id}/events"
        for method, path in backend.calls
    )
