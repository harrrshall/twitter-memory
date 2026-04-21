import asyncio

import pytest

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


async def _ingest(db, events):
    from backend.ingest import ingest_batch
    return await ingest_batch(db, events)


async def test_graphql_then_impression(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()

    alice = make_user("100", "alice")
    t = make_tweet("1001", alice, "hi", likes=5)
    res = await _ingest(db, [{"type": "graphql_payload", "operation_name": "HomeTimeline",
                              "payload": home_timeline_payload([t])}])
    assert res["accepted"] == 1

    res = await _ingest(db, [
        {"type": "session_start", "session_id": "s1", "timestamp": "2026-04-21T09:10:00+00:00"},
        {"type": "impression_end", "session_id": "s1", "tweet_id": "1001",
         "first_seen_at": "2026-04-21T09:14:00+00:00", "dwell_ms": 2000, "feed_source": "for_you"},
        {"type": "interaction", "session_id": "s1", "tweet_id": "1001",
         "action": "like", "timestamp": "2026-04-21T09:17:00+00:00"},
        {"type": "search", "session_id": "s1", "query": "rust async",
         "timestamp": "2026-04-21T09:22:00+00:00"},
        {"type": "session_end", "session_id": "s1", "timestamp": "2026-04-21T09:31:00+00:00",
         "total_dwell_ms": 1200000, "tweet_count": 1, "feeds_visited": ["for_you"]},
    ])
    assert res["accepted"] == 5

    # Verify rows
    row = await (await db.execute("SELECT COUNT(*) FROM impressions")).fetchone()
    assert row[0] == 1
    row = await (await db.execute("SELECT action FROM my_interactions")).fetchone()
    assert row[0] == "like"
    row = await (await db.execute("SELECT query FROM searches")).fetchone()
    assert row[0] == "rust async"
    row = await (await db.execute("SELECT session_id, ended_at FROM sessions")).fetchone()
    assert row[0] == "s1" and row[1] == "2026-04-21T09:31:00+00:00"
    await db.close()


async def test_unknown_event_type(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    res = await _ingest(db, [{"type": "bogus"}])
    assert res["accepted"] == 0
    assert "unknown event type" in res["errors"][0]["error"]
    await db.close()


async def test_impression_without_prior_tweet_row(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    # impression arrives before GraphQL — stub row should be created
    res = await _ingest(db, [
        {"type": "impression_end", "session_id": None, "tweet_id": "9999",
         "first_seen_at": "2026-04-21T09:14:00+00:00", "dwell_ms": 1000, "feed_source": "for_you"},
    ])
    assert res["accepted"] == 1
    row = await (await db.execute("SELECT tweet_id FROM tweets WHERE tweet_id='9999'")).fetchone()
    assert row is not None
    await db.close()


async def test_event_id_dedup_rejects_retry(tmp_data_dir):
    # Regression: service worker retries a batch after a network error. The
    # same event_id must not insert a second row.
    from backend.db import init_db, connect
    await init_db()
    db = await connect()

    ev = {
        "type": "impression_end",
        "event_id": "00000000-0000-4000-8000-000000000001",
        "session_id": None,
        "tweet_id": "42",
        "first_seen_at": "2026-04-21T09:14:00+00:00",
        "dwell_ms": 1500,
        "feed_source": "for_you",
    }
    r1 = await _ingest(db, [ev])
    r2 = await _ingest(db, [ev])  # identical retry

    assert r1 == {"accepted": 1, "skipped": 0, "errors": []}
    assert r2 == {"accepted": 0, "skipped": 1, "errors": []}

    # Only one impression row despite two POSTs.
    row = await (await db.execute("SELECT COUNT(*) FROM impressions WHERE tweet_id='42'")).fetchone()
    assert row[0] == 1
    row = await (await db.execute("SELECT COUNT(*) FROM event_log")).fetchone()
    assert row[0] == 1
    await db.close()


async def test_event_id_different_ids_both_land(tmp_data_dir):
    # Two tabs each saw the same tweet. Different event_ids — both count.
    from backend.db import init_db, connect
    await init_db()
    db = await connect()

    base = {
        "type": "impression_end",
        "session_id": None,
        "tweet_id": "77",
        "first_seen_at": "2026-04-21T09:14:00+00:00",
        "dwell_ms": 1500,
        "feed_source": "for_you",
    }
    r = await _ingest(db, [
        {**base, "event_id": "aaaaaaaa-0000-4000-8000-000000000001", "tab_id": 1},
        {**base, "event_id": "bbbbbbbb-0000-4000-8000-000000000002", "tab_id": 2},
    ])
    assert r["accepted"] == 2
    assert r["skipped"] == 0

    row = await (await db.execute("SELECT COUNT(*) FROM impressions WHERE tweet_id='77'")).fetchone()
    assert row[0] == 2
    row = await (await db.execute("SELECT COUNT(DISTINCT tab_id) FROM event_log WHERE tab_id IS NOT NULL")).fetchone()
    assert row[0] == 2
    await db.close()


async def test_legacy_events_without_event_id_still_ingest(tmp_data_dir):
    # Backwards compat: old events without event_id must still insert.
    from backend.db import init_db, connect
    await init_db()
    db = await connect()

    r = await _ingest(db, [
        {"type": "impression_end", "session_id": None, "tweet_id": "legacy-1",
         "first_seen_at": "2026-04-21T09:14:00+00:00", "dwell_ms": 1000, "feed_source": "for_you"},
    ])
    assert r["accepted"] == 1
    assert r["skipped"] == 0
    await db.close()
