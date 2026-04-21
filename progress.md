# Implementation progress: 10x extraction via query replay

Plan: `~/.claude/plans/twitter-memory-enrichment.md`

Started: 2026-04-21. Status updated inline as each phase lands.

---

## Phase 1 — Request template capture

**Status:** done

- `graphql_templates` table added to `backend/schema.sql`
- `backend/ingest.py` got `_handle_graphql_template` + `_QUERY_ID_RE` + urllib import, registered in HANDLERS
- `extension/injected.js` captures `authorization` request header, emits TEMPLATE_TAG postMessage once per operation per page load
- `extension/content-script.js` relays template events as `{type: 'graphql_template', operation_name, url, auth}` to the SW

## Phase 2 — Enrichment queue + endpoints

**Status:** done

- `enrichment_queue` table added to schema (UNIQUE on target_type+target_id+reason)
- New `backend/enrichment.py` module with `populate_queue()` + `sweep_loop()` + `REPLAY_ALLOWLIST` + `REASON_TO_OP`
- Sweep runs every 5 min as its own asyncio task in app lifespan (separate from retention)
- Endpoints: `GET /enrichment/next?limit=N`, `POST /enrichment/complete`, `GET /enrichment/stats`, `GET /debug/data-quality`
- All loopback-only. `/next` joins queue ↔ templates via CASE-mapped reason→operation, filters to allowlist, skips rows attempted in last 10min or attempts>=5
- Decision: separate sweep loop instead of piggybacking on retention (retention is once/day, sweep needs 5min cadence)

## Phase 3 — Enrichment worker in the SW

**Status:** done

- `extension/enrichment.js` shipped: alarm every 1min, per-endpoint hourly caps (TweetDetail 20, TweetResultByRestId 40, UserByScreenName 10, UserByRestId 10, UserTweets 5), global min-interval 12s, random pre-fire jitter 0-8s, activity gate 2min, visibility gate (active x.com tab), 429 → 30min backoff, 401/403 → authBroken break-glass
- `manifest.json` gained `"cookies"` permission (needed to read `ct0` for CSRF header)
- `service-worker.js` imports `noteOrganicEvent` + `forceTick`; calls `noteOrganicEvent()` on every organic content-script message
- Popup → SW message kind `force_enrichment` wires to `forceTick()` for the "Force backfill now" button
- Replay sends the minimum x.com header set: authorization (captured bearer), x-csrf-token (ct0 cookie), x-twitter-auth-type OAuth2Session, x-twitter-client-language en, x-twitter-active-user yes

## Phase 4 — Parser walks full TweetDetail payloads

**Status:** done (no code change needed)

- Re-read `backend/parser.py::extract_from_payload` — it already walks the full payload tree via `_iter_entries` and extracts every `Tweet` / `TweetWithVisibilityResults` node
- `legacy.conversation_id_str`, `in_reply_to_status_id_str`, `quoted_status_id_str` are all already populated per tweet row
- The reason thread coverage was low was NOT parser limitations — it was that TweetDetail rarely gets captured (only 3 payloads ever). The enrichment worker (Phase 3) fixes that by replaying TweetDetail for tweets referenced in threads

## Phase 5 — Popup + data quality visibility

**Status:** done

- Popup `popup.html` gained an Enrichment section: on/off state, 3 data-quality counters (missing tweet text, queued to enrich, templates captured), "Toggle enrichment" button, "Force backfill now" button, inline status line
- Popup `popup.js` fetches `/debug/data-quality` alongside `/stats` and `/debug/config` on every 2s refresh
- "Force backfill now" sends `{kind:"force_enrichment"}` to the SW and surfaces the returned status (ok / rate_limited / auth_failed / user_idle / etc.)
- Backend `/debug/data-quality` endpoint added in Phase 2 already covers this

## Phase 6 — Tests + end-to-end verification

**Status:** done

