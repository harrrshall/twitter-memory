# Personal Twitter Memory — Build Specification (v2)

## 1. What we're building

A personal tool that silently captures everything you see and do on Twitter/X while you browse, stores it locally in a structured format, and exposes a single feature to Claude (via MCP): **given a date, return a complete markdown report of that day's Twitter activity.** Claude reads the markdown and answers any question from it.

**Example queries it should answer (all via the daily export):**
- "Summarize what I read on Twitter this morning."
- "How much time did I spend on Twitter last Tuesday?"
- "Who are the accounts I saw most often yesterday?"
- "What did I search for on Twitter yesterday?"
- "Which threads did I read on 2026-04-20?"
- "List every tweet I liked last Saturday."

**Constraints locked in:**
- Solo use, local-first (nothing leaves your machine)
- Desktop web only (twitter.com / x.com in Chrome)
- Realtime capture (visible within ~30 seconds)
- 60-day retention for impression/session/search/raw-payload data; tweets, authors, and interactions retained indefinitely
- Read-only export interface via MCP (no posting, no deleting tweets)
- **Capture via Chrome extension with in-page GraphQL interception** (no MITM proxy — explicitly ruled out)
- **No embeddings, no vector search, no semantic retrieval.** The daily markdown file is the retrieval interface.

## 2. System architecture

Three components, each in its own process:

**Component A — Chrome Extension (capture)**
Runs in the browser. Intercepts Twitter's GraphQL responses via `fetch`/XHR monkey-patching for clean structured data, observes the DOM for what you actually saw, tracks dwell time per tweet, captures clicks and searches, and batches everything to the local backend.

**Component B — Local Backend (ingest + storage)**
FastAPI service on `127.0.0.1:8765`. Receives batches from the extension, writes to SQLite, stores raw GraphQL payloads for resilience, runs the nightly retention cleanup.

**Component C — MCP Server (daily export)**
Exposes one read-only tool over stdio to Claude Desktop: `export_day`. Reads SQLite, renders a markdown report for the requested date, writes it to `~/.twitter-memory/exports/YYYY-MM-DD.md`, returns the file path and inline content.

**Data flow:**
```
Browser (Twitter)
  → Extension: injected.js (monkey-patches fetch/XHR)
  → Extension: content-script.js (MutationObserver + IntersectionObserver + click listeners)
  → Extension: service-worker.js (batches events)
  → POST http://127.0.0.1:8765/ingest
  → FastAPI writes to SQLite + raw_payloads

Claude Desktop
  → spawns MCP server via stdio
  → export_day(date) reads SQLite
  → writes ~/.twitter-memory/exports/YYYY-MM-DD.md
  → returns {file_path, content, truncated}
```

Two long-running processes on your machine: FastAPI backend, and (transient) MCP server spawned on-demand by Claude Desktop.

## 3. Component A — Chrome Extension

**Manifest V3.** Permissions: `storage`, `scripting`, `tabs`. Host permissions: `https://twitter.com/*`, `https://x.com/*`.

### Files

- **`manifest.json`** — MV3 config
- **`injected.js`** — runs in page context (not extension context) so it can monkey-patch `window.fetch` and `XMLHttpRequest`. Captures GraphQL responses and forwards to the content script via `window.postMessage`.
- **`content-script.js`** — injects `injected.js` into the page, sets up MutationObserver and IntersectionObserver, listens for clicks on like/retweet/reply/bookmark buttons, relays everything to the service worker.
- **`service-worker.js`** — background. Batches events from all tabs, tracks session boundaries, POSTs to local backend every 3 seconds or when batch hits 50 events. Persists queue to `chrome.storage.local` on failure.
- **`popup.html`** + **`popup.js`** — tiny UI: capture on/off toggle, events captured today, backend connection status.

### What gets captured

**1. GraphQL interception (primary data source).**
The injected script wraps `fetch` and `XMLHttpRequest`. When Twitter calls endpoints like `HomeTimeline`, `TweetDetail`, `UserTweets`, `SearchTimeline`, `UserByScreenName`, the wrapper reads the response and forwards the JSON to the content script. From this you get: tweet ID, full text, author object with all metadata, `created_at`, `conversation_id`, engagement counts (likes/retweets/replies/quotes/views/bookmarks), media entities, quoted/replied/retweeted references, language, tweet URL entities.

