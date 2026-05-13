#!/usr/bin/env bash
# Deploy noted-sync to the droplet. Idempotent — safe to re-run.
#
# Run as root on the droplet:
#   bash sync-server/deploy.sh [TOKEN]
#
# If TOKEN is omitted, a random 48-char hex token is generated and printed.
# Store the printed token in your client's sync config — it's needed on every API call.
set -euo pipefail

TOKEN="${1:-$(python3 -c 'import secrets; print(secrets.token_hex(24))')}"

INSTALL_DIR=/opt/noted-sync
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] Setting up install dir at $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data"

echo "[2/6] Creating venv + installing deps"
if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q -r "$SRC_DIR/requirements.txt"

echo "[3/6] Installing app code as the 'noted_sync' module"
# Drop app.py into a tiny package so systemd can launch via `python -m noted_sync`.
mkdir -p "$INSTALL_DIR/noted_sync"
cp "$SRC_DIR/app.py" "$INSTALL_DIR/noted_sync/__init__.py"
cat > "$INSTALL_DIR/noted_sync/__main__.py" <<'PY'
from . import run
run()
PY
# Make it importable from the venv site-packages so -m works regardless of cwd.
SP=$(ls -d "$INSTALL_DIR"/venv/lib/python*/site-packages | head -1)
rm -rf "$SP/noted_sync"
cp -r "$INSTALL_DIR/noted_sync" "$SP/"

echo "[4/6] Installing systemd unit"
sed "s/CHANGE_ME_BEFORE_ENABLING/$TOKEN/" "$SRC_DIR/noted-sync.service" \
  > /etc/systemd/system/noted-sync.service
systemctl daemon-reload
systemctl enable noted-sync
systemctl restart noted-sync
sleep 1
systemctl status noted-sync --no-pager | head -10

echo "[5/6] Health check"
curl -sS http://127.0.0.1:8770/health && echo

echo "[6/6] Done"
echo
echo "  Token: $TOKEN"
echo "  Add this nginx block inside your existing server { ... }:"
echo
cat <<'NGINX'
    location = /noted { return 301 /noted/; }
    location /noted/ {
        proxy_pass http://127.0.0.1:8770/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 20M;
    }
NGINX
echo
echo "Then: sudo nginx -t && sudo systemctl reload nginx"
echo "Then: curl -H \"Authorization: Bearer $TOKEN\" https://jimmyspianotuning.com.au/noted/health"
