#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="${AUTOESSAY_APP_DIR:-$REPO_ROOT}"
COMPOSE_FILE="${AUTOESSAY_COMPOSE_FILE:-docker-compose.yml}"
API_HEALTH_URL="${AUTOESSAY_API_HEALTH_URL:-http://127.0.0.1:8017/healthz}"
WEB_HEALTH_URL="${AUTOESSAY_WEB_HEALTH_URL:-http://127.0.0.1:3017/}"

cd "$APP_DIR"

docker compose -f "$COMPOSE_FILE" build
docker compose -f "$COMPOSE_FILE" run --rm migrate
docker compose -f "$COMPOSE_FILE" up -d

wait_for_url() {
  local url="$1" label="$2" attempts=30
  for i in $(seq 1 "$attempts"); do
    if curl --fail --silent --show-error --max-time 5 "$url" >/dev/null 2>&1; then
      echo "$label up after ${i}s"
      return 0
    fi
    sleep 1
  done
  echo "$label not healthy after ${attempts}s" >&2
  return 1
}

wait_for_url "$API_HEALTH_URL" "api"
wait_for_url "$WEB_HEALTH_URL" "frontend"

echo "Deployed appleseed-autoessay"
