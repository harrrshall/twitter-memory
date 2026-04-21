"""Enrichment queue population + allowlist.

Runs as a periodic asyncio task started in app lifespan. Every SWEEP_INTERVAL
seconds, scans the db for gaps (stub tweets, stale engagement, thread context
references, cold authors) and INSERTs into enrichment_queue. UNIQUE
constraint on (target_type, target_id, reason) makes re-sweeps idempotent.

The SW pulls work via /enrichment/next and reports back via /enrichment/complete.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite

log = logging.getLogger("twitter_memory.enrichment")

SWEEP_INTERVAL_S = 300  # 5 minutes

# Operations the SW is allowed to replay. READ-ONLY GraphQL queries only —
# never any mutation, even if we intercepted one organically. Anything not
# on this list is silently dropped from /enrichment/next.
REPLAY_ALLOWLIST = {
    "TweetDetail",
    "TweetResultByRestId",
    "UserByScreenName",
    "UserTweets",
    "UserByRestId",
}

# What operations can fulfill each reason, in preference order. /enrichment/next
# will pick the first one that has a captured template. If a reason has no
# captured template for any of its candidates yet (e.g., user hasn't clicked
# into a tweet this session), it stays queued waiting.
REASON_TO_OPS = {
    "stub_tweet": ["TweetResultByRestId", "TweetDetail"],
    "thread_context": ["TweetDetail"],
    "stale_engagement": ["TweetResultByRestId", "TweetDetail"],
    "cold_author": ["UserByRestId", "UserByScreenName"],
}

# Flattened list of every operation any reason might use — the allowlist is
# the union of what the mapping can request.
REASON_TO_OP = REASON_TO_OPS  # back-compat export name


async def populate_queue(db: aiosqlite.Connection) -> dict[str, int]:
    """One sweep. Returns counts inserted per reason."""
    now = datetime.now(timezone.utc).isoformat()
    added: dict[str, int] = {}

    async def _run(label: str, sql: str, params: tuple) -> None:
        cur = await db.execute(sql, params)
        added[label] = cur.rowcount or 0

    # stub_tweet: we saw the tweet_id (impression) but never got the payload
    await _run(
        "stub_tweet",
        """
        INSERT OR IGNORE INTO enrichment_queue
            (target_type, target_id, reason, priority, queued_at)
        SELECT 'tweet', t.tweet_id, 'stub_tweet', 100, ?
        FROM tweets t
        WHERE t.text IS NULL
          AND EXISTS (SELECT 1 FROM impressions i WHERE i.tweet_id = t.tweet_id)
        """,
        (now,),
    )

    # thread_context: tweets referenced as parent/quoted where target is a stub
    await _run(
        "thread_context",
        """
        INSERT OR IGNORE INTO enrichment_queue
            (target_type, target_id, reason, priority, queued_at)
        SELECT 'tweet', r.referenced_id, 'thread_context', 60, ?
        FROM (
            SELECT reply_to_tweet_id AS referenced_id FROM tweets WHERE reply_to_tweet_id IS NOT NULL
            UNION
            SELECT quoted_tweet_id FROM tweets WHERE quoted_tweet_id IS NOT NULL
        ) r
        JOIN tweets t ON t.tweet_id = r.referenced_id
        WHERE t.text IS NULL
        """,
        (now,),
    )

    # stale_engagement: no snapshot, or most recent > 7 days old, on recently-seen tweets
    await _run(
        "stale_engagement",
        """
        INSERT OR IGNORE INTO enrichment_queue
            (target_type, target_id, reason, priority, queued_at)
        SELECT 'tweet', t.tweet_id, 'stale_engagement', 40, ?
        FROM tweets t
        WHERE t.text IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM impressions i
              WHERE i.tweet_id = t.tweet_id
                AND i.first_seen_at > datetime('now','-14 days')
          )
          AND NOT EXISTS (
              SELECT 1 FROM engagement_snapshots es
              WHERE es.tweet_id = t.tweet_id
                AND es.captured_at > datetime('now','-7 days')
          )
        """,
        (now,),
    )

    # cold_author: authors we have a row for but no follower_count yet
    await _run(
        "cold_author",
        """
        INSERT OR IGNORE INTO enrichment_queue
            (target_type, target_id, reason, priority, queued_at)
        SELECT 'user', a.user_id, 'cold_author', 30, ?
        FROM authors a
        WHERE a.follower_count IS NULL
          AND a.user_id NOT LIKE 'dom-%'
        """,
        (now,),
    )

    await db.commit()
    return added


async def sweep_loop(db: aiosqlite.Connection, stop: asyncio.Event) -> None:
    """Long-running task. Runs populate_queue every SWEEP_INTERVAL_S."""
    # First sweep runs after a short delay so startup isn't blocked.
    try:
        await asyncio.wait_for(stop.wait(), timeout=30)
        return
    except asyncio.TimeoutError:
        pass
    while not stop.is_set():
        try:
            added = await populate_queue(db)
            if any(added.values()):
                log.info("enrichment sweep: %s", added)
        except Exception:
            log.exception("enrichment sweep failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            continue
