# Agent Notes — Twitter Memory

**Audience:** future AI agents assigned to this repo. Read this BEFORE touching code. The point is that you don't repeat the mistakes documented below and you do repeat the patterns that worked.

**Also read:** `plan.md` (original v1 spec + retention policy + schema intent), `progress.md` (pre-v2 retrospective — MV3 quirks, XHR vs fetch, bearer/ct0 facts), `IMPLEMENTATION_TODO.md` (what was implemented in the v2 pass and what needs user-side verification). Don't re-explain what those already say.

---

## The #1 thing to know: JS world boundaries

Chrome extensions run content scripts in an **ISOLATED world**. Everything in `extension/content-script.js` is in this world. Everything the page itself runs, and everything in `extension/injected.js`, is in the **MAIN world**.

These two worlds **share the DOM** (DOM events cross worlds freely), but they have **separate JavaScript globals**, including separate `Window` prototype chains. That has one non-obvious consequence that will bite you every single time you forget it:

**Monkey-patching built-ins in one world does not affect the other world.** `history.pushState = fn` in isolated world only changes the isolated world's copy. Calls from main world still hit the original. This project's v2 pass shipped with exactly this bug (see ISSUE-002 below).

Two ways to cross the boundary:
- **DOM events** — dispatched in one world, listeners in either world fire. Click listeners on `document` receive clicks dispatched from the page. Use this for passive observation.
- **`window.postMessage`** — this is the cross-world channel for explicit signals. `injected.js` → `content-script.js` always uses this. Pick a unique `type` tag (`__tm_graphql__`, `__tm_nav__`, `__tm_mutation__` are the live ones).

**Debugging tip:** `window.__tm_content_loaded__` reads `false` from main world even when the content script IS loaded — the flag lives in isolated world. Don't chase ghosts. To probe presence from the page, send a `window.postMessage` and check if it's echoed back, or check `chrome.storage.local` state from a known entry point.

---

## Architecture in 60 seconds

```
x.com tab
├── MAIN world
│   └── injected.js  ────┐   patches window.fetch, window.XMLHttpRequest,
│                        │   history.pushState/replaceState.
│                        │   Observes GraphQL responses (queries + mutations).
│                        │   Signals isolated world via window.postMessage.
│                        ▼
├── ISOLATED world                                  (no direct function calls
│   └── content-script.js                            between worlds; only
│       ├── listens for __tm_* postMessages          DOM events + postMessage)
│       ├── IntersectionObserver for impressions
│       ├── document-level delegated click/copy/
│       │   selectionchange/scroll listeners
│       └── chrome.runtime.sendMessage({kind:"event",event}) to SW
│
└── background service worker (service-worker.js)
    ├── per-tab session stamping (global across tabs, not per-tab)
    ├── queue (in-memory + chrome.storage.local persistent backup)
    └── POST http://127.0.0.1:8765/ingest every ~3s or at 50 events

FastAPI backend (backend/main.py on 127.0.0.1:8765)
├── /ingest  → ingest.ingest_batch(events)
│    ├── event_log dedup by event_id
│    └── HANDLERS dict dispatches by event.type → per-table insert
├── /stats, /health, /debug/data-quality
└── retention_loop (nightly 3am, see retention.py)

SQLite at ~/.twitter-memory/db.sqlite
├── tweets, authors, engagement_snapshots (content)
├── impressions, my_interactions, sessions, searches (v1 behavior)
├── link_clicks, media_events, text_selections,
│   scroll_bursts, nav_events, relationship_changes (v2 behavior)
├── event_log (dedup ledger)
└── graphql_templates, enrichment_queue (active backfill)

MCP server (mcp_server/server.py)
└── ONE tool: export_day(date) → writes markdown to ~/.twitter-memory/exports/YYYY-MM-DD.md
    Everything new is surfaced via the markdown, not via more MCP tools.
```

---

## Mistakes made during the v2 build (each followed by the lesson)

