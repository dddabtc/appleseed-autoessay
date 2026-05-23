#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${AUTOESSAY_SOURCE_DIR:-/opt/appleseed-autoessay/source}"
DEPLOY_REMOTE="${AUTOESSAY_DEPLOY_REMOTE:-origin}"
DEPLOY_BRANCH="${AUTOESSAY_DEPLOY_BRANCH:-main}"
DEPLOY_SCRIPT="${AUTOESSAY_DEPLOY_SCRIPT:-$SOURCE_DIR/ops/deploy.sh}"
LOCK_FILE="${AUTOESSAY_DEPLOY_LOCK_FILE:-/tmp/appleseed-autoessay-deploy.lock}"
LOG_FILE="${AUTOESSAY_DEPLOY_LOG:-}"

if [[ -n "$LOG_FILE" ]]; then
  mkdir -p "$(dirname "$LOG_FILE")"
  exec > >(tee -a "$LOG_FILE") 2>&1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "deploy already running"
  exit 75
fi

echo "deploy start $(date -u +%FT%TZ)"

cd "$SOURCE_DIR"
git fetch --prune "$DEPLOY_REMOTE" \
  "+refs/heads/$DEPLOY_BRANCH:refs/remotes/$DEPLOY_REMOTE/$DEPLOY_BRANCH"
git reset --hard "refs/remotes/$DEPLOY_REMOTE/$DEPLOY_BRANCH"

"$DEPLOY_SCRIPT"

echo "deploy done $(date -u +%FT%TZ)"
