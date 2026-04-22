"""Ingest logic - routes events from the extension into SQLite."""
from __future__ import annotations

import json
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque
from urllib.parse import parse_qs, urlparse

import aiosqlite

from backend.parser import extract_from_payload
from backend.settings import PARSER_VERSION

_QUERY_ID_RE = re.compile(r"/i/api/graphql/([A-Za-z0-9_-]+)/([A-Za-z0-9_]+)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Rolling window of recent batch timings. Bounded size so memory stays flat.
# Each entry: {"ts": epoch_s, "events": int, "accepted": int, "skipped": int,
#              "duration_ms": float}
BATCH_METRICS: Deque[dict[str, Any]] = deque(maxlen=200)


async def _claim_event(
    db: aiosqlite.Connection, event_id: str, event_type: str, tab_id: Any, now: str
) -> bool:
    """Record the event in event_log. Returns True if this is a new event
    (caller should process it), False if it's a duplicate (caller should skip)."""
    row = await (
        await db.execute("SELECT 1 FROM event_log WHERE event_id = ?", (event_id,))
    ).fetchone()
    if row is not None:
        return False
    await db.execute(
        "INSERT INTO event_log (event_id, event_type, tab_id, ingested_at) "
        "VALUES (?, ?, ?, ?)",
        (event_id, event_type, tab_id if isinstance(tab_id, int) else None, now),
    )
    return True


async def _upsert_author(db: aiosqlite.Connection, a: dict, now: str) -> None:
    await db.execute(
        """
        INSERT INTO authors (user_id, handle, display_name, bio, verified,
                             follower_count, following_count, first_seen_at, last_updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            handle = excluded.handle,
            display_name = COALESCE(excluded.display_name, authors.display_name),
            bio = COALESCE(excluded.bio, authors.bio),
            verified = COALESCE(excluded.verified, authors.verified),
            follower_count = COALESCE(excluded.follower_count, authors.follower_count),
            following_count = COALESCE(excluded.following_count, authors.following_count),
            last_updated_at = excluded.last_updated_at
        """,
        (
            a["user_id"],
            a.get("handle"),
            a.get("display_name"),
            a.get("bio"),
            a.get("verified"),
            a.get("follower_count"),
            a.get("following_count"),
            now,
            now,
        ),
    )


async def _upsert_tweet(db: aiosqlite.Connection, t: dict, now: str) -> None:
    await db.execute(
        """
        INSERT INTO tweets (tweet_id, author_id, text, created_at, captured_at,
                            last_updated_at, lang, conversation_id,
                            reply_to_tweet_id, reply_to_user_id,
                            quoted_tweet_id, retweeted_tweet_id, media_json, is_my_tweet)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(tweet_id) DO UPDATE SET
            text = COALESCE(excluded.text, tweets.text),
            author_id = COALESCE(excluded.author_id, tweets.author_id),
            created_at = COALESCE(excluded.created_at, tweets.created_at),
            lang = COALESCE(excluded.lang, tweets.lang),
            conversation_id = COALESCE(excluded.conversation_id, tweets.conversation_id),
            reply_to_tweet_id = COALESCE(excluded.reply_to_tweet_id, tweets.reply_to_tweet_id),
            reply_to_user_id = COALESCE(excluded.reply_to_user_id, tweets.reply_to_user_id),
            quoted_tweet_id = COALESCE(excluded.quoted_tweet_id, tweets.quoted_tweet_id),
            retweeted_tweet_id = COALESCE(excluded.retweeted_tweet_id, tweets.retweeted_tweet_id),
            media_json = COALESCE(excluded.media_json, tweets.media_json),
            last_updated_at = excluded.last_updated_at
        """,
        (
            t["tweet_id"],
            t.get("author_id"),
            t.get("text"),
            t.get("created_at"),
            now,
            now,
            t.get("lang"),
            t.get("conversation_id"),
            t.get("reply_to_tweet_id"),
            t.get("reply_to_user_id"),
            t.get("quoted_tweet_id"),
            t.get("retweeted_tweet_id"),
            t.get("media_json"),
        ),
    )


async def _insert_engagement(db: aiosqlite.Connection, e: dict, now: str) -> None:
    # Skip if last snapshot <5 min old AND counts unchanged
    row = await (
        await db.execute(
            """
            SELECT likes, retweets, replies, quotes, views, bookmarks, captured_at
            FROM engagement_snapshots
            WHERE tweet_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (e["tweet_id"],),
        )
    ).fetchone()
    if row is not None:
        same = (
            row["likes"] == e.get("likes")
            and row["retweets"] == e.get("retweets")
            and row["replies"] == e.get("replies")
            and row["quotes"] == e.get("quotes")
            and row["views"] == e.get("views")
            and row["bookmarks"] == e.get("bookmarks")
        )
        if same:
            try:
                prev = datetime.fromisoformat(row["captured_at"])
                age = (datetime.now(timezone.utc) - prev).total_seconds()
                if age < 300:
                    return
            except (TypeError, ValueError):
                pass
    await db.execute(
        """
        INSERT INTO engagement_snapshots (tweet_id, captured_at, likes, retweets,
                                          replies, quotes, views, bookmarks)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            e["tweet_id"],
            now,
            e.get("likes"),
            e.get("retweets"),
            e.get("replies"),
            e.get("quotes"),
            e.get("views"),
            e.get("bookmarks"),
        ),
    )


async def _handle_graphql(db: aiosqlite.Connection, event: dict, now: str) -> None:
    payload = event.get("payload") or {}
    op_name = event.get("operation_name") or "unknown"
    if payload:
        await db.execute(
            "INSERT INTO raw_payloads (operation_name, payload_json, captured_at, parser_version) VALUES (?, ?, ?, ?)",
            (op_name, json.dumps(payload), now, PARSER_VERSION),
        )
        extracted = extract_from_payload(payload)
        for a in extracted["authors"]:
            await _upsert_author(db, a, now)
        for t in extracted["tweets"]:
            await _upsert_tweet(db, t, now)
        for e in extracted["engagements"]:
            if any(e.get(k) is not None for k in ("likes", "retweets", "replies", "quotes", "views", "bookmarks")):
                await _insert_engagement(db, e, now)


async def _handle_session_start(db: aiosqlite.Connection, event: dict, now: str) -> None:
    sid = event.get("session_id") or event.get("s")
    if not sid:
        raise ValueError("session_start missing session_id")
    await db.execute(
        """
        INSERT INTO sessions (session_id, started_at, ended_at, total_dwell_ms, tweet_count, feeds_visited)
        VALUES (?, ?, NULL, 0, 0, ?)
        ON CONFLICT(session_id) DO NOTHING
        """,
        (sid, event.get("timestamp") or now, json.dumps(event.get("feeds_visited") or [])),
    )


async def _handle_session_end(db: aiosqlite.Connection, event: dict, now: str) -> None:
    sid = event.get("session_id") or event.get("s")
    if not sid:
        raise ValueError("session_end missing session_id")
    await db.execute(
        """
        UPDATE sessions
        SET ended_at = ?,
            total_dwell_ms = ?,
            tweet_count = ?,
            feeds_visited = ?
        WHERE session_id = ?
        """,
        (
            event.get("timestamp") or now,
            event.get("total_dwell_ms") or 0,
            event.get("tweet_count") or 0,
            json.dumps(event.get("feeds_visited") or []),
            sid,
        ),
    )


async def _handle_impression_end(db: aiosqlite.Connection, event: dict, now: str) -> None:
    tweet_id = event.get("tweet_id")
    if not tweet_id:
        raise ValueError("impression_end missing tweet_id")
    sid = event.get("session_id") or event.get("s")
    # Ensure tweet stub exists so FK holds even if GraphQL hasn't landed yet
    await db.execute(
        "INSERT OR IGNORE INTO tweets (tweet_id, captured_at, last_updated_at) VALUES (?, ?, ?)",
        (tweet_id, now, now),
    )
    # Ensure session exists — handles DB wipe while SW retains in-memory session_id
    if sid:
        await db.execute(
            "INSERT OR IGNORE INTO sessions (session_id, started_at, total_dwell_ms, tweet_count) VALUES (?, ?, 0, 0)",
            (sid, event.get("first_seen_at") or now),
        )
    await db.execute(
        """
        INSERT INTO impressions (tweet_id, session_id, first_seen_at, dwell_ms, feed_source)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            tweet_id,
            sid,
            event.get("first_seen_at") or now,
            event.get("dwell_ms") or 0,
            event.get("feed_source"),
        ),
    )


async def _handle_interaction(db: aiosqlite.Connection, event: dict, now: str) -> None:
    tweet_id = event.get("tweet_id")
    action = event.get("action")
    if not tweet_id or not action:
        raise ValueError("interaction missing tweet_id or action")
    await db.execute(
        "INSERT OR IGNORE INTO tweets (tweet_id, captured_at, last_updated_at) VALUES (?, ?, ?)",
        (tweet_id, now, now),
    )
    await db.execute(
        "INSERT INTO my_interactions (tweet_id, action, timestamp) VALUES (?, ?, ?)",
        (tweet_id, action, event.get("timestamp") or now),
    )


async def _handle_dom_tweet(db: aiosqlite.Connection, event: dict, now: str) -> None:
    """Minimal tweet record extracted from DOM when GraphQL interception missed it.
    Fills in text, handle, display_name on stub tweet rows. Idempotent.
    Accepts both full keys (author_handle, author_display) and short aliases (ah, ad)
    for transport layers that strip sensitive-looking field names."""
    tweet_id = event.get("tweet_id")
    handle = event.get("author_handle") or event.get("ah")
    if not tweet_id or not handle:
        raise ValueError("dom_tweet missing tweet_id or author handle")
    # Stable pseudo user_id for DOM-only authors (prefixed so we don't collide
    # with real numeric Twitter user IDs).
    user_id = f"dom-{handle.lower()}"
    await _upsert_author(db, {
        "user_id": user_id,
        "handle": handle,
        "display_name": event.get("author_display") or event.get("ad"),
    }, now)
    created_iso = event.get("created_at_iso")
    # ISO datetime string passes straight through; leave as-is
    await _upsert_tweet(db, {
        "tweet_id": tweet_id,
        "author_id": user_id,
        "text": event.get("text"),
        "created_at": created_iso,
        "conversation_id": event.get("conversation_id") or tweet_id,
    }, now)


async def _handle_graphql_template(db: aiosqlite.Connection, event: dict, now: str) -> None:
    """Capture the request shape of a real GraphQL call so the enrichment
    worker can replay it with mutated variables later."""
    op = event.get("operation_name") or ""
    url = event.get("url") or ""
    auth = event.get("auth")
    if not op or not url:
        return
    parsed = urlparse(url)
    m = _QUERY_ID_RE.search(parsed.path)
    if not m:
        return
    query_id = m.group(1)
    qs = parse_qs(parsed.query)
    variables = qs.get("variables", [""])[0] or "{}"
    features = qs.get("features", [""])[0] or "{}"
    # Keep bearer non-blank: if the capture missed the header, don't clobber
    # an existing good value with NULL.
    await db.execute(
        """
        INSERT INTO graphql_templates
          (operation_name, query_id, url_path, features_json, variables_json, bearer, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(operation_name) DO UPDATE SET
            query_id = excluded.query_id,
            url_path = excluded.url_path,
            features_json = excluded.features_json,
            variables_json = excluded.variables_json,
            bearer = COALESCE(excluded.bearer, graphql_templates.bearer),
            last_seen_at = excluded.last_seen_at
        """,
        (op, query_id, parsed.path, features, variables, auth or None, now),
    )


async def _handle_search(db: aiosqlite.Connection, event: dict, now: str) -> None:
    q = event.get("query")
    if not q:
        raise ValueError("search missing query")
    await db.execute(
        "INSERT INTO searches (query, timestamp, session_id) VALUES (?, ?, ?)",
        (q, event.get("timestamp") or now, event.get("session_id")),
    )


HANDLERS = {
    "graphql_payload": _handle_graphql,
    "graphql_template": _handle_graphql_template,
    "session_start": _handle_session_start,
    "session_end": _handle_session_end,
    "impression_start": lambda db, ev, now: None,  # recorded via impression_end
    "impression_end": _handle_impression_end,
    "interaction": _handle_interaction,
    "search": _handle_search,
    "dom_tweet": _handle_dom_tweet,
}


async def ingest_batch(db: aiosqlite.Connection, events: list[dict]) -> dict:
    accepted = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    now = _now()
    t0 = time.perf_counter()
    for i, event in enumerate(events):
        etype = event.get("type")
        handler = HANDLERS.get(etype)
        if handler is None:
            errors.append({"index": i, "error": f"unknown event type: {etype}"})
            continue
        event_id = event.get("event_id")
        # Dedup only when the client supplied an id. Legacy events without
        # one ingest unconditionally (they pre-date the dedup layer).
        if event_id:
            is_new = await _claim_event(db, event_id, etype, event.get("tab_id"), now)
            if not is_new:
                skipped += 1
                continue
        try:
            res = handler(db, event, now)
            # handler may be a regular function (for impression_start)
            if res is not None:
                await res
            accepted += 1
        except Exception as exc:  # keep ingest tolerant
            errors.append({"index": i, "error": f"{type(exc).__name__}: {exc}"})
    await db.commit()
    BATCH_METRICS.append({
        "ts": time.time(),
        "events": len(events),
        "accepted": accepted,
        "skipped": skipped,
        "duration_ms": (time.perf_counter() - t0) * 1000,
    })
    return {"accepted": accepted, "skipped": skipped, "errors": errors}
