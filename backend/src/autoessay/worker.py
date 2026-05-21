import logging
import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from redis import Redis
from rq import Queue, Worker

from autoessay.agents.critic import run_critic
from autoessay.agents.curator import run_curator
from autoessay.agents.drafter import run_drafter
from autoessay.agents.exporter import run_exports
from autoessay.agents.final_rewrite import run_final_rewrite_then_critic
from autoessay.agents.ideator import run_ideator
from autoessay.agents.integrity import run_integrity
from autoessay.agents.proposal import run_proposal_draft
from autoessay.agents.scout import run_scout
from autoessay.agents.stylist import run_stylist
from autoessay.agents.synthesizer import run_synthesizer
from autoessay.auth.session import cleanup_and_reschedule
from autoessay.config import get_settings
from autoessay.corpus import run_corpus_ingest_job, run_corpus_style_profile_job
from autoessay.express_runner import run_express
from autoessay.harness import HookRegistry
from autoessay.run_writer import append_ledger_event
from autoessay.zombie_reaper import reap_zombies_once

logger = logging.getLogger(__name__)


# Per-phase RQ job timeout (seconds). RQ default is 180s which is way
# too short for the LLM-heavy phases — scout/curator/synth/drafter
# routinely run multi-minute and were silently failing on prod with
# ``JobTimeoutException`` (real-paper e2e on 2026-05-06 hit this on
# scout @ 180s). Codex round-1 AGREE-w-amend on these values + 2x
# safety margin. Override per-phase with
# ``AUTOESSAY_RQ_TIMEOUT_<PHASE>=<seconds>`` env var.
_PHASE_TIMEOUT_DEFAULTS: dict[str, int] = {
    "proposal": 5 * 60,  # 5 min (codex amend: cold-start safety)
    "express": 5 * 60,
    "scout": 15 * 60,
    "curator": 10 * 60,
    "synthesizer": 20 * 60,
    "framework_lens": 10 * 60,
    "tension_extraction": 10 * 60,
    "ideator": 5 * 60,
    "drafter": 45 * 60,
    "stylist": 10 * 60,
    # 2026-05-12 PR-360/361 raised the upper bound: gpt-5.5 streaming
    # round-0 stage B alone can run 6+ minutes, stage C is the standard
    # rewriter (~3-5 min), polish loop iterations may add more, and the
    # job is the combined ``run_final_rewrite_then_critic`` which then
    # runs critic phase (10-15 min). Real-paper canary on 2026-05-12
    # measured 61 min for the chain to complete. 90 min ceiling gives
    # comfortable headroom without unbounding the job. critic alone
    # also bumped to 60 min for the same chain reason.
    "final_rewrite": 90 * 60,
    "critic": 60 * 60,
    "integrity": 10 * 60,
    "exports": 30 * 60,
}


def _resolve_phase_timeout(phase: str) -> int:
    """Per-phase RQ timeout with env override.

    Env override key: ``AUTOESSAY_RQ_TIMEOUT_<PHASE_UPPERCASE>``.
    Centralized here so the timeout table stays the single source of
    truth — agent code never reads env directly.
    """
    env_key = f"AUTOESSAY_RQ_TIMEOUT_{phase.upper()}"
    raw = os.environ.get(env_key)
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("ignoring non-int env %s=%r; falling back to default", env_key, raw)
    return _PHASE_TIMEOUT_DEFAULTS.get(phase, 5 * 60)


# Public alias so other modules (e.g. zombie_reaper) can read the
# phase-aware timeout. Computed lazily from env at access time so
# tests can monkeypatch env without re-importing.
def phase_job_timeout_seconds(phase: str) -> int:
    return _resolve_phase_timeout(phase)


def noop_job(run_id: str, runs_root: str | None = None) -> dict[str, str]:
    settings = get_settings()
    root = Path(runs_root) if runs_root else settings.data_dir / "runs"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    append_ledger_event(run_dir, {"event": "noop_worker_job", "run_id": run_id})
    return {"run_id": run_id, "status": "ok"}


def build_worker() -> Worker:
    settings = get_settings()
    redis_connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=redis_connection)
    return Worker([queue], connection=redis_connection)


