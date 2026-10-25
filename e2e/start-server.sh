#!/usr/bin/env bash
# Boot the app for the Playwright suite: seed a throwaway DB, then serve it.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi
fi

export CLEANPLEX_DATA="${CLEANPLEX_DATA:-$PWD/e2e/.data}"
export CLEANPLEX_PORT="${CLEANPLEX_PORT:-7985}"

rm -rf "$CLEANPLEX_DATA"
mkdir -p "$CLEANPLEX_DATA"
"$PY" e2e/seed.py

exec "$PY" -m cleanplex
