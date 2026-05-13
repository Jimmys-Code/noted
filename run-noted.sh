#!/usr/bin/env bash
# Fast-launch noted. Uses a pre-built renderer (no Vite at startup).
# Override with NOTED_DEV=1 ./run-noted.sh for hot-reload dev mode.
set -e
ROOT="$(dirname "$(readlink -f "$0")")"
cd "$ROOT/frontend"

if [ "${NOTED_DEV:-0}" = "1" ]; then
  exec npm run dev
fi

# Build once if dist/ is missing or stale.
if [ ! -f dist/index.html ] || [ src/App.tsx -nt dist/index.html ]; then
  npm run build >/dev/null
fi

exec node_modules/.bin/electron .
