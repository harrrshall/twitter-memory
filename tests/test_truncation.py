"""Verify the 200KB inline cap: heavy day produces truncated=True but file is complete."""
from datetime import date

import pytest

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


async def test_truncation_on_heavy_day(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from backend.db import init_db, connect
    from backend.ingest import ingest_batch
    from mcp_server import export, settings

    await init_db()
    db = await connect()
    author = make_user("1", "heavy", "Heavy User", followers=1)

    # Enough tweets to blow past 200KB. Each rendered impression is roughly
    # 250-350 bytes with author + text + engagement line.
    big_text = ("the quick brown fox jumps over the lazy dog. " * 8).strip()
    tweets = [make_tweet(str(1000 + i), author, big_text, likes=i, views=i * 10)
              for i in range(700)]
    await ingest_batch(db, [{"type": "graphql_payload", "operation_name": "HomeTimeline",
                             "payload": home_timeline_payload(tweets)}])
    impressions = [
        {"type": "impression_end", "session_id": None, "tweet_id": str(1000 + i),
         "first_seen_at": "2026-04-21T10:00:00+00:00", "dwell_ms": 1000,
         "feed_source": "for_you"}
        for i in range(700)
    ]
    # Batch in chunks of 100 to stay polite
    for i in range(0, len(impressions), 100):
        await ingest_batch(db, impressions[i:i + 100])
    await db.close()

    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    assert res["byte_size"] > 200_000
    assert res["truncated"] is True
    assert res["content"] == ""
    # But the file on disk is the full markdown
    from pathlib import Path
    p = Path(res["file_path"])
    assert p.exists()
    assert p.stat().st_size == res["byte_size"]
    # Verify real content landed in the file
    text = p.read_text(encoding="utf-8")
    assert "# Twitter — 2026-04-21" in text
    assert text.count("## Impressions") == 1
    assert text.count("[link](https://x.com/heavy/status/") >= 700