**2. DOM observation for "what you actually saw."**
MutationObserver watches for tweet elements entering the DOM (`article[data-testid="tweet"]`). IntersectionObserver fires when a tweet crosses 50% visibility → record `first_seen_at`. When it leaves viewport → compute `dwell_ms` and emit `impression_end`. Separate from GraphQL: a GraphQL response might return 20 tweets but you only scrolled past 8.

**3. Feed source detection.**
From URL + active tab: `/home` → `for_you` or `following` (check which tab is active), `/search` → `search`, `/<handle>` → `profile`, `/<handle>/status/<id>` → `thread`, `/i/bookmarks` → `bookmarks`, `/notifications` → `notifications`. Attached to each impression.

**4. Your interactions.**
Delegated click listeners on like, retweet, reply, bookmark, profile link, media expand. Capture tweet ID (from closest tweet `article`) + action + timestamp.

**5. Searches.**
Watch URL changes for `/search?q=...` and capture the query string.

**6. Session boundaries.**
Service worker tracks: session starts when a Twitter tab becomes active after 5+ minutes of no activity on any Twitter tab. Ends when no Twitter tab has been focused for 5 minutes. Session ID is a UUID generated at start.

### Batching and delivery

- Events queued in service worker memory
- Flush every 3 seconds OR when queue ≥ 50 events
- POST JSON array to `http://127.0.0.1:8765/ingest`
- On failure: keep in queue, retry with exponential backoff (max 5 min)
- If backend is down, queue persists in `chrome.storage.local` (up to 5000 events) so nothing is lost across browser restarts

### Privacy guardrails

- Capture toggle in popup (stored in `chrome.storage.sync`)
- Never capture DMs, never capture password fields, never capture form inputs
- Destination allowlist: extension will only POST to `127.0.0.1:8765` — hardcoded

### Defensive parsing (the resilience layer)

Since Twitter changes GraphQL shapes frequently:
- Every captured GraphQL response is sent to the backend with its operation name and a raw payload
- Backend stores the raw JSON in a `raw_payloads` table during the first 30 days of each payload's life
- Parser extracts fields with graceful fallbacks — missing field → log warning, continue
- When Twitter changes something, you can re-parse historical raw payloads without losing data

## 4. Component B — Local Backend

**Stack:** Python 3.11+, FastAPI, `aiosqlite`.

**Binds to `127.0.0.1:8765` only.** No auth (nothing external can reach it).

### Endpoints

- **`POST /ingest`** — receives event batch from extension. Validates, writes to SQLite, returns `{accepted: N, errors: [...]}`. Target latency <50ms — heavy work is queued, not done inline.
- **`GET /health`** — returns `{status: "ok", db: "ok", last_event_at: ...}`. Used by extension popup.
- **`GET /stats`** — counters for popup: tweets today, sessions today, total dwell today, last event timestamp.

### Event types

Events are typed and routed:
- `graphql_payload` — raw GraphQL response, extract tweets/authors/engagement/conversation_id
- `impression_start` — tweet entered viewport
- `impression_end` — tweet left viewport (includes dwell_ms)
- `interaction` — like/retweet/reply/bookmark/profile_click/expand
- `search` — search query entered
- `session_start` / `session_end` — session boundaries

### Ingest logic

1. Upsert authors from any tweet we see
2. Upsert tweets (same tweet seen again → update `last_updated_at`, keep original `captured_at`). Parser extracts `conversation_id_str` and stores it on the tweet row. Missing `conversation_id` → NULL (tolerate).
3. Insert new engagement snapshot if counts changed materially (skip if last snapshot <5 min old AND counts unchanged, to avoid bloat)
4. Insert impression rows (never dedupe — repeated views are the point)
5. Insert interactions and searches as append-only
6. Store raw GraphQL payload in `raw_payloads` with operation name and parse version

### Retention job

Runs daily at 3am local via a simple asyncio sleep-until-3am loop (no APScheduler dependency):
```sql
DELETE FROM impressions WHERE first_seen_at < now() - INTERVAL 60 DAY;
DELETE FROM engagement_snapshots WHERE captured_at < now() - INTERVAL 60 DAY;
DELETE FROM sessions WHERE ended_at < now() - INTERVAL 60 DAY;
DELETE FROM searches WHERE timestamp < now() - INTERVAL 60 DAY;
DELETE FROM raw_payloads WHERE captured_at < now() - INTERVAL 30 DAY;
-- Keep tweets, authors, my_interactions indefinitely
```
After delete: `PRAGMA wal_checkpoint(TRUNCATE)`. Monthly `VACUUM` on the first of each month.

## 5. Database schema

