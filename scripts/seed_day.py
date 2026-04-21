"""Seed a realistic browsing day into the DB for testing export_day.

Usage:
    TWITTER_MEMORY_DATA=./data python -m scripts.seed_day [YYYY-MM-DD]

If no date given, uses today's local date.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date as date_cls, datetime, timedelta, timezone

from backend.db import connect, init_db
from backend.ingest import ingest_batch
from tests.fixtures import home_timeline_payload, make_tweet, make_user


def _at(day: date_cls, hh: int, mm: int, ss: int = 0) -> str:
    local = datetime.now().astimezone().tzinfo
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=local).astimezone(timezone.utc).isoformat()


async def seed(day: date_cls) -> None:
    await init_db()
    db = await connect()

    alice = make_user("100", "alice", "Alice Andersen", followers=12_450)
    bob = make_user("200", "bob", "Bob Baxter", followers=3_201)
    carol = make_user("300", "carol", "Carol Chen", followers=58_900)
    dave = make_user("400", "dave", "Dave Dey", followers=781)

    tweets = [
        make_tweet(
            "1001", alice,
            "Rust's async story is finally coming together. Tokio + axum is legit production-ready.",
            created_at="Mon Apr 21 09:14:00 +0000 2026",
            conversation_id="1001", likes=421, retweets=37, views=18_211,
        ),
        make_tweet(
            "1002", bob,
            "hard disagree — borrow checker still bites on streams.",
            created_at="Mon Apr 21 09:20:00 +0000 2026",
            reply_to_tweet_id="1001", reply_to_user_id="100", conversation_id="1001",
            likes=12, retweets=1,
        ),
        make_tweet(
            "1003", alice,
            "Fair, but pin/unpin ergonomics shipped in 1.82. Have you tried the new pattern?",
            created_at="Mon Apr 21 09:25:00 +0000 2026",
            reply_to_tweet_id="1002", reply_to_user_id="200", conversation_id="1001",
            likes=33, views=4_512,
        ),
        make_tweet(
            "1004", alice,
            "here's a worked example: https://github.com/alice/async-demo",
            created_at="Mon Apr 21 09:27:00 +0000 2026",
            reply_to_tweet_id="1003", reply_to_user_id="100", conversation_id="1001",
            likes=18,
        ),
        make_tweet(
            "2001", carol,
            "thread on why latency > throughput for user-facing systems. 1/",
            created_at="Mon Apr 21 11:02:00 +0000 2026",
            conversation_id="2001", likes=2_410, retweets=512, views=142_900,
        ),
        make_tweet(
            "3001", dave,
            "shipped my first side project today — mushroom foraging app for the SF bay area 🍄",
            created_at="Mon Apr 21 13:41:00 +0000 2026",
            conversation_id="3001", likes=87, retweets=4, views=3_210,
        ),
    ]

    payload = home_timeline_payload(tweets)
    await ingest_batch(db, [{"type": "graphql_payload", "operation_name": "HomeTimeline", "payload": payload}])

    # Session 1 - morning
    sid1 = str(uuid.uuid4())
    # Session 2 - midday
    sid2 = str(uuid.uuid4())

    events: list[dict] = [
        {"type": "session_start", "session_id": sid1, "timestamp": _at(day, 9, 10)},
        {"type": "impression_end", "session_id": sid1, "tweet_id": "1001",
         "first_seen_at": _at(day, 9, 14, 10), "dwell_ms": 4200, "feed_source": "for_you"},
        {"type": "impression_end", "session_id": sid1, "tweet_id": "1002",
         "first_seen_at": _at(day, 9, 20, 15), "dwell_ms": 1800, "feed_source": "thread"},
        {"type": "impression_end", "session_id": sid1, "tweet_id": "1003",
         "first_seen_at": _at(day, 9, 25, 30), "dwell_ms": 3600, "feed_source": "thread"},
        {"type": "impression_end", "session_id": sid1, "tweet_id": "1004",
         "first_seen_at": _at(day, 9, 27, 2), "dwell_ms": 2700, "feed_source": "thread"},
        {"type": "interaction", "session_id": sid1, "tweet_id": "1001",
         "action": "like", "timestamp": _at(day, 9, 17, 45)},
        {"type": "interaction", "session_id": sid1, "tweet_id": "1004",
         "action": "bookmark", "timestamp": _at(day, 9, 28, 10)},
        {"type": "search", "session_id": sid1, "query": "rust async runtime",
         "timestamp": _at(day, 9, 22, 0)},
        {"type": "session_end", "session_id": sid1, "timestamp": _at(day, 9, 31),
         "total_dwell_ms": 17 * 60 * 1000, "tweet_count": 142,
         "feeds_visited": ["for_you", "thread"]},

        {"type": "session_start", "session_id": sid2, "timestamp": _at(day, 11, 0)},
        {"type": "impression_end", "session_id": sid2, "tweet_id": "2001",
         "first_seen_at": _at(day, 11, 2, 30), "dwell_ms": 8_400, "feed_source": "for_you"},
        {"type": "impression_end", "session_id": sid2, "tweet_id": "3001",
         "first_seen_at": _at(day, 13, 41, 20), "dwell_ms": 5_100, "feed_source": "for_you"},
        {"type": "interaction", "session_id": sid2, "tweet_id": "2001",
         "action": "reply", "timestamp": _at(day, 11, 5, 0)},
        {"type": "interaction", "session_id": sid2, "tweet_id": "3001",
         "action": "like", "timestamp": _at(day, 13, 42, 10)},
        {"type": "search", "session_id": sid2, "query": "alpaca farming",
         "timestamp": _at(day, 11, 5, 30)},
        {"type": "session_end", "session_id": sid2, "timestamp": _at(day, 13, 55),
         "total_dwell_ms": 30 * 60 * 1000, "tweet_count": 88,
         "feeds_visited": ["for_you"]},
    ]

    res = await ingest_batch(db, events)
    print("seeded:", res)
    await db.close()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    day = date_cls.fromisoformat(arg) if arg else datetime.now().date()
    asyncio.run(seed(day))
