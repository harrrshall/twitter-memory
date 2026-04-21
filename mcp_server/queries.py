"""SQL per section. All queries parameterize on [day_start_utc, day_end_utc)."""
from __future__ import annotations

import sqlite3
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


def day_window_utc(local_date: date_cls, tz: ZoneInfo) -> tuple[str, str]:
    start = datetime(local_date.year, local_date.month, local_date.day, 0, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    return (
        start.astimezone(ZoneInfo("UTC")).isoformat(),
        end.astimezone(ZoneInfo("UTC")).isoformat(),
    )


def connect_ro(db_path: Path) -> sqlite3.Connection:
    # Read-only URI connection for the export tool. Safe since FastAPI owns writes.
    uri = f"file:{db_path}?mode=ro"
    c = sqlite3.connect(uri, uri=True)
    c.row_factory = sqlite3.Row
    return c


def summary(db: sqlite3.Connection, day_start: str, day_end: str) -> dict[str, Any]:
    cur = db.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT tweet_id) AS unique_tweets FROM impressions WHERE first_seen_at >= ? AND first_seen_at < ?",
        (day_start, day_end),
    )
    impr = cur.fetchone()
    cur = db.execute(
        """
        SELECT i.tweet_id, t.author_id
        FROM impressions i LEFT JOIN tweets t ON i.tweet_id = t.tweet_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
        """,
        (day_start, day_end),
    )
    authors = {r["author_id"] for r in cur.fetchall() if r["author_id"]}
    cur = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_dwell_ms),0) AS dwell FROM sessions WHERE started_at >= ? AND started_at < ?",
        (day_start, day_end),
    )
    sess = cur.fetchone()
    cur = db.execute(
        """
        SELECT action, COUNT(*) AS n FROM my_interactions
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY action
        """,
        (day_start, day_end),
    )
    inter = {r["action"]: r["n"] for r in cur.fetchall()}
    cur = db.execute(
        "SELECT COUNT(*) AS n FROM searches WHERE timestamp >= ? AND timestamp < ?",
        (day_start, day_end),
    )
    searches = cur.fetchone()["n"]
    return {
        "tweets_seen": impr["n"],
        "unique_tweets": impr["unique_tweets"],
        "unique_authors": len(authors),
        "sessions": sess["n"],
        "total_dwell_ms": sess["dwell"],
        "interactions": inter,
        "searches": searches,
    }


def sessions_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT session_id, started_at, ended_at, total_dwell_ms, tweet_count, feeds_visited
        FROM sessions
        WHERE started_at >= ? AND started_at < ?
        ORDER BY started_at
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def searches_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT query, timestamp, session_id
        FROM searches
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def interactions_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT mi.tweet_id, mi.action, mi.timestamp,
               t.text, a.handle
        FROM my_interactions mi
        LEFT JOIN tweets t ON mi.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE mi.timestamp >= ? AND mi.timestamp < ?
        ORDER BY mi.timestamp
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def top_authors_by_impressions(db: sqlite3.Connection, day_start: str, day_end: str, limit: int = 10) -> list[dict]:
    cur = db.execute(
        """
        SELECT a.handle, a.user_id, COUNT(*) AS n
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        JOIN authors a ON t.author_id = a.user_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
        GROUP BY a.user_id
        ORDER BY n DESC
        LIMIT ?
        """,
        (day_start, day_end, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def top_authors_by_dwell(db: sqlite3.Connection, day_start: str, day_end: str, limit: int = 10) -> list[dict]:
    cur = db.execute(
        """
        SELECT a.handle, a.user_id, COALESCE(SUM(i.dwell_ms),0) AS dwell_ms
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        JOIN authors a ON t.author_id = a.user_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
        GROUP BY a.user_id
        ORDER BY dwell_ms DESC
        LIMIT ?
        """,
        (day_start, day_end, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def threads_rows(db: sqlite3.Connection, day_start: str, day_end: str, min_tweets: int = 3) -> list[dict]:
    # Conversations with >= min_tweets distinct tweets seen this day, ordered.
    cur = db.execute(
        """
        WITH seen AS (
            SELECT DISTINCT t.tweet_id, t.conversation_id, t.created_at, t.text, a.handle
            FROM impressions i
            JOIN tweets t ON i.tweet_id = t.tweet_id
            LEFT JOIN authors a ON t.author_id = a.user_id
            WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
              AND t.conversation_id IS NOT NULL
        )
        SELECT conversation_id, tweet_id, created_at, text, handle
        FROM seen
        WHERE conversation_id IN (
            SELECT conversation_id FROM seen GROUP BY conversation_id HAVING COUNT(*) >= ?
        )
        ORDER BY conversation_id, created_at
        """,
        (day_start, day_end, min_tweets),
    )
    return [dict(r) for r in cur.fetchall()]


def impressions_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    # Latest engagement snapshot per tweet via correlated subquery.
    cur = db.execute(
        """
        SELECT
            i.session_id, i.first_seen_at, i.dwell_ms, i.feed_source,
            t.tweet_id, t.text, t.created_at, t.retweeted_tweet_id, t.quoted_tweet_id,
            a.handle, a.display_name,
            (SELECT likes FROM engagement_snapshots e WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS likes,
            (SELECT retweets FROM engagement_snapshots e WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS retweets,
            (SELECT replies FROM engagement_snapshots e WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS replies,
            (SELECT views FROM engagement_snapshots e WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS views
        FROM impressions i
        LEFT JOIN tweets t ON i.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
        ORDER BY i.first_seen_at
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]
