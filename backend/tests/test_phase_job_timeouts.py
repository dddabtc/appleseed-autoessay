"""Real-paper-fix: verify every phase enqueue passes a non-default
RQ ``job_timeout`` so the LLM-heavy phases don't silently die at
the 180s RQ default.

Background: real-paper e2e on prod (2026-05-06) hit
``JobTimeoutException(180s)`` on scout, because the
``queue.enqueue(...)`` calls in ``worker.py`` did not pass a
``job_timeout`` kwarg → RQ's 180s default kicked in. Codex round-1
AGREE-w-amend on a per-phase timeout dict + helper.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from autoessay import worker


@pytest.fixture(autouse=True)
def _no_real_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub Redis + Queue so enqueue tests don't touch a real broker."""

    class _FakeJob:
        id = "fake-job-id"

    class _FakeQueue:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            self.enqueue_args: list[tuple[Any, ...]] = []
            self.enqueue_kwargs: list[dict[str, Any]] = []

        def enqueue(self, *args: Any, **kwargs: Any) -> _FakeJob:
            self.enqueue_args.append(args)
            self.enqueue_kwargs.append(kwargs)
            return _FakeJob()

        def enqueue_in(self, *_a: Any, **_kw: Any) -> _FakeJob:
            return _FakeJob()

    instances: list[_FakeQueue] = []

    def _factory(*a: Any, **kw: Any) -> _FakeQueue:
        q = _FakeQueue(*a, **kw)
        instances.append(q)
        return q

    monkeypatch.setattr(worker, "Queue", _factory)
    monkeypatch.setattr(worker, "Redis", MagicMock())
    # Expose the captured queues on the test module so each test can
    # read back the last enqueue's job_timeout.
    monkeypatch.setattr(worker, "_test_fake_queues", instances, raising=False)


def _last_kwargs() -> dict[str, Any]:
    queues = worker._test_fake_queues  # type: ignore[attr-defined]
    assert queues, "no queue created — enqueue helper did not run"
    return queues[-1].enqueue_kwargs[-1]


@pytest.mark.parametrize(
    "phase,enqueue_fn,expected_timeout",
    [
        ("proposal", lambda: worker.enqueue_proposal_job("run_x"), 5 * 60),
        ("scout", lambda: worker.enqueue_scout_job("run_x"), 15 * 60),
        ("curator", lambda: worker.enqueue_curator_job("run_x"), 10 * 60),
        ("synthesizer", lambda: worker.enqueue_synthesizer_job("run_x"), 20 * 60),
        ("ideator", lambda: worker.enqueue_ideator_job("run_x"), 5 * 60),
        ("drafter", lambda: worker.enqueue_drafter_job("run_x"), 45 * 60),
        ("stylist", lambda: worker.enqueue_stylist_job("run_x"), 10 * 60),
        ("final_rewrite", lambda: worker.enqueue_final_rewrite_job("run_x"), 90 * 60),
        ("critic", lambda: worker.enqueue_critic_job("run_x"), 60 * 60),
        ("integrity", lambda: worker.enqueue_integrity_job("run_x"), 10 * 60),
        ("exports", lambda: worker.enqueue_exports_job("run_x"), 30 * 60),
    ],
)
def test_enqueue_passes_per_phase_job_timeout(
    phase: str, enqueue_fn: Any, expected_timeout: int
) -> None:
    enqueue_fn()
    kwargs = _last_kwargs()
    assert kwargs.get("job_timeout") == expected_timeout, (
        f"phase={phase}: expected job_timeout={expected_timeout}s, "
        f"got {kwargs.get('job_timeout')!r}"
    )


def test_env_override_for_scout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_RQ_TIMEOUT_SCOUT", "999")
    worker.enqueue_scout_job("run_x")
    assert _last_kwargs()["job_timeout"] == 999


def test_env_override_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOESSAY_RQ_TIMEOUT_SCOUT", "not-a-number")
    worker.enqueue_scout_job("run_x")
    assert _last_kwargs()["job_timeout"] == 15 * 60


def test_env_override_zero_or_negative_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOESSAY_RQ_TIMEOUT_SCOUT", "0")
    worker.enqueue_scout_job("run_x")
    assert _last_kwargs()["job_timeout"] == 15 * 60


def test_phase_job_timeout_seconds_helper() -> None:
    # Helper used by zombie_reaper to pick a phase-aware idle threshold.
    assert worker.phase_job_timeout_seconds("scout") == 15 * 60
    assert worker.phase_job_timeout_seconds("drafter") == 45 * 60
    # Unknown phase falls back to a safe 5min default (not the 180s
    # RQ default — we never want that to leak through).
    assert worker.phase_job_timeout_seconds("unknown_phase") == 5 * 60


with patch.object(worker, "_test_fake_queues", [], create=True):
    pass  # exists at module scope so the fixture's setattr is a no-op shadow
