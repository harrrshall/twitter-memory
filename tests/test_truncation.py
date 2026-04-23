"""Verify the 200KB inline cap under the v3 directory layout.

Under v3 the `content` field inlines only `digest.md`. On a heavy day:
- `digest.md` stays small (TL;DR + summary + top-15 authors + threads)
- `tweets.md` + `timeline.md` can each blow past 200KB
- `byte_size_total_md` reflects all four .md files combined

So the test checks that even when the overall export is huge, the digest
still fits inline — that's the whole point of the split.
"""
from datetime import date
from pathlib import Path

import pytest

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


async def test_heavy_day_digest_still_fits_inline(tmp_data_dir, monkeypatch):
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

    # Enough tweets to make tweets.md + timeline.md collectively exceed 200KB.
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
    for i in range(0, len(impressions), 100):
        await ingest_batch(db, impressions[i:i + 100])
    await db.close()

    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))

    # Heavy day: total markdown across the four files crosses 200KB.
    assert res["byte_size_total_md"] > 200_000
    # But the digest stays small and fits inline — that's the whole point of
    # the split. Agents scanning "what happened yesterday" do not need to
    # paginate through the full timeline.
    assert res["byte_size_digest"] < 200_000
    assert res["truncated"] is False
    assert res["content"]

    # Verify files on disk are complete and named correctly.
    dir_path = Path(res["dir_path"])
    assert dir_path.is_dir()
    for name in ("digest.md", "tweets.md", "activity.md", "timeline.md", "data.json"):
        assert (dir_path / name).exists(), f"missing {name}"

    # The ranked tweets file holds all 700 tweet links
    tweets_text = Path(res["tweets_path"]).read_text(encoding="utf-8")
    assert tweets_text.count("| @heavy | t10") + tweets_text.count("| @heavy | t1") >= 50
    # The timeline file holds all 700 impression lines
    timeline_text = Path(res["timeline_path"]).read_text(encoding="utf-8")
    assert timeline_text.count("**impression**") >= 700
    # No ## Impressions heading anywhere — raw impressions live only in JSON
    for key in ("digest_path", "tweets_path", "activity_path", "timeline_path"):
        assert "## Impressions" not in Path(res[key]).read_text(encoding="utf-8")
