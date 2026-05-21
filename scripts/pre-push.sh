#!/usr/bin/env bash
# Git pre-push hook: refuse to push if HEAD doesn't carry a fresh
# .ci-attestation.json with result='pass'.
#
# Install once per clone:
#   ln -sf ../../scripts/pre-push.sh .git/hooks/pre-push
#
# Workflow (each push):
#   1. make changes + commit
#   2. backend/scripts/ci-local.sh        # full sweep, writes attestation
#   3. git add .ci-attestation.json
#      git commit --amend --no-edit       # bake attestation into HEAD
#   4. git push                           # this hook verifies attestation
#
# Trust model: solo project — author runs the sweep honestly. The
# attestation deliberately does not bind to a commit SHA (that would
# create an amend / SHA-change loop). The 6h timestamp window is the
# actual freshness signal. Multi-author repos should add GPG signing
# on the attestation file.
#
# Emergency override:
#   git push --no-verify                  # skip hook (hotfix only)
#   ...then catch up with proper attestation in a follow-up.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

while read -r local_ref local_sha remote_ref remote_sha; do
  # Skip deletes (local_sha is all zeros).
  if [[ "$local_sha" =~ ^0+$ ]]; then
    continue
  fi

  if ! git cat-file -e "$local_sha:.ci-attestation.json" 2>/dev/null; then
    cat <<EOF >&2
[pre-push] ERROR: commit $local_sha is missing .ci-attestation.json

Run the local CI sweep and amend the attestation into HEAD:
  backend/scripts/ci-local.sh
  git add .ci-attestation.json
  git commit --amend --no-edit
  git push

Or skip this hook for a hotfix (catch up later):
  git push --no-verify
EOF
    exit 1
  fi

  RESULT=$(git show "$local_sha:.ci-attestation.json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("result",""))')
  if [ "$RESULT" != "pass" ]; then
    cat <<EOF >&2
[pre-push] ERROR: attestation result='$RESULT' (expected 'pass')

The local CI sweep didn't pass. Fix and re-run:
  backend/scripts/ci-local.sh
EOF
    exit 1
  fi

  TS=$(git show "$local_sha:.ci-attestation.json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("timestamp",""))')
  if [ -z "$TS" ]; then
    echo "[pre-push] ERROR: attestation has no timestamp" >&2
    exit 1
  fi
  AGE=$(( $(date +%s) - $(date -d "$TS" +%s) ))
  if [ $AGE -ge 21600 ]; then
    cat <<EOF >&2
[pre-push] ERROR: attestation timestamp '$TS' is older than 6h (age=${AGE}s)

The cached sweep is too old to trust. Re-run:
  backend/scripts/ci-local.sh
  git add .ci-attestation.json
  git commit --amend --no-edit
  git push
EOF
    exit 1
  fi
done

echo "[pre-push] CI attestation verified for HEAD; proceeding with push."
