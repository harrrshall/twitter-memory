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
    """Full tweet record extracted from the rendered DOM — the primary capture
    path in DOM-only mode. Includes author, text, timestamp, engagement counts,
    media, and quote linkage. Idempotent (upserts).
    Accepts both full keys (author_handle, author_display) and short aliases
    (ah, ad) for transport layers that strip sensitive-looking field names."""
    tweet_id = event.get("tweet_id")
    handle = event.get("author_handle") or event.get("ah")
    if not tweet_id or not handle:
        raise ValueError("dom_tweet missing tweet_id or author handle")
    # Stable pseudo user_id for DOM-only authors (prefixed so we don't collide
    # with real numeric Twitter user IDs from historical GraphQL rows).
    user_id = f"dom-{handle.lower()}"
    await _upsert_author(db, {
        "user_id": user_id,
        "handle": handle,
        "display_name": event.get("author_display") or event.get("ad"),
    }, now)
    await _upsert_tweet(db, {
        "tweet_id": tweet_id,
        "author_id": user_id,
        "text": event.get("text"),
        "created_at": event.get("created_at_iso"),
        "conversation_id": event.get("conversation_id") or tweet_id,
        "quoted_tweet_id": event.get("quoted_tweet_id"),
        "media_json": event.get("media_json"),
    }, now)
    # Engagement snapshot: insert when at least one count was observed. Uses
    # the same 5-minute dedup as GraphQL-sourced snapshots.
    if any(
        event.get(k) is not None
        for k in ("like_count", "retweet_count", "reply_count", "view_count")
    ):
        await _insert_engagement(db, {
            "tweet_id": tweet_id,
            "likes": event.get("like_count"),
            "retweets": event.get("retweet_count"),
            "replies": event.get("reply_count"),
            "views": event.get("view_count"),
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


# --- Interaction v2 handlers ---------------------------------------------
# Pattern copied from _handle_interaction: stub the referenced tweet row if
# tweet_id is present, then append. All handlers tolerate missing session_id.


async def _ensure_tweet_stub(db: aiosqlite.Connection, tweet_id: str, now: str) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO tweets (tweet_id, captured_at, last_updated_at) VALUES (?, ?, ?)",
        (tweet_id, now, now),
    )


async def _ensure_session_stub(
    db: aiosqlite.Connection, session_id: str | None, now: str
) -> None:
    """Guarantee a session row exists so FK inserts into new interaction tables
    succeed even when the DB was wiped while the SW held an in-memory session_id.
    Mirrors the pattern in _handle_impression_end."""
    if not session_id:
        return
    await db.execute(
        "INSERT OR IGNORE INTO sessions (session_id, started_at, total_dwell_ms, tweet_count) "
        "VALUES (?, ?, 0, 0)",
        (session_id, now),
    )


async def _handle_link_click(db: aiosqlite.Connection, event: dict, now: str) -> None:
    url = event.get("url")
    if not url:
        raise ValueError("link_click missing url")
    tweet_id = event.get("tweet_id")
    if tweet_id:
        await _ensure_tweet_stub(db, tweet_id, now)
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO link_clicks
          (tweet_id, session_id, url, domain, link_kind, modifiers, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tweet_id,
            event.get("session_id"),
            url,
            event.get("domain"),
            event.get("link_kind"),
            event.get("modifiers"),
            event.get("timestamp") or now,
        ),
    )


async def _handle_media_open(db: aiosqlite.Connection, event: dict, now: str) -> None:
    tweet_id = event.get("tweet_id")
    if not tweet_id:
        raise ValueError("media_open missing tweet_id")
    await _ensure_tweet_stub(db, tweet_id, now)
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO media_events
          (tweet_id, session_id, media_kind, media_index, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            tweet_id,
            event.get("session_id"),
            event.get("media_kind"),
            event.get("media_index"),
            event.get("timestamp") or now,
        ),
    )


async def _handle_text_selection(db: aiosqlite.Connection, event: dict, now: str) -> None:
    text = event.get("text")
    if not text:
        raise ValueError("text_selection missing text")
    # Server-side cap as defense-in-depth against a malformed client.
    text = text[:500]
    tweet_id = event.get("tweet_id")
    if tweet_id:
        await _ensure_tweet_stub(db, tweet_id, now)
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO text_selections
          (tweet_id, session_id, text, via, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            tweet_id,
            event.get("session_id"),
            text,
            event.get("via"),
            event.get("timestamp") or now,
        ),
    )


