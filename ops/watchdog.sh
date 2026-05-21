#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="${AUTOESSAY_APP_DIR:-$REPO_ROOT}"
COMPOSE_FILE="${AUTOESSAY_COMPOSE_FILE:-docker-compose.yml}"
API_HEALTH_URL="${AUTOESSAY_API_HEALTH_URL:-http://127.0.0.1:8017/healthz}"
WEB_HEALTH_URL="${AUTOESSAY_WEB_HEALTH_URL:-http://127.0.0.1:3017/}"

cd "$APP_DIR"

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

restart_service() {
  local service="$1"
  echo "Restarting unhealthy service: $service"
  compose up -d "$service"
}

for service in api frontend worker; do
  if ! compose ps --status running "$service" >/dev/null 2>&1; then
    restart_service "$service"
  fi
done

if ! curl --fail --silent --show-error --max-time 5 "$API_HEALTH_URL" >/dev/null; then
  restart_service api
fi

if ! curl --fail --silent --show-error --max-time 5 "$WEB_HEALTH_URL" >/dev/null; then
  restart_service frontend
fi

echo "autoessay watchdog completed"