def _enqueue_phase(phase: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """Single chokepoint for phase-job enqueues so every job picks up
    the per-phase RQ ``job_timeout`` (instead of the 180s default).
    """
    settings = get_settings()
    redis_connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=redis_connection)
    job_timeout = _resolve_phase_timeout(phase)
    job = queue.enqueue(fn, *args, job_timeout=job_timeout, **kwargs)
    return str(job.id)


def enqueue_scout_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("scout", run_scout_job, run_id, lock_token)


def run_scout_job(run_id: str, lock_token: str | None = None) -> dict[str, object]:
    return run_scout(run_id, hooks=HookRegistry(), lock_token=lock_token)


def enqueue_proposal_job(
    run_id: str, user_draft: str | None = None, lock_token: str | None = None
) -> str:
    return _enqueue_phase(
        "proposal", run_proposal_draft, run_id, user_draft=user_draft, lock_token=lock_token
    )


def enqueue_express_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("express", run_express, run_id, lock_token=lock_token)


def enqueue_curator_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("curator", run_curator, run_id, lock_token=lock_token)


def enqueue_synthesizer_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("synthesizer", run_synthesizer, run_id, lock_token=lock_token)


def enqueue_ideator_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("ideator", run_ideator, run_id, lock_token=lock_token)


def enqueue_drafter_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("drafter", run_drafter, run_id, lock_token=lock_token)


def enqueue_stylist_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("stylist", run_stylist, run_id, lock_token=lock_token)


def enqueue_final_rewrite_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase(
        "final_rewrite",
        run_final_rewrite_then_critic,
        run_id,
        lock_token=lock_token,
    )


def enqueue_critic_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("critic", run_critic, run_id, lock_token=lock_token)


def enqueue_integrity_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("integrity", run_integrity, run_id, lock_token=lock_token)


def enqueue_exports_job(run_id: str, lock_token: str | None = None) -> str:
    return _enqueue_phase("exports", run_exports, run_id, lock_token=lock_token)


def enqueue_corpus_ingest_job(document_id: str) -> str:
    settings = get_settings()
    redis_connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=redis_connection)
    # Corpus ingest is not a pipeline phase; keep its own (longer)
    # timeout for pdf parsing + embedding.
    job = queue.enqueue(run_corpus_ingest_job, document_id, job_timeout=15 * 60)
    return str(job.id)


def enqueue_corpus_style_profile_job(user_id: str) -> str:
    settings = get_settings()
    redis_connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=redis_connection)
    job = queue.enqueue(run_corpus_style_profile_job, user_id, job_timeout=10 * 60)
    return str(job.id)


def enqueue_auth_session_cleanup_job(delay_seconds: int = 3600) -> str:
    """Initial bootstrap of the recurring auth-session cleanup job.

    The recurring function itself lives in autoessay.auth.session
    (cleanup_and_reschedule) — not in this module — because RQ refuses
    to enqueue functions from __main__, and worker.py is loaded as
    __main__ when the worker process starts via `python -m autoessay.worker`.
    """
    settings = get_settings()
    redis_connection = Redis.from_url(settings.redis_url)
    queue = Queue(settings.rq_queue_name, connection=redis_connection)
    job = queue.enqueue_in(
        timedelta(seconds=delay_seconds),
        cleanup_and_reschedule,
    )
    return str(job.id)


def _startup_zombie_sweep() -> None:
    """PR-I3: best-effort full sweep of zombie phase locks at worker
    startup. Catches runs whose previous worker container died with
    a held lock — without this, recovery has to wait for the api-side
    reaper interval (default 300s) or a user click. Failure here must
    NOT block worker startup; the reaper background loop will retry.
    """
    try:
        visited = reap_zombies_once()
        logger.info("worker startup zombie sweep: visited %d run(s)", visited)
    except Exception:
        logger.exception("worker startup zombie sweep failed; continuing")


def main() -> int:
    worker = build_worker()
    enqueue_auth_session_cleanup_job()
    _startup_zombie_sweep()
    worker.work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
