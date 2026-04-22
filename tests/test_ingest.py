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


# ---------- Interaction v2 handlers ---------------------------------------


async def test_link_click_happy_path(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{
        "type": "link_click",
        "session_id": "s1",
        "tweet_id": "2001",
        "url": "https://example.com/article",
        "domain": "example.com",
        "link_kind": "external",
        "modifiers": "meta,middle",
        "timestamp": "2026-04-22T10:00:00+00:00",
    }])
    assert r["accepted"] == 1
    row = await (await db.execute(
        "SELECT url, domain, link_kind, modifiers, tweet_id FROM link_clicks"
    )).fetchone()
    assert row["url"] == "https://example.com/article"
    assert row["domain"] == "example.com"
    assert row["link_kind"] == "external"
    assert row["modifiers"] == "meta,middle"
    # Stub tweet row created
    t = await (await db.execute("SELECT tweet_id FROM tweets WHERE tweet_id='2001'")).fetchone()
    assert t is not None
    await db.close()


async def test_link_click_without_tweet_id_still_inserts(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    # External link clicked outside any tweet article (e.g. sidebar link).
    r = await _ingest(db, [{
        "type": "link_click",
        "session_id": "s1",
        "url": "https://help.twitter.com/foo",
        "domain": "help.twitter.com",
        "link_kind": "external",
        "modifiers": "",
    }])
    assert r["accepted"] == 1
    row = await (await db.execute("SELECT tweet_id FROM link_clicks")).fetchone()
    assert row["tweet_id"] is None
    await db.close()


async def test_link_click_missing_url_rejected(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{"type": "link_click", "session_id": "s1"}])
    assert r["accepted"] == 0
    assert len(r["errors"]) == 1
    assert "missing url" in r["errors"][0]["error"]
    await db.close()


async def test_media_open_happy_path(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{
        "type": "media_open",
        "session_id": "s1",
        "tweet_id": "3001",
        "media_kind": "image",
        "media_index": 2,
        "timestamp": "2026-04-22T10:05:00+00:00",
    }])
    assert r["accepted"] == 1
    row = await (await db.execute(
        "SELECT tweet_id, media_kind, media_index FROM media_events"
    )).fetchone()
    assert row["tweet_id"] == "3001"
    assert row["media_kind"] == "image"
    assert row["media_index"] == 2
    await db.close()


async def test_media_open_missing_tweet_id_rejected(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{"type": "media_open", "media_kind": "video"}])
    assert r["accepted"] == 0
    assert "missing tweet_id" in r["errors"][0]["error"]
    await db.close()


async def test_text_selection_truncates_and_stores(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    long_text = "x" * 900
    r = await _ingest(db, [{
        "type": "text_selection",
        "session_id": "s1",
        "tweet_id": "4001",
        "text": long_text,
        "via": "copy",
        "timestamp": "2026-04-22T10:10:00+00:00",
    }])
    assert r["accepted"] == 1
    row = await (await db.execute(
        "SELECT tweet_id, text, via FROM text_selections"
    )).fetchone()
    assert row["tweet_id"] == "4001"
    assert len(row["text"]) == 500  # server-side cap
    assert row["via"] == "copy"
    await db.close()


async def test_text_selection_missing_text_rejected(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{"type": "text_selection", "tweet_id": "4002"}])
    assert r["accepted"] == 0
    assert "missing text" in r["errors"][0]["error"]
    await db.close()


async def test_scroll_burst_happy_path(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{
        "type": "scroll_burst",
        "session_id": "s1",
        "feed_source": "for_you",
        "started_at": "2026-04-22T10:15:00+00:00",
        "ended_at": "2026-04-22T10:15:04+00:00",
        "duration_ms": 4000,
        "start_y": 0,
        "end_y": 5200,
        "delta_y": 5200,
        "reversals_count": 1,
    }])
    assert r["accepted"] == 1
    row = await (await db.execute(
        "SELECT feed_source, delta_y, reversals_count FROM scroll_bursts"
    )).fetchone()
    assert row["feed_source"] == "for_you"
    assert row["delta_y"] == 5200
    assert row["reversals_count"] == 1
    await db.close()


async def test_scroll_burst_missing_times_rejected(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{"type": "scroll_burst", "session_id": "s1", "start_y": 0}])
    assert r["accepted"] == 0
    assert "started_at or ended_at" in r["errors"][0]["error"]
    await db.close()


