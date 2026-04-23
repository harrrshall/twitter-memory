#!/usr/bin/env bash
# One-click install for twitter-memory.
#
# Flow:
#   1. Verify Python >=3.11 is available.
#   2. Create .venv and install requirements (skipped if requirements.txt is
#      unchanged since the last install).
#   3. Delegate to the Python orchestrator, which handles everything else:
#      data dir, env file, systemd/launchd service, Claude Desktop config
#      merge, health check, and extension nudge.
#
# Forward any flags (e.g. --dry-run, --port 8766, --skip-claude, --force)
# straight to the orchestrator.
#
# Usage:
#   ./install.sh                 # fresh install
#   ./install.sh --dry-run       # preview every command + file diff
#   ./install.sh --yes           # non-interactive (assume defaults)
#   ./install.sh --autostart-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 1. Find a Python >= 3.11.
find_python() {
  local candidates=("${PYTHON:-}" python3.12 python3.11 python3)
  for p in "${candidates[@]}"; do
    [ -z "$p" ] && continue
    if command -v "$p" >/dev/null 2>&1; then
      local ver
      ver="$("$p" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true)"
      case "$ver" in
        3.11|3.12|3.13|3.14) echo "$p"; return 0 ;;
      esac
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  cat >&2 <<'EOF'
ERROR: No Python >=3.11 found on PATH.

Install one:
  macOS: brew install python@3.12
  Linux: apt install python3.12   (or use pyenv)

Then re-run with PYTHON=/abs/path/to/python3.12 ./install.sh
EOF
  exit 1
fi

# 2. Create / update the venv. pip-install-requirements only if the hash
#    changed since last install — the orchestrator records it in the
#    install manifest so repeat runs are fast.
if [ ! -x ".venv/bin/python" ]; then
  echo "Creating .venv using $PY ..."
  "$PY" -m venv .venv
fi

# Always upgrade pip quietly — avoids noisy "A new release of pip" lines
# and saves real minutes on slow networks the next time requirements shift.
.venv/bin/python -m pip install --upgrade --quiet pip

MANIFEST="${HOME}/.twitter-memory/.install-manifest.json"
REQ_HASH="$(sha256sum requirements.txt 2>/dev/null | awk '{print $1}' || shasum -a 256 requirements.txt | awk '{print $1}')"
PREV_HASH=""
if [ -f "$MANIFEST" ]; then
  PREV_HASH="$(.venv/bin/python -c "import json,sys; print(json.load(open(sys.argv[1])).get('requirements_sha256',''))" "$MANIFEST" 2>/dev/null || true)"
fi

if [ "$REQ_HASH" != "$PREV_HASH" ]; then
  echo "Installing Python dependencies ..."
  .venv/bin/python -m pip install --quiet -r requirements.txt
else
  echo "requirements.txt unchanged since last install — skipping pip install."
fi

# 3. Hand off to the Python orchestrator.
exec .venv/bin/python -m scripts.install_helpers.installer "$@"
