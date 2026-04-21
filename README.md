# twitter-memory

Local-first personal Twitter/X activity log. Captures what you see and do on x.com via a Chrome extension, stores it in SQLite on your machine, and exposes a single MCP tool (`export_day`) that writes a complete markdown report of any day's activity for Claude Desktop to read.

## Layout

```
backend/     FastAPI ingest server (localhost:8765)
mcp_server/  MCP stdio server exposing export_day
extension/   Chrome MV3 extension (inject + content + SW + popup)
scripts/     seed_day.py — fills the DB with realistic test data
tests/       pytest suite
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Backend (long-running)
.venv/bin/python -m backend.main

# Seed a test day if you want something to export
TWITTER_MEMORY_DATA=./data .venv/bin/python -m scripts.seed_day 2026-04-21

# Run tests
.venv/bin/pytest -q
```

Data lives at `~/.twitter-memory/` by default (override with `TWITTER_MEMORY_DATA`).

## Claude Desktop MCP config

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "twitter-memory": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "env": {}
    }
  }
}
```

Then in Claude: call `export_day` with a YYYY-MM-DD date and optionally `exclude=["impressions", ...]`.

## Chrome extension

Load `extension/` unpacked in `chrome://extensions/` with Developer Mode on. The extension captures only while backend is running — events queue to `chrome.storage.local` during outages and flush on reconnect.
