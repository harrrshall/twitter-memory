# One-click install — Implementation progress

Last updated: 2026-04-23
Plan: /home/cybernovas/.claude/plans/curried-floating-parasol.md

## Goal

Collapse the multi-step setup into `./install.sh` on Linux + macOS. Thin
bash bootstrap → stdlib-only Python orchestrator → systemd / launchd
service install → Claude Desktop config merge → Chrome-side one-click
documented.

## Step tracker

| # | Step | Implemented | Tested | Notes |
|--:|:---|:---:|:---:|:---|
| 1 | `runner.py` — dry-run-aware side-effect wrapper | ✓ | ✓ | dry-run writes captured, never touch disk |
| 2 | `detect.py` — OS / Python / browser / port detection | ✓ | ✓ | port-probe tested via real bind |
| 3 | `merge_claude_config.py` — atomic JSON merge helper | ✓ | ✓ | 13 pytest cases incl. malformed-refuse, conflict-prompt, backup |
| 4 | `service_linux.py` — systemd unit render + install | ✓ | ✓ | converted to `.service.template` with `{{REPO}}/{{VENV_PYTHON}}/{{ENV_FILE}}` |
| 5 | `service_macos.py` — launchd plist via plistlib | ✓ | ✓ | RunAtLoad + KeepAlive={SuccessfulExit:False}; golden-value test |
| 6 | `installer.py` — orchestrator | ✓ | ✓ | shares code between install() and uninstall() |
| 7 | `install.sh` — bash bootstrap | ✓ | ✓ | finds Python ≥3.11; skips `pip install` when requirements sha unchanged |
| 8 | `uninstall.sh` — symmetric teardown | ✓ | ✓ | `--clean` (venv) and `--purge` (~/.twitter-memory, with typed DELETE confirm) |
| 9 | Delete `scripts/install-autostart.sh` + old non-template `.service` | ✓ | — | confirmed no remaining references |
| 10 | `tests/test_install_helpers.py` | ✓ | ✓ | 26 tests |
| 11 | Full pytest green | ✓ | ✓ | **179 passed** (153 baseline + 26 new) |
| 12 | Manual verify on this machine (Linux) | ✓ | ✓ | see Real-run verification below |
| 13 | README Quick install section | ✓ | — | Manual install demoted to fallback |

## Real-run verification (2026-04-23, Ubuntu 24.04.4, Python 3.12.3)

- **Port conflict auto-resolved** — detected stray backend squatting 8765, installer offered 8766 via prompt.
- **systemd unit** installed to `~/.config/systemd/user/twitter-memory.service` (EnvironmentFile points at shared env); `daemon-reload`, `enable --now`, `is-active = active`.
- **Health check** returned 200 at `http://127.0.0.1:8766/health` within 30s budget.
- **Claude Desktop config** created at `~/.config/Claude/claude_desktop_config.json` with just our MCP stanza; timestamped `.bak-*` would be created on any conflict.
- **Idempotency** — second run: `requirements.txt unchanged — skipping pip install`; `write (unchanged): env`; `write (unchanged): unit file`; no new backup file. systemd `daemon-reload/enable/restart` still run (accepted — cheap + guarantees state).
- **Uninstall** (`./uninstall.sh --yes`): service disabled + unit removed + MCP stanza popped from Claude config (backup saved) + manifest removed. DB + exports + env file preserved.
- **Dry-run** (`./install.sh --dry-run --yes`): printed every command + unified diffs for env file and manifest; zero disk writes confirmed.

## Log (append-only)

- 2026-04-23T11:15: Plan approved. Tracker initialized.
- 2026-04-23T12:00: All 13 steps green. 179/179 pytest. Real install + idempotency + uninstall verified on this machine. Shipped.
