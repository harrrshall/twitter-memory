#!/usr/bin/env bash
# Symmetric teardown for `./install.sh`. Data preserved by default.
#
# Usage:
#   ./uninstall.sh            # stop service, drop Claude Desktop MCP entry
#   ./uninstall.sh --clean    # also remove .venv
#   ./uninstall.sh --purge    # ALSO remove ~/.twitter-memory (prompts for DELETE)
#   ./uninstall.sh --dry-run  # preview only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -x ".venv/bin/python" ]; then
  # Nothing to do without the venv — the orchestrator lives there.
  echo "No .venv found at $SCRIPT_DIR/.venv — nothing to uninstall."
  echo "If the service is installed but the venv is gone, remove the"
  echo "systemd unit / launchd plist manually:"
  echo "  systemctl --user disable --now twitter-memory ; rm ~/.config/systemd/user/twitter-memory.service"
  echo "  launchctl unload ~/Library/LaunchAgents/com.local.twitter-memory.plist ; rm ~/Library/LaunchAgents/com.local.twitter-memory.plist"
  exit 0
fi

exec .venv/bin/python -c "from scripts.install_helpers.installer import uninstall; import sys; sys.exit(uninstall())" "$@"
