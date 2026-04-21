#!/usr/bin/env bash
# Install twitter-memory as a systemd --user service so it auto-starts on
# login and restarts on crash. Idempotent — safe to re-run.
#
# Usage:
#   bash scripts/install-autostart.sh
#
# After install:
#   systemctl --user status twitter-memory
#   systemctl --user stop twitter-memory
#   systemctl --user disable twitter-memory   # turn off auto-start
#   journalctl --user -u twitter-memory -f    # follow logs
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$REPO/scripts/systemd/twitter-memory.service"
UNIT_DST_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DST_DIR/twitter-memory.service"

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "ERROR: $REPO/.venv/bin/python not found. Create the venv first:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

mkdir -p "$UNIT_DST_DIR"

# Rewrite the WorkingDirectory / ExecStart / Environment to this repo's real path
# so %h resolution issues never bite us.
sed \
  -e "s|%h/Desktop/attention_is_all_you_need/twitter_memory|$REPO|g" \
  "$UNIT_SRC" > "$UNIT_DST"

systemctl --user daemon-reload
systemctl --user enable twitter-memory.service
systemctl --user restart twitter-memory.service

# Survives logout (optional — only matters on headless servers)
loginctl enable-linger "$(id -un)" 2>/dev/null || true

echo ""
echo "=== twitter-memory autostart installed ==="
echo "unit: $UNIT_DST"
echo ""
sleep 2
systemctl --user --no-pager status twitter-memory.service | head -15
echo ""
echo "health check:"
curl -s http://127.0.0.1:8765/health || echo "(not yet responding — give it a moment)"
echo ""
echo ""
echo "manage:"
echo "  systemctl --user status   twitter-memory"
echo "  systemctl --user stop     twitter-memory"
echo "  systemctl --user disable  twitter-memory   # stop auto-start on login"
echo "  journalctl --user -u twitter-memory -f     # follow logs"
