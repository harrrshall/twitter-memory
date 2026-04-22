# Export v2 — Implementation progress

Last updated: 2026-04-22T12:40
Branch: master
Pytest baseline: 33 passed (before v2 work begins)
Pytest current: **112 passed · +82 v2 tests** (topics 21 + scoring 18 + anomalies 14 + export_v2 9 + parallel agent's test_export 7 + test_ingest delta 13)

| # | Feature | Implemented | Working | Tested | Commit | Notes |
|--:|:---|:---:|:---:|:---:|:---|:---|
| 3 | Topics module + rules (`mcp_server/topics.py`) | ✓ | ✓ | ✓ | `69ed0c6` | 21 tests; 8 buckets + untagged |
| 4 | Importance scoring (`mcp_server/scoring.py`) | ✓ | ✓ | ✓ | `424c8a4` | 18 tests; impression bonus refined to only fire above 1 impression |
| 5 | Anomaly detection (`mcp_server/anomalies.py`) | ✓ | ✓ | ✓ | `021e38c` | 14 tests; 4 rules — back-to-back / doomscroll / late-night / topic drift |
| 1 | Aggregated unique-tweets query (`queries.py::unique_tweets_with_engagement`) | ✓ | ✓ | ✓ | `ccb1437` | canonical input for TL;DR / ranked / repeat / topics |
| 2 | Timeline query | ✓ | ✓ | ✓ | `2060e51` | done by parallel agent (`session_timeline` UNION across 8 tables) |
|   | Author context query (`queries.py::author_context_rows`) | ✓ | ✓ | ✓ | `ccb1437` | follower_count + verified + impressions + dwell |
|   | Threads relaxed threshold (3→2) | ✓ | ✓ | ✓ | `ccb1437` | one-line default tweak |
| 6 | TL;DR renderer | ✓ | ✓ | ✓ | `0884b19` | 5 bullets incl. topics, read, pressure, scroll, anomalies |
| 7 | Sessions table renderer | ✓ | ✓ | ✓ | `2060e51` | parallel agent kept existing + added nav-path + relationship changes |
| 8 | Tweets (ranked) table renderer | ✓ | ✓ | ✓ | `0884b19` | compact table with ×N, dwell, engagement, topics, media flag |
| 9 | Repeat-exposure renderer | ✓ | ✓ | ✓ | `0884b19` | tweets with impressions_count ≥ 3 |
| 10 | Topics renderer | ✓ | ✓ | ✓ | `0884b19` | per-bucket rollup with notable handles |
| 11 | Threads renderer | ✓ | ✓ | ✓ | — | unchanged, threshold tweak already applied |
| 12 | Authors renderer (with follower context) | ✓ | ✓ | ✓ | `0884b19` | new `render_authors_v2` replaces render_top_authors |
| 13 | Your actions renderer | ✓ | ✓ | ✓ | — | covered by existing `render_interactions` + `render_searches` |
| 14 | Chronological timeline renderer | ✓ | ✓ | ✓ | `2060e51` | done by parallel agent |
| 15 | Schema block renderer | ✓ | ✓ | ✓ | `0884b19` | explains importance formula, tid prefix, dwell semantics, topic rules |
| 16 | HTML entity decode + stub filtering | ✓ | ✓ | ✓ | `0884b19` | `_clean_text` + `_stub_free` helpers applied across renderers incl. timeline |
| 17 | Update `settings.ALL_SECTIONS` + orchestrator | ✓ | ✓ | ✓ | `0884b19` | new 16-section order front-loads TL;DR / Tweets-ranked / Repeat / Topics |
| 18 | Snapshot test fixture | ✓ | ✓ | ✓ | `0884b19` | `tests/test_export_v2.py` — 9 regression tests using a shared `_seed_heavy_scroll` fixture |
| 19 | Regen CLI (`python -m mcp_server.regen --since YYYY-MM-DD`) | ☐ | ☐ | ☐ | — | OPEN — not needed for new exports; only for backfilling historical files |
| 20 | End-to-end real-DB diff + manual LLM smoke test | ✓ | ✓ | — | — | real /export/day against live backend produced a readable LLM-first digest; sample saved to `.gstack/v2-samples/2026-04-21-v2-sample.md`. Manual paste-to-Claude smoke test is a 🧑 step |

## Cell legend
- **Implemented**: code lives on the branch.
- **Working**: manually exercised the code path and it produced the expected shape.
- **Tested**: a committed test asserts the behavior and is green in the full suite.
- ☐ pending · ✓ done · ✗ failing (with note) · ⏸ blocked (with note).

## Sample output from real data

`.gstack/v2-samples/2026-04-21-v2-sample.md` (742 lines) — a real `/export/day` run against the live DB. TL;DR catches:
- 2 doomscroll sessions (25 + 58 impressions at 0.0s median dwell)
- 6 tweets under algorithmic pressure (@akseljoonas ×5, @sowmay_jain ×4, @SiddharthKG7 ×4, @Ozacle23 ×3, @rrhoover ×3, @Literariium ×3)
- 11 tweets I actually read (dwell ≥3s), top 5 called out by handle
- `ai-tooling` dominating with 12 tweets
- Multiple topic-drift windows (3-4 topics in 10-impression sliding windows)

These 15 lines of TL;DR summarize what used to require reading ~700 lines of raw impressions.

## Progress log (append-only)

- 2026-04-22T10:45: Tracker initialized. Baseline 33/33 green. Starting with F3 (topics).
- 2026-04-22T10:50: F3 landed (`69ed0c6`). 71/71 passing (+21 new tests). Moving to F4 (scoring).
- 2026-04-22T10:55: F4 landed. 89/89 passing (+18 new tests). Refined the impression_bonus formula mid-feature — the plan's `count/5` meant a tweet seen once already contributed 0.04; switched to `(count-1)/4` so a single impression is the baseline. Documented in scoring.py module docstring.
- 2026-04-22T11:00: F5 anomalies landed (`021e38c`). 103/103 passing (+14 new tests).
- 2026-04-22T11:10: Discovered a parallel /plan session (`cozy-doodling-avalanche`) had landed "Interaction Capture v2" on `fix/nav-pushstate-main-world`. Stopped and reported instead of destroying their work.
- 2026-04-22T12:00: Per user direction, consolidated both sessions onto master via fast-forward merge. Remote feature branch deleted. 103/103 green.
- 2026-04-22T12:10: F1 unique_tweets query + F12 author context query + F11 thread threshold all landed (`ccb1437`).
- 2026-04-22T12:30: Big renderer batch — F6/F8/F9/F10/F12/F15/F16/F17/F18 all landed (`0884b19`). 112/112 green (+9 new tests in test_export_v2.py). Three false-start failures on tests: wrong fixture param name (`follower_count` → `followers`), section-split regex matching `\`## Schema\`` in a prose line (fixed by splitting on `\n## ` not `## `), HTML entities leaking through the timeline payload (fixed by wiring `_clean_text` into `_timeline_compact`).
- 2026-04-22T12:40: End-to-end verification (F20). Hit live `/export/day?date=2026-04-21` against the real DB; produced a readable LLM-first digest with working TL;DR, ranked tweets, repeat-exposure, topics, anomalies. Sample saved to `.gstack/v2-samples/` for reference.

## Still open

- **F19 (regen CLI)**: not blocking for new exports — every next-day run uses v2 automatically. Only needed for backfilling pre-v2 history, and the existing `/export/day?date=YYYY-MM-DD` route already does that on demand. Deferring unless asked.
- **F20 manual LLM smoke test**: a 🧑 step — paste the sample into Claude and ask "what did I read about AI yesterday? Was I doomscrolling?". Answer should cite the TL;DR bullets directly.
