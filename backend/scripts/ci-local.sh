#!/usr/bin/env bash
# Run the full local CI sweep and write .ci-attestation.json at the
# repo root. Replaces the old server-side CI workflow per issue #322 —
# GitHub Actions only verifies the attestation file existence + sha
# match against PR HEAD; the actual lint/typecheck/test runs locally.
#
# Default sweep: backend ruff + mypy + pytest, frontend tsc + lint +
# vitest. Playwright stub e2e is opt-in via --with-playwright (slow,
# requires running mirror).
#
# Usage:
#   backend/scripts/ci-local.sh                 # default sweep
#   backend/scripts/ci-local.sh --with-playwright   # + stub e2e
#
# After a green sweep this script writes:
#   .ci-attestation.json
# Amend the file into the HEAD commit before pushing:
#   git add .ci-attestation.json && git commit --amend --no-edit
# Then `git push`. The pre-push hook (scripts/pre-push.sh) verifies
# the attestation exists for the commit being pushed.

set -euo pipefail

WITH_PLAYWRIGHT=0
for arg in "$@"; do
  case "$arg" in
    --with-playwright) WITH_PLAYWRIGHT=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOG_DIR="$(mktemp -d -t autoessay-ci-local.XXXX)"
cleanup() { rm -rf "$LOG_DIR"; }
trap cleanup EXIT

echo "[ci-local] log dir: $LOG_DIR"
echo "[ci-local] repo:    $REPO_ROOT"

# Environment checks first — fail fast with friendly hints.
if [ ! -d "$REPO_ROOT/backend/.venv" ]; then
  echo "[ci-local] ERROR: backend/.venv missing. Run: cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -e \".[dev]\"" >&2
  exit 1
fi
if [ ! -d "$REPO_ROOT/frontend/node_modules" ]; then
  echo "[ci-local] ERROR: frontend/node_modules missing. Run: cd frontend && npm install" >&2
  exit 1
fi

# 1. backend
echo "[ci-local] backend ruff format + check + mypy + pytest..."
cd "$REPO_ROOT/backend"
# shellcheck disable=SC1091
source .venv/bin/activate
ruff format --check . > "$LOG_DIR/ruff_format.log" 2>&1
ruff check . > "$LOG_DIR/ruff_check.log" 2>&1
mypy src > "$LOG_DIR/mypy.log" 2>&1
pytest -x -q > "$LOG_DIR/pytest.log" 2>&1
PYTEST_PASSED=$(grep -oE '[0-9]+ passed' "$LOG_DIR/pytest.log" | tail -1 | grep -oE '[0-9]+' || echo 0)
echo "[ci-local] backend OK (pytest $PYTEST_PASSED passed)"

# 2. frontend
echo "[ci-local] frontend tsc + lint + vitest..."
cd "$REPO_ROOT/frontend"
npx tsc --noEmit > "$LOG_DIR/tsc.log" 2>&1
npm run lint --silent > "$LOG_DIR/eslint.log" 2>&1
npm test --silent > "$LOG_DIR/vitest.log" 2>&1
VITEST_PASSED=$(grep -oE 'Tests +[0-9]+ passed' "$LOG_DIR/vitest.log" | tail -1 | grep -oE '[0-9]+' || echo 0)
echo "[ci-local] frontend OK (vitest $VITEST_PASSED passed)"

# 3. optional playwright stub e2e
PLAYWRIGHT_PASSED=0
if [ $WITH_PLAYWRIGHT -eq 1 ]; then
  echo "[ci-local] playwright stub e2e (slow)..."
  npx playwright test --reporter=line > "$LOG_DIR/playwright.log" 2>&1
  PLAYWRIGHT_PASSED=$(grep -oE '[0-9]+ passed' "$LOG_DIR/playwright.log" | tail -1 | grep -oE '[0-9]+' || echo 0)
  echo "[ci-local] playwright OK ($PLAYWRIGHT_PASSED passed)"
fi

# 4. write attestation
#
# Note: we do NOT bind the attestation to a commit SHA. Doing so
# creates a chicken-and-egg loop because amending the commit to
# include the attestation file changes the commit's SHA. Instead the
# attestation records timestamp + content hashes + tool versions; the
# GitHub workflow enforces a 6h freshness window which is the actual
# trust signal. For solo projects this is sufficient; multi-author
# repos should add a GPG signature on the attestation and verify the
# signing key in the workflow.
cd "$REPO_ROOT"
TS=$(date -u +%FT%TZ)
LOG_HASH=$(cat "$LOG_DIR"/*.log | sha256sum | cut -d' ' -f1)
HEAD_SHA_AT_WRITE=$(git rev-parse --verify HEAD 2>/dev/null || echo "unborn")
TREE_SHA_AT_WRITE=$(git write-tree)
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
NODE_VERSION=$(node --version 2>&1 | sed 's/^v//')

RAN_JOBS='"ruff_format","ruff_check","mypy","pytest","tsc","eslint","vitest"'
if [ $WITH_PLAYWRIGHT -eq 1 ]; then
  RAN_JOBS="$RAN_JOBS,\"playwright\""
fi

cat > "$REPO_ROOT/.ci-attestation.json" <<JSON
{
  "version": 1,
  "timestamp": "$TS",
  "result": "pass",
  "test_log_sha256": "$LOG_HASH",
  "pytest_passed": $PYTEST_PASSED,
  "vitest_passed": $VITEST_PASSED,
  "playwright_passed": $PLAYWRIGHT_PASSED,
  "ran_jobs": [$RAN_JOBS],
  "tool_versions": {
    "python": "$PYTHON_VERSION",
    "node": "$NODE_VERSION"
  },
  "info_only_head_sha_at_sweep": "$HEAD_SHA_AT_WRITE",
  "info_only_tree_sha_at_sweep": "$TREE_SHA_AT_WRITE"
}
JSON

echo "[ci-local] attestation written: $REPO_ROOT/.ci-attestation.json"
echo ""
echo "next:"
echo "  git add .ci-attestation.json && git commit --amend --no-edit && git push"
