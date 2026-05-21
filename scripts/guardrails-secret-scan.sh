#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

patterns=(
  'sk-[a-zA-Z0-9_-]{20,}'
  'AKIA[A-Z0-9]{16}'
  'gh''p_'
  'gh''o_'
  'gh''s_'
  'github_pat_[A-Za-z0-9_]{20,}'
  'aA''123456'
  'aA''977910'
  'autoessay[.]bayestone[.]org'
  'auth[.]bayestone[.]org'
  '/etc/appleseed-''autoessay'
  '/srv/appleseed-''autoessay'
  'Tail''scale'
  'dddabtc@gmail[.]com'
  '100[.]119[.]6[.]34'
  '100[.]72[.]152[.]96'
  '54[.]176[.]239[.]225'
)

joined="$(IFS='|'; echo "${patterns[*]}")"

if rg -n --hidden --glob '!.git/**' --glob '!backend/.venv/**' --glob '!frontend/node_modules/**' -e "$joined" .; then
  echo "guardrails: denied private string or token-shaped value found" >&2
  exit 1
fi

echo "guardrails: denylist clean"
