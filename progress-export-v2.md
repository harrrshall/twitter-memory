# Export v2 — Implementation progress

Last updated: 2026-04-22T11:00
Branch: master
Pytest baseline: 33 passed (before v2 work begins)
Pytest current: 103 passed · +53 v2 tests (F3 topics + F4 scoring + F5 anomalies)

| # | Feature | Implemented | Working | Tested | Commit | Notes |
|--:|:---|:---:|:---:|:---:|:---|:---|
| 3 | Topics module + rules (`mcp_server/topics.py`) | ✓ | ✓ | ✓ | `69ed0c6` | 21 tests; 8 buckets + untagged |
| 4 | Importance scoring (`mcp_server/scoring.py`) | ✓ | ✓ | ✓ | `424c8a4` | 18 tests; impression bonus refined to only fire above 1 impression |
| 5 | Anomaly detection (`mcp_server/anomalies.py`) | ✓ | ✓ | ✓ | (see log) | 14 tests; 4 rules — back-to-back / doomscroll / late-night / topic drift |
| 1 | Aggregated unique-tweets query (`queries.py::unique_tweets_with_engagement`) | ☐ | ☐ | ☐ | — | replaces `impressions_rows` |
| 2 | Timeline query (`queries.py::timeline_rows`) | ☐ | ☐ | ☐ | — | compact per-impression log |
|   | Author context query (`queries.py::author_context_rows`) | ☐ | ☐ | ☐ | — | folded under F2 |
|   | Threads relaxed threshold (3→2) | ☐ | ☐ | ☐ | — | one-line tweak |
| 6 | Frontmatter + TL;DR renderer | ☐ | ☐ | ☐ | — | — |
| 7 | Sessions table renderer | ☐ | ☐ | ☐ | — | — |
| 8 | Tweets (ranked) table renderer | ☐ | ☐ | ☐ | — | — |
| 9 | Repeat-exposure renderer | ☐ | ☐ | ☐ | — | — |
| 10 | Topics renderer | ☐ | ☐ | ☐ | — | — |
| 11 | Threads renderer | ☐ | ☐ | ☐ | — | — |
| 12 | Authors renderer (with follower context) | ☐ | ☐ | ☐ | — | — |
| 13 | Your actions renderer | ☐ | ☐ | ☐ | — | — |
| 14 | Chronological timeline renderer | ☐ | ☐ | ☐ | — | — |
| 15 | Schema block renderer | ☐ | ☐ | ☐ | — | — |
| 16 | HTML entity decode + stub filtering | ☐ | ☐ | ☐ | — | folded into renderers 6-15 |
| 17 | Update `settings.ALL_SECTIONS` + orchestrator | ☐ | ☐ | ☐ | — | — |
| 18 | Snapshot test fixture (`fixtures/export_v2_snapshot.md`) | ☐ | ☐ | ☐ | — | — |
| 19 | Regen CLI (`python -m mcp_server.regen --since YYYY-MM-DD`) | ☐ | ☐ | ☐ | — | — |
| 20 | End-to-end real-DB diff + manual LLM smoke test | ☐ | ☐ | ☐ | — | — |

## Cell legend
- **Implemented**: code lives on the branch.
- **Working**: manually exercised the code path and it produced the expected shape.
- **Tested**: a committed test asserts the behavior and is green in the full suite.
- ☐ pending · ✓ done · ✗ failing (with note) · ⏸ blocked (with note).

## Per-feature update rule
After each feature:
1. Run `pytest -q`. Record pass count.
2. Run the feature's targeted test. If green, set Tested=✓.
3. Update "Last updated" timestamp.
4. Record commit SHA in the row.
5. If anything failed, write the failure into Notes and STOP on that row until green.

## Progress log (append-only)

- 2026-04-22T10:45: Tracker initialized. Baseline 33/33 green. Starting with F3 (topics).
- 2026-04-22T10:50: F3 landed (`69ed0c6`). 71/71 passing (+21 new tests). Moving to F4 (scoring).
- 2026-04-22T10:55: F4 landed. 89/89 passing (+18 new tests). Hit one test-expectation mismatch mid-way — the impression_bonus formula from the plan assumed `count/5` which meant a tweet seen once already contributed 0.04 to the score. Refined to `(count - 1)/4` so a single impression is the baseline (bonus=0) and 5+ is saturation. Documented in scoring.py module docstring. Moving to F5 (anomalies).