```sql
CREATE TABLE authors (
  user_id TEXT PRIMARY KEY,
  handle TEXT NOT NULL,
  display_name TEXT,
  bio TEXT,
  verified BOOLEAN,
  follower_count INTEGER,
  following_count INTEGER,
  first_seen_at TIMESTAMP,
  last_updated_at TIMESTAMP
);
CREATE INDEX idx_authors_handle ON authors(handle);

CREATE TABLE tweets (
  tweet_id TEXT PRIMARY KEY,
  author_id TEXT REFERENCES authors(user_id),
  text TEXT,
  created_at TIMESTAMP,
  captured_at TIMESTAMP,
  last_updated_at TIMESTAMP,
  lang TEXT,
  conversation_id TEXT,
  reply_to_tweet_id TEXT,
  reply_to_user_id TEXT,
  quoted_tweet_id TEXT,
  retweeted_tweet_id TEXT,
  media_json TEXT,
  is_my_tweet BOOLEAN DEFAULT 0
);
CREATE INDEX idx_tweets_author ON tweets(author_id);
CREATE INDEX idx_tweets_created ON tweets(created_at);
CREATE INDEX idx_tweets_reply ON tweets(reply_to_tweet_id);
CREATE INDEX idx_tweets_conversation ON tweets(conversation_id);

CREATE TABLE engagement_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT REFERENCES tweets(tweet_id),
  captured_at TIMESTAMP,
  likes INTEGER,
  retweets INTEGER,
  replies INTEGER,
  quotes INTEGER,
  views INTEGER,
  bookmarks INTEGER
);
CREATE INDEX idx_engagement_tweet ON engagement_snapshots(tweet_id, captured_at);

CREATE TABLE impressions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT REFERENCES tweets(tweet_id),
  session_id TEXT REFERENCES sessions(session_id),
  first_seen_at TIMESTAMP,
  dwell_ms INTEGER,
  feed_source TEXT
);
CREATE INDEX idx_impressions_time ON impressions(first_seen_at);
CREATE INDEX idx_impressions_tweet ON impressions(tweet_id);
CREATE INDEX idx_impressions_session ON impressions(session_id);

CREATE TABLE my_interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT REFERENCES tweets(tweet_id),
  action TEXT,
  timestamp TIMESTAMP
);
CREATE INDEX idx_interactions_time ON my_interactions(timestamp);
CREATE INDEX idx_interactions_tweet ON my_interactions(tweet_id);

CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  started_at TIMESTAMP,
  ended_at TIMESTAMP,
  total_dwell_ms INTEGER,
  tweet_count INTEGER,
  feeds_visited TEXT
);
CREATE INDEX idx_sessions_start ON sessions(started_at);

CREATE TABLE searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT,
  timestamp TIMESTAMP,
  session_id TEXT REFERENCES sessions(session_id)
);

CREATE TABLE raw_payloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_name TEXT,
  payload_json TEXT,
  captured_at TIMESTAMP,
  parser_version TEXT
);
CREATE INDEX idx_raw_op ON raw_payloads(operation_name, captured_at);
```

### SQLite PRAGMAs (set on connection open)

```
journal_mode = WAL
synchronous = NORMAL
wal_autocheckpoint = 1000
mmap_size = 268435456      -- 256MB
busy_timeout = 5000
temp_store = MEMORY
cache_size = -64000        -- 64MB
```

## 6. Component C — MCP Server (daily export)

**Stack:** Python, official `mcp` SDK, stdio transport.

**Registered in Claude Desktop** via `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "twitter-memory": {
      "command": "/path/to/venv/bin/python",
      "args": ["/path/to/project/mcp/server.py"]
    }
  }
}
```

### File layout

```
mcp/
  server.py        # registers the single tool, starts stdio server
  export.py        # builds markdown from SQLite rows
  queries.py       # parameterized SQL per section
  settings.py      # paths, timezone, content cap
```

### Tool: `export_day`

```python
export_day(
    date: str,               # "YYYY-MM-DD", interpreted in local timezone
    exclude: list[str] = []  # optional: section keys to omit
) -> dict
```

Returns:
```python
{
    "file_path": "/home/.../.twitter-memory/exports/YYYY-MM-DD.md",
    "sections_included": ["summary", "sessions", ...],
    "tweet_count": 312,
    "interaction_count": 11,
    "byte_size": 84231,
    "content": "...",      # full markdown if byte_size <= 200_000, else ""
    "truncated": false     # true if content omitted — caller should read file_path
}
```

