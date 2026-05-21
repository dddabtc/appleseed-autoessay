"""Constants and small helpers for the A/B/B'/C architecture experiment."""

from __future__ import annotations

import os
import subprocess

EXPERIMENT_ID = "abc-architecture-comparison-v1"
GENERATION_MODEL_ID = "provider-configured-fallback-chain"
TOKEN_CAP_TOTAL = 1_800_000
MANUSCRIPT_MAX_TOKENS = 25_000
SELF_CRITIQUE_MAX_TOKENS = 25_000
PROVIDER_FALLBACK_ALLOWED = True
PROVIDER_FALLBACK_CHAIN = ("rightcode", "apiport", "minimax")
DEFAULT_MAX_CONCURRENCY = 1
MAX_ALLOWED_CONCURRENCY = 3
PRODUCTION_COMMIT_SHA_ENV = "AUTOESSAY_EXPERIMENT_ABC_PRODUCTION_SHA"
EXPERIMENT_SCRIPT_SHA_ENV = "AUTOESSAY_EXPERIMENT_ABC_SCRIPT_SHA"

MODEL_ENV = "AUTOESSAY_EXPERIMENT_ABC_MODEL"
TOKEN_CAP_ENV = "AUTOESSAY_EXPERIMENT_ABC_TOKEN_CAP"
RESULTS_DIR_ENV = "AUTOESSAY_EXPERIMENT_ABC_RESULTS_DIR"


def generation_model_id() -> str:
    """Return the model pinned for experiment generation."""
    return os.getenv(MODEL_ENV, GENERATION_MODEL_ID).strip() or GENERATION_MODEL_ID


def token_cap_total() -> int:
    """Return the total token cap with the experiment namespace override."""
    raw = os.getenv(TOKEN_CAP_ENV, "").strip()
    if not raw:
        return TOKEN_CAP_TOTAL
    try:
        value = int(raw)
    except ValueError:
        return TOKEN_CAP_TOTAL
    return value if value > 0 else TOKEN_CAP_TOTAL


def production_commit_sha() -> str | None:
    """Return the pinned A production SHA from the protocol env var."""
    return os.getenv(PRODUCTION_COMMIT_SHA_ENV, "").strip() or None


def experiment_script_sha() -> str | None:
    """Return the pinned experiment script SHA, falling back to local HEAD."""
    explicit = os.getenv(EXPERIMENT_SCRIPT_SHA_ENV, "").strip()
    if explicit:
        return explicit
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def require_frozen_shas() -> tuple[str, str]:
    """Return explicitly pinned production and script SHAs or fail hard."""
    production = os.getenv(PRODUCTION_COMMIT_SHA_ENV, "").strip()
    script = os.getenv(EXPERIMENT_SCRIPT_SHA_ENV, "").strip()
    missing = []
    if not production:
        missing.append(PRODUCTION_COMMIT_SHA_ENV)
    if not script:
        missing.append(EXPERIMENT_SCRIPT_SHA_ENV)
    if missing:
        raise RuntimeError(
            "ABC experiment requires explicit frozen SHA env vars: " + ", ".join(missing)
        )
    return production, script