async def test_nav_change_happy_path(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{
        "type": "nav_change",
        "session_id": "s1",
        "from_path": "/home",
        "to_path": "/alice/status/12345",
        "feed_source_before": "for_you",
        "feed_source_after": "thread",
        "timestamp": "2026-04-22T10:20:00+00:00",
    }])
    assert r["accepted"] == 1
    row = await (await db.execute(
        "SELECT from_path, to_path, feed_source_after FROM nav_events"
    )).fetchone()
    assert row["from_path"] == "/home"
    assert row["to_path"] == "/alice/status/12345"
    assert row["feed_source_after"] == "thread"
    await db.close()


async def test_nav_change_missing_to_path_rejected(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{"type": "nav_change", "from_path": "/home"}])
    assert r["accepted"] == 0
    assert "missing to_path" in r["errors"][0]["error"]
    await db.close()


async def test_relationship_change_happy_path(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{
        "type": "relationship_change",
        "session_id": "s1",
        "target_user_id": "555",
        "action": "follow",
        "timestamp": "2026-04-22T10:25:00+00:00",
    }])
    assert r["accepted"] == 1
    row = await (await db.execute(
        "SELECT target_user_id, action FROM relationship_changes"
    )).fetchone()
    assert row["target_user_id"] == "555"
    assert row["action"] == "follow"
    await db.close()


async def test_dom_tweet_full_record(tmp_data_dir):
    # DOM-only path: single event should populate tweets, authors, and an
    # engagement_snapshots row in one shot.
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{
        "type": "dom_tweet",
        "tweet_id": "9001",
        "author_handle": "alice",
        "author_display": "Alice A.",
        "text": "hello world",
        "created_at_iso": "2026-04-22T09:00:00.000Z",
        "like_count": 12,
        "retweet_count": 3,
        "reply_count": 1,
        "media_json": '[{"type":"image","url":"https://pbs.twimg.com/media/x.jpg"}]',
        "quoted_tweet_id": "8000",
    }])
    assert r["accepted"] == 1, r
    tweet = await (await db.execute(
        "SELECT author_id, text, media_json, quoted_tweet_id FROM tweets WHERE tweet_id = '9001'"
    )).fetchone()
    assert tweet["author_id"] == "dom-alice"
    assert tweet["text"] == "hello world"
    assert tweet["quoted_tweet_id"] == "8000"
    assert "pbs.twimg.com" in tweet["media_json"]
    author = await (await db.execute(
        "SELECT handle, display_name FROM authors WHERE user_id = 'dom-alice'"
    )).fetchone()
    assert author["handle"] == "alice"
    assert author["display_name"] == "Alice A."
    snap = await (await db.execute(
        "SELECT likes, retweets, replies FROM engagement_snapshots WHERE tweet_id = '9001'"
    )).fetchone()
    assert snap["likes"] == 12
    assert snap["retweets"] == 3
    assert snap["replies"] == 1
    await db.close()


async def test_dom_tweet_no_engagement_skips_snapshot(tmp_data_dir):
    # A dom_tweet without any counts (e.g., a stub where aria-labels hadn't
    # hydrated yet) must still land the tweet row, but not create a bogus
    # all-null engagement snapshot.
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{
        "type": "dom_tweet",
        "tweet_id": "9002",
        "author_handle": "bob",
    }])
    assert r["accepted"] == 1
    n = await (await db.execute(
        "SELECT COUNT(*) AS c FROM engagement_snapshots WHERE tweet_id = '9002'"
    )).fetchone()
    assert n["c"] == 0
    await db.close()


async def test_relationship_change_missing_fields_rejected(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    r = await _ingest(db, [{"type": "relationship_change", "action": "follow"}])
    assert r["accepted"] == 0
    assert "target_user_id" in r["errors"][0]["error"]
    await db.close()


async def test_v2_event_id_dedup(tmp_data_dir):
    # Same event_id for a link_click must not insert a second row.
    from backend.db import init_db, connect
    await init_db()
    db = await connect()
    ev = {
        "type": "link_click",
        "event_id": "cccccccc-0000-4000-8000-000000000001",
        "session_id": "s1",
        "url": "https://example.com/x",
        "domain": "example.com",
        "link_kind": "external",
    }
    r1 = await _ingest(db, [ev])
    r2 = await _ingest(db, [ev])
    assert r1["accepted"] == 1
    assert r2["accepted"] == 0 and r2["skipped"] == 1
    row = await (await db.execute("SELECT COUNT(*) FROM link_clicks")).fetchone()
    assert row[0] == 1
    await db.close()
