from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from autoessay.experiments.abc_driver import (
    DEFAULT_KERNELS_PATH,
    DriverOptions,
    KernelDefinition,
    parse_kernels,
    run_driver,
)

os.environ.setdefault("AUTOESSAY_EXPERIMENT_ABC_PRODUCTION_SHA", "test-production-sha")
os.environ.setdefault("AUTOESSAY_EXPERIMENT_ABC_SCRIPT_SHA", "test-script-sha")


class FakeAPIClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.project_counter = 0
        self.run_counter = 0
        self.run_states: dict[str, str] = {}
        self.lock = threading.Lock()

    def create_project(
        self,
        kernel: KernelDefinition,
        *,
        domain_id: str,
        language: str,
    ) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("create_project", kernel.kernel_id))
            self.project_counter += 1
            project_id = f"proj_{self.project_counter}"
        return {
            "id": project_id,
            "title": kernel.title,
            "domain_id": domain_id,
            "language": language,
        }

    def create_run(self, project_id: str) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("create_run", project_id))
            self.run_counter += 1
            run_id = f"run_{self.run_counter}"
            self.run_states[run_id] = "DOMAIN_LOADED"
        return {
            "id": run_id,
            "project_id": project_id,
            "state": "DOMAIN_LOADED",
            "research_kernel_hash": "hash0",
            "proposal_version": 0,
        }

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("get_run", run_id))
            state = self.run_states.get(run_id, "DOMAIN_LOADED")
            if state != "EXPORTS_DONE":
                state = "EXPORTS_DONE"
                self.run_states[run_id] = state
        return {
            "id": run_id,
            "state": state,
            "research_kernel_hash": "hash0",
            "proposal_version": 0,
            "paper_mode": "case_analysis",
        }

    def edit_research_kernel(
        self,
        run_id: str,
        kernel: KernelDefinition,
        *,
        paper_mode: str,
        run_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("edit_research_kernel", run_id))
        assert paper_mode == "case_analysis"
        assert kernel.research_kernel["kernel_schema_version"] == 1
        assert run_snapshot is not None
        return {"paper_mode": paper_mode, "kernel": kernel.research_kernel}

    def enable_auto_advance(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("enable_auto_advance", run_id))
            self.run_states[run_id] = "EXPORTS_DONE"
        return {"id": run_id, "state": "EXPORTS_DONE"}


class FakeArtifactRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def dump_front_half(self, *, run_id: str, kernel_id: str, results_dir: Path) -> None:
        self.calls.append(("dump_front_half", kernel_id))
        front_half = results_dir / kernel_id / "front_half"
        front_half.mkdir(parents=True)
        package_md = f"# package\n\nrun={run_id}\n"
        (front_half / "package.md").write_text(package_md, encoding="utf-8")
        (front_half / "package.json").write_text(
            json.dumps({"a_run_id": run_id}) + "\n",
            encoding="utf-8",
        )
        (front_half / "package.sha256").write_text("abc123\n", encoding="utf-8")
        kernel_json = results_dir / kernel_id / "kernel.json"
        payload = json.loads(kernel_json.read_text(encoding="utf-8"))
        payload["a_run_id"] = run_id
        payload["a_run_dir"] = f"/tmp/{run_id}"
        kernel_json.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    def generate_arm(self, *, kernel_id: str, arm: str, results_dir: Path) -> None:
        self.calls.append(("generate_arm", arm))
        arm_dir = results_dir / kernel_id / arm
        arm_dir.mkdir(parents=True)
        (arm_dir / "manuscript.md").write_text(f"# {kernel_id} {arm}\n", encoding="utf-8")
        (arm_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "provider": "fake-provider" if arm != "A" else "production",
                    "token_usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                        "budget_exceeded": False,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )


def _options(tmp_path: Path, **overrides: Any) -> DriverOptions:
    values: dict[str, Any] = {
        "kernels_path": DEFAULT_KERNELS_PATH,
        "results_dir": tmp_path / "results",
        "poll_interval_seconds": 0.0,
        "run_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return DriverOptions(**values)


def test_kernel_parser_reads_ten_kernels() -> None:
    kernels = parse_kernels(DEFAULT_KERNELS_PATH)

    assert len(kernels) == 10
    hist_01 = next(kernel for kernel in kernels if kernel.kernel_id == "hist-01")
    assert hist_01.title == "清末海关统计表中的口岸层级变化与地方财政想象"
    assert hist_01.direction == "历史"
    assert hist_01.target_journal == "《历史研究》"
    assert hist_01.research_kernel["observed_puzzle"]
    assert hist_01.research_kernel["tentative_question"]
    assert hist_01.research_kernel["scope"]
    assert hist_01.research_kernel["theory_preference"]


def test_stub_driver_flow_writes_all_artifacts(tmp_path: Path) -> None:
    api = FakeAPIClient()
    runner = FakeArtifactRunner()
    result = run_driver(
        _options(tmp_path, smoke_kernel_id="hist-01"),
        api_client=api,
        artifact_runner=runner,
    )

    root = tmp_path / "results"
    assert result.completed_kernel_ids == ("hist-01",)
    assert (root / "manifest.json").exists()
    assert (root / "driver_state.json").exists()
    for arm in ("A", "B", "B_prime", "C"):
        assert (root / "hist-01" / arm / "manuscript.md").exists()
    assert runner.calls == [
        ("dump_front_half", "hist-01"),
        ("generate_arm", "A"),
        ("generate_arm", "B"),
        ("generate_arm", "B_prime"),
        ("generate_arm", "C"),
    ]
    state = json.loads((root / "driver_state.json").read_text(encoding="utf-8"))
    assert state["kernels"]["hist-01"]["B_prime_generated"] is True
    assert state["total_token_usage"]["total_tokens"] == 120
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kernel_ids"] == ["hist-01"]
    assert manifest["provider"] == "fake-provider,production"
    assert manifest["max_concurrency"] == 1


def test_resumability_skips_completed_steps(tmp_path: Path) -> None:
    root = tmp_path / "results"
    (root / "hist-01" / "B").mkdir(parents=True)
    (root / "hist-01" / "B" / "manuscript.md").write_text("# B\n", encoding="utf-8")
    (root / "hist-01" / "B" / "provenance.json").write_text(
        json.dumps(
            {
                "provider": "fake-provider",
                "token_usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "budget_exceeded": False,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "driver_state.json").write_text(
        json.dumps(
            {
                "experiment_id": "abc-architecture-comparison-v1",
                "started_at": "2026-05-16T00:00:00Z",
                "updated_at": "2026-05-16T00:00:00Z",
                "ended_at": None,
                "status": "running",
                "kernels": {
                    "hist-01": {
                        "project_id": "proj_existing",
                        "a_run_id": "run_existing",
                        "a_run_state": "EXPORTS_DONE",
                        "research_kernel_set": True,
                        "auto_advance_enabled": True,
                        "front_half_dumped": True,
                        "A_manuscript_copied": True,
                        "B_generated": True,
                        "B_prime_generated": False,
                        "C_generated": False,
                        "blocker": None,
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    api = FakeAPIClient()
    runner = FakeArtifactRunner()

    result = run_driver(
        _options(tmp_path, smoke_kernel_id="hist-01", resume=True),
        api_client=api,
        artifact_runner=runner,
    )

    assert result.completed_kernel_ids == ("hist-01",)
    assert api.calls == []
    assert runner.calls == [("generate_arm", "B_prime"), ("generate_arm", "C")]


def test_dry_run_does_not_call_api_or_write_results(tmp_path: Path) -> None:
    api = FakeAPIClient()
    runner = FakeArtifactRunner()

    result = run_driver(
        _options(tmp_path, all_kernels=True, dry_run=True),
        api_client=api,
        artifact_runner=runner,
    )

    assert api.calls == []
    assert runner.calls == []
    assert not (tmp_path / "results").exists()
    assert result.dry_run is True
    assert "hist-01: create project" in result.planned_actions


def test_smoke_mode_only_processes_requested_kernel(tmp_path: Path) -> None:
    api = FakeAPIClient()
    runner = FakeArtifactRunner()

    result = run_driver(
        _options(tmp_path, smoke_kernel_id="lit-02"),
        api_client=api,
        artifact_runner=runner,
    )

    assert result.selected_kernel_ids == ("lit-02",)
    assert result.completed_kernel_ids == ("lit-02",)
    assert all(call[1] == "lit-02" for call in runner.calls if call[0] == "dump_front_half")
    manifest = json.loads((tmp_path / "results" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kernel_ids"] == ["lit-02"]


def test_driver_max_concurrency_processes_two_kernels_in_parallel(tmp_path: Path) -> None:
    class BarrierArtifactRunner(FakeArtifactRunner):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = threading.Barrier(2)
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def dump_front_half(self, *, run_id: str, kernel_id: str, results_dir: Path) -> None:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                self.barrier.wait(timeout=2.0)
                time.sleep(0.01)
                super().dump_front_half(
                    run_id=run_id,
                    kernel_id=kernel_id,
                    results_dir=results_dir,
                )
            finally:
                with self.lock:
                    self.active -= 1

    api = FakeAPIClient()
    runner = BarrierArtifactRunner()

    result = run_driver(
        _options(
            tmp_path,
            kernel_ids=("hist-01", "lit-02"),
            max_concurrency=2,
        ),
        api_client=api,
        artifact_runner=runner,
    )

    assert set(result.completed_kernel_ids) == {"hist-01", "lit-02"}
    assert not result.blocked_kernel_ids
    assert runner.max_active == 2
    state = json.loads((tmp_path / "results" / "driver_state.json").read_text(encoding="utf-8"))
    assert state["driver"]["max_concurrency"] == 2
