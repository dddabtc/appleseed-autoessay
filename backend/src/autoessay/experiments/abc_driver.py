"""Orchestration driver for the ABC architecture comparison experiment."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from autoessay.auth.middleware import SESSION_COOKIE_NAME
from autoessay.experiments.abc_architecture import (
    DEFAULT_MAX_CONCURRENCY,
    EXPERIMENT_ID,
    MAX_ALLOWED_CONCURRENCY,
    PROVIDER_FALLBACK_ALLOWED,
    experiment_script_sha,
    generation_model_id,
    production_commit_sha,
    require_frozen_shas,
    token_cap_total,
)

ARMS: tuple[str, ...] = ("A", "B", "B_prime", "C")
SUPPORTED_ARMS: tuple[str, ...] = ("A", "B", "B_prime", "C", "E", "F", "G")
SUCCESS_TERMINAL_STATE = "EXPORTS_DONE"
DEFAULT_DOMAIN_ID = "general_academic"
DEFAULT_LANGUAGE = "zh"
DEFAULT_PAPER_MODE = "case_analysis"
DEFAULT_API_BASE = "http://127.0.0.1:8000"
DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 120.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


DEFAULT_EXPERIMENT_DIR = _repo_root() / "docs" / "experiments" / "abc-architecture-comparison"
DEFAULT_KERNELS_PATH = DEFAULT_EXPERIMENT_DIR / "kernels.md"
DEFAULT_RESULTS_DIR = DEFAULT_EXPERIMENT_DIR / "results"


@dataclass(frozen=True)
class KernelDefinition:
    kernel_id: str
    direction: str
    title: str
    target_journal: str | None
    tags: tuple[str, ...]
    research_kernel: dict[str, object]
    medium_difficulty_reason: str | None = None


@dataclass(frozen=True)
class DriverOptions:
    kernels_path: Path = DEFAULT_KERNELS_PATH
    results_dir: Path = DEFAULT_RESULTS_DIR
    state_path: Path | None = None
    api_base: str = DEFAULT_API_BASE
    all_kernels: bool = False
    smoke_kernel_id: str | None = None
    kernel_ids: tuple[str, ...] = ()
    dry_run: bool = False
    resume: bool = False
    force: bool = False
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    run_timeout_seconds: float = 0.0
    domain_id: str = DEFAULT_DOMAIN_ID
    language: str = DEFAULT_LANGUAGE
    paper_mode: str = DEFAULT_PAPER_MODE
    username: str | None = None
    password: str | None = None
    session_cookie: str | None = None
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    arms: tuple[str, ...] = ARMS

    @property
    def resolved_state_path(self) -> Path:
        return self.state_path or self.results_dir / "driver_state.json"


@dataclass(frozen=True)
class DriverResult:
    selected_kernel_ids: tuple[str, ...]
    state_path: Path
    manifest_path: Path
    dry_run: bool
    planned_actions: tuple[str, ...]
    completed_kernel_ids: tuple[str, ...]
    blocked_kernel_ids: tuple[str, ...]


class ABCAPIClient(Protocol):
    def create_project(
        self, kernel: KernelDefinition, *, domain_id: str, language: str
    ) -> dict[str, Any]: ...

    def create_run(self, project_id: str) -> dict[str, Any]: ...

    def get_run(self, run_id: str) -> dict[str, Any]: ...

    def edit_research_kernel(
        self,
        run_id: str,
        kernel: KernelDefinition,
        *,
        paper_mode: str,
        run_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def enable_auto_advance(self, run_id: str) -> dict[str, Any]: ...


class ABCArtifactRunner(Protocol):
    def dump_front_half(self, *, run_id: str, kernel_id: str, results_dir: Path) -> None: ...

    def generate_arm(self, *, kernel_id: str, arm: str, results_dir: Path) -> None: ...


class HTTPABCAPIClient:
    """Small typed wrapper over the production FastAPI endpoints."""

    def __init__(
        self,
        *,
        api_base: str,
        username: str | None = None,
        password: str | None = None,
        session_cookie: str | None = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        if session_cookie:
            self._client.cookies.set(SESSION_COOKIE_NAME, session_cookie)
        if username or password:
            if not username or not password:
                raise ValueError("username and password must be provided together")
            self._login(username=username, password=password)

    def close(self) -> None:
        self._client.close()

    def create_project(
        self,
        kernel: KernelDefinition,
        *,
        domain_id: str,
        language: str,
    ) -> dict[str, Any]:
        return self._request_json(
            "POST",
            "/api/projects",
            json_body={
                "title": kernel.title,
                "domain_id": domain_id,
                "target_journal": kernel.target_journal,
                "language": language,
            },
        )

    def create_run(self, project_id: str) -> dict[str, Any]:
        # Keep auto_advance off until the research kernel has been written.
        return self._request_json(
            "POST",
            f"/api/projects/{project_id}/runs",
            json_body={"auto_advance": False, "mathematical_mode": False},
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/api/runs/{run_id}")

    def edit_research_kernel(
        self,
        run_id: str,
        kernel: KernelDefinition,
        *,
        paper_mode: str,
        run_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = dict(run_snapshot or self.get_run(run_id))
        base_hash = str(snapshot.get("research_kernel_hash") or "")
        if not base_hash:
            raise RuntimeError(f"GET /api/runs/{run_id} did not include research_kernel_hash")
        base_version = _int_value(snapshot.get("proposal_version"))
        return self._request_json(
            "PUT",
            f"/api/runs/{run_id}/research_kernel",
            json_body={
                "paper_mode": paper_mode,
                "kernel": kernel.research_kernel,
                "base_proposal_version": base_version,
                "base_kernel_hash": base_hash,
                "accept_developer_preview": False,
            },
        )

    def enable_auto_advance(self, run_id: str) -> dict[str, Any]:
        return self._request_json(
            "PATCH",
            f"/api/runs/{run_id}/settings",
            json_body={"auto_advance": True},
        )

    def _login(self, *, username: str, password: str) -> None:
        self._request_json(
            "POST",
            "/api/auth/login",
            json_body={"username": username, "password": password},
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        response = self._client.request(method, path, json=json_body)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _response_detail(response)
            raise RuntimeError(f"{method} {path} failed: {response.status_code} {detail}") from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"{method} {path} returned non-object JSON")
        return dict(payload)


class SubprocessABCArtifactRunner:
    """Invoke scripts/abc-run.py so generation behavior stays in one place."""

    def __init__(self, *, script_path: Path | None = None) -> None:
        self._script_path = script_path or _repo_root() / "scripts" / "abc-run.py"

    def dump_front_half(self, *, run_id: str, kernel_id: str, results_dir: Path) -> None:
        self._run(
            (
                "dump-front-half",
                "--run-id",
                run_id,
                "--kernel-id",
                kernel_id,
                "--results-dir",
                str(results_dir),
            )
        )

    def generate_arm(self, *, kernel_id: str, arm: str, results_dir: Path) -> None:
        self._run(
            (
                "generate",
                "--kernel-id",
                kernel_id,
                "--arm",
                arm,
                "--results-dir",
                str(results_dir),
            )
        )

    def _run(self, args: Sequence[str]) -> None:
        completed = subprocess.run(
            [sys.executable, str(self._script_path), *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise RuntimeError(f"abc-run.py {' '.join(args)} failed: {detail}")


def parse_kernels(path: str | Path = DEFAULT_KERNELS_PATH) -> list[KernelDefinition]:
    text = Path(path).read_text(encoding="utf-8")
    overview = _parse_overview(text)
    kernels: list[KernelDefinition] = []
    matches = list(re.finditer(r"^##\s+([a-z]+-\d+)\s*$", text, flags=re.MULTILINE))
    for index, match in enumerate(matches):
        kernel_id = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end]
        title = _required_field(body, "题目", kernel_id)
        target_journal = _optional_field(body, "目标期刊")
        tags = tuple(_split_tags(_optional_field(body, "学科方向标签") or ""))
        direction = overview.get(kernel_id, {}).get("direction") or (tags[0] if tags else "")
        if not direction:
            raise ValueError(f"Kernel {kernel_id} is missing direction")
        research_kernel = _parse_research_kernel(body, kernel_id, direction, tags)
        medium_reason = _optional_field(body, "中等难度理由")
        kernels.append(
            KernelDefinition(
                kernel_id=kernel_id,
                direction=direction,
                title=title,
                target_journal=target_journal,
                tags=tags,
                research_kernel=research_kernel,
                medium_difficulty_reason=medium_reason,
            )
        )
    return kernels


def select_kernels(
    kernels: Sequence[KernelDefinition],
    *,
    all_kernels: bool = False,
    smoke_kernel_id: str | None = None,
    kernel_ids: Sequence[str] = (),
) -> list[KernelDefinition]:
    if smoke_kernel_id:
        requested = {smoke_kernel_id}
    elif kernel_ids:
        requested = {item for item in kernel_ids if item}
    elif all_kernels:
        return list(kernels)
    else:
        raise ValueError("select one of --all, --smoke, or --kernels")

    by_id = {kernel.kernel_id: kernel for kernel in kernels}
    missing = sorted(requested - set(by_id))
    if missing:
        raise ValueError(f"unknown kernel id(s): {', '.join(missing)}")
    return [kernel for kernel in kernels if kernel.kernel_id in requested]


def run_driver(
    options: DriverOptions,
    *,
    api_client: ABCAPIClient | None = None,
    artifact_runner: ABCArtifactRunner | None = None,
) -> DriverResult:
    _validate_max_concurrency(options.max_concurrency)
    _validate_arms(options.arms)
    kernels = parse_kernels(options.kernels_path)
    selected = select_kernels(
        kernels,
        all_kernels=options.all_kernels,
        smoke_kernel_id=options.smoke_kernel_id,
        kernel_ids=options.kernel_ids,
    )
    if options.dry_run:
        state = _load_state_for_dry_run(options)
        actions = tuple(_plan_actions(state, selected))
        return _driver_result(options, selected, state, actions, dry_run=True)
    require_frozen_shas()

    if options.force and options.results_dir.exists():
        shutil.rmtree(options.results_dir)
    options.results_dir.mkdir(parents=True, exist_ok=True)

    state = _load_or_create_state(options)
    runner = artifact_runner or SubprocessABCArtifactRunner()
    output_lock: Any | None = None
    if options.max_concurrency > 1:
        from threading import Lock

        output_lock = Lock()
    _write_outputs(options, selected, state)
    for kernel in selected:
        _ensure_kernel_state(state, kernel)
    if options.max_concurrency == 1:
        owned_api: HTTPABCAPIClient | None = None
        if api_client is None:
            owned_api = HTTPABCAPIClient(
                api_base=options.api_base,
                username=options.username,
                password=options.password,
                session_cookie=options.session_cookie,
            )
            api: ABCAPIClient = owned_api
        else:
            api = api_client
        try:
            for kernel in selected:
                _process_kernel(
                    options=options,
                    state=state,
                    kernel=kernel,
                    api=api,
                    runner=runner,
                    selected=selected,
                    output_lock=output_lock,
                )
        finally:
            if owned_api is not None:
                owned_api.close()
    else:
        _process_kernels_concurrently(
            options=options,
            state=state,
            selected=selected,
            api_client=api_client,
            runner=runner,
            output_lock=output_lock,
        )
    state["ended_at"] = _utc_now()
    state["status"] = _overall_status(state, selected)
    _write_outputs(options, selected, state)
    return _driver_result(options, selected, state, (), dry_run=False)


def _process_kernels_concurrently(
    *,
    options: DriverOptions,
    state: dict[str, Any],
    selected: Sequence[KernelDefinition],
    api_client: ABCAPIClient | None,
    runner: ABCArtifactRunner,
    output_lock: Any | None,
) -> None:
    with ThreadPoolExecutor(max_workers=options.max_concurrency) as executor:
        futures = [
            executor.submit(
                _process_kernel_concurrent_worker,
                options=options,
                state=state,
                kernel=kernel,
                selected=selected,
                api_client=api_client,
                runner=runner,
                output_lock=output_lock,
            )
            for kernel in selected
        ]
        for future in as_completed(futures):
            future.result()


def _process_kernel_concurrent_worker(
    *,
    options: DriverOptions,
    state: dict[str, Any],
    kernel: KernelDefinition,
    selected: Sequence[KernelDefinition],
    api_client: ABCAPIClient | None,
    runner: ABCArtifactRunner,
    output_lock: Any | None,
) -> None:
    owned_api: HTTPABCAPIClient | None = None
    try:
        if api_client is None:
            owned_api = HTTPABCAPIClient(
                api_base=options.api_base,
                username=options.username,
                password=options.password,
                session_cookie=options.session_cookie,
            )
            api: ABCAPIClient = owned_api
        else:
            api = api_client
        _process_kernel(
            options=options,
            state=state,
            kernel=kernel,
            api=api,
            runner=runner,
            selected=selected,
            output_lock=output_lock,
        )
    except Exception as exc:  # noqa: BLE001 - isolate one kernel from thread startup errors.
        kernel_state = _ensure_kernel_state(state, kernel)
        _record_blocker(
            kernel_state,
            step=str(kernel_state.get("current_step") or "kernel_startup"),
            exc=exc,
            provider_failed=_looks_like_provider_failure(exc),
        )
        _write_outputs_locked(options, selected, state, output_lock)
    finally:
        if owned_api is not None:
            owned_api.close()


def _process_kernel(
    *,
    options: DriverOptions,
    state: dict[str, Any],
    kernel: KernelDefinition,
    api: ABCAPIClient,
    runner: ABCArtifactRunner,
    selected: Sequence[KernelDefinition],
    output_lock: Any | None = None,
) -> None:
    kernel_state = _ensure_kernel_state(state, kernel)
    if _is_kernel_complete(kernel_state, options.arms):
        _write_outputs_locked(options, selected, state, output_lock)
        return
    kernel_state["blocker"] = None
    kernel_state["provider_failed"] = False

    def save() -> None:
        _write_outputs_locked(options, selected, state, output_lock)

    try:
        _write_kernel_seed(options.results_dir, kernel, kernel_state)
        save()

        if not kernel_state.get("project_id"):
            kernel_state["current_step"] = "create_project"
            project = api.create_project(
                kernel,
                domain_id=options.domain_id,
                language=options.language,
            )
            kernel_state["project_id"] = _required_response_id(project, "project")
            save()

        if not kernel_state.get("a_run_id"):
            kernel_state["current_step"] = "create_a_run"
            run = api.create_run(str(kernel_state["project_id"]))
            kernel_state["a_run_id"] = _required_response_id(run, "run")
            kernel_state["a_run_state"] = _run_state(run)
            save()

        a_run_id = str(kernel_state["a_run_id"])
        if not kernel_state.get("research_kernel_set"):
            kernel_state["current_step"] = "set_research_kernel"
            snapshot = api.get_run(a_run_id)
            api.edit_research_kernel(
                a_run_id,
                kernel,
                paper_mode=options.paper_mode,
                run_snapshot=snapshot,
            )
            kernel_state["research_kernel_set"] = True
            save()

        if not kernel_state.get("auto_advance_enabled"):
            kernel_state["current_step"] = "enable_auto_advance"
            run = api.enable_auto_advance(a_run_id)
            kernel_state["auto_advance_enabled"] = True
            kernel_state["a_run_state"] = _run_state(run)
            save()

        if kernel_state.get("a_run_state") != SUCCESS_TERMINAL_STATE:
            kernel_state["current_step"] = "poll_a_run"
            terminal = _poll_a_run(
                api=api,
                run_id=a_run_id,
                kernel_state=kernel_state,
                save=save,
                poll_interval_seconds=options.poll_interval_seconds,
                timeout_seconds=options.run_timeout_seconds,
            )
            terminal_state = _run_state(terminal)
            kernel_state["a_run_state"] = terminal_state
            if terminal_state != SUCCESS_TERMINAL_STATE:
                _record_blocker(
                    kernel_state,
                    step="poll_a_run",
                    exc=RuntimeError(f"A run reached terminal non-success state {terminal_state}"),
                    provider_failed=_is_provider_failure_state(terminal_state),
                )
                save()
                return
            save()

        if not kernel_state.get("front_half_dumped"):
            kernel_state["current_step"] = "dump_front_half"
            runner.dump_front_half(
                run_id=a_run_id,
                kernel_id=kernel.kernel_id,
                results_dir=options.results_dir,
            )
            kernel_state["front_half_dumped"] = True
            save()

        for arm in options.arms:
            if arm == "B_prime" and not kernel_state.get("B_generated"):
                raise RuntimeError("B must complete before B_prime")
            _generate_if_needed(
                runner=runner,
                results_dir=options.results_dir,
                kernel_state=kernel_state,
                kernel_id=kernel.kernel_id,
                arm=arm,
                flag=_arm_flag(arm),
                save=save,
            )
        kernel_state["current_step"] = None
        kernel_state["completed"] = _is_kernel_complete(kernel_state, options.arms)
        save()
    except Exception as exc:  # noqa: BLE001 - isolate one kernel from the rest.
        _record_blocker(
            kernel_state,
            step=str(kernel_state.get("current_step") or "unknown"),
            exc=exc,
            provider_failed=_looks_like_provider_failure(exc),
        )
        save()


def _generate_if_needed(
    *,
    runner: ABCArtifactRunner,
    results_dir: Path,
    kernel_state: dict[str, Any],
    kernel_id: str,
    arm: str,
    flag: str,
    save: Callable[[], None],
) -> None:
    if kernel_state.get(flag):
        return
    kernel_state["current_step"] = f"generate_{arm}"
    runner.generate_arm(kernel_id=kernel_id, arm=arm, results_dir=results_dir)
    _assert_arm_within_token_cap(results_dir=results_dir, kernel_id=kernel_id, arm=arm)
    kernel_state[flag] = True
    save()


def _poll_a_run(
    *,
    api: ABCAPIClient,
    run_id: str,
    kernel_state: dict[str, Any],
    save: Callable[[], None],
    poll_interval_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    while True:
        run = api.get_run(run_id)
        state = _run_state(run)
        kernel_state["a_run_state"] = state
        kernel_state["a_run_last_polled_at"] = _utc_now()
        save()
        if _is_terminal_run_state(state):
            return run
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"A run {run_id} did not finish within {timeout_seconds:.0f}s")
        if poll_interval_seconds > 0:
            time.sleep(poll_interval_seconds)


def _parse_overview(text: str) -> dict[str, dict[str, str]]:
    match = re.search(
        r"^## Overview\s*(?P<body>.*?)(?=^##\s+)", text, flags=re.DOTALL | re.MULTILINE
    )
    if match is None:
        return {}
    rows: dict[str, dict[str, str]] = {}
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("|---") or "| ID |" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        rows[cells[0]] = {
            "direction": cells[1],
            "title": cells[2],
            "target_journal": cells[3],
        }
    return rows


def _required_field(section: str, label: str, kernel_id: str) -> str:
    value = _optional_field(section, label)
    if not value:
        raise ValueError(f"Kernel {kernel_id} is missing {label}")
    return value


def _optional_field(section: str, label: str) -> str | None:
    match = re.search(rf"^{re.escape(label)}[：:]\s*(.+?)\s*$", section, flags=re.MULTILINE)
    if match is None:
        return None
    return match.group(1).strip()


def _split_tags(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[;；]", value) if item.strip()]


def _parse_research_kernel(
    section: str,
    kernel_id: str,
    direction: str,
    tags: Sequence[str],
) -> dict[str, object]:
    label_map = {
        "观察到的疑问": "observed_puzzle",
        "试探性问题": "tentative_question",
        "范围": "scope",
        "理论偏好": "theory_preference",
        "方法偏好": "method_preference",
    }
    values: dict[str, object] = {
        "kernel_schema_version": 1,
        "abc_kernel_id": kernel_id,
        "abc_direction": direction,
        "abc_tags": list(tags),
        "primary_materials_status": "none",
    }
    in_kernel = False
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if line == "研究核：":
            in_kernel = True
            continue
        if in_kernel and line.startswith("中等难度理由"):
            break
        if not in_kernel:
            continue
        bullet = re.match(r"^-\s*([^：:]+)[：:]\s*(.+?)\s*$", line)
        if bullet is None:
            continue
        label = bullet.group(1).strip()
        key = label_map.get(label, f"kernel_note_{len(values)}")
        values[key] = bullet.group(2).strip()
    for required_key in ("observed_puzzle", "tentative_question", "scope"):
        if not values.get(required_key):
            raise ValueError(f"Kernel {kernel_id} is missing research kernel {required_key}")
    return values


def _load_state_for_dry_run(options: DriverOptions) -> dict[str, Any]:
    path = options.resolved_state_path
    if options.resume and path.exists():
        return _load_json_object(path)
    return _new_state(options)


def _load_or_create_state(options: DriverOptions) -> dict[str, Any]:
    path = options.resolved_state_path
    if options.resume and path.exists():
        return _load_json_object(path)
    if path.exists() and not options.force:
        raise RuntimeError(f"{path} already exists; use --resume or --force")
    return _new_state(options)


def _new_state(options: DriverOptions) -> dict[str, Any]:
    now = _utc_now()
    return {
        "experiment_id": EXPERIMENT_ID,
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "ended_at": None,
        "api_base": options.api_base,
        "results_dir": str(options.results_dir),
        "kernels_path": str(options.kernels_path),
        "driver": {
            "poll_interval_seconds": options.poll_interval_seconds,
            "run_timeout_seconds": options.run_timeout_seconds,
            "max_concurrency": options.max_concurrency,
            "domain_id": options.domain_id,
            "language": options.language,
            "paper_mode": options.paper_mode,
            "arms": list(options.arms),
        },
        "total_token_usage": _empty_usage(),
        "kernels": {},
    }


def _ensure_kernel_state(state: dict[str, Any], kernel: KernelDefinition) -> dict[str, Any]:
    kernels = state.setdefault("kernels", {})
    if not isinstance(kernels, dict):
        raise ValueError("driver_state.json field 'kernels' must be an object")
    raw = kernels.setdefault(kernel.kernel_id, {})
    if not isinstance(raw, dict):
        raise ValueError(f"driver_state kernel entry {kernel.kernel_id} must be an object")
    raw.update(
        {
            "kernel_id": kernel.kernel_id,
            "direction": kernel.direction,
            "title": kernel.title,
            "target_journal": kernel.target_journal,
        }
    )
    raw.setdefault("project_id", None)
    raw.setdefault("a_run_id", None)
    raw.setdefault("a_run_state", None)
    raw.setdefault("research_kernel_set", False)
    raw.setdefault("auto_advance_enabled", False)
    raw.setdefault("front_half_dumped", False)
    for arm in SUPPORTED_ARMS:
        raw.setdefault(_arm_flag(arm), False)
    raw.setdefault("provider_failed", False)
    raw.setdefault("blocker", None)
    raw.setdefault("completed", False)
    raw.setdefault("arm_token_usage", {})
    raw.setdefault("total_token_usage", _empty_usage())
    return raw


def _write_kernel_seed(
    results_dir: Path, kernel: KernelDefinition, kernel_state: Mapping[str, Any]
) -> None:
    kernel_dir = results_dir / kernel.kernel_id
    kernel_path = kernel_dir / "kernel.json"
    payload = _load_json_object(kernel_path) if kernel_path.exists() else {}
    payload.update(
        {
            "schema_version": "abc_kernel_metadata_v1",
            "experiment_id": EXPERIMENT_ID,
            "kernel_id": kernel.kernel_id,
            "title": kernel.title,
            "research_kernel": kernel.research_kernel,
            "target_journal": kernel.target_journal,
            "direction": kernel.direction,
            "tags": list(kernel.tags),
        }
    )
    if kernel_state.get("a_run_id"):
        payload["a_run_id"] = kernel_state["a_run_id"]
    _atomic_write_json(kernel_path, payload)


def _write_outputs(
    options: DriverOptions,
    selected: Sequence[KernelDefinition],
    state: dict[str, Any],
) -> None:
    state["updated_at"] = _utc_now()
    _refresh_token_usage(options.results_dir, state)
    _atomic_write_json(options.resolved_state_path, state)
    _write_manifest(options=options, selected=selected, state=state)


def _write_outputs_locked(
    options: DriverOptions,
    selected: Sequence[KernelDefinition],
    state: dict[str, Any],
    output_lock: Any | None,
) -> None:
    if output_lock is None:
        _write_outputs(options, selected, state)
        return
    with output_lock:
        _write_outputs(options, selected, state)


def _write_manifest(
    *,
    options: DriverOptions,
    selected: Sequence[KernelDefinition],
    state: Mapping[str, Any],
) -> None:
    manifest = {
        "experiment_id": EXPERIMENT_ID,
        "generation_model_id": generation_model_id(),
        "production_commit_sha": production_commit_sha(),
        "experiment_script_sha": experiment_script_sha(),
        "provider": _provider_summary(options.results_dir, selected, options.arms),
        "provider_fallback_allowed": PROVIDER_FALLBACK_ALLOWED,
        "token_cap_total": token_cap_total(),
        "arms": list(options.arms),
        "kernel_ids": [kernel.kernel_id for kernel in selected],
        "generation_window_utc": {
            "started_at": state.get("started_at"),
            "ended_at": state.get("ended_at"),
        },
        "driver_state_path": str(options.resolved_state_path),
        "api_base": options.api_base,
        "max_concurrency": options.max_concurrency,
    }
    _atomic_write_json(options.results_dir / "manifest.json", manifest)


def _plan_actions(state: dict[str, Any], selected: Sequence[KernelDefinition]) -> list[str]:
    arms = _state_arms(state)
    actions: list[str] = []
    for kernel in selected:
        kernel_state = _ensure_kernel_state(state, kernel)
        prefix = kernel.kernel_id
        if _is_kernel_complete(kernel_state, arms):
            actions.append(f"{prefix}: skip completed kernel")
            continue
        steps: list[tuple[str, bool]] = [
            ("create project", not kernel_state.get("project_id")),
            ("create A run", not kernel_state.get("a_run_id")),
            ("set research kernel", not kernel_state.get("research_kernel_set")),
            ("enable auto_advance", not kernel_state.get("auto_advance_enabled")),
            (
                "poll A run to EXPORTS_DONE",
                kernel_state.get("a_run_state") != SUCCESS_TERMINAL_STATE,
            ),
            ("dump front-half package", not kernel_state.get("front_half_dumped")),
        ]
        steps.extend((f"generate arm {arm}", not kernel_state.get(_arm_flag(arm))) for arm in arms)
        for label, predicate in steps:
            if predicate:
                actions.append(f"{prefix}: {label}")
    return actions


def _refresh_token_usage(results_dir: Path, state: dict[str, Any]) -> None:
    kernels = state.get("kernels")
    if not isinstance(kernels, dict):
        return
    total = _empty_usage()
    exceeded: list[str] = []
    for kernel_id, raw_kernel_state in kernels.items():
        if not isinstance(raw_kernel_state, dict):
            continue
        kernel_total = _empty_usage()
        arm_usage = raw_kernel_state.setdefault("arm_token_usage", {})
        if not isinstance(arm_usage, dict):
            arm_usage = {}
            raw_kernel_state["arm_token_usage"] = arm_usage
        for arm in _state_arms(state):
            usage = _read_arm_usage(results_dir / str(kernel_id) / arm / "provenance.json")
            if usage is not None:
                arm_usage[arm] = usage
            existing = arm_usage.get(arm)
            if isinstance(existing, dict):
                _add_usage(kernel_total, existing)
                if bool(existing.get("budget_exceeded")):
                    exceeded.append(f"{kernel_id}:{arm}")
        kernel_total["budget_exceeded_arms"] = [
            item for item in exceeded if item.startswith(f"{kernel_id}:")
        ]
        raw_kernel_state["total_token_usage"] = kernel_total
        _add_usage(total, kernel_total)
    total["budget_exceeded_arms"] = exceeded
    state["total_token_usage"] = total


def _read_arm_usage(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = _load_json_object(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    raw_usage = payload.get("token_usage")
    if not isinstance(raw_usage, Mapping):
        return None
    return {
        "prompt_tokens": _int_value(raw_usage.get("prompt_tokens")),
        "completion_tokens": _int_value(raw_usage.get("completion_tokens")),
        "total_tokens": _int_value(raw_usage.get("total_tokens")),
        "budget_exceeded": bool(raw_usage.get("budget_exceeded")),
    }


def _assert_arm_within_token_cap(*, results_dir: Path, kernel_id: str, arm: str) -> None:
    path = results_dir / kernel_id / arm / "provenance.json"
    usage = _read_arm_usage(path)
    if usage is None:
        raise RuntimeError(f"Missing token usage after generating {kernel_id}/{arm}: {path}")
    total_tokens = _int_value(usage.get("total_tokens"))
    cap = token_cap_total()
    if total_tokens > cap or bool(usage.get("budget_exceeded")):
        raise RuntimeError(
            f"ABC arm {kernel_id}/{arm} exceeded token cap {cap}: total_tokens={total_tokens}"
        )


def _add_usage(target: dict[str, object], source: Mapping[str, object]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        target[key] = _int_value(target.get(key)) + _int_value(source.get(key))
    target["budget_exceeded"] = bool(target.get("budget_exceeded")) or bool(
        source.get("budget_exceeded")
    )


def _empty_usage() -> dict[str, object]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "budget_exceeded": False,
        "budget_exceeded_arms": [],
    }


def _provider_summary(
    results_dir: Path, selected: Sequence[KernelDefinition], arms: Sequence[str]
) -> str:
    providers: set[str] = set()
    for kernel in selected:
        for arm in arms:
            path = results_dir / kernel.kernel_id / arm / "provenance.json"
            if not path.exists():
                continue
            try:
                payload = _load_json_object(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            provider = str(payload.get("provider") or "").strip()
            if provider:
                providers.add(provider)
    return ",".join(sorted(providers)) if providers else "unknown"


def _is_kernel_complete(kernel_state: Mapping[str, Any], arms: Sequence[str] = ARMS) -> bool:
    return (
        kernel_state.get("a_run_state") == SUCCESS_TERMINAL_STATE
        and bool(kernel_state.get("front_half_dumped"))
        and all(bool(kernel_state.get(_arm_flag(arm))) for arm in arms)
    )


def _overall_status(state: Mapping[str, Any], selected: Sequence[KernelDefinition]) -> str:
    completed = 0
    blocked = 0
    kernels = state.get("kernels")
    if not isinstance(kernels, Mapping):
        return "failed"
    for kernel in selected:
        raw = kernels.get(kernel.kernel_id)
        if not isinstance(raw, Mapping):
            continue
        if _is_kernel_complete(raw, _state_arms(state)):
            completed += 1
        elif raw.get("blocker"):
            blocked += 1
    if completed == len(selected):
        return "complete"
    if blocked:
        return "partial"
    return "running"


def _driver_result(
    options: DriverOptions,
    selected: Sequence[KernelDefinition],
    state: Mapping[str, Any],
    planned_actions: Sequence[str],
    *,
    dry_run: bool,
) -> DriverResult:
    kernels = state.get("kernels")
    completed: list[str] = []
    blocked: list[str] = []
    if isinstance(kernels, Mapping):
        for kernel in selected:
            raw = kernels.get(kernel.kernel_id)
            if not isinstance(raw, Mapping):
                continue
            if _is_kernel_complete(raw, _state_arms(state)):
                completed.append(kernel.kernel_id)
            if raw.get("blocker"):
                blocked.append(kernel.kernel_id)
    return DriverResult(
        selected_kernel_ids=tuple(kernel.kernel_id for kernel in selected),
        state_path=options.resolved_state_path,
        manifest_path=options.results_dir / "manifest.json",
        dry_run=dry_run,
        planned_actions=tuple(planned_actions),
        completed_kernel_ids=tuple(completed),
        blocked_kernel_ids=tuple(blocked),
    )


def _record_blocker(
    kernel_state: dict[str, Any],
    *,
    step: str,
    exc: Exception,
    provider_failed: bool,
) -> None:
    kernel_state["blocker"] = {
        "step": step,
        "type": type(exc).__name__,
        "message": str(exc),
        "recorded_at": _utc_now(),
    }
    if provider_failed:
        kernel_state["provider_failed"] = True
        kernel_state["provider_retry_deadline_utc"] = _utc_now_dt_plus(hours=24)
    kernel_state["completed"] = False


def _is_terminal_run_state(state: str) -> bool:
    return (
        state == SUCCESS_TERMINAL_STATE
        or state in {"FAILED", "ERROR", "CANCELLED"}
        or state.startswith("FAILED_")
        or state.endswith("_ERROR")
    )


def _is_provider_failure_state(state: str) -> bool:
    return state == "FAILED_VENDOR"


def _looks_like_provider_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return any(term in message for term in ("provider", "vendor", "rate limit", "timeout", "llm"))


def _required_response_id(payload: Mapping[str, Any], label: str) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{label} response did not include id")
    return value


def _run_state(payload: Mapping[str, Any]) -> str:
    value = payload.get("state")
    if not isinstance(value, str) or not value:
        raise RuntimeError("run response did not include state")
    return value


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


def _validate_max_concurrency(value: int) -> None:
    if value < 1 or value > MAX_ALLOWED_CONCURRENCY:
        raise ValueError(
            f"max_concurrency must be between 1 and {MAX_ALLOWED_CONCURRENCY}; got {value}"
        )


def _validate_arms(arms: Sequence[str]) -> None:
    if not arms:
        raise ValueError("at least one arm must be selected")
    unsupported = [arm for arm in arms if arm not in SUPPORTED_ARMS]
    if unsupported:
        raise ValueError(f"unsupported arm(s): {', '.join(unsupported)}")
    if len(set(arms)) != len(tuple(arms)):
        raise ValueError("arms must not contain duplicates")
    if "A" not in arms:
        raise ValueError("abc_driver requires arm A because front-half extraction comes from A")
    if "B_prime" in arms and "B" not in arms:
        raise ValueError("arm B_prime requires arm B")


def _arm_flag(arm: str) -> str:
    return "A_manuscript_copied" if arm == "A" else f"{arm}_generated"


def _state_arms(state: Mapping[str, Any]) -> tuple[str, ...]:
    driver = state.get("driver")
    if isinstance(driver, Mapping):
        raw_arms = driver.get("arms")
        if isinstance(raw_arms, Sequence) and not isinstance(raw_arms, (str, bytes, bytearray)):
            arms = tuple(str(arm) for arm in raw_arms if str(arm))
            if arms:
                return arms
    return ARMS


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return dict(payload)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
    return json.dumps(payload, ensure_ascii=False)[:500]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now_dt_plus(*, hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def driver_options_from_env(**overrides: Any) -> DriverOptions:
    """Build options with auth defaults from environment for the CLI wrapper."""

    values: dict[str, Any] = {
        "api_base": os.getenv("AUTOESSAY_ABC_API_BASE", DEFAULT_API_BASE),
        "username": os.getenv("AUTOESSAY_API_USERNAME") or None,
        "password": os.getenv("AUTOESSAY_API_PASSWORD") or None,
        "session_cookie": os.getenv("AUTOESSAY_SESSION_COOKIE") or None,
        "max_concurrency": _env_int("AUTOESSAY_ABC_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY),
        "arms": _env_arms("AUTOESSAY_ABC_ARMS", ARMS),
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    return DriverOptions(**values)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_arms(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())