### M1. History patch in the wrong world — ISSUE-002 (shipped, then fixed live during QA)

**What happened:** The initial v2 implementation put the `history.pushState` / `replaceState` monkey-patch inside `content-script.js` (isolated world). It mirrored the existing `search` detection code. pytest was 50/50 green. QA caught it: 4 synthetic `pushState` calls → zero `nav_events`. Back/forward (real `popstate`, which crosses worlds) → `nav_events` landed.

**Root cause:** X.com's SPA router is MAIN-world JS, so its `pushState` calls never hit the isolated-world patch. The existing `search` detection had the same latent bug — it mostly "worked" by accident (initial load, popstate, and the `window.load` handler all fire cross-world, which covers a lot of cases but not SPA route clicks).

**Fix shape (see commit `5d4ee0c`):** Move the patch into `injected.js` (MAIN world). Post a `__tm_nav__` message to the isolated world, which re-runs `checkSearchAndNav()`. Dead isolated-world patch removed.

**Lesson:** Any runtime mutation of built-ins (`window.*`, `history.*`, prototype methods) must live in the world where the **actual callers** are. Before patching, ask: "who calls this?" If the answer is "the page's own JS", it has to be MAIN-world code (injected.js). If the answer is "my extension's code", isolated world is fine. Python tests will never catch this — it's a browser-runtime concern.

### M2. Forgot `_ensure_session_stub` on new handlers

**What happened:** First pass at the six new ingest handlers just called `INSERT INTO …`. Tests failed with accepted=0 and confusing `aiosqlite` thread errors. Spent minutes reading the error before realizing the new tables have `session_id TEXT REFERENCES sessions(session_id)` and `PRAGMA foreign_keys = ON`, so any event with a `session_id` pointing at a non-yet-existing row fails the FK.

**Lesson:** The existing `_handle_impression_end` (in `backend/ingest.py`) already dealt with this exact case. It had an inline `INSERT OR IGNORE INTO sessions (…)` right before its main insert. **When adding new handlers that reference `sessions`, grep for that pattern first.** I factored it into `_ensure_session_stub()` — use it for every future handler that has a session_id FK. Same applies for `_ensure_tweet_stub()`.

### M3. Forgot `auxclick`

**What happened:** First iteration of `link_click` capture only listened for `click`. Middle-click in browsers emits `auxclick`, not `click`. So "open in new tab via middle button" — which is a very high-signal "I want to return to this link later" action — would have been invisible.

**Lesson:** Browser "click" handling is actually 3 events: `click` (left button), `auxclick` (middle + right buttons), `contextmenu` (right button). If you want full click coverage, listen for both `click` and `auxclick` at minimum.

### M4. Over-engineered the v1 plan (1 Hz scroll sampling)

**What happened:** First plan draft proposed 1 Hz `scroll_sample` rows. User corrected with "reevaluate for performance + anti-bot". Back-of-envelope: 30-min session ≈ 1,800 rows for scroll alone.

**Lesson:** On extensions injected into adversarial SPAs, default to **event-driven aggregation**, not time-sampled polling. A "burst" that closes on quiescence (or direction reversal) gives the same signal at 2% of the row count and much lower detection surface. The pattern: maintain in-memory state, emit one rich row per meaningful interval.

### M5. Auto-mode churn on the background Bash tool

**What happened:** Early in the QA phase I tried to run `pytest` inside `source .venv/bin/activate && pytest …`. The Bash tool silently put it in background mode and I burned several turns polling output files. Meanwhile stale pytest processes accumulated.

**Lesson:** For "wait for output" commands, prefer the direct binary path (`.venv/bin/pytest`) over `source .venv/bin/activate && pytest`. Sourcing the activate script appears to be a trigger for background-mode detection in this environment.

### M6. Misread cross-world message flow in the first plan

**What happened:** v1 plan said injected.js would "postMessage with a new tag" for mutations and content-script would forward. I wrote it correctly on the second pass but initially reached for a less-portable pattern.