async def _handle_scroll_burst(db: aiosqlite.Connection, event: dict, now: str) -> None:
    if event.get("started_at") is None or event.get("ended_at") is None:
        raise ValueError("scroll_burst missing started_at or ended_at")
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO scroll_bursts
          (session_id, feed_source, started_at, ended_at, duration_ms,
           start_y, end_y, delta_y, reversals_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.get("session_id"),
            event.get("feed_source"),
            event.get("started_at"),
            event.get("ended_at"),
            event.get("duration_ms"),
            event.get("start_y"),
            event.get("end_y"),
            event.get("delta_y"),
            event.get("reversals_count") or 0,
        ),
    )


async def _handle_nav_change(db: aiosqlite.Connection, event: dict, now: str) -> None:
    to_path = event.get("to_path")
    if not to_path:
        raise ValueError("nav_change missing to_path")
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO nav_events
          (session_id, from_path, to_path, feed_source_before, feed_source_after, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event.get("session_id"),
            event.get("from_path"),
            to_path,
            event.get("feed_source_before"),
            event.get("feed_source_after"),
            event.get("timestamp") or now,
        ),
    )


async def _handle_window_state(db: aiosqlite.Connection, event: dict, now: str) -> None:
    """Tab visibility / window focus transition. state is one of
    visible|hidden|focused|blurred."""
    state = event.get("state")
    if state not in {"visible", "hidden", "focused", "blurred"}:
        raise ValueError(f"window_state invalid state: {state!r}")
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        "INSERT INTO window_state_events (session_id, state, timestamp) "
        "VALUES (?, ?, ?)",
        (event.get("session_id"), state, event.get("timestamp") or now),
    )


async def _handle_button_hover_intent(db: aiosqlite.Connection, event: dict, now: str) -> None:
    """Cursor hovered an engagement button without clicking. action is one of
    like|retweet|reply|bookmark. dwell_ms is how long the cursor lingered."""
    action = event.get("action")
    tweet_id = event.get("tweet_id")
    dwell = event.get("dwell_ms")
    if action not in {"like", "retweet", "reply", "bookmark"}:
        raise ValueError(f"button_hover_intent invalid action: {action!r}")
    if not tweet_id:
        raise ValueError("button_hover_intent missing tweet_id")
    if not isinstance(dwell, int) or dwell < 0:
        raise ValueError("button_hover_intent invalid dwell_ms")
    await _ensure_tweet_stub(db, tweet_id, now)
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        "INSERT INTO button_hover_intent (session_id, tweet_id, action, dwell_ms, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            event.get("session_id"),
            tweet_id,
            action,
            dwell,
            event.get("timestamp") or now,
        ),
    )


_CURSOR_TRAIL_MAX_POINTS = 200
_VIDEO_EVENT_TYPES = {"play", "pause", "ended", "seeked", "timeupdate"}


async def _handle_cursor_trail(db: aiosqlite.Connection, event: dict, now: str) -> None:
    """Per-impression cursor trail within a tweet's bounding box.

    Validates the points payload: array of [x, y, t] numeric tuples where
    x/y ∈ [0, 1] (relative to the article box) and t >= 0 ms. Trails longer
    than ``_CURSOR_TRAIL_MAX_POINTS`` are truncated at the server boundary
    so a misbehaving client can't spam the row.
    """
    tweet_id = event.get("tweet_id")
    if not tweet_id:
        raise ValueError("cursor_trail missing tweet_id")
    raw_points = event.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise ValueError("cursor_trail missing points array")

    clean: list[list[float]] = []
    for p in raw_points[:_CURSOR_TRAIL_MAX_POINTS]:
        if not isinstance(p, (list, tuple)) or len(p) != 3:
            continue
        try:
            x, y, t = float(p[0]), float(p[1]), float(p[2])
        except (TypeError, ValueError):
            continue
        # Clamp x/y to the tweet box. Drop negative times.
        if t < 0:
            continue
        clean.append([
            max(0.0, min(1.0, x)),
            max(0.0, min(1.0, y)),
            t,
        ])
    if not clean:
        raise ValueError("cursor_trail: no valid points after validation")

    await _ensure_tweet_stub(db, tweet_id, now)
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO cursor_trails
          (session_id, tweet_id, point_count, points_json, first_seen_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            event.get("session_id"),
            tweet_id,
            len(clean),
            json.dumps(clean, separators=(",", ":")),
            event.get("first_seen_at") or event.get("timestamp") or now,
        ),
    )