- `tests/test_enrichment.py` shipped with 5 tests: template upsert (incl. null-bearer-no-clobber), queue population of stub tweets, exclusion of tweets-with-text and unseen stubs, allowlist-never-contains-mutations (structural), reason→op maps into allowlist
- Full suite: **24 passed** (19 from before + 5 new). No parser tests added — parser already correct
- End-to-end verified on the running systemd backend:
  - `/debug/data-quality` → 810 tweets_without_text, 1303 total
  - 30s after restart, sweep auto-populated: 695 stub_tweet + 3 thread_context entries
  - `/enrichment/next?limit=1` correctly returns `{"items": []}` because no templates captured yet (user hasn't browsed since restart)
  - Once user reloads extension + browses x.com thread/detail page, templates arrive → next call returns work → SW (if enrichment toggled on) starts replay cycle

---

## What user does next (5 steps)

1. `chrome://extensions` → reload Twitter Memory (picks up new SW, manifest, popup)
2. Click the extension toolbar icon and confirm popup shows new Enrichment section + data-quality counters
3. Open any tweet detail page on x.com for ~5s (populates at least TweetDetail template)
4. In popup, flip "Toggle enrichment" to on
5. Keep browsing. Watch the "Missing tweet text" counter shrink in real time, at the rate of ~1 replay per 12-25s when user is active

---

## Post-ship fix 1 — XHR path was missing template capture (2026-04-21, same day)

Smoke test after first ship surfaced: 466 organic graphql_payloads landed in 10 min, but only 0 templates from organic browsing (only synthetic probes). Twitter's bundle uses **XMLHttpRequest, not fetch**, for most GraphQL calls. The XHR wrapper in `injected.js` had `postToContentScript` (payload path) but no `postTemplate` (template path).

Fix: wrapped `xhr.setRequestHeader` to capture the authorization header when Twitter calls it, and fire `postTemplate` both then and on the `load` event (belt-and-braces for the case where auth isn't explicitly set and the XHR rides on cookies alone).

Files: `extension/injected.js`. After reload + one /home scroll, 14 templates captured.

## Post-ship fix 2 — reason→op needed fallback candidates (2026-04-21, same day)

After fix 1, 14 templates arrived but `/enrichment/next` still returned empty. Cause: `REASON_TO_OP` mapped `stub_tweet` → `TweetResultByRestId` hard, and users on /home only organically fire HomeTimeline + sidebar queries. TweetResultByRestId only gets captured when user clicks into a tweet.

Fix: `REASON_TO_OPS` is now a preference-ordered list per reason. `/enrichment/next` inspects `graphql_templates` to find which op is captured for each reason, picks the highest-preference match. Means: first time user clicks any tweet, `TweetResultByRestId` + `TweetDetail` land together → queue unblocks for every reason.

Files: `backend/enrichment.py`, `backend/main.py`, `tests/test_enrichment.py`. 24 tests still green.

## End-to-end verified

After both fixes, on the live systemd backend:
- Navigating to `https://x.com/ycombinator/status/<id>` captured `TweetDetail`, `TweetResultByRestId`, AND `UserByScreenName` templates in one shot — all with real bearer tokens
- `/enrichment/next?limit=2` now returns 2 work items, each with a full replayable `{operation_name, query_id, url_path, features_json, variables_json, bearer}` payload pointing at `TweetResultByRestId`
- SW worker will use these the moment user flips `enrichmentEnabled` on in the popup. Each replay fills one stub. At ~1 per 12-25s, 695 stubs fill in ~3 hours of active browsing.

## Hard caps (documented for future me)

- TweetDetail: 20/hr · TweetResultByRestId: 40/hr · UserByScreenName: 10/hr · UserByRestId: 10/hr · UserTweets: 5/hr
- Global: one replay per 12s minimum with 0-8s jitter
- Activity gate: organic event within last 2 min
- Visibility gate: at least one active x.com tab
- 429 → 30min backoff · 401/403 → full stop until user re-toggles

---

# Retrospective: what a future agent should read before touching this repo

This section exists so a future agent (or future me) can hit the ground running instead of re-discovering the landmines I already stepped on. Written 2026-04-21 after two days of work on this codebase.

## Architecture mental model (30 seconds)

```
Twitter bundle on x.com
        │ (fetch + XHR calls to /i/api/graphql/<qid>/<OpName>)
        ▼
extension/injected.js           MAIN world, document_start
   wraps window.fetch + XMLHttpRequest
   → postMessage(__tm_graphql__ | __tm_graphql_template__)
        │
        ▼
extension/content-script.js     ISOLATED world, document_start
   listens on window 'message'
   → chrome.runtime.sendMessage({kind:"event", event})
        │
        ▼
extension/service-worker.js     MV3 SW (sleeps after ~30s idle)
   queue persisted to chrome.storage.local
   flushes every ~30s via chrome.alarms (platform caps 1/20min → 30s)
   → POST http://127.0.0.1:8765/ingest
        │
        ▼
backend/main.py + ingest.py
   HANDLERS dict dispatches by event.type
   parser.py walks the full GraphQL payload tree (dedupes by rest_id)
        │
        ▼
SQLite at $TWITTER_MEMORY_DATA/db.sqlite
   FK-enforced, WAL mode, PRAGMA foreign_keys = ON
```

Active enrichment is a parallel loop: `backend/enrichment.py::sweep_loop` populates `enrichment_queue` every 5 min; `extension/enrichment.js` (imported by the SW) pulls work via `/enrichment/next`, replays the captured template against x.com with the user's cookies, ships the response back through `/ingest`. Opt-in, heavily rate-limited.

**Key boundaries, internalize these:**
- `extension/*` (browser) and `backend/*` + `mcp_server/*` (Python) share only the HTTP wire format and the SQLite schema. There is no RPC.
- Content-script (ISOLATED) and injected.js (MAIN) share a DOM but NOT a JS realm. They talk via `window.postMessage` tagged with `__tm_graphql__` / `__tm_graphql_template__`.
- Popup and SW share extension origin. They talk via `chrome.runtime.sendMessage` with a `kind` field.
- SW can fetch x.com (has host_permissions + cookies) AND can fetch 127.0.0.1 (has host_permissions). Page cannot fetch 127.0.0.1 (mixed content + PNA).

## Where I burned tokens that you should skip

1. **Tried `gstack browse` first for a Chrome MV3 extension project.** It's a headless Playwright Chromium — won't run MV3 content scripts, and Ubuntu 23.10+ kills it with sandbox errors. **Go straight to `mcp__claude-in-chrome__*`** tools which drive the user's real Chrome. Check tab context first (`tabs_context_mcp`).

2. **Assumed Twitter uses `fetch` for GraphQL.** It uses **`XMLHttpRequest`** for most calls. `fetch` also works but is minority traffic. Any instrumentation that only wraps `fetch` will catch payloads inconsistently. Wrap **both**. This cost me one extension reload cycle to diagnose.

3. **Assumed parser was missing thread traversal.** `backend/parser.py::extract_from_payload` already walks the entire payload tree via `_iter_entries` and catches every `Tweet` / `TweetWithVisibilityResults` / `User` node. The data hole was **never parser limitations** — it was that `TweetDetail` almost never got captured. Fix the capture side, not the parse side.

4. **Started with `rowcount > 0` on INSERT OR IGNORE in aiosqlite.** Doesn't reliably signal "inserted vs ignored". Switched to explicit `SELECT 1` pre-check. Same pattern works in other aiosqlite projects too.

5. **Wrote tests with `session_id: "s-dedup"` before creating matching sessions rows.** `PRAGMA foreign_keys = ON` is in `backend/db.py`. Use `session_id: None` for isolated tests unless you're explicitly testing session FKs.

6. **Explored `chrome://extensions` via MCP navigate.** Returns `chrome-error://chromewebdata`. You cannot drive the extensions page or toggle extensions from MCP. User must reload manually.

7. **Assumed `chrome-extension://<id>/popup.html` would load in a regular tab.** It doesn't, unless `popup.html` is in `web_accessible_resources`. It isn't, by design. MCP cannot open the popup.

8. **Tried to toggle `chrome.storage.sync` from page context.** No `chrome.*` API in MAIN world. You need either (a) the popup, (b) content-script, or (c) SW. Page context is a dead end for extension storage.

9. **Missed that MV3 content-scripts don't re-inject into open tabs on extension reload.** Symptom: user reloads extension, x.com tab still runs old code. Fix: `chrome.runtime.onInstalled` calls `chrome.scripting.executeScript` on matching open tabs. This is in `service-worker.js::injectIntoOpenTabs`. Don't remove it.

10. **Initial CORS `allow_origins=["https://x.com","https://twitter.com"]` vs a comment above it claiming "Permissive".** Intent-vs-code drift. The SW's origin is `chrome-extension://<id>`, not x.com. Always set `allow_origins=["*"]` for a 127.0.0.1-bound backend. Safe because the socket is loopback-only.

11. **Hardcoded `REASON_TO_OP = {...}` as single-operation mapping.** User browsing /home only captures `HomeTimeline` + sidebar queries. `TweetResultByRestId` only captures on tweet-detail click. A single-op mapping leaves the queue unreachable until a rarely-hit template arrives. Use `REASON_TO_OPS` (plural) with a **fallback list per reason**. First template captured for any candidate unblocks the queue.

12. **Missing icon files block the ENTIRE manifest from loading.** The old `extension/icons/README.md` claimed Chrome silently falls back to a default. **This is false.** Chrome hard-errors and refuses the manifest. Don't reference files in `icons`, `content_scripts[].js`, `background.service_worker`, etc. unless they exist on disk. `tests/test_manifest.py::test_manifest_file_references_all_exist` catches this.

## Critical unknowns that cost me time, now documented

- **Bearer token is NOT per-user.** It's a constant string Twitter ships in their JS bundle (`Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzU...`). Capturing it from any organic request is fine and works for all replays. `ct0` cookie is the per-user CSRF secret.
- **`ct0` is non-HttpOnly**, so `document.cookie` in MAIN world can read it. That's how the SW gets CSRF for replay. It's also why a page-context script can forge x.com API calls — this is by design, not our bug.
- **MV3 `chrome.alarms` minimum period is 30s** regardless of what you request. Don't design flows that need sub-30s granularity.
- **MV3 SW unloads after ~30s idle.** Module-level state (`currentSessionId`, `lastOrganicEventAt`, `perEndpointWindow`) resets on wake. Persist anything that MUST survive wakes to `chrome.storage.local`.
- **`window.__tm_content_loaded__` set in content-script (ISOLATED) is invisible to MAIN world.** Don't use it to probe from page context.

## Workflows that actually work fast

**Backend code change:**
```bash
systemctl --user restart twitter-memory && sleep 2 && curl -s http://127.0.0.1:8765/health
```

**Run tests:**
```bash
TWITTER_MEMORY_DATA=./data .venv/bin/pytest -q
```
Always set `TWITTER_MEMORY_DATA` or tests scribble into `~/.twitter-memory/`.

**Inspect db:**
```bash
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('./data/db.sqlite'); print(c.execute('SELECT COUNT(*) FROM tweets WHERE text IS NOT NULL').fetchone())"
```
Or `sqlite3 ./data/db.sqlite '.tables'` for interactive.

**Tail backend logs:**
```bash
journalctl --user -u twitter-memory -f
```

**Extension code change:**
1. Edit files in `extension/`
2. Tell user: reload at `chrome://extensions` (there is no automation for this)
3. Tell user: hard-refresh x.com tab (MV3 doesn't re-inject into existing tabs automatically — even with our onInstalled handler, fresh loads are cleanest for testing)

**Test a GraphQL replay without waiting on the SW worker:**
1. Pick a queued stub + captured template from db
2. From MCP `javascript_tool` in the x.com tab, `fetch()` the GraphQL URL with `authorization` + `x-csrf-token` (from `document.cookie`) — same shape the SW would
3. Confirm HTTP 200 + response shape
4. You cannot POST result back to `127.0.0.1` from page context (mixed content + PNA). Trust the unit tests for the ingest side.

## Where to ask the user instead of investigating

- **Scope of "10x effective"** — could mean enrichment, analysis, UX, UI. Ask with four labeled options.
- **Whether a thing should be opt-in** — active enrichment needs explicit consent (detection risk). Default OFF. Don't assume.
- **Which tier of QA** — quick / standard / exhaustive changes the scope by 10x.
- **Target URL** — if the project has backend + extension + MCP, "run /qa" is ambiguous. Pick the surface.

## Where NOT to ask the user (handle yourself)

- Which systemd unit mode to use (`--user` is correct for a personal dev tool).
- File structure / naming within a module.
- Whether to add a regression test after a fix (always yes, unless the fix is pure CSS).
- Whether to keep or delete ad/promoted content in ingested data (default: keep — it's the user's own attention log).

## Mental shortcuts

- If the backend serves but stats don't update → queue is sitting in SW persistence, backend was down when events were sent. Restart backend, wait 30s for next alarm.
- If extension seems inactive but other extensions work → check `chrome://extensions` for a red error badge. Almost always a manifest file reference to something that doesn't exist.
- If `window.fetch.toString()` contains `[native code]` on x.com → injected.js didn't run. Probably `world:"MAIN"` content_scripts not supported (Chrome < 111) or manifest blocked the whole extension.
- If templates stop refreshing → check `last_seen_at` on `graphql_templates`. If old, user hasn't browsed since last backend restart AND SW hasn't fired its flush.
- If `/enrichment/next` returns empty but queue is non-empty → no captured template covers the queued reason. User needs to click into a thread once to seed TweetDetail.

## File-by-file, one sentence each

- `backend/main.py` — FastAPI app, lifespan starts retention + enrichment sweep tasks, all `/debug/*` and `/enrichment/*` endpoints are loopback-only via `_require_loopback`.
- `backend/ingest.py` — Event dispatch via `HANDLERS` dict, `_claim_event` dedup, `BATCH_METRICS` ring buffer.
- `backend/parser.py` — Whole-tree walk via `_iter_entries`, don't add per-operation special-cases here.
- `backend/enrichment.py` — `populate_queue` is four SQL sweeps, `REASON_TO_OPS` is preference-ordered candidates, `REPLAY_ALLOWLIST` is the safety boundary (mutations never appear).
- `backend/retention.py` — Runs at 3am local daily. Do not piggyback fast-cadence sweeps on it.
- `backend/schema.sql` — All `CREATE TABLE IF NOT EXISTS`. Migrations are additive only, no data backfill in `init_db`.
- `extension/manifest.json` — MV3, `world:"MAIN"` needed for injected.js, `cookies` permission needed for ct0 replay, host_permissions must include 127.0.0.1 to bypass CORS coupling.
- `extension/injected.js` — MAIN world, wraps fetch AND XHR, emits `__tm_graphql__` (payload) and `__tm_graphql_template__` (template) postMessages with de-dup.
- `extension/content-script.js` — ISOLATED world, window-message listener + IntersectionObserver + click delegation + `__tm_dev_set_enrichment__` dev hook.
- `extension/service-worker.js` — Queue + alarm flush + retroactive injection on install/update + message routing. Imports `enrichment.js`.
- `extension/enrichment.js` — Worker loop. Every gate in one file. If a gate is wrong, fix it here, don't move it.
- `extension/popup.html` / `popup.js` — 2s polling loop, three backend fetches per refresh (`/stats`, `/debug/config`, `/debug/data-quality`).
- `mcp_server/server.py` — Single MCP tool `export_day`. Reuses `mcp_server/export.py::write_export`. Backend exposes `/export/day` that calls the same renderer.
- `scripts/install-autostart.sh` — Idempotent systemd `--user` installer. Re-run safely.
- `scripts/perf_ingest.py` — Pure-Python load generator. Useful sanity check for ingest throughput (target >1000 events/s).
- `scripts/perf_browser.py` — Playwright harness. Optional. Needs `requirements-dev.txt` and auth cookies.

## Open known-unknowns (for the next agent)

- **`/enrichment/next` concurrency.** Current impl marks `last_attempt_at` and bumps `attempts` inside a single transaction. If two SW alarms fire within ms of each other (shouldn't, but possible on resume from sleep), they could both grab the same row. Add a `SELECT ... FOR UPDATE`-equivalent (SQLite: `BEGIN IMMEDIATE`) if this ever shows up in practice.
- **Queue row ordering ties.** `ORDER BY priority DESC, queued_at` — many rows will have identical `queued_at` from one sweep. Fine for now; if fairness matters, add `RANDOM()` tiebreak.
- **Twitter GraphQL schema drift.** `features_json` is captured fresh per organic request, so it self-heals when Twitter rotates. But if Twitter adds a REQUIRED new feature flag, our replays will 400 until the user organically re-fetches. No automatic detection of this today.
- **Bearer token rotation.** Historically stable, but if Twitter ever rotates, `bearer` in `graphql_templates` refreshes on the next organic request. No stale-bearer retry logic in SW yet. Expected result: 401 → authBroken → user re-toggles after next organic call.
- **Empty `session_id` on 69.5% of legacy impressions.** Pre-fix data. Not worth backfilling.

## Anti-patterns to reject if asked

- "Let's hit Twitter's REST API directly" — non-GraphQL surfaces have different auth + different rate limits. Stay on GraphQL.
- "Let's use Puppeteer to scrape x.com at scale" — different fingerprint, will trip bot detection fast.
- "Add a button that marks all stubs as succeeded without replaying" — destroys the dedup story.
- "Fetch the full following list on a schedule" — was explicitly out of scope per the /plan decision on 2026-04-21.
- "Replay a TweetDelete mutation we captured" — REPLAY_ALLOWLIST exists exactly to stop this.

---

# Session 2 learnings (2026-04-21) — DB wipe + export verification

## Post-ship fix 3 — `_handle_impression_end` FK failure after DB wipe

**Symptom:** After wiping `db.sqlite` and restarting the backend, `/stats` showed `tweets_today: 0`, `last_event_at: null`, and `sessions_today: 0` — even though the SW was actively sending events and `graphql_payload` events were creating tweet rows. `impressions` table stayed empty despite `impression_end` events arriving.

**Root cause:** The SW keeps `currentSessionId` in module-level memory across backend restarts. When the DB is wiped, the session row for that `currentSessionId` is gone. `_handle_impression_end` tries to INSERT into `impressions` with a `session_id` FK reference that no longer exists. With `PRAGMA foreign_keys = ON` (set per-connection in `db.py`), the insert fails silently — it's caught by the `except Exception` block in `ingest_batch` and added to the `errors` list in the response body, but nothing logs it to stderr and the HTTP response is still 200 OK. The `event_id` is already claimed in `event_log`, so the SW never retries, and the impression is permanently lost.

**Why it's subtle:** Running `PRAGMA foreign_keys;` via the CLI `sqlite3` tool returns `0` (off) because PRAGMAs are per-connection. The Python aiosqlite connection has it ON. Don't use CLI checks to verify FK state of the live backend.

**Fix (shipped):** `_handle_impression_end` now does `INSERT OR IGNORE INTO sessions` before inserting the impression row. This auto-creates a minimal session stub if the session_id doesn't exist, making the handler resilient to DB wipes and any other scenario where `session_start` fires after `impression_end`.

**File:** `backend/ingest.py::_handle_impression_end`

**How to detect this in future:** If `impression_end` events appear in `event_log` but `impressions` table has 0 rows, and `sessions` table is also empty, this is the bug. Confirm by checking `event_log` for `session_start` absence alongside `impression_end` presence.

## What `scripts/perf_ingest.py` does to the DB — always wipe after running

`scripts/perf_ingest.py` injects synthetic events (fake searches, fake impressions) directly into the backend at high throughput. If you run it against the live DB to test ingest speed, it permanently pollutes the export with test queries like `rust async`, `claude code`, `llm eval`. **Always wipe the DB after any perf test run.** The test data is realistic-looking enough that it won't be obvious in the export — it just shows up as dozens of identical searches at the same timestamp.

## How to do a clean DB wipe

```bash
systemctl --user stop twitter-memory
rm data/db.sqlite data/db.sqlite-shm data/db.sqlite-wal
rm data/exports/*.md          # old exports are now stale
systemctl --user start twitter-memory
sleep 2 && curl -s http://127.0.0.1:8765/health   # should show last_event_at: null
```

After restart, the SW still has its old `currentSessionId` in memory. Don't worry — the fix in `_handle_impression_end` handles this transparently. New impressions will land correctly. The old impression events that were sent before the wipe (and claimed in the old event_log) are permanently lost — that's acceptable, they were test-session data anyway.

## How to verify the extension is actually running on x.com

Run this from the MCP `javascript_tool` on any x.com tab:

```js
({
  injected_loaded: !!window.__tm_injected_loaded__,
  fetch_wrapped: !window.fetch.toString().includes('[native code]'),
  xhr_factory_wrapped: !window.XMLHttpRequest.toString().includes('[native code]'),
  ct0_present: document.cookie.split(';').some(c => c.trim().startsWith('ct0=')),
})
```

All four should be `true`. If `injected_loaded` is false, injected.js didn't run — check `chrome://extensions` for a manifest error badge. If `xhr_factory_wrapped` is false but `injected_loaded` is true, the XHR wrap broke — injected.js uses a factory pattern (`window.XMLHttpRequest = PatchedXHR`), not `prototype.open` patching, so the correct check is `XMLHttpRequest.toString()` not `XMLHttpRequest.prototype.open.toString()`.

## How impressions flow end-to-end (concise)

1. IntersectionObserver in `content-script.js` (ISOLATED world) fires when a tweet enters/leaves viewport
2. It sends `{type: "impression_end", tweet_id, dwell_ms, session_id, feed_source}` via `chrome.runtime.sendMessage`
3. SW receives it, calls `ensureSession()` (creates `session_start` event if needed), attaches `session_id`, enqueues
4. SW alarm (every 30s) flushes batch to `POST /ingest`
5. Backend `_handle_impression_end` writes to `impressions` table
6. `/stats` `tweets_today` counts distinct tweet_ids from `impressions` joined to today's date

If any step in this chain breaks, `tweets_today` stays 0 even if `tweets_total` grows (because tweet rows come from `graphql_payload` events which bypass the impression path entirely).

## `tweets_today` vs `tweets_total` — they measure different things

- `tweets_total` = rows in the `tweets` table, populated by `graphql_payload` events (GraphQL interception). Will grow even if impressions are broken.
- `tweets_today` = distinct tweet_ids seen in `impressions` today. Requires the full impression pipeline to be working.
- A large `tweets_total` with `tweets_today: 0` means GraphQL capture works but impression tracking is broken. Debug the session/impression pipeline, not the parser.

## Export reflects impressions, not tweet inventory

`/export/day` renders from the `impressions` table joined to `tweets`. A tweet in `tweets` with full text but no impression row will NOT appear in the export. The export is "what you actually saw and when", not "what's in the database". This is by design — the export is an attention log.

## How to trigger a fresh export

```bash
curl -s -X POST "http://127.0.0.1:8765/export/day"
# Returns: {"file_path": "...", "tweet_count": N, ...}
```

The file is always written to `$TWITTER_MEMORY_DATA/exports/YYYY-MM-DD.md`. Re-running overwrites it. Safe to run multiple times.

## MCP `javascript_tool` check for XHR wrapping — the right way

The XHR wrap in `injected.js` replaces `window.XMLHttpRequest` with a factory function `PatchedXHR`. Checking `XMLHttpRequest.prototype.open.toString()` will always show `[native code]` because the prototype is unchanged. The correct check is `window.XMLHttpRequest.toString()` — if it returns a function body (not `[native code]`), the factory wrap is active.

## `PRAGMA foreign_keys` in aiosqlite — CLI vs runtime

`PRAGMA foreign_keys = ON` in `backend/db.py` is applied per-connection at open time. The CLI `sqlite3` tool opens a fresh connection with FK OFF by default. So:
- `sqlite3 data/db.sqlite "PRAGMA foreign_keys;"` → `0` (misleading, doesn't reflect backend behavior)
- The backend enforces FKs. Silent FK failures surface as entries in the `errors` list returned by `ingest_batch` — not in uvicorn logs, not in `journalctl`.
- To see actual ingest errors, POST a test batch and inspect the response body, or temporarily add logging to `ingest_batch`.