**Lesson:** Whenever you need a MAIN → ISOLATED signal, reach for `window.postMessage` with a unique `type` tag. Don't try CustomEvents on `window` (world-crossing behavior is inconsistent across Chrome versions). Don't try DOM attributes on a shared element (fragile + detection risk). postMessage is the canonical pattern.

---

## Patterns that worked — repeat these

### P1. "One table per signal family" beats "one column on a fat table"

New tables: `link_clicks`, `media_events`, `text_selections`, `scroll_bursts`, `nav_events`, `relationship_changes`. Each has exactly the columns its signal needs. Could have stuffed `link_clicks.url` into `my_interactions.action`, but you'd lose `domain`, `link_kind`, `modifiers`, and the export section would be worse. Rule of thumb: if the new signal has ≥2 fields that don't map onto an existing table's columns, give it its own table.

### P2. `CREATE TABLE IF NOT EXISTS` = zero-migration additive schema

`backend/db.py::init_db` runs `schema.sql` on every backend boot. Because every statement uses `IF NOT EXISTS`, adding new tables requires no migration step — restart the backend, the new tables appear. Existing data is untouched. **Don't introduce destructive `ALTER TABLE` or migration machinery** unless you absolutely must change an existing column. Add a new table instead.

### P3. `event_id` UUID + `event_log` dedup makes retries free

Every event has `event_id = crypto.randomUUID()` stamped at emit time. Backend checks `event_log` before processing. SW retries (after backend restart, network blips, etc.) reuse the same `event_id` → second POST is silently skipped. This means you can be aggressive about retries in the SW without worrying about duplicate rows.

### P4. Success-gate observed mutations

For GraphQL mutations (follow/mute/block), only emit after confirming the response was successful: `response.ok && !body.errors` for fetch, `2xx && !body.errors` for XHR. Failed/rate-limited actions should produce zero rows — otherwise the DB lies about what the user actually did.

### P5. Render, don't store, the timeline

`session_timeline` is a UNION ALL over all source tables, ordered by `(session_id, ts)`. It's computed on every `export_day` call. There's no denormalized timeline table. Writes stay cheap, drift is impossible. If the UNION ever gets slow, the right fix is indexes on `(session_id, timestamp)` (which we already have), not a materialized view.

### P6. MCP tool surface stays minimal

There's exactly one MCP tool: `export_day`. New signals surface as **new sections inside the markdown**, not as new MCP tools. An LLM reading the daily markdown can answer almost every question about the user's behavior without drilling through structured queries. Only add an MCP tool if the markdown genuinely can't carry the data (we haven't hit that yet).

### P7. Delegated listeners over per-node listeners