async def _handle_video_event(db: aiosqlite.Connection, event: dict, now: str) -> None:
    """HTMLVideoElement event: play | pause | ended | seeked | timeupdate."""
    event_type = event.get("event_type")
    tweet_id = event.get("tweet_id")
    if event_type not in _VIDEO_EVENT_TYPES:
        raise ValueError(f"video_event invalid event_type: {event_type!r}")
    if not tweet_id:
        raise ValueError("video_event missing tweet_id")

    def _num(x):
        if x is None:
            return None
        try:
            v = float(x)
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None

    await _ensure_tweet_stub(db, tweet_id, now)
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO video_events
          (session_id, tweet_id, media_index, event_type, current_time_s, duration_s, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.get("session_id"),
            tweet_id,
            event.get("media_index"),
            event_type,
            _num(event.get("current_time")),
            _num(event.get("duration")),
            event.get("timestamp") or now,
        ),
    )


_DRAFT_TEXT_MAX_CHARS = 5000


async def _handle_draft_activity(db: aiosqlite.Connection, event: dict, now: str) -> None:
    """Compose-box activity. Counts-only by default. `text_final` is stored
    only when the extension sent it (user opted in via popup toggle)."""
    def _nonneg_int(v, label: str) -> int:
        if not isinstance(v, int) or v < 0:
            raise ValueError(f"draft_activity invalid {label}: {v!r}")
        return v

    keystroke_count = _nonneg_int(event.get("keystroke_count"), "keystroke_count")
    char_count_final = _nonneg_int(event.get("char_count_final"), "char_count_final")
    delete_count = _nonneg_int(event.get("delete_count"), "delete_count")
    duration_ms = _nonneg_int(event.get("duration_ms"), "duration_ms")
    discarded_raw = event.get("discarded")
    if not isinstance(discarded_raw, bool):
        raise ValueError("draft_activity missing/invalid discarded")
    discarded = 1 if discarded_raw else 0

    text_final = event.get("text_final")
    if text_final is not None:
        if not isinstance(text_final, str):
            raise ValueError("draft_activity text_final must be a string")
        # Server-side cap as defense-in-depth against a misbehaving client.
        text_final = text_final[:_DRAFT_TEXT_MAX_CHARS]

    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO draft_activity
          (session_id, keystroke_count, char_count_final, delete_count,
           duration_ms, discarded, text_final, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.get("session_id"),
            keystroke_count,
            char_count_final,
            delete_count,
            duration_ms,
            discarded,
            text_final,
            event.get("timestamp") or now,
        ),
    )


async def _handle_relationship_change(db: aiosqlite.Connection, event: dict, now: str) -> None:
    action = event.get("action")
    target = event.get("target_user_id")
    if not action or not target:
        raise ValueError("relationship_change missing action or target_user_id")
    await _ensure_session_stub(db, event.get("session_id"), now)
    await db.execute(
        """
        INSERT INTO relationship_changes
          (session_id, target_user_id, action, timestamp)
        VALUES (?, ?, ?, ?)
        """,
        (
            event.get("session_id"),
            target,
            action,
            event.get("timestamp") or now,
        ),
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
    "link_click": _handle_link_click,
    "media_open": _handle_media_open,
    "text_selection": _handle_text_selection,
    "scroll_burst": _handle_scroll_burst,
    "nav_change": _handle_nav_change,
    "relationship_change": _handle_relationship_change,
    "window_state": _handle_window_state,
    "button_hover_intent": _handle_button_hover_intent,
    "cursor_trail": _handle_cursor_trail,
    "video_event": _handle_video_event,
    "draft_activity": _handle_draft_activity,
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
