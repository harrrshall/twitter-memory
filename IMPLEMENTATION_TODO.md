# Implementation TODO — Interaction Capture v2

Living tracker for the interaction-capture extension. Each item is checked off as it's implemented AND tested. See `/home/cybernovas/.claude/plans/cozy-doodling-avalanche.md` for the full plan.

Legend: `[ ]` not started · `[~]` in progress · `[x]` done & tested · `[🧑]` needs user to verify live in Chrome

## Phase 1 — Backend schema + ingest ✅
- [x] 1.1 Append 6 new `CREATE TABLE IF NOT EXISTS` blocks to `backend/schema.sql` (link_clicks, media_events, text_selections, scroll_bursts, nav_events, relationship_changes) + indexes
- [x] 1.2 Add 6 handler functions in `backend/ingest.py` (+ shared `_ensure_session_stub` helper to cover FK `session_id → sessions`)
- [x] 1.3 Register the 6 new handlers in the `HANDLERS` dict in `backend/ingest.py`
- [x] 1.4 Add retention DELETEs in `backend/retention.py` (60d for 5 tables; 30d for text_selections)
- [x] 1.5 Write 13 unit tests in `tests/test_ingest.py` — happy path, missing-field reject, truncation, FK stubbing, event_id dedup
- [x] 1.6 Full pytest: all green, 0 regressions
- [x] 1.7 Fresh-DB verification: all 6 new tables present in `.tables` listing

## Phase 2 — Extension content-script ✅
- [x] 2.1 Link-click detection extended into the existing click listener (early return before INTERACTION_MAP); classifies `external / internal_tweet / internal_profile / hashtag / mention`; captures modifiers (shift/ctrl/meta/alt/middle)
- [x] 2.2 `auxclick` listener added so middle-click "open in new tab" is recorded; `sessionStorage` fallback stashes same-tab navigation link events pre-send
- [x] 2.3 `copy` listener emits immediately (higher-signal); dedup via `lastEmittedSelection`
- [x] 2.4 `selectionchange` listener with 1000 ms debounce, 500 char cap, min 10 chars, anchored-in-tweet requirement
- [x] 2.5 Passive `scroll` listener on `window` with event-driven burst aggregation: close on 1500 ms quiescence or direction reversal > 400 px; 50 px displacement floor
- [x] 2.6 `checkSearch` → `checkSearchAndNav`: still emits `search`, adds `nav_change` (every path change) and `media_open` (regex on `/status/{id}/photo|video/{n}`)
- [x] 2.7 `__tm_mutation__` postMessage listener added alongside `__tm_graphql__`; forwards as `relationship_change`
- [x] 2.8 `flushAll` extended: closes open scroll burst, fires debounced selection, drains sessionStorage link events
- [x] 2.9 `node --check` clean

## Phase 3 — Extension injected.js ✅
- [x] 3.1 `MUTATION_OP_TO_ACTION` map defined (FollowUser/UnfollowUser/MuteUser/UnmuteUser/BlockUser/UnblockUser → action strings)
- [x] 3.2 Outbound fetch body captured locally per call (no cross-call state) for `variables.user_id` / `userId` extraction
- [x] 3.3 Fetch response handler: success-gates on `response.ok && !body.errors`, then postMessage `__tm_mutation__`
- [x] 3.4 XHR handler: wraps `send()` to capture body, gates on `status >= 200 && < 300 && !body.errors`
- [x] 3.5 Zero new MAIN-world globals beyond map + helper; no listeners on X DOM nodes; same detection surface as existing `graphql_payload` path

## Phase 4 — Export (MCP) ✅
- [x] 4.1 Day-windowed queries added to `mcp_server/queries.py`: `link_clicks_rows`, `media_events_rows`, `text_selections_rows`, `scroll_bursts_rows`, `nav_events_rows`, `relationship_changes_rows`
- [x] 4.2 `session_timeline(day)` UNION ALL query across all 8 source tables, producing `(session_id, ts, kind, payload_json)` ordered chronologically per session
- [x] 4.3 `revisits(day)` → `(session_id, tweet_id) → N` for N > 1
- [x] 4.4 `render_timeline` — chronological event stream per session, compact one-liner payloads (`_timeline_compact`)
- [x] 4.5 `render_link_outs` — grouped by domain with source tweet snippet
- [x] 4.6 `render_selections` — blockquoted text with tweet link
- [x] 4.7 `render_media` — grouped by tweet with media kind + index
- [x] 4.8 `render_impressions` extended: de-dupes `(session, tweet)` rows, shows `×N` (revisited) marker
- [x] 4.9 `render_sessions` extended: "Nav path: for_you → profile" chain + "Relationship changes: follow @alice"
- [x] 4.10 `ALL_SECTIONS` updated: added `link_outs`, `selections`, `media`, `timeline` (registered alongside existing sections)
- [x] 4.11 `tests/test_export.py` extended with `_seed_v2` fixture exercising all new event types; asserts all new sections render, revisit marker appears, nav path present, timeline ordering correct, kinds present
- [x] 4.12 Export tests: 7/7 green

## Phase 5 — Verification
- [x] 5.1 **Full pytest: 50/50 passing (0 regressions)**
- [x] 5.7 **Live-render spot check**: v2 fixture → markdown reads well; timeline lets an agent narrate "saw → clicked arxiv link → opened image → copied text → followed @alice" directly from the text.
- [🧑] 5.2 Load unpacked extension in Chrome, boot backend, open X.com
- [🧑] 5.3 Live smoke pass: click an external link from a tweet; open a tweet photo; select ≥10 chars and copy; scroll up then back down; switch For You ↔ Following; visit a profile; follow and unfollow a test account
- [🧑] 5.4 Run the verification SQL in plan §Verification — all 6 new tables should have rows > 0
- [🧑] 5.5 Performance guard — a ~200-tweet session should produce < 500 total new rows across the 6 tables. If `scroll_bursts` blows past 200, raise `SCROLL_QUIESCENT_MS` to 2000 in `content-script.js`.
- [🧑] 5.6 Anti-detection guard — DevTools Performance panel: no long tasks attributable to content-script during a 10-min session; scroll must feel identical to extension-off baseline.
- [🧑] 5.8 Agent narration: feed the generated markdown to Claude with "narrate what I was doing in session X". Answer should cite specific link-outs, selections, and nav — not just guess from tweet text.

## Open decisions / risks
- None blocking. `target_user_id` extraction in `injected.js` handles both `user_id` and `userId` variable-name variants (Twitter shapes vary across operations).
- The scroll-burst 50 px displacement floor in `closeBurst` suppresses tiny jitter bursts — if live testing shows too many no-ops suppressed OR too many short bursts emitted, adjust the floor first (cheaper than changing quiescence).

## Changelog
- 2026-04-22 · Plan approved, TODO created, implementation starting
- 2026-04-22 · Phases 1–4 implemented; 50/50 pytest green; live-smoke items (5.2–5.6, 5.8) deferred to user (require Chrome + real X.com session)
