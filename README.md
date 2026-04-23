# twitter-memory

Local-first personal Twitter/X activity log. A Chrome extension captures what
you see and do on x.com, a FastAPI backend on localhost ingests the events
into a SQLite database on your machine, and an MCP server exposes that
data — both as a daily markdown export you (or an LLM) can read, and as a
small set of sliced query tools agents can call directly.

No data leaves your computer. No Twitter credentials are used. No feed is
modified — the extension is pure observation.

---

## Table of contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
  - [Quick install (one command)](#quick-install-one-command)
  - [Manual install](#manual-install)
- [Daily use](#daily-use)
  - [Passive capture](#passive-capture)
  - [Exporting a day](#exporting-a-day)
  - [The per-day export layout](#the-per-day-export-layout)
- [MCP tool reference](#mcp-tool-reference)
- [Configuration](#configuration)
- [Running the backend as a service](#running-the-backend-as-a-service)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Data retention and privacy](#data-retention-and-privacy)
- [File reference](#file-reference)

---

## Architecture

```
x.com / twitter.com tab
  └── content-script.js            observes clicks, impressions, selections,
                                   media opens, nav, scroll bursts, follows
       │ chrome.runtime.sendMessage
       ▼
background service worker
  └── service-worker.js            queues, batches (~3s or 50 events), POSTs
       │ http://127.0.0.1:8765/ingest
       ▼
FastAPI backend (backend/main.py)
  ├── /ingest           parse + dedup by event_id + INSERT per-table
  ├── /export/day       build the per-day export on demand
  ├── /health, /stats
  └── nightly retention (3am local, configurable per-table)
       │ reads/writes
       ▼
SQLite at ~/.twitter-memory/db.sqlite

MCP stdio server (mcp_server/server.py)
  ├── export_day          full per-day digest + JSON
  ├── daily_summary_json  structured JSON only
  ├── daily_briefing      opt-in LLM synthesis
  └── agent-query tools   search_tweets, top_dwelled, read_but_not_engaged,
                          algorithmic_pressure, author_report, session_detail,
                          recent_sessions, hesitation_report
```

See `AGENT_NOTES.md` for the deeper design rationale, world-boundary rules,
and patterns that worked. See `plan.md` for the original v1 spec.

---

## Prerequisites

- Python 3.11+
- Chromium, Chrome, or Brave (with Developer Mode available in extensions)
- Claude Desktop (only needed if you want the MCP tools wired into Claude)
- macOS or Linux. Everything is loopback-only; no inbound ports.

---

## Setup

### Quick install (one command)

```bash
./install.sh
```

This creates the venv, installs dependencies, writes `~/.twitter-memory/env`
with a free port, installs the backend as a user service (systemd on Linux,
launchd on macOS), merges the MCP stanza into Claude Desktop's
`claude_desktop_config.json` (atomically, with a timestamped `.bak-*`
backup), runs a `/health` check against the live backend, and prints the
exact path to load-unpacked the Chrome extension.

Useful flags:

| Flag | Behavior |
|---|---|
| `--dry-run` | Print every command + JSON/file diff. No disk writes. |
| `--yes` | Non-interactive — take defaults for all prompts. |
| `--port N` | Pin the backend port (otherwise auto-picks the first free port from 8765..8775). |
| `--skip-claude` | Don't touch `claude_desktop_config.json`. |
| `--skip-extension` | Suppress the Chrome nudge. |
| `--autostart-only` | Re-install just the systemd/launchd service. |
| `--force` | Overwrite an existing `twitter-memory` MCP entry whose paths don't match. |

After install, you still do **one** Chrome step: open
`chrome://extensions/` → Developer mode on → **Load unpacked** → select
`./extension/`. The installer prints the absolute path.

Re-running `./install.sh` is a no-op if nothing has changed
(`requirements.txt` SHA, unit file content, and Claude config entry are all
compared before writing). Upgrading: just `git pull` and re-run.

Uninstall — data preserved by default:

```bash
./uninstall.sh             # stop service, drop MCP entry, keep DB + exports
./uninstall.sh --clean     # ALSO remove .venv
./uninstall.sh --purge     # ALSO remove ~/.twitter-memory (prompts for DELETE)
```

### Manual install

If you'd rather wire things up by hand — or `install.sh` hits an
unsupported platform (Windows, WSL1, non-systemd Linux) — follow the three
steps below.

#### 1. Python backend

```bash
cd /path/to/twitter_memory

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Start the ingest backend (long-running — keep a terminal open, or set up a
background service; see below):

```bash
.venv/bin/python -m backend.main
```

By default it listens on `127.0.0.1:8765` and stores the SQLite database at
`~/.twitter-memory/db.sqlite`. Override the data directory with
`TWITTER_MEMORY_DATA=/some/path`. Override the port with
`TWITTER_MEMORY_PORT=8766`.

You can confirm it's alive with:

```bash
curl -s http://127.0.0.1:8765/health
```

If you want to try the export pipeline before doing any real browsing, seed
a fake day:

```bash
.venv/bin/python -m scripts.seed_day 2026-04-21
```

#### 2. Chrome extension

1. Open `chrome://extensions/` in Chrome/Chromium/Brave.
2. Toggle **Developer mode** on (top right).
3. Click **Load unpacked** and select the `extension/` directory of this repo.
4. The extension appears as *Twitter Memory (local)*. Pin it if you want a
   one-click status view via its popup.

The extension has no settings — if the backend is reachable on
`127.0.0.1:8765`, it captures. If the backend is down, events queue into
`chrome.storage.local` and flush on reconnect, so brief outages don't lose
data.

Host permissions are scoped to `twitter.com`, `x.com`, and
`127.0.0.1:8765`. The extension does not request any other origins.

Whenever you change extension code, return to `chrome://extensions/` and
click **Reload** on the Twitter Memory card. An unreloaded extension is the
single most common source of confusion — verify the reload worked by
opening an x.com tab, browsing for ~10 seconds, and checking:

```bash
sqlite3 ~/.twitter-memory/db.sqlite "SELECT max(captured_at) FROM impressions;"
```

The returned timestamp should be within the last minute.

#### 3. Claude Desktop MCP server

Edit Claude Desktop's config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Add (or merge) a `twitter-memory` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "twitter-memory": {
      "command": "/absolute/path/to/twitter_memory/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "env": {}
    }
  }
}
```

Use the absolute venv Python path — Claude Desktop does not source your
shell rc, so relative paths and aliases won't resolve. Restart Claude
Desktop for the change to pick up.

Ask Claude something like *"export my Twitter day for yesterday"* or
*"what did I read about AI this week"*. The MCP tools in `server.py` will
be listed in Claude's UI under *twitter-memory*.

---

## Daily use

### Passive capture

Just browse x.com / twitter.com. With the backend running and the extension
loaded, every impression, click, like, reply, search, text selection, media
open, navigation, and follow/unfollow gets captured into the local
database. There is no UI to interact with; the extension popup only shows
capture status.

The signals currently captured:

| Signal | Notes |
|---|---|
| Impressions | per-tweet visibility + dwell time, aggregated as bursts |
| Interactions | like / retweet / reply / bookmark / profile-click / expand |
| Searches | query text + session attribution |
| Link clicks | URL, domain, link kind, modifiers (meta/ctrl/alt) |
| Text selections | selected text + tweet source + copy vs select |
| Media opens | photo / video / GIF — per-tweet + index |
| Scroll bursts | duration, pixel delta, reversals — not 1 Hz sampling |
| Nav changes | SPA route transitions, feed-source before/after |
| Relationship changes | follow / unfollow / block / mute — success-gated |
| Sessions | start, end, feeds visited, total dwell |

### Exporting a day

Three equivalent entry points:

**From Claude Desktop:** ask *"export my Twitter day for 2026-04-22"* —
Claude invokes the `export_day` MCP tool.

**From the shell:**

```bash
curl -X POST "http://127.0.0.1:8765/export/day?date=2026-04-22"
```

**From Python:**

```python
from datetime import date
from mcp_server import export, settings
res = export.write_export(settings.DB_PATH, date(2026, 4, 22))
print(res["dir_path"])
```

All three write identical files on disk. The first one also inlines the
`digest.md` contents into Claude's conversation so it can answer follow-up
questions immediately.

### The per-day export layout

Each export writes a per-day directory under `~/.twitter-memory/exports/`:

```
exports/
  SCHEMA.md                 shared interpretive guide (written once)
  2026-04-22/
    digest.md               TL;DR + Summary + Topics + Authors + Threads
    tweets.md               ranked tweets + repeat-exposure
    activity.md             sessions + searches + interactions + link-outs + selections + media
    timeline.md             chronological event stream per session
    data.json               complete structured companion (agents prefer this)
  2026-04-21/
    …
```

Which file answers which question:

| Question | Open |
|---|---|
| *"What happened yesterday, in one glance?"* | `digest.md` |
| *"What did the feed actually show me?"* | `tweets.md` |
| *"What did I do (clicks, selections, searches)?"* | `activity.md` |
| *"Reconstruct my journey minute-by-minute."* | `timeline.md` |
| Structured programmatic access | `data.json` |
| Field meanings, scoring formulas, topic rules | `SCHEMA.md` |

`digest.md` is intentionally small (typically under 50 KB even on heavy
days) so that Claude inlines it into a response without truncation.
`data.json` contains every key the markdown files carry plus the raw
per-impression log (`impressions`, `revisits`, and the full `timeline`) —
agents should prefer parsing JSON over regex-ing markdown.

The legacy flat layout (`2026-04-22.md` + `2026-04-22.json` at the exports
root) is auto-deleted on the first successful write into the new
per-day-directory layout.

---

## MCP tool reference

All tools live in `mcp_server/server.py` and are surfaced to Claude Desktop
via the MCP config above. Everything is read-only against the local DB.

| Tool | Purpose |
|---|---|
| `export_day` | Write the full per-day export and return paths + inlined digest. |
| `daily_summary_json` | Structured JSON for one day (no disk write — same shape as `data.json`). |
| `daily_briefing` | Opt-in LLM synthesis of a day's data (requires API key — see [Configuration](#configuration)). |
| `search_tweets` | Substring search over tweet text in a date range; optional author, min dwell, engaged-only filters. |
| `top_dwelled` | Tweets with the most total dwell time — "what did I actually read". |
| `read_but_not_engaged` | Silent-but-meaningful tweets: dwelled, but you didn't like/reply/rt/bookmark. |
| `algorithmic_pressure` | Tweets the feed showed you ≥N times — what's being pushed at you. |
| `author_report` | Engagement portrait with one author in a date range. |
| `session_detail` | Everything observed in one session (by session_id). |
| `recent_sessions` | Last N sessions with per-session counts. |
| `hesitation_report` | Cursor-linger-without-click signal (placeholder until Sprint 2 capture lands). |

Agent-query tools (`search_tweets` through `hesitation_report`) all return
a `_meta` envelope (`row_count`, `truncated`, `query_ms`, `date_range`) and
cap at 500 rows per call. Narrow your filters if you hit the cap.

Examples you can paste into Claude Desktop once the MCP is wired up:

- *"What did I read about AI this week? Was I doomscrolling?"*
- *"Show me tweets I dwelled on for more than 10 seconds but didn't engage with."*
- *"Which accounts has the feed been pushing hardest at me this month?"*
- *"Pull the full session detail for the one that started at 11pm yesterday."*

---

## Configuration

### Environment variables

| Variable | Default | Where |
|---|---|---|
| `TWITTER_MEMORY_DATA` | `~/.twitter-memory` | Root for `db.sqlite`, `exports/`, `backups/`, `config.toml`. |
| `TWITTER_MEMORY_PORT` | `8765` | Backend ingest port (loopback only). |
| `TWITTER_MEMORY_TZ` | system localtime | IANA zone used for "local day" boundaries in the export. |

### Optional: daily_briefing LLM synthesis

`daily_briefing` is opt-in. It sends that day's `data.json` to a
configured LLM and returns a compact synthesis (headline, hesitations,
suggested replies, follow-ups, topic gaps). Without configuration it
returns `{"error": "not configured"}` — your day's data is not sent
anywhere by default.

To enable, either set env vars when launching Claude Desktop's MCP process
(edit your `claude_desktop_config.json` `env` block) or drop a TOML file at
`~/.twitter-memory/config.toml`:

```toml
[briefing]
provider = "anthropic"
model = "claude-sonnet-4-6"
api_key = "sk-ant-..."
```

Equivalent environment variables:

- `TWITTER_MEMORY_BRIEFING_PROVIDER`
- `TWITTER_MEMORY_BRIEFING_MODEL`
- `TWITTER_MEMORY_BRIEFING_API_KEY`

Env vars override config.toml. The key is only read when `daily_briefing`
is called — no background outreach.

Note: the `~/.twitter-memory/env` file that `install.sh` creates is the
single source of truth for the *service's* environment. Both systemd and
launchd consume it (systemd via `EnvironmentFile=`, launchd via a plist
re-render on install). If you edit values there, re-run `./install.sh` so
the launchd plist picks up the new values (systemd reads the file on each
start, so no re-render is needed on Linux).

---

## Running the backend as a service

`./install.sh` sets this up for you. What it does under the hood:

### Linux (systemd user service)

Renders `scripts/systemd/twitter-memory.service.template` with the actual
repo + venv + env-file paths and drops it at
`~/.config/systemd/user/twitter-memory.service`, then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now twitter-memory
```

Manage manually after install:

```bash
systemctl --user status twitter-memory
systemctl --user stop twitter-memory
journalctl --user -u twitter-memory -f
```

`install.sh` asks before enabling `loginctl enable-linger` — default is
No, which is right for a laptop (service runs while you're logged in). Say
yes only on always-on machines.

### macOS (launchd LaunchAgent)

Generates `~/Library/LaunchAgents/com.local.twitter-memory.plist` via
`plistlib` (no string-template XML footguns) with `RunAtLoad=true` and
`KeepAlive={"SuccessfulExit": False}` — restart on crash, don't fight
`launchctl unload`. Logs land at `~/Library/Logs/twitter-memory/{stdout,stderr}.log`.

```bash
launchctl list | grep twitter-memory
tail -f ~/Library/Logs/twitter-memory/stderr.log
```

### Just a terminal

Skip the installer's autostart step (`./install.sh --skip-claude
--skip-extension`, then stop the service it creates) — or simply don't run
`install.sh` at all and use `.venv/bin/python -m backend.main` in a spare
tab. Nothing here strictly needs systemd.

---

## Development

```bash
# Install dev deps (adds pytest + playwright for e2e smoke tests)
.venv/bin/pip install -r requirements-dev.txt

# Run the test suite
.venv/bin/pytest -q

# Run one file
.venv/bin/pytest tests/test_export.py -q

# Run a single test with output
.venv/bin/pytest tests/test_export.py::test_full_export -v -s
```

Schema lives in `backend/schema.sql` and every statement uses
`CREATE TABLE IF NOT EXISTS`, so adding a new table requires no migration —
restart the backend, the new table appears. See `AGENT_NOTES.md` ("How to
add a new event type") for the full checklist.

Anti-bot design: the extension attaches all listeners to `document` /
`window` in capture phase, never modifies the page, and never probes React
fibers / Redux / dev-tools hooks. Don't change this.

---

## Troubleshooting

**Capture isn't happening / no new rows in `impressions`.**
1. Confirm the backend is up: `curl -s http://127.0.0.1:8765/health`.
2. Confirm the extension reloaded after any code change — see [Step 2](#2-chrome-extension).
3. Check the service worker console: open `chrome://extensions/` →
   Twitter Memory → **service worker** link → Console tab. Failed POSTs
   are logged there.
4. Compare `max(captured_at)` against when you made the change: if
   `captured_at` is *older* than your edit, the extension is still on
   stale code.

**"Database not found" from the MCP tool.** The MCP server looks at
`TWITTER_MEMORY_DATA` (or `~/.twitter-memory`). If you pinned the backend
to a different path via env var, pin the MCP server the same way via the
`env` block in `claude_desktop_config.json`.

**Export is empty for "today" but data exists.** The day window is local,
not UTC. If your `TWITTER_MEMORY_TZ` is wrong the day boundary will slide.
Check with:

```bash
.venv/bin/python -c "from mcp_server import settings; print(settings.local_tz())"
```

**Legacy flat export files still present.** They will be removed the first
time you re-export that date. Manual cleanup:

```bash
rm ~/.twitter-memory/exports/*.md ~/.twitter-memory/exports/*.json
# (does NOT touch the per-day directories)
```

**Claude Desktop can't find the MCP tool.** Check absolute paths in
`claude_desktop_config.json`, restart Claude Desktop fully, and check
Claude's own log panel for spawn errors.

---

## Data retention and privacy

- Everything lives at `~/.twitter-memory/`. The backend binds only to
  `127.0.0.1`. The extension's host permissions are limited to
  `twitter.com`, `x.com`, and `127.0.0.1:8765`.
- `backend/retention.py` runs nightly at 3am local and prunes per-table.
  Defaults: behavioral tables (impressions, clicks, etc.) kept 60 days;
  user-content tables (selections, raw payloads) kept 30 days.
- `daily_briefing` is the only path where data can leave your machine. It
  is opt-in and requires explicit API-key configuration. Without it,
  nothing is transmitted off-box.
- Backups: `scripts/backup.py` dumps the DB to
  `~/.twitter-memory/backups/YYYY-MM-DD.sqlite.gz`. Not scheduled by
  default — run it under cron/launchd if you want nightly backups.

---

## File reference

| Path | What lives here |
|---|---|
| `backend/main.py` | FastAPI app: `/ingest`, `/export/day`, `/health`, `/stats`, `/debug/data-quality` |
| `backend/schema.sql` | SQLite schema, additive (`IF NOT EXISTS` only) |
| `backend/ingest.py` | `HANDLERS` dict + per-event handlers + `event_log` dedup |
| `backend/retention.py` | Nightly pruning |
| `backend/parser.py` | GraphQL payload walker (legacy path; current capture is DOM-only) |
| `extension/manifest.json` | MV3 config |
| `extension/content-script.js` | DOM observers + delegated listeners |
| `extension/service-worker.js` | Queue, batching, retries, session stamping |
| `extension/popup.html` + `popup.js` | Status UI for the extension |
| `mcp_server/server.py` | MCP stdio tool definitions |
| `mcp_server/export.py` | `write_export`, `build_digest/tweets/activity/timeline_markdown`, `build_json_from_bundle` |
| `mcp_server/queries.py` | Day-windowed SQL, `session_timeline` UNION, `day_window_utc` |
| `mcp_server/agent_queries.py` | SQL for the slice-tool family (search, top-dwelled, etc.) |
| `mcp_server/topics.py` | Keyword + hashtag rules — 8 buckets, multi-label |
| `mcp_server/scoring.py` | `importance()` formula |
| `mcp_server/anomalies.py` | back-to-back / doomscroll / late-night / topic-drift detectors |
| `mcp_server/briefing.py` | Opt-in LLM synthesis entry point |
| `mcp_server/settings.py` | Paths, timezone, `ALL_SECTIONS`, inline cap |
| `scripts/seed_day.py` | Seed a realistic test day into the DB |
| `scripts/backup.py` | gz-compressed DB dump |
| `scripts/systemd/twitter-memory.service.template` | Linux user-service template rendered by `install.sh` |
| `scripts/install_helpers/` | Python orchestrator modules used by `install.sh` / `uninstall.sh` |
| `install.sh` / `uninstall.sh` | One-click install / teardown (thin bash → Python orchestrator) |
| `tests/` | pytest suite (179 tests across export v3 + installer helpers) |
| `AGENT_NOTES.md` | Design rationale, world-boundary rules, anti-patterns |
| `plan.md` | Original v1 spec + retention policy |
| `PROGRESS-v3.md` | Tracker for the export restructure |
| `PROGRESS-install.md` | Tracker for the one-click install |
