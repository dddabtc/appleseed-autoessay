#!/usr/bin/env bash
# Boot api + vite for Playwright. AUTH_BYPASS + SYNC_WORKER + every
# per-agent stub flag means no Casdoor / Redis / external LLM
# dependency. Cleanup on EXIT covers the temp dir and child processes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d -t autoessay-e2e.XXXX)"

# Ports default to 8017 / 5173 (matches docs and CI). Override via
# AUTOESSAY_E2E_API_PORT / AUTOESSAY_E2E_VITE_PORT when running on a
# host where those ports are already taken (e.g. on the prod host
# alongside the live api).
API_PORT="${AUTOESSAY_E2E_API_PORT:-8017}"
VITE_PORT="${AUTOESSAY_E2E_VITE_PORT:-5173}"
export AUTOESSAY_E2E_API_PORT="$API_PORT"

cleanup() {
  if [[ -n "${API_PID:-}" ]]; then
    kill "$API_PID" 2>/dev/null || true
  fi
  if [[ -n "${VITE_PID:-}" ]]; then
    kill "$VITE_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

export AUTOESSAY_AUTH_BYPASS=1
export AUTOESSAY_SYNC_WORKER=1
# PR-248 — test-only fail-phase endpoint is gated on AUTOESSAY_TEST_MODE.
# Enabling it here lets the retry-leg specs (FR-01.30 ~ .40) hit
# POST /api/test/runs/{id}/fail-phase to drop a run into FAILED_FIXABLE
# deterministically. The flag is hard-rejected in
# AUTOESSAY_ENV=production via the Settings root_validator, so it is
# structurally impossible to reach in prod even if this script leaks.
export AUTOESSAY_TEST_MODE=1
for stub in PROPOSAL SCOUT CURATOR SYNTHESIZER FRAMEWORK_LENS IDEATOR DRAFTER \
            STYLIST CRITIC INTEGRITY FRONT_MATTER SELF_CHECK DETAILED_OUTLINE \
            MATERIAL_DIAGNOSTIC OPENALEX LOCAL_DEDUP CNKI SAFETY_GATE \
            CANONICAL_MINING CURATOR_RERANK TENSION_EXTRACTION KERNEL_SUGGEST; do
  export "AUTOESSAY_${stub}_STUB=1"
done
# Slice G (#313): verification gate defaults to verified-only. Stub-mode
# e2e fixtures don't carry verified_by metadata so all sources classify
# as UNVERIFIED → gate clears the shortlist → spec timeout. Enable the
# experimental include flag for e2e (matches backend pytest fixture in
# conftest.py).
export AUTOESSAY_INCLUDE_UNVERIFIED_IN_CITATION_POOL=1
export AUTOESSAY_DATA_DIR="$TMP/data"
export DATABASE_URL="sqlite:///$TMP/autoessay.sqlite3"

mkdir -p "$AUTOESSAY_DATA_DIR"

# Apply migrations against the temp DB before booting the API.
( cd "$REPO_ROOT/backend" && alembic upgrade head ) >/dev/null

(
  cd "$REPO_ROOT/backend"
  uvicorn autoessay.main:app --host 127.0.0.1 --port "$API_PORT" --log-level warning
) &
API_PID=$!

# Wait for /readyz before starting Vite. Fail loudly if api dies OR
# stays alive but never becomes ready (e.g. DB hang, alembic
# regression). Without this, Vite would start anyway and Playwright
# would surface a misleading frontend error instead of the real
# backend startup failure.
ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "uvicorn exited before /readyz responded" >&2
    exit 1
  fi
  if curl -sf "http://127.0.0.1:${API_PORT}/readyz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
if [[ "$ready" -ne 1 ]]; then
  echo "uvicorn /readyz did not return 200 within 30s; aborting" >&2
  exit 1
fi

(
  cd "$REPO_ROOT/frontend"
  npx vite --host 127.0.0.1 --port "$VITE_PORT" --strictPort
) &
VITE_PID=$!

wait "$VITE_PID"
