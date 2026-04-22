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


def threads_rows(db: sqlite3.Connection, day_start: str, day_end: str, min_tweets: int = 2) -> list[dict]:
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


def link_clicks_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT lc.timestamp, lc.url, lc.domain, lc.link_kind, lc.modifiers,
               lc.tweet_id, lc.session_id, t.text, a.handle
        FROM link_clicks lc
        LEFT JOIN tweets t ON lc.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE lc.timestamp >= ? AND lc.timestamp < ?
        ORDER BY lc.timestamp
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def media_events_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT me.timestamp, me.tweet_id, me.session_id, me.media_kind, me.media_index,
               t.text, a.handle
        FROM media_events me
        LEFT JOIN tweets t ON me.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE me.timestamp >= ? AND me.timestamp < ?
        ORDER BY me.timestamp
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def text_selections_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT ts.timestamp, ts.tweet_id, ts.session_id, ts.text, ts.via,
               t.text AS tweet_text, a.handle
        FROM text_selections ts
        LEFT JOIN tweets t ON ts.tweet_id = t.tweet_id
        LEFT JOIN authors a ON t.author_id = a.user_id
        WHERE ts.timestamp >= ? AND ts.timestamp < ?
        ORDER BY ts.timestamp
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def scroll_bursts_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT session_id, feed_source, started_at, ended_at, duration_ms,
               start_y, end_y, delta_y, reversals_count
        FROM scroll_bursts
        WHERE started_at >= ? AND started_at < ?
        ORDER BY started_at
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def nav_events_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT session_id, from_path, to_path, feed_source_before, feed_source_after, timestamp
        FROM nav_events
        WHERE timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def relationship_changes_rows(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    cur = db.execute(
        """
        SELECT rc.session_id, rc.target_user_id, rc.action, rc.timestamp,
               a.handle
        FROM relationship_changes rc
        LEFT JOIN authors a ON rc.target_user_id = a.user_id
        WHERE rc.timestamp >= ? AND rc.timestamp < ?
        ORDER BY rc.timestamp
        """,
        (day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def revisits(db: sqlite3.Connection, day_start: str, day_end: str) -> dict[tuple[str, str], int]:
    """(session_id, tweet_id) -> view count, only where count > 1."""
    cur = db.execute(
        """
        SELECT session_id, tweet_id, COUNT(*) AS n
        FROM impressions
        WHERE first_seen_at >= ? AND first_seen_at < ?
          AND session_id IS NOT NULL AND tweet_id IS NOT NULL
        GROUP BY session_id, tweet_id
        HAVING COUNT(*) > 1
        """,
        (day_start, day_end),
    )
    return {(r["session_id"], r["tweet_id"]): r["n"] for r in cur.fetchall()}


def session_timeline(db: sqlite3.Connection, day_start: str, day_end: str) -> list[dict]:
    """All events in one session, interleaved chronologically. UNION ALL the
    contributing tables into a common shape:
       (session_id, timestamp, event_kind, payload_json)
    Ordered by (session_id, timestamp, event_log.ingested_at) so cross-tab
    ties resolve deterministically.

    Payload is compact JSON sized for inline markdown rendering; callers that
    need richer data should hit the per-section queries.
    """
    # Dump each source to a (session, ts, kind, payload) tuple. Using json_object()
    # keeps this a single SQL pass; sqlite 3.38+ has it, which aiosqlite ships with.
    q = """
    WITH timeline AS (
      -- Impression ends
      SELECT i.session_id AS session_id, i.first_seen_at AS ts,
             'impression' AS kind,
             json_object(
               'tweet_id', i.tweet_id,
               'dwell_ms', i.dwell_ms,
               'feed_source', i.feed_source,
               'handle', a.handle,
               'text', substr(COALESCE(t.text, ''), 1, 120)
             ) AS payload
      FROM impressions i
      LEFT JOIN tweets t ON i.tweet_id = t.tweet_id
      LEFT JOIN authors a ON t.author_id = a.user_id
      WHERE i.first_seen_at >= ? AND i.first_seen_at < ?

      UNION ALL
      -- Clicks (like/retweet/reply/bookmark/profile_click/expand)
      SELECT NULL AS session_id, mi.timestamp AS ts,
             'interaction:' || COALESCE(mi.action, '') AS kind,
             json_object(
               'tweet_id', mi.tweet_id,
               'handle', a.handle
             ) AS payload
      FROM my_interactions mi
      LEFT JOIN tweets t ON mi.tweet_id = t.tweet_id
      LEFT JOIN authors a ON t.author_id = a.user_id
      WHERE mi.timestamp >= ? AND mi.timestamp < ?

      UNION ALL
      SELECT s.session_id, s.timestamp, 'search',
             json_object('query', s.query)
      FROM searches s
      WHERE s.timestamp >= ? AND s.timestamp < ?

      UNION ALL
      SELECT lc.session_id, lc.timestamp, 'link',
             json_object(
               'url', lc.url, 'domain', lc.domain,
               'link_kind', lc.link_kind, 'modifiers', lc.modifiers,
               'tweet_id', lc.tweet_id
             )
      FROM link_clicks lc
      WHERE lc.timestamp >= ? AND lc.timestamp < ?

      UNION ALL
      SELECT me.session_id, me.timestamp,
             'media:' || COALESCE(me.media_kind, ''),
             json_object('tweet_id', me.tweet_id, 'media_index', me.media_index)
      FROM media_events me
      WHERE me.timestamp >= ? AND me.timestamp < ?

      UNION ALL
      SELECT ts.session_id, ts.timestamp, 'select',
             json_object(
               'tweet_id', ts.tweet_id, 'text', ts.text, 'via', ts.via
             )
      FROM text_selections ts
      WHERE ts.timestamp >= ? AND ts.timestamp < ?

      UNION ALL
      SELECT ne.session_id, ne.timestamp, 'nav',
             json_object(
               'from_path', ne.from_path, 'to_path', ne.to_path,
               'feed_source_after', ne.feed_source_after
             )
      FROM nav_events ne
      WHERE ne.timestamp >= ? AND ne.timestamp < ?

      UNION ALL
      SELECT rc.session_id, rc.timestamp,
             'rel:' || COALESCE(rc.action, ''),
             json_object('target_user_id', rc.target_user_id, 'handle', a.handle)
      FROM relationship_changes rc
      LEFT JOIN authors a ON rc.target_user_id = a.user_id
      WHERE rc.timestamp >= ? AND rc.timestamp < ?
    )
    SELECT session_id, ts, kind, payload FROM timeline
    ORDER BY session_id NULLS LAST, ts, kind
    """
    params = (day_start, day_end) * 8
    cur = db.execute(q, params)
    return [dict(r) for r in cur.fetchall()]


def unique_tweets_with_engagement(
    db: sqlite3.Connection, day_start: str, day_end: str
) -> list[dict]:
    """One row per unique tweet seen today — aggregated across all impressions.

    Fields include impressions_count, total_dwell_ms, sessions_hit (CSV),
    latest engagement snapshot, rich tweet metadata (media, conversation,
    reply chain), author context, and a flag for whether the user
    interacted with the tweet today. This is the canonical input for
    importance scoring, repeat-exposure flagging, topic rollups, and the
    TL;DR digest.

    Ordered by impressions_count DESC then total_dwell_ms DESC so the
    noisiest/most-dwelled tweets surface first; callers typically re-sort
    by importance score which is computed in Python (scoring.importance).
    """
    cur = db.execute(
        """
        SELECT
            t.tweet_id,
            t.text,
            t.lang,
            t.conversation_id,
            t.media_json,
            t.reply_to_tweet_id,
            t.quoted_tweet_id,
            t.retweeted_tweet_id,
            a.handle,
            a.display_name,
            a.user_id AS author_id,
            a.follower_count,
            a.verified,
            COUNT(i.id) AS impressions_count,
            COALESCE(SUM(i.dwell_ms), 0) AS total_dwell_ms,
            GROUP_CONCAT(DISTINCT i.session_id) AS sessions_hit_csv,
            MIN(i.first_seen_at) AS first_seen_at,
            (SELECT likes FROM engagement_snapshots e WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS likes,
            (SELECT retweets FROM engagement_snapshots e WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS retweets,
            (SELECT replies FROM engagement_snapshots e WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS replies,
            (SELECT views FROM engagement_snapshots e WHERE e.tweet_id = t.tweet_id ORDER BY e.id DESC LIMIT 1) AS views,
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
        ORDER BY impressions_count DESC, total_dwell_ms DESC
        """,
        (day_start, day_end, day_start, day_end),
    )
    return [dict(r) for r in cur.fetchall()]


def author_context_rows(
    db: sqlite3.Connection, day_start: str, day_end: str
) -> list[dict]:
    """Top authors seen today, enriched with follower_count and verified flag
    plus impression + unique-tweet + total-dwell aggregates. Feeds the v2
    Authors section with real context ('who is this person who keeps showing
    up in my feed?')."""
    cur = db.execute(
        """
        SELECT
            a.handle,
            a.user_id,
            a.display_name,
            a.follower_count,
            a.verified,
            COUNT(i.id) AS impressions_count,
            COUNT(DISTINCT i.tweet_id) AS unique_tweets,
            COALESCE(SUM(i.dwell_ms), 0) AS total_dwell_ms
        FROM impressions i
        JOIN tweets t ON i.tweet_id = t.tweet_id
        JOIN authors a ON t.author_id = a.user_id
        WHERE i.first_seen_at >= ? AND i.first_seen_at < ?
        GROUP BY a.user_id
        ORDER BY impressions_count DESC, total_dwell_ms DESC
        """,
        (day_start, day_end),
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