### Sections (rendered in fixed order; all on by default)

| Section key | What it contains |
|---|---|
| `summary` | Date header, total session time, session count, tweets seen, interactions breakdown, searches count. |
| `sessions` | Per session: start, end, duration, feeds visited, tweet count. |
| `searches` | Queries in chronological order with timestamps. |
| `interactions` | Every like/retweet/reply/bookmark/profile-click/expand, in order, linked to tweet URL. |
| `top_authors` | Top 10 authors by impression count and top 10 by dwell time for the day. |
| `threads` | Groups of 3+ tweets from the same `conversation_id` seen that day, ordered by conversation then tweet time. |
| `impressions` | Every tweet seen that day. Grouped by session, then feed source. Includes dwell, author, full text, engagement counts, timestamp, direct link. |

**`exclude` semantics:** `exclude=["impressions"]` drops that section. Unknown keys raise a tool error. Excluding everything still emits the date header.

### Date window

`date` is a local calendar day. Query window: `[YYYY-MM-DD 00:00:00 local, YYYY-MM-DD+1 00:00:00 local)`. Convert to UTC for SQLite comparisons. Timezone sourced from `zoneinfo.ZoneInfo('localtime')`. The tool description documents the day boundary so Claude knows.

### Output file

- Directory: `~/.twitter-memory/exports/` (created with `0o755` if missing).
- Filename: `YYYY-MM-DD.md`.
- Always overwritten on regenerate (source of truth is SQLite).
- UTF-8, LF line endings.

### Inline content cap

- Rendered markdown ≤ 200KB → return full `content` in the tool response.
- Otherwise → `content: ""`, `truncated: true`. Caller reads the file via filesystem MCP or a separate Read.

### Markdown template

```markdown
# Twitter — {YYYY-MM-DD}

_Generated {ISO-8601 timestamp} · Local timezone: {tzname}_

## Summary

- **Total time on Twitter:** {Hh Mm}
- **Sessions:** {N}
- **Tweets seen:** {N} ({unique_authors} unique authors)
- **Interactions:** {likes} likes · {retweets} retweets · {replies} replies · {bookmarks} bookmarks
- **Searches:** {N}

## Sessions

### Session 1 — 09:14 to 09:31 (17m)
- Feeds: for_you, profile:@dhh
- Tweets seen: 142

...

## Searches

- 09:22 — `rust async runtime`
- 11:05 — `alpaca farming`

## Interactions

- 09:17 · **liked** @user/status/123 — "tweet preview..." · [link](https://x.com/user/status/123)
- 09:19 · **bookmarked** @user/status/456 — "..."

## Top authors

### By impressions
1. @pmarca — 34 tweets seen
2. @dhh — 22 tweets seen

### By dwell time
1. @swyx — 4m 12s

## Threads

### Thread 1 — 5 tweets from @author (conversation_id=...)

> **@author** 09:15 · [link](...)
> first tweet text

> **@author** 09:16 · [link](...)
> reply text

## Impressions

### Session 1 · for_you

**@elonmusk** · 09:15:22 · dwell 4.2s · [link](https://x.com/elonmusk/status/...)
tweet text here
_likes: 15,234 · retweets: 2,301 · replies: 412 · views: 1.2M_

---

**@dhh** · 09:15:47 · dwell 1.8s · [link](...)
...
```

**Rendering rules:**
- Use `@handle` form (not display names) for scannability.
- Put the tweet URL on every tweet block — it's the only way to "zoom in" since there's no `get_tweet` tool.
- Format dwell: `Ns` under 60s, `Nm Ns` otherwise.
- Engagement counts: thousands separators; views collapsed to `1.2M` form.
- Retweets render once under the retweeter's handle with the original tweet inline.
- Quoted tweets render inline under the quoting tweet, indented one level with `>`.

### SQL per section (in `mcp/queries.py`)

- **summary:** `COUNT(*)` over impressions, interactions grouped by action, searches, sessions with `SUM(total_dwell_ms)`.
- **sessions:** `SELECT * FROM sessions WHERE started_at >= ? AND started_at < ? ORDER BY started_at`.
- **searches:** `SELECT query, timestamp FROM searches WHERE ...`.
- **interactions:** `SELECT mi.*, t.text, a.handle FROM my_interactions mi JOIN tweets t JOIN authors a WHERE mi.timestamp BETWEEN ? AND ?`.
- **top_authors:**
  - by impressions: `JOIN impressions ... GROUP BY author_id ORDER BY COUNT(*) DESC LIMIT 10`.
  - by dwell: `... ORDER BY SUM(dwell_ms) DESC LIMIT 10`.
