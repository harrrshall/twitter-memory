"""MCP stdio server.

Exposes:
- ``export_day`` — full markdown report for one local day (original tool).
- Query tools (new, Sprint 1): slice/filter the DB without round-tripping a
  70K-token markdown file.
- ``daily_summary_json`` — structured JSON companion to export_day.

All query tools wrap their result in a ``_meta`` envelope:
    { "rows": [...], "row_count": N, "truncated": bool,
      "query_ms": float, "date_range": {"start": "...", "end": "..."} }

Row-count caps: default 50, max 500 per tool call. Rows beyond the cap are
truncated and ``truncated`` is True — agents should re-call with a tighter
filter or a narrower date range.
"""
from __future__ import annotations

import time
from datetime import date as date_cls
from typing import Any

from mcp.server.fastmcp import FastMCP

from mcp_server import agent_queries, briefing, export, queries, settings

mcp = FastMCP(name="twitter-memory")

# Hard ceiling — even if an agent asks for 5000, the tool refuses. Keeps the
# response small enough that an LLM can reason over it without truncation.
_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


def _check_db() -> None:
    if not settings.DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {settings.DB_PATH}. "
            "Is the backend running and has data been ingested?"
        )


def _clamp_limit(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


def _envelope(
    rows: list[dict],
    started: float,
    date_range: tuple[str, str] | None = None,
    limit: int | None = None,
) -> dict:
    query_ms = round((time.perf_counter() - started) * 1000, 2)
    truncated = limit is not None and len(rows) >= limit
    out: dict[str, Any] = {
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "query_ms": query_ms,
    }
    if date_range:
        out["date_range"] = {"start": date_range[0], "end": date_range[1]}
    return out


# --- existing tool, unchanged surface -----------------------------------------


@mcp.tool(
    name="export_day",
    description=(
        "Write one day's Twitter/X activity to a per-day directory under "
        f"{settings.EXPORTS_DIR}/YYYY-MM-DD/ and return the paths plus the inline "
        "digest markdown.\n\n"
        "Layout written on disk:\n"
        "- SCHEMA.md — shared interpretive guide (written once at the exports root).\n"
        "- YYYY-MM-DD/digest.md — TL;DR + summary + topics + authors + threads. "
        "Load this first when you need a fast human-readable scan.\n"
        "- YYYY-MM-DD/tweets.md — ranked tweets + repeat-exposure. Load this to "
        "answer 'what did the feed show me'.\n"
        "- YYYY-MM-DD/activity.md — sessions + searches + interactions + link-outs + "
        "selections + media. Load this to answer 'what did I do'.\n"
        "- YYYY-MM-DD/timeline.md — chronological per-session event stream. Deep dive.\n"
        "- YYYY-MM-DD/data.json — complete structured companion (tweets_ranked, "
        "topics, authors, sessions, interactions, searches, link_outs, selections, "
        "media, threads, repeat_exposure, impressions, revisits, timeline). Prefer "
        "this over regex-ing the markdown.\n\n"
        "For sliced queries (search, top-dwelled, one author, one session) prefer "
        "the query tools — they return 50 rows of JSON, not a full day's export.\n\n"
        "Parameters:\n"
        "- date (required, YYYY-MM-DD): local calendar day to export.\n"
        "- exclude (optional, list of section names): omit those sections from both "
        f"markdown and JSON. Allowed: {', '.join(settings.ALL_SECTIONS)}. Default: all included.\n\n"
        "Returns: { dir_path, digest_path, tweets_path, activity_path, timeline_path, "
        "json_path, schema_path, sections_included, tweet_count, interaction_count, "
        "session_count, search_count, byte_size_digest, byte_size_total_md, content, "
        "truncated }. 'content' inlines digest.md when it fits under the size cap; "
        "otherwise read digest_path from disk."
    ),
)
def export_day(date: str, exclude: list[str] | None = None) -> dict:
    try:
        target = date_cls.fromisoformat(date)
    except ValueError as e:
        raise ValueError(f"Invalid date '{date}'. Expected YYYY-MM-DD. ({e})")
    _check_db()
    return export.write_export(settings.DB_PATH, target, exclude or [])


# --- new tools ----------------------------------------------------------------


@mcp.tool(
    name="daily_summary_json",
    description=(
        "Return the structured JSON companion for one day's activity — the same data "
        "as the digest/tweets/activity/timeline markdown files but as parseable JSON. "
        "Agents should prefer this over regex-ing markdown.\n\n"
        "Sections: summary, anomalies, tweets_ranked, repeat_exposure, topics, authors, "
        "sessions, interactions, searches, link_outs, selections, media, threads, "
        "impressions, revisits, timeline.\n\n"
        "The JSON is also written to disk at exports/YYYY-MM-DD/data.json whenever "
        "export_day is called, so this tool just re-returns it."
    ),
)
def daily_summary_json(date: str) -> dict:
    try:
        target = date_cls.fromisoformat(date)
    except ValueError as e:
        raise ValueError(f"Invalid date '{date}'. Expected YYYY-MM-DD. ({e})")
    _check_db()
    return export.build_json(settings.DB_PATH, target)


@mcp.tool(
    name="search_tweets",
    description=(
        "Search tweets by substring of their text, within a date range.\n\n"
        "Parameters:\n"
        "- q (required): case-insensitive substring to match against tweet text.\n"
        "- start (optional YYYY-MM-DD): first local day to include. Defaults to 6 days ago.\n"
        "- end (optional YYYY-MM-DD): last local day to include. Defaults to today.\n"
        "- author (optional): handle without @ — restrict to one author.\n"
        "- min_dwell_ms (optional int): minimum total dwell_ms across all impressions.\n"
        "- engaged_only (optional bool): if true, only tweets the user liked/rt/reply/bookmarked.\n"
        "- limit (optional int, default 50, max 500).\n\n"
        "Sort: total dwell DESC, then impressions DESC. Rows are aggregated per unique "
        "tweet and include latest engagement snapshot + topic tags + a user_had_interaction flag."
    ),
)
def search_tweets(
    q: str,
    start: str | None = None,
    end: str | None = None,
    author: str | None = None,
    min_dwell_ms: int | None = None,
    engaged_only: bool = False,
    limit: int | None = None,
) -> dict:
    _check_db()
    if not q or not q.strip():
        raise ValueError("search_tweets: q must be a non-empty string")
    limit = _clamp_limit(limit)
    t0 = time.perf_counter()
    start_iso, end_iso, start_d, end_d = agent_queries.parse_range(
        start, end, default_days=7
    )
    db = queries.connect_ro(settings.DB_PATH)
    try:
        rows = agent_queries.search_tweets(
            db, q, start_iso, end_iso,
            author=author, min_dwell_ms=min_dwell_ms,
            engaged_only=engaged_only, limit=limit,
        )
    finally:
        db.close()
    agent_queries.tag_rows_with_topics(rows)
    return _envelope(rows, t0, (start_d, end_d), limit)


@mcp.tool(
    name="top_dwelled",
    description=(
        "Tweets with the most total dwell in a date range — the 'what did I actually "
        "read' question.\n\n"
        "Parameters:\n"
        "- start, end (optional YYYY-MM-DD, defaults to last 7 days).\n"
        "- author (optional handle without @).\n"
        "- limit (optional, default 50, max 500).\n\n"
        "Excludes tweets without text (stubs from impression events where the "
        "dom_tweet payload hadn't landed yet)."
    ),
)
def top_dwelled(
    start: str | None = None,
    end: str | None = None,
    author: str | None = None,
    limit: int | None = None,
) -> dict:
    _check_db()
    limit = _clamp_limit(limit)
    t0 = time.perf_counter()
    start_iso, end_iso, start_d, end_d = agent_queries.parse_range(
        start, end, default_days=7
    )
    db = queries.connect_ro(settings.DB_PATH)
    try:
        rows = agent_queries.top_dwelled(
            db, start_iso, end_iso, author=author, limit=limit
        )
    finally:
        db.close()
    agent_queries.tag_rows_with_topics(rows)
    return _envelope(rows, t0, (start_d, end_d), limit)


@mcp.tool(
    name="read_but_not_engaged",
    description=(
        "The 'silent-but-meaningful' corpus: tweets the user dwelled on above a "
        "threshold but didn't like, retweet, reply to, or bookmark.\n\n"
        "Parameters:\n"
        "- start, end (optional YYYY-MM-DD, defaults to last 7 days).\n"
        "- min_dwell_ms (optional int, default 3000 = 3 seconds).\n"
        "- limit (optional, default 50, max 500)."
    ),
)
def read_but_not_engaged(
    start: str | None = None,
    end: str | None = None,
    min_dwell_ms: int = 3000,
    limit: int | None = None,
) -> dict:
    _check_db()
    limit = _clamp_limit(limit)
    t0 = time.perf_counter()
    start_iso, end_iso, start_d, end_d = agent_queries.parse_range(
        start, end, default_days=7
    )
    db = queries.connect_ro(settings.DB_PATH)
    try:
        rows = agent_queries.read_but_not_engaged(
            db, start_iso, end_iso, min_dwell_ms=min_dwell_ms, limit=limit
        )
    finally:
        db.close()
    agent_queries.tag_rows_with_topics(rows)
    return _envelope(rows, t0, (start_d, end_d), limit)


@mcp.tool(
    name="algorithmic_pressure",
    description=(
        "Tweets the feed shoved at the user ≥N times in a date range — a proxy for "
        "'what is Twitter pushing hardest at me'.\n\n"
        "Parameters:\n"
        "- start, end (optional YYYY-MM-DD, defaults to last 7 days).\n"
        "- min_impressions (optional int, default 3).\n"
        "- limit (optional, default 50, max 500)."
    ),
)
def algorithmic_pressure(
    start: str | None = None,
    end: str | None = None,
    min_impressions: int = 3,
    limit: int | None = None,
) -> dict:
    _check_db()
    limit = _clamp_limit(limit)
    t0 = time.perf_counter()
    start_iso, end_iso, start_d, end_d = agent_queries.parse_range(
        start, end, default_days=7
    )
    db = queries.connect_ro(settings.DB_PATH)
    try:
        rows = agent_queries.algorithmic_pressure(
            db, start_iso, end_iso,
            min_impressions=min_impressions, limit=limit,
        )
    finally:
        db.close()
    agent_queries.tag_rows_with_topics(rows)
    return _envelope(rows, t0, (start_d, end_d), limit)


@mcp.tool(
    name="author_report",
    description=(
        "Full engagement portrait with one author in a date range.\n\n"
        "Parameters:\n"
        "- handle (required): @handle or plain handle.\n"
        "- start, end (optional YYYY-MM-DD, defaults to last 30 days — wider than "
        "other tools since an individual author tends to have thinner daily signal).\n"
        "- tweet_limit (optional, default 50, max 500): cap on the tweets list.\n\n"
        "Returns: { author, stats, tweets, recent_selections, recent_link_clicks } "
        "plus _meta. `author` is null when the handle has no matching row (you've "
        "never seen this user)."
    ),
)
def author_report(
    handle: str,
    start: str | None = None,
    end: str | None = None,
    tweet_limit: int | None = None,
) -> dict:
    _check_db()
    if not handle or not handle.strip():
        raise ValueError("author_report: handle must be a non-empty string")
    tweet_limit = _clamp_limit(tweet_limit)
    t0 = time.perf_counter()
    start_iso, end_iso, start_d, end_d = agent_queries.parse_range(
        start, end, default_days=30
    )
    db = queries.connect_ro(settings.DB_PATH)
    try:
        data = agent_queries.author_report(
            db, handle, start_iso, end_iso, tweet_limit=tweet_limit
        )
    finally:
        db.close()
    agent_queries.tag_rows_with_topics(data.get("tweets") or [])
    data["_meta"] = {
        "query_ms": round((time.perf_counter() - t0) * 1000, 2),
        "date_range": {"start": start_d, "end": end_d},
        "tweet_limit": tweet_limit,
    }
    return data


@mcp.tool(
    name="session_detail",
    description=(
        "Everything observed in one session: impressions, interactions, scroll "
        "bursts, nav events, searches, selections, link clicks, media opens.\n\n"
        "Parameters:\n"
        "- session_id (required): UUID of the session (list via recent_sessions).\n\n"
        "Returns: { session, impressions, interactions, scroll_bursts, nav_events, "
        "searches, selections, link_clicks, media_events }. `session` is null when "
        "the ID is unknown."
    ),
)
def session_detail(session_id: str) -> dict:
    _check_db()
    if not session_id or not session_id.strip():
        raise ValueError("session_detail: session_id is required")
    t0 = time.perf_counter()
    db = queries.connect_ro(settings.DB_PATH)
    try:
        data = agent_queries.session_detail(db, session_id)
    finally:
        db.close()
    data["_meta"] = {
        "query_ms": round((time.perf_counter() - t0) * 1000, 2),
    }
    return data


@mcp.tool(
    name="recent_sessions",
    description=(
        "List the last N sessions by start time, with impression-derived counts.\n\n"
        "Parameters:\n"
        "- limit (optional, default 10, max 100)."
    ),
)
def recent_sessions(limit: int | None = None) -> dict:
    _check_db()
    if limit is None or limit <= 0:
        limit = 10
    else:
        limit = min(limit, 100)
    t0 = time.perf_counter()
    db = queries.connect_ro(settings.DB_PATH)
    try:
        rows = agent_queries.recent_sessions(db, limit=limit)
    finally:
        db.close()
    return _envelope(rows, t0, None, limit)


@mcp.tool(
    name="hesitation_report",
    description=(
        "Tweets where the cursor lingered over a like/retweet/reply/bookmark button "
        "without actually clicking — the 'almost-engaged' signal. Requires capture "
        "data from Sprint 2 (button hover intent).\n\n"
        "Returns an empty result until Sprint 2 is deployed. Safe to call anyway; "
        "agents should check row_count > 0 before relying on it.\n\n"
        "Parameters:\n"
        "- start, end (optional YYYY-MM-DD, defaults to last 7 days).\n"
        "- min_dwell_ms (optional int, default 200 — filters accidental mouse "
        "pass-through).\n"
        "- limit (optional, default 50, max 500)."
    ),
)
def hesitation_report(
    start: str | None = None,
    end: str | None = None,
    min_dwell_ms: int = 200,
    limit: int | None = None,
) -> dict:
    _check_db()
    limit = _clamp_limit(limit)
    t0 = time.perf_counter()
    start_iso, end_iso, start_d, end_d = agent_queries.parse_range(
        start, end, default_days=7
    )
    db = queries.connect_ro(settings.DB_PATH)
    try:
        rows = agent_queries.hesitation_report(
            db, start_iso, end_iso, min_dwell_ms=min_dwell_ms, limit=limit
        )
    finally:
        db.close()
    return _envelope(rows, t0, (start_d, end_d), limit)


@mcp.tool(
    name="daily_briefing",
    description=(
        "OPT-IN LLM synthesis of one day's activity. Reads the structured "
        "daily JSON (same data as daily_summary_json) and returns a compact "
        "briefing: headline, hesitations (what you almost engaged with), "
        "suggested replies, follow-ups, and topic gaps.\n\n"
        "Requires model + API key configured in "
        f"{settings.DATA_DIR}/config.toml under [briefing], or via env vars "
        "TWITTER_MEMORY_BRIEFING_MODEL / TWITTER_MEMORY_BRIEFING_API_KEY.\n\n"
        "When unconfigured, returns { error: ... } rather than raising so "
        "agents can gracefully fall back to daily_summary_json + their own synthesis.\n\n"
        "Parameters:\n"
        "- date (required, YYYY-MM-DD): local calendar day to summarize."
    ),
)
def daily_briefing(date: str) -> dict:
    try:
        target = date_cls.fromisoformat(date)
    except ValueError as e:
        raise ValueError(f"Invalid date '{date}'. Expected YYYY-MM-DD. ({e})")
    _check_db()
    day_json = export.build_json(settings.DB_PATH, target)
    return briefing.generate(day_json)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