All new listeners attach to `document` or `window`, capture phase, passive where applicable. Zero listeners on per-tweet nodes. This is both a performance win (O(1) listeners regardless of tweet count) and an anti-detection win (nothing for X's automation detection to notice on its own DOM).

### P8. Living TODO alongside implementation

`IMPLEMENTATION_TODO.md` in the project root was more useful than in-conversation task lists alone for a multi-phase build. It survives context compaction, the user can scan it, and it stays accurate because edits are cheap. For anything > ~3 phases, create one.

---

## Anti-patterns to avoid

- **Don't attach listeners to per-tweet nodes.** Memory bloat, detection surface, and the DOM re-mounts frequently anyway as you scroll. Delegate from `document`.
- **Don't `preventDefault` / `stopPropagation` in any listener.** The extension is pure observation. The moment it modifies the page's behavior, X can fingerprint it.
- **Don't probe `window.__TWTR__`, React fibers, Redux stores, `__REACT_DEVTOOLS_GLOBAL_HOOK__`.** These are known bot signals.
- **Don't add listeners to `<video>` / `<img>` tags inside tweets.** Use URL-based detection (`/status/{id}/photo|video/{n}`) instead. Lower cost, lower detection risk, and the lightbox route is the authoritative signal anyway.
- **Don't 1 Hz sample anything.** Event-driven bursts. Always.
- **Don't put world-crossing monkey-patches in isolated world.** See M1.
- **Don't capture compose drafts.** Privacy-sensitive; low ROI vs the other signals. Explicitly out of scope in the v2 plan.
- **Don't add new MCP tools if the markdown export can carry the signal.** More tools = more context burn in the agent consuming the data.
- **Don't `git add .` / `git add -A`** — always stage specific files. There's transient state in `~/.twitter-memory/` and `.gstack/` that should never go in a commit.

---

## X.com-specific facts (from `progress.md` + this session)

- Tweet articles: `article[data-testid="tweet"]`.
- Tweet ID extraction: first `a[href*="/status/"]` inside the article, regex `/\/status\/(\d+)/`.
- Bearer token is **NOT per-user** — it's shipped in X's JS bundle. One captured template works for anyone.
- `ct0` cookie is the per-user CSRF secret. Non-HttpOnly. Readable from MAIN world via `document.cookie`.
- X's SPA router calls `history.pushState` from **MAIN world**. See M1.
- Media lightbox routes: `/status/{id}/photo/{n}` and `/status/{id}/video/{n}`.
- External links use `t.co` + default `target="_blank"` (so same-tab navigation is rare, but the sessionStorage fallback still earns its keep).
- Feed source heuristic lives in `feedSourceFromPath()` in content-script — `for_you`/`search`/`bookmarks`/`notifications`/`thread`/`profile`/`other`. Refine if you add a new feed type.
- MV3 `chrome.alarms` minimum period is 30s. Don't design sub-30s flows.
- MV3 SW unloads after ~30s idle. Module-level state resets. Persist anything that must survive to `chrome.storage.local`.
- `raw_payloads` table stores every GraphQL response verbatim for 30 days — use this to re-parse historical data when X's response shape changes, without re-fetching.

---

## How to add a new event type — checklist

1. **Schema** — add `CREATE TABLE IF NOT EXISTS` block + indexes in `backend/schema.sql`.
2. **Retention** — add entry to `RETENTION_TABLES` in `backend/retention.py`. 60d for behavioral, 30d if text contains user content (treat like `raw_payloads`).
3. **Handler** — function in `backend/ingest.py` following the `_handle_interaction` pattern. Call `_ensure_tweet_stub` if event has a `tweet_id`. Call `_ensure_session_stub` if the new table has a `session_id` FK.
4. **Register** — add the handler to the `HANDLERS` dict at the bottom of `ingest.py`.
5. **Client emit** — decide the world:
   - **MAIN world** if observation requires intercepting something the page does (GraphQL, history, page events). Add to `injected.js` and postMessage a new `__tm_*__` tag. Then in `content-script.js`, add a branch to the existing `message` listener that forwards the event.
   - **ISOLATED world** (content-script) for DOM-layer observation (clicks, selections, scroll, IntersectionObserver). Emit via `send({ type: "your_event", … })`.
6. **Query** — day-windowed function in `mcp_server/queries.py`. If the event should appear in the session timeline, add it to the `session_timeline` UNION ALL with a `kind` tag and a JSON payload.
7. **Render** — new section function in `mcp_server/export.py`, and register the section name in `mcp_server/settings.ALL_SECTIONS`. Hook it into `build_markdown` in dependency order (if it needs other query results, preload them once).
8. **Test** — add a happy-path + missing-required-field test in `tests/test_ingest.py`. If it gets its own export section, extend `_seed_v2` in `tests/test_export.py` and assert the section renders.
9. **Run** — `pytest tests/` should stay green. If it isn't, the failure message will usually tell you which stub you forgot (tweet or session).
10. **Live-test on x.com** after the user reloads the extension. **Python tests don't see world boundaries; a live test on x.com is not optional if you touched `injected.js` or anything that patches browser built-ins.**

---

## Key files and why

| File | What lives here | When you should touch it |
|---|---|---|
| `extension/manifest.json` | MV3 config, host permissions, content_scripts declarations | Adding new host permissions, new scripts, or an options page |
| `extension/injected.js` | MAIN-world code: fetch/XHR wrappers + history patch | You need to observe something the **page's own JS** does |
| `extension/content-script.js` | ISOLATED-world code: DOM observers + click/copy/selection/scroll listeners | You need to observe user actions on the rendered page |
| `extension/service-worker.js` | Queue, batching, session stamping, persistent queue, backoff | **Rarely.** It's event-type agnostic. If you're editing this to add a new event type, you're probably doing it wrong |
| `extension/enrichment.js` | Active GraphQL replay worker (fills stub tweets, stale engagement, cold authors) | Adding a new replay-worthy operation or changing rate limits |
| `backend/schema.sql` | SQLite schema — additive, `IF NOT EXISTS` only | Every new event type |
| `backend/ingest.py` | `HANDLERS` dict + per-event-type handlers, upsert helpers, event_log dedup | Every new event type |
| `backend/parser.py` | GraphQL payload walker — extracts tweets/authors/engagement from arbitrary shapes | X's GraphQL response shape changed |
| `backend/retention.py` | Nightly cleanup | Adding a new table that needs retention |
| `backend/enrichment.py` | Sweep (backend) + replay candidate selection | Adding a new "reason" for enrichment |
| `mcp_server/queries.py` | Day-windowed SQL per section, plus `session_timeline` UNION ALL | Every new export section |
| `mcp_server/export.py` | Markdown renderers per section + `build_markdown` orchestrator | Every new export section |
| `mcp_server/settings.py` | `ALL_SECTIONS` list, paths, timezone | Adding/removing a section name |
| `mcp_server/server.py` | MCP tool definition (`export_day`) | Almost never — resist adding new tools |
| `tests/fixtures.py` | `make_user`, `make_tweet`, `home_timeline_payload` | Seeding new kinds of synthetic GraphQL shapes |
| `tests/conftest.py` | `tmp_data_dir` fixture (env-var isolation + `importlib.reload`) | Almost never |
| `plan.md` | Original v1 build spec | Historical reference; don't rewrite it, write new docs |
| `progress.md` | Pre-v2 retrospective — contains the load-bearing anti-patterns from the initial build | Historical reference |
| `IMPLEMENTATION_TODO.md` | Living tracker for the v2 pass (what shipped, what user still needs to verify live) | Update as status changes during a build |
| `.gstack/qa-reports/` | QA reports across sessions | QA output destination |

---

## Verification habits worth keeping

- After any content-script / injected.js change, **live-test on x.com** in a session where the user has reloaded the extension. Do not skip this step just because pytest is green.
- Before committing extension JS, run `node --check` on both files. Syntax errors there produce silent failures in the service worker console, not build errors.
- After schema changes, restart the backend and run `sqlite3 ~/.twitter-memory/db.sqlite '.tables'` to confirm the new tables appeared. If they didn't, you have a typo in `schema.sql`.
- When a handler test fails with `accepted=0`, read the `errors` array in the response — not just the count. Most commonly the error is FK failure (missing tweet or session stub).

---

## Things I would change if redoing this pass

1. Put the `history.pushState` patch in `injected.js` from turn one. The isolated-world patch was a clear category error.
2. Write a tiny Playwright smoke test for `extension/*.js` so world-boundary regressions are caught automatically. pytest-only coverage left too much to manual live-testing.
3. Extract a `make_handler(table_name, required_fields, extras)` helper in `ingest.py`. The six new handlers all follow the same shape (validate → stub tweet → stub session → insert). A table-driven version would be less code and less risk of missing a stub.
4. Add a `/debug/events` endpoint that echoes the last N events per type. This would have made the nav_events bug obvious in one curl without needing a SQLite query.
