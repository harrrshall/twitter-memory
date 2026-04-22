"""Agent-facing slice/filter queries.

Distinct from mcp_server/queries.py, which is the day-export SQL layer
(every query parameterizes on one day's [start, end) window). These queries
support flexible date ranges, keyword filters, author filters, and
row-count caps suitable for direct agent consumption.

All queries are read-only. All functions accept a bounded date range in
UTC ISO format — callers should use ``parse_range`` to convert user-supplied
YYYY-MM-DD strings.

Return shape is always a list of dicts. Tool wrappers in server.py add the
``_meta`` envelope (row_count, truncated, query_ms, date_range).
"""
from __future__ import annotations

import sqlite3
from datetime import date as date_cls, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from mcp_server import queries, settings, topics


# --- helpers -----------------------------------------------------------------


def parse_range(
    start: str | None,
    end: str | None,
    default_days: int = 7,
    tz: ZoneInfo | None = None,
) -> tuple[str, str, str, str]:
    """Parse YYYY-MM-DD strings to a UTC ISO range [start_iso, end_iso).

    Returns (start_iso, end_iso, start_display, end_display) where display
    values echo back the local calendar day the caller asked for so the
    `_meta.date_range` in the response stays human-readable.

    If both ``start`` and ``end`` are None, defaults to the last
    ``default_days`` local days ending today.
    """
    tz = tz or settings.local_tz()
    today = datetime.now(tz).date()
    if start:
        start_d = date_cls.fromisoformat(start)
    else:
        start_d = today - timedelta(days=default_days - 1)
    if end:
        end_d = date_cls.fromisoformat(end)
    else:
        end_d = today
    if end_d < start_d:
        raise ValueError(f"end {end_d} is before start {start_d}")
    # day_window_utc returns (start_of_day, start_of_next_day). For a range we
    # want start-of-start-day → start-of-(end+1)-day.
    start_iso, _ = queries.day_window_utc(start_d, tz)
    _, end_iso = queries.day_window_utc(end_d, tz)
    return start_iso, end_iso, start_d.isoformat(), end_d.isoformat()


def tag_rows_with_topics(rows: list[dict]) -> list[dict]:
    """Attach a ``topics`` list to each row in-place (dict mutation).

    Safe to call on agent-query results so downstream filters can be done
    in Python. Rows without ``text`` get ``["untagged"]``.
    """
    for r in rows:
        r["topics"] = topics.tag_tweet(r.get("text"), r.get("handle"))
    return rows


# Latest-engagement subquery shared across several queries. Keep as a string
# so we don't repeat it; embedded with explicit aliasing inline.
_LATEST_ENG = (
    "(SELECT {col} FROM engagement_snapshots e "
    "WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS {col}"
)


def _engagement_subselects() -> str:
    return ", ".join(
        _LATEST_ENG.format(col=c) for c in ("likes", "retweets", "replies", "views")
    )


# --- queries -----------------------------------------------------------------


