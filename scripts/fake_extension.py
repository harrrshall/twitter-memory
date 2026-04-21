"""Simulates what the Chrome extension would POST over a browsing session.

Talks to a running backend at 127.0.0.1:8765. Exercises the full wire format:
batches of events, mixed event types, retries through a backend restart.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date as date_cls, datetime, timedelta, timezone

import httpx

from tests.fixtures import home_timeline_payload, make_tweet, make_user


BACKEND = "http://127.0.0.1:8765"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


async def post_batch(client: httpx.AsyncClient, events: list[dict]) -> dict:
    r = await client.post(f"{BACKEND}/ingest", json={"events": events}, timeout=5.0)
    r.raise_for_status()
    return r.json()


async def simulate(day: date_cls) -> None:
    # Build up the same GraphQL + events the extension would produce.
    alice = make_user("100", "alice", "Alice Andersen", followers=12_450)
    bob = make_user("200", "bob", "Bob Baxter", followers=3_201)
    carol = make_user("300", "carol", "Carol Chen", followers=58_900)
    dave = make_user("400", "dave", "Dave Dey", followers=781)

    # Base time: 9:00 UTC on target day
    t0 = datetime(day.year, day.month, day.day, 9, 0, 0, tzinfo=timezone.utc)

    tweets = [
        make_tweet("2001", alice, "morning thoughts about streaming arch.",
                   created_at=f"Mon Apr {day.day:02d} 09:00:00 +0000 2026",
                   conversation_id="2001", likes=120, retweets=15, views=9_100),
        make_tweet("2002", bob, "counterpoint: batch still wins on cost.",
                   created_at=f"Mon Apr {day.day:02d} 09:08:00 +0000 2026",
                   reply_to_tweet_id="2001", reply_to_user_id="100",
                   conversation_id="2001", likes=8, retweets=0),
        make_tweet("2003", carol, "fascinating thread on attention is all you need retrospective 1/",
                   created_at=f"Mon Apr {day.day:02d} 10:30:00 +0000 2026",
                   conversation_id="2003", likes=5_200, retweets=1_400, views=820_000),
        make_tweet("2004", carol, "2/ the thing people missed was that positional encoding...",
                   created_at=f"Mon Apr {day.day:02d} 10:32:00 +0000 2026",
                   reply_to_tweet_id="2003", reply_to_user_id="300",
                   conversation_id="2003", likes=2_100),
        make_tweet("2005", carol, "3/ and now we see it everywhere.",
                   created_at=f"Mon Apr {day.day:02d} 10:34:00 +0000 2026",
                   reply_to_tweet_id="2004", reply_to_user_id="300",
                   conversation_id="2003", likes=1_800),
        make_tweet("2006", dave, "pet alpaca update",
                   created_at=f"Mon Apr {day.day:02d} 14:12:00 +0000 2026",
                   conversation_id="2006", likes=22, views=890),
    ]

    sid1 = str(uuid.uuid4())
    sid2 = str(uuid.uuid4())

    async with httpx.AsyncClient() as client:
        # 1) Extension boots, session starts, GraphQL intercepts the home timeline
        r = await post_batch(client, [
            {"type": "session_start", "session_id": sid1, "timestamp": _iso(t0)},
            {"type": "graphql_payload", "operation_name": "HomeTimeline",
             "payload": home_timeline_payload([tweets[0], tweets[1]])},
        ])
        print("batch 1:", r)

        # 2) User scrolls: impressions on t1, t2
        events = []
        for i, (tid, dwell, feed) in enumerate([("2001", 4_200, "for_you"),
                                                  ("2002", 1_800, "thread")]):
            events.append({
                "type": "impression_end", "session_id": sid1, "tweet_id": tid,
                "first_seen_at": _iso(t0 + timedelta(minutes=1 + i)),
                "dwell_ms": dwell, "feed_source": feed,
            })
        events.append({
            "type": "interaction", "session_id": sid1, "tweet_id": "2001",
            "action": "like", "timestamp": _iso(t0 + timedelta(minutes=5)),
        })
        events.append({
            "type": "search", "session_id": sid1, "query": "streaming vs batch",
            "timestamp": _iso(t0 + timedelta(minutes=10)),
        })
        r = await post_batch(client, events)
        print("batch 2:", r)

        # 3) Thread expansion loads more graphql + more impressions
        r = await post_batch(client, [
            {"type": "graphql_payload", "operation_name": "TweetDetail",
             "payload": home_timeline_payload([tweets[2], tweets[3], tweets[4]])},
            *[
                {"type": "impression_end", "session_id": sid1, "tweet_id": tid,
                 "first_seen_at": _iso(t0 + timedelta(minutes=20 + i)),
                 "dwell_ms": 5_000, "feed_source": "thread"}
                for i, tid in enumerate(["2003", "2004", "2005"])
            ],
            {"type": "interaction", "session_id": sid1, "tweet_id": "2003",
             "action": "bookmark", "timestamp": _iso(t0 + timedelta(minutes=25))},
        ])
        print("batch 3:", r)

        # 4) Session ends
        r = await post_batch(client, [
            {"type": "session_end", "session_id": sid1,
             "timestamp": _iso(t0 + timedelta(minutes=30)),
             "total_dwell_ms": 30 * 60 * 1000, "tweet_count": 5,
             "feeds_visited": ["for_you", "thread"]},
        ])
        print("batch 4:", r)

        # 5) Second session, afternoon, quick scroll
        t1 = t0 + timedelta(hours=5)
        r = await post_batch(client, [
            {"type": "session_start", "session_id": sid2, "timestamp": _iso(t1)},
            {"type": "graphql_payload", "operation_name": "HomeTimeline",
             "payload": home_timeline_payload([tweets[5]])},
            {"type": "impression_end", "session_id": sid2, "tweet_id": "2006",
             "first_seen_at": _iso(t1 + timedelta(minutes=2)),
             "dwell_ms": 2_100, "feed_source": "for_you"},
            {"type": "interaction", "session_id": sid2, "tweet_id": "2006",
             "action": "like", "timestamp": _iso(t1 + timedelta(minutes=3))},
            {"type": "session_end", "session_id": sid2,
             "timestamp": _iso(t1 + timedelta(minutes=10)),
             "total_dwell_ms": 10 * 60 * 1000, "tweet_count": 1,
             "feeds_visited": ["for_you"]},
        ])
        print("batch 5:", r)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    day = date_cls.fromisoformat(arg) if arg else datetime.now(timezone.utc).date()
    asyncio.run(simulate(day))
