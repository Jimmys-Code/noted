#!/usr/bin/env bash
# Optional: install a systemd --user service so the noted backend is always
# warm. Electron will detect the running backend on launch and skip spawn,
# giving you a near-instant window.
#
# Usage:
#   ./install-warm-backend.sh           # install + start
#   ./install-warm-backend.sh disable   # stop + remove
set -e
ROOT="$(dirname "$(readlink -f "$0")")"
UNIT="$HOME/.config/systemd/user/noted-backend.service"

if [ "${1:-}" = "disable" ]; then
  systemctl --user disable --now noted-backend.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "noted-backend disabled."
  exit 0
fi

mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<EOF
[Unit]
Description=noted local backend (warm)
After=default.target

[Service]
Type=simple
ExecStart=$ROOT/backend/.venv/bin/python $ROOT/backend/app.py
Environment=NOTED_PORT=8765
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now noted-backend.service
sleep 1
systemctl --user status noted-backend.service --no-pager -l | head -10 || true
echo
echo "OK — backend will auto-start on login and survive across app restarts."
echo "Launching noted will now skip Python startup and connect immediately."