def search_tweets(
    db: sqlite3.Connection,
    q: str,
    start_iso: str,
    end_iso: str,
    author: str | None = None,
    min_dwell_ms: int | None = None,
    engaged_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Tweets whose text contains ``q`` (case-insensitive), aggregated per
    unique tweet within the range. Sorted by total dwell DESC, impressions DESC.
    """
    eng = _engagement_subselects()
    sql = f"""
        SELECT
            t.tweet_id, t.text, t.created_at, t.conversation_id, t.media_json,
            t.quoted_tweet_id, t.retweeted_tweet_id, t.reply_to_tweet_id,
            a.handle, a.display_name, a.verified, a.follower_count,
            COUNT(i.id) AS impressions_count,
            COALESCE(SUM(i.dwell_ms), 0) AS total_dwell_ms,
            MIN(i.first_seen_at) AS first_seen_at,
            {eng},
            EXISTS (
                SELECT 1 FROM my_interactions mi
                WHERE mi.tweet_id = t.tweet_id
                  AND mi.timestamp >= ? AND mi.timestamp < ?
            ) AS user_had_interaction
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
          AND LOWER(COALESCE(t.text, '')) LIKE LOWER(?)
    """
    params: list[Any] = [start_iso, end_iso, start_iso, end_iso, f"%{q}%"]
    if author:
        sql += " AND a.handle = ?"
        params.append(author.lstrip("@"))
    sql += " GROUP BY t.tweet_id"
    having: list[str] = []
    if min_dwell_ms is not None:
        having.append("total_dwell_ms >= ?")
        params.append(min_dwell_ms)
    if engaged_only:
        having.append("user_had_interaction = 1")
    if having:
        sql += " HAVING " + " AND ".join(having)
    sql += " ORDER BY total_dwell_ms DESC, impressions_count DESC LIMIT ?"
    params.append(limit)
    cur = db.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def top_dwelled(
    db: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
    author: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Tweets with the most total dwell in the range. Stubs without text
    (no dom_tweet captured yet) are excluded so the list surfaces real reads."""
    eng = _engagement_subselects()
    sql = f"""
        SELECT
            t.tweet_id, t.text, t.created_at, t.media_json, t.quoted_tweet_id,
            a.handle, a.display_name, a.verified, a.follower_count,
            COUNT(i.id) AS impressions_count,
            COALESCE(SUM(i.dwell_ms), 0) AS total_dwell_ms,
            MIN(i.first_seen_at) AS first_seen_at,
            {eng},
            EXISTS (
                SELECT 1 FROM my_interactions mi
                WHERE mi.tweet_id = t.tweet_id
                  AND mi.timestamp >= ? AND mi.timestamp < ?
            ) AS user_had_interaction
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
          AND t.text IS NOT NULL AND t.text != ''
    """
    params: list[Any] = [start_iso, end_iso, start_iso, end_iso]
    if author:
        sql += " AND a.handle = ?"
        params.append(author.lstrip("@"))
    sql += (
        " GROUP BY t.tweet_id"
        " ORDER BY total_dwell_ms DESC, impressions_count DESC"
        " LIMIT ?"
    )
    params.append(limit)
    cur = db.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def read_but_not_engaged(
    db: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
    min_dwell_ms: int = 3000,
    limit: int = 50,
) -> list[dict]:
    """Tweets the user dwelled on (>= min_dwell_ms total) but didn't like,
    retweet, reply, or bookmark. The "silent but meaningful" corpus."""
    eng = _engagement_subselects()
    cur = db.execute(
        f"""
        SELECT
            t.tweet_id, t.text, t.created_at, t.media_json,
            a.handle, a.display_name, a.verified,
            COUNT(i.id) AS impressions_count,
            COALESCE(SUM(i.dwell_ms), 0) AS total_dwell_ms,
            MIN(i.first_seen_at) AS first_seen_at,
            {eng}
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
          AND t.text IS NOT NULL AND t.text != ''
          AND NOT EXISTS (
              SELECT 1 FROM my_interactions mi
              WHERE mi.tweet_id = t.tweet_id
                AND mi.timestamp >= ? AND mi.timestamp < ?
          )
        GROUP BY t.tweet_id
        HAVING total_dwell_ms >= ?
        ORDER BY total_dwell_ms DESC
        LIMIT ?
        """,
        (start_iso, end_iso, start_iso, end_iso, min_dwell_ms, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def algorithmic_pressure(
    db: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
    min_impressions: int = 3,
    limit: int = 50,
) -> list[dict]:
    """Tweets shown to the user at least ``min_impressions`` times in the
    range. A proxy for what the algorithm is pushing hardest."""
    eng = _engagement_subselects()
    cur = db.execute(
        f"""
        SELECT
            t.tweet_id, t.text, t.created_at, t.media_json,
            a.handle, a.display_name, a.verified,
            COUNT(i.id) AS impressions_count,
            COUNT(DISTINCT i.session_id) AS sessions_hit,
            COALESCE(SUM(i.dwell_ms), 0) AS total_dwell_ms,
            MIN(i.first_seen_at) AS first_seen_at,
            {eng},
            EXISTS (
                SELECT 1 FROM my_interactions mi
                WHERE mi.tweet_id = t.tweet_id
                  AND mi.timestamp >= ? AND mi.timestamp < ?
            ) AS user_had_interaction
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
        GROUP BY t.tweet_id
        HAVING impressions_count >= ?
        ORDER BY impressions_count DESC, total_dwell_ms DESC
        LIMIT ?
        """,
        (start_iso, end_iso, start_iso, end_iso, min_impressions, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def author_report(
    db: sqlite3.Connection,
    handle: str,
    start_iso: str,
    end_iso: str,
    tweet_limit: int = 50,
) -> dict[str, Any]:
    """Full engagement portrait with one author in the range.

    Returns a dict with the author row, aggregate impressions, interactions,
    link-clicks on their tweets, text selections from their tweets, and
    up to ``tweet_limit`` ranked tweets."""
    handle = handle.lstrip("@")
    author_row = db.execute(
        "SELECT user_id, handle, display_name, verified, follower_count, "
        "following_count, bio FROM authors WHERE handle = ? COLLATE NOCASE",
        (handle,),
    ).fetchone()
    if not author_row:
        return {"author": None, "handle": handle, "tweets": [], "stats": {}}
    user_id = author_row["user_id"]
    stats = db.execute(
        """
        SELECT COUNT(DISTINCT i.tweet_id) AS unique_tweets_seen,
               COUNT(i.id) AS impressions,
               COALESCE(SUM(i.dwell_ms), 0) AS total_dwell_ms
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        WHERE t.author_id = ?
          AND i.first_seen_at >= ? AND i.first_seen_at < ?
        """,
        (user_id, start_iso, end_iso),
    ).fetchone()
    inter = db.execute(
        """
        SELECT mi.action, COUNT(*) AS n
        FROM my_interactions mi
        JOIN tweets t ON mi.tweet_id = t.tweet_id
        WHERE t.author_id = ? AND mi.timestamp >= ? AND mi.timestamp < ?
        GROUP BY mi.action
        """,
        (user_id, start_iso, end_iso),
    ).fetchall()
    selections = db.execute(
        """
        SELECT ts.text, ts.via, ts.timestamp, ts.tweet_id
        FROM text_selections ts
        JOIN tweets t ON ts.tweet_id = t.tweet_id
        WHERE t.author_id = ? AND ts.timestamp >= ? AND ts.timestamp < ?
        ORDER BY ts.timestamp DESC
        LIMIT 25
        """,
        (user_id, start_iso, end_iso),
    ).fetchall()
    link_clicks = db.execute(
        """
        SELECT lc.url, lc.domain, lc.link_kind, lc.timestamp, lc.tweet_id
        FROM link_clicks lc
        JOIN tweets t ON lc.tweet_id = t.tweet_id
        WHERE t.author_id = ? AND lc.timestamp >= ? AND lc.timestamp < ?
        ORDER BY lc.timestamp DESC
        LIMIT 25
        """,
        (user_id, start_iso, end_iso),
    ).fetchall()
    eng = _engagement_subselects()
    tweets_rows = db.execute(
        f"""
        SELECT t.tweet_id, t.text, t.created_at, t.media_json, t.quoted_tweet_id,
               COUNT(i.id) AS impressions_count,
               COALESCE(SUM(i.dwell_ms), 0) AS total_dwell_ms,
               MIN(i.first_seen_at) AS first_seen_at,
               {eng},
               EXISTS (
                   SELECT 1 FROM my_interactions mi WHERE mi.tweet_id = t.tweet_id
               ) AS user_had_interaction
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        WHERE t.author_id = ?
          AND i.first_seen_at >= ? AND i.first_seen_at < ?
        GROUP BY t.tweet_id
        ORDER BY total_dwell_ms DESC, impressions_count DESC
        LIMIT ?
        """,
        (user_id, start_iso, end_iso, tweet_limit),
    ).fetchall()
    return {
        "author": dict(author_row),
        "handle": handle,
        "stats": {
            **dict(stats),
            "interactions": {r["action"]: r["n"] for r in inter},
        },
        "tweets": [dict(r) for r in tweets_rows],
        "recent_selections": [dict(r) for r in selections],
        "recent_link_clicks": [dict(r) for r in link_clicks],
    }


def session_detail(db: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    """All signals from one session — impressions, interactions, scroll
    bursts, nav, searches, selections, link clicks, media opens.

    ``my_interactions`` has no ``session_id`` column, so interactions are
    joined by timestamp falling within the session's started_at/ended_at
    window.
    """
    session_row = db.execute(
        "SELECT session_id, started_at, ended_at, feeds_visited, "
        "tweet_count, total_dwell_ms FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if not session_row:
        return {"session": None}
    started_at = session_row["started_at"]
    # When a session never ended (crash, tab close), cap the interaction
    # window at started_at + 1 day so we don't pull in unrelated later events.
    if session_row["ended_at"]:
        ended_at = session_row["ended_at"]
    else:
        try:
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            ended_at = (started_dt + timedelta(days=1)).isoformat()
        except (TypeError, ValueError):
            ended_at = started_at  # degenerate: no interactions returned

    def rows(sql: str, params: tuple = (session_id,)) -> list[dict]:
        return [dict(r) for r in db.execute(sql, params).fetchall()]

    eng = _engagement_subselects()
    impressions = rows(
        f"""
        SELECT i.first_seen_at, i.dwell_ms, i.feed_source,
               t.tweet_id, t.text, t.media_json,
               a.handle, a.display_name, {eng}
        FROM impressions i
        LEFT JOIN tweets t ON i.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE i.session_id = ?
        ORDER BY i.first_seen_at
        """
    )
    interactions = rows(
        """
        SELECT mi.tweet_id, mi.action, mi.timestamp, a.handle
        FROM my_interactions mi
        LEFT JOIN tweets t ON mi.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE mi.timestamp >= ? AND mi.timestamp <= ?
        ORDER BY mi.timestamp
        """,
        (started_at, ended_at),
    )
    return {
        "session": dict(session_row),
        "impressions": impressions,
        "interactions": interactions,
        "scroll_bursts": rows(
            "SELECT started_at, ended_at, duration_ms, start_y, end_y, "
            "delta_y, reversals_count, feed_source FROM scroll_bursts "
            "WHERE session_id = ? ORDER BY started_at"
        ),
        "nav_events": rows(
            "SELECT timestamp, from_path, to_path, feed_source_before, "
            "feed_source_after FROM nav_events WHERE session_id = ? ORDER BY timestamp"
        ),
        "searches": rows(
            "SELECT query, timestamp FROM searches WHERE session_id = ? ORDER BY timestamp"
        ),
        "selections": rows(
            "SELECT tweet_id, text, via, timestamp FROM text_selections "
            "WHERE session_id = ? ORDER BY timestamp"
        ),
        "link_clicks": rows(
            "SELECT url, domain, link_kind, modifiers, timestamp, tweet_id "
            "FROM link_clicks WHERE session_id = ? ORDER BY timestamp"
        ),
        "media_events": rows(
            "SELECT tweet_id, media_kind, media_index, timestamp FROM media_events "
            "WHERE session_id = ? ORDER BY timestamp"
        ),
    }


def recent_sessions(db: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Most recent N sessions (by start time), with impression-derived counts
    so the numbers match the export."""
    cur = db.execute(
        """
        SELECT s.session_id, s.started_at, s.ended_at, s.feeds_visited,
               COALESCE(i.n, 0) AS tweet_count,
               COALESCE(i.dwell, 0) AS total_dwell_ms
        FROM sessions s
        LEFT JOIN (
            SELECT session_id, COUNT(*) AS n, SUM(dwell_ms) AS dwell
            FROM impressions GROUP BY session_id
        ) i ON i.session_id = s.session_id
        ORDER BY s.started_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def hesitation_report(
    db: sqlite3.Connection,
    start_iso: str,
    end_iso: str,
    min_dwell_ms: int = 200,
    limit: int = 50,
) -> list[dict]:
    """Tweets where the cursor hovered a like/retweet/reply/bookmark button
    for >= ``min_dwell_ms`` without the user clicking it.

    Requires the ``button_hover_intent`` table from Sprint 2. Returns an
    empty list with a ``not_implemented`` marker row if the table doesn't
    exist yet — agents can detect and skip the insight gracefully.
    """
    try:
        db.execute("SELECT 1 FROM button_hover_intent LIMIT 1")
    except sqlite3.OperationalError:
        return []
    eng = _engagement_subselects()
    cur = db.execute(
        f"""
        SELECT
            bhi.action AS almost_clicked,
            bhi.dwell_ms AS hover_dwell_ms,
            bhi.timestamp AS hovered_at,
            t.tweet_id, t.text, t.created_at, t.media_json,
            a.handle, a.display_name, a.verified,
            {eng}
        FROM button_hover_intent bhi
        JOIN tweets t ON bhi.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE bhi.timestamp >= ? AND bhi.timestamp < ?
          AND bhi.dwell_ms >= ?
        ORDER BY bhi.dwell_ms DESC
        LIMIT ?
        """,
        (start_iso, end_iso, min_dwell_ms, limit),
    )
    return [dict(r) for r in cur.fetchall()]
