# Export v3 — Directory-per-day restructure

Last updated: 2026-04-23
Plan: /home/cybernovas/.claude/plans/curried-floating-parasol.md

## Goal

Split the flat `YYYY-MM-DD.md` export into a directory-per-day with purpose-split files, share SCHEMA.md across days, drop the redundant `## Impressions` markdown section (raw data stays in `data.json`), auto-delete legacy flat files.

```
exports/
  SCHEMA.md
  2026-04-22/
    digest.md    # TL;DR + Summary + Topics + Authors + Threads
    tweets.md    # tweets_ranked + repeat_exposure
    activity.md  # sessions + searches + interactions + link_outs + selections + media
    timeline.md  # chronological event stream
    data.json    # complete structured companion
```

## Baseline (pre-change)

- pytest: 112 passed (from `progress-export-v2.md`)
- real export `2026-04-22.md`: 499 KB / 7678 lines (Impressions section = 50%)
- real export `2026-04-22.json`: missing timeline, impressions, threads, repeat_exposure

## Step tracker

| # | Step | Implemented | Tested | Notes |
|--:|:---|:---:|:---:|:---|
| 1 | Refactor `export.py`: `_fetch_day_bundle` + 4 builders + extended `build_json` | ✓ | ✓ | `build_json_from_bundle` is the new primary; `build_json(db_path,date)` kept as a thin wrapper for callers |
| 2 | Rewrite `write_export` — dir layout + SCHEMA.md + legacy cleanup | ✓ | ✓ | SCHEMA.md gated by `exists()` — no mtime churn on repeat exports |
| 3 | Update `mcp_server/server.py::export_day` docstring + response | ✓ | ✓ | Also updated `daily_summary_json` description to list the four new keys |
| 4 | Update `backend/main.py` handler response | ✓ | ✓ | Returns all five paths + byte_size_digest / byte_size_total_md |
| 5 | Update `tests/test_export.py` + `tests/test_export_v2.py` | ✓ | ✓ | Plus `test_truncation.py` + `test_mcp_tools.py` touch-ups |
| 6 | New tests: shared schema, json completeness, legacy-delete, impressions-not-in-md | ✓ | ✓ | All four added to `test_export.py` |
| 7 | pytest green | ✓ | ✓ | **153 passed** (up from 112 baseline — net +41 new tests incl. split-file reads) |
| 8 | Live verify on 2026-04-21 + 2026-04-22 real DB | ✓ | ✓ | See "Real-data verification" below |

## Real-data verification (2026-04-23)

- **Legacy files auto-deleted** — `exports/2026-04-21.md`, `2026-04-22.md`, `.json` removed after new-layout write succeeded.
- **Layout confirmed:**
  ```
  exports/SCHEMA.md              (1942 B, written once)
  exports/2026-04-21/            digest 4.3 KB · tweets 12 KB · activity 0.4 KB · timeline 15 KB · data.json 201 KB
  exports/2026-04-22/            digest 48 KB · tweets 180 KB · activity 25 KB · timeline 324 KB · data.json 3.6 MB
  ```
- **Size: 2026-04-22 — 734-line digest.md** (was buried in 7678-line monolith). Digest fits the 200KB inline cap by a wide margin.
- **data.json completeness** — 19 top-level keys; previously-missing keys all present: `timeline` (2219 rows), `impressions` (1738 rows), `threads` (21), `repeat_exposure` (223), `revisits` (488).
- **Schema shared** — grepped `## Schema` across all 8 per-day .md files, zero hits. Only in `exports/SCHEMA.md`.
- **Digest TL;DR reads clean**: anomalies, read-speed, topics, algorithmic pressure all surface.

## Log (append-only)

- 2026-04-23T10:45: Plan approved. Tracker initialized.
- 2026-04-23T11:00: Implementation + test updates complete. 153/153 pytest green.
- 2026-04-23T11:00: Live verification against real DB succeeded on 2026-04-21 and 2026-04-22. Legacy flat files auto-deleted.