- **threads:** `conversation_id` groups with ≥3 distinct tweets seen that day, joined to `tweets` for text, ordered by `conversation_id, created_at`.
- **impressions:** `JOIN tweets + authors + latest engagement_snapshot per tweet`, grouped in Python by `session_id, feed_source`.

All queries share the `[day_start_utc, day_end_utc)` filter. Pre-compute timestamps once per invocation.

## 7. Build order

**Week 1 — Capture → storage loop working end-to-end**
1. Repo structure: `extension/`, `backend/`, `mcp/`, `scripts/`
2. SQLite schema + migrations script (no `tweet_embeddings`; `conversation_id` on tweets)
3. FastAPI `/ingest` handling `graphql_payload` only
4. Extension: manifest, injected.js with fetch/XHR monkey-patching, content-script.js to relay, service-worker.js batching and POSTing
5. Validation: open Twitter, scroll, verify tweets + authors + conversation_id in SQLite

**Week 2 — Full capture surface**
1. IntersectionObserver dwell tracking → `impression_start` / `impression_end`
2. Click listeners for like/retweet/reply/bookmark/profile_click/expand
3. Session detection in service worker (5-min idle boundary)
4. Search URL detection
5. `/ingest` handles all event types
6. Raw payload storage + defensive parser with version tag

**Week 3 — MCP server + daily export**
1. Scaffold MCP server with the `mcp` SDK (stdio)
2. Implement `mcp/queries.py` (SQL per section)
3. Implement `mcp/export.py` (markdown rendering, file writing, content truncation)
4. Implement `export_day` tool in `server.py`: arg validation, section assembly, file write, response packaging
5. Configure Claude Desktop via `claude_desktop_config.json`
6. Test: call `export_day` for a day you browsed — verify file appears, all sections render, links work, `exclude` drops sections correctly

**Week 4 — Polish**
1. Retention cleanup job + `wal_checkpoint(TRUNCATE)` + monthly VACUUM
2. Extension popup UI (capture toggle, events today, backend status)
3. Structured logging across all components (stdlib `logging`, JSON formatter)
4. SQLite backup script: `VACUUM INTO ~/.twitter-memory/backups/YYYY-MM-DD.db` daily
5. README with setup + architecture diagram

## 8. Things to watch out for

- **Twitter changes GraphQL frequently.** `raw_payloads` + versioned parser lets you re-parse history after a shape change.
- **Same tweet, many impressions.** Don't dedupe. Repeated views are the signal.
- **Retweets are wrappers.** A retweet of X by Y is a new tweet row with `retweeted_tweet_id = X`. Store both.
- **Dwell time lies when tab is backgrounded.** Combine IntersectionObserver with `document.visibilityState` — only count dwell when tab is visible.
- **MV3 service worker goes idle.** Use `chrome.alarms` for the batch flush timer — `setInterval` stops when the worker suspends.
- **`conversation_id` may be absent** in some GraphQL shapes. Tolerate NULL; the threads section just excludes those tweets. Never fail ingest on missing `conversation_id`.
- **Very active day → large markdown.** A multi-hour scrolling session can produce multi-MB markdown. The 200KB inline cap keeps the MCP response small; the file on disk is always complete.
- **Timezone.** Day boundary follows local machine timezone. Document this in the tool description. Out-of-scope: multi-timezone users.
- **ToS awareness.** Personal scraping has near-zero enforcement risk, but technically violates Twitter's ToS. Don't publish the extension without thinking about it.

## 9. Success criteria for v1

1. Browse Twitter normally with zero perceptible lag
2. `export_day` for any date with data produces a complete, valid markdown file in <2 seconds for a typical day
3. Claude, given only the markdown for a day, can accurately answer "what did I search for", "who did I see most", "summarize what I read this morning"
4. Verify via network inspection that nothing leaves `127.0.0.1`
5. Kill the backend, keep browsing for 20 minutes, restart it → queued events flush and nothing is lost
6. Exactly two long-running processes related to this tool: the FastAPI backend, and whatever Claude Desktop spawns on demand (the MCP server). No Ollama, no embedding worker.

---

Ready to start coding. The highest-risk piece is `injected.js` — the fetch/XHR monkey-patching and GraphQL parsing. If that works reliably, the rest is mostly plumbing and markdown rendering.
