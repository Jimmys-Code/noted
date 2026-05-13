#!/usr/bin/env bash
# Launches noted (Electron + Python sidecar in dev mode).
cd "$(dirname "$(readlink -f "$0")")/frontend"
exec npm run dev
