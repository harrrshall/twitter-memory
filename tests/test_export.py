import asyncio
from datetime import date

import pytest

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


async def _seed(tmp_data_dir):
    from backend.db import init_db, connect
    from backend.ingest import ingest_batch
    await init_db()
    db = await connect()
    alice = make_user("100", "alice", "Alice")
    bob = make_user("200", "bob", "Bob")
    t1 = make_tweet("1001", alice, "hello world", likes=10, views=500, conversation_id="1001")
    t2 = make_tweet("1002", bob, "hi alice",
                    reply_to_tweet_id="1001", reply_to_user_id="100", conversation_id="1001")
    t3 = make_tweet("1003", alice, "thanks bob",
                    reply_to_tweet_id="1002", reply_to_user_id="200", conversation_id="1001")
    await ingest_batch(db, [{"type": "graphql_payload", "operation_name": "HomeTimeline",
                             "payload": home_timeline_payload([t1, t2, t3])}])
    events = [
        {"type": "session_start", "session_id": "s1", "timestamp": "2026-04-21T09:10:00+00:00"},
        {"type": "impression_end", "session_id": "s1", "tweet_id": "1001",
         "first_seen_at": "2026-04-21T09:14:00+00:00", "dwell_ms": 2500, "feed_source": "for_you"},
        {"type": "impression_end", "session_id": "s1", "tweet_id": "1002",
         "first_seen_at": "2026-04-21T09:16:00+00:00", "dwell_ms": 1500, "feed_source": "thread"},
        {"type": "impression_end", "session_id": "s1", "tweet_id": "1003",
         "first_seen_at": "2026-04-21T09:18:00+00:00", "dwell_ms": 1200, "feed_source": "thread"},
        {"type": "interaction", "session_id": "s1", "tweet_id": "1001",
         "action": "like", "timestamp": "2026-04-21T09:15:00+00:00"},
        {"type": "search", "session_id": "s1", "query": "hello",
         "timestamp": "2026-04-21T09:13:00+00:00"},
        {"type": "session_end", "session_id": "s1", "timestamp": "2026-04-21T09:30:00+00:00",
         "total_dwell_ms": 20 * 60 * 1000, "tweet_count": 3, "feeds_visited": ["for_you", "thread"]},
    ]
    await ingest_batch(db, events)
    await db.close()


async def test_full_export(tmp_data_dir, monkeypatch):
    # The target date here must match the UTC date of the seeded timestamps.
    # We set the timezone to UTC so the local day boundary lines up.
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))

    assert res["sections_included"] == settings.ALL_SECTIONS
    assert res["tweet_count"] == 3
    assert res["interaction_count"] == 1
    assert res["session_count"] == 1
    assert res["search_count"] == 1
    assert not res["truncated"]
    assert res["content"]
    md = res["content"]
    assert "# Twitter — 2026-04-21" in md
    assert "## Summary" in md
    assert "## Sessions" in md
    assert "## Searches" in md
    assert "`hello`" in md
    assert "## Interactions" in md
    assert "liked** @alice" in md
    assert "## Threads" in md
    assert "## Impressions" in md
    assert "https://x.com/alice/status/1001" in md

    # File was written
    from pathlib import Path
    p = Path(res["file_path"])
    assert p.exists()
    assert p.read_text(encoding="utf-8") == md


async def test_exclude_section(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21), exclude=["impressions", "threads"])
    assert "impressions" not in res["sections_included"]
    assert "threads" not in res["sections_included"]
    assert "## Impressions" not in res["content"]
    assert "## Threads" not in res["content"]
    # Summary still there
    assert "## Summary" in res["content"]


async def test_exclude_unknown_raises(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed(tmp_data_dir)
    with pytest.raises(ValueError, match="unknown section"):
        export.write_export(settings.DB_PATH, date(2026, 4, 21), exclude=["nope"])


async def test_empty_day(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings
    from backend.db import init_db
    await init_db()
    res = export.write_export(settings.DB_PATH, date(2020, 1, 1))
    assert res["tweet_count"] == 0
    assert res["interaction_count"] == 0
    assert "# Twitter — 2020-01-01" in res["content"]
