import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


def _read_all_md(res: dict) -> str:
    """Concatenate the four per-day markdown files into one string — keeps
    section-presence assertions concise across the split layout."""
    parts = [
        Path(res["digest_path"]).read_text(encoding="utf-8"),
        Path(res["tweets_path"]).read_text(encoding="utf-8"),
        Path(res["activity_path"]).read_text(encoding="utf-8"),
        Path(res["timeline_path"]).read_text(encoding="utf-8"),
    ]
    return "\n".join(parts)


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

    digest = Path(res["digest_path"]).read_text(encoding="utf-8")
    activity = Path(res["activity_path"]).read_text(encoding="utf-8")
    combined = _read_all_md(res)

    # Digest holds the TL;DR + summary + threads
    assert "# Twitter — 2026-04-21 — digest" in digest
    assert digest == res["content"]
    assert "## Summary" in digest
    assert "## Threads" in digest
    # Activity holds sessions/searches/interactions
    assert "## Sessions" in activity
    assert "## Searches" in activity
    assert "`hello`" in activity
    assert "## Interactions" in activity
    assert "liked** @alice" in activity
    # Per plan: the `## Impressions` markdown section is gone in v3 — data
    # lives in data.json only.
    assert "## Impressions" not in combined
    # Tweet URL still surfaces in the tweets ranked table
    assert "https://x.com/alice/status/1001" in combined

    # Shared schema file exists at exports root
    schema_file = Path(res["schema_path"])
    assert schema_file.exists()
    assert schema_file.parent == Path(res["dir_path"]).parent

    # Structured companion carries the raw impressions + timeline + threads + revisits
    data = json.loads(Path(res["json_path"]).read_text(encoding="utf-8"))
    for key in ("impressions", "threads", "timeline", "repeat_exposure", "revisits"):
        assert key in data, f"missing {key} in data.json"


async def test_exclude_section(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed(tmp_data_dir)
    res = export.write_export(
        settings.DB_PATH, date(2026, 4, 21),
        exclude=["impressions", "threads"],
    )
    assert "impressions" not in res["sections_included"]
    assert "threads" not in res["sections_included"]

    combined = _read_all_md(res)
    assert "## Threads" not in combined
    # "## Impressions" never in markdown under v3
    assert "## Impressions" not in combined
    assert "## Summary" in combined

    # Exclude drops those keys from JSON too
    data = json.loads(Path(res["json_path"]).read_text(encoding="utf-8"))
    assert "impressions" not in data
    assert "threads" not in data


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
    assert "# Twitter — 2020-01-01 — digest" in res["content"]


async def test_schema_is_shared_across_days(tmp_data_dir, monkeypatch):
    """SCHEMA.md lives at the exports root and is written once. Subsequent
    export_day calls on other dates must not rewrite it."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed(tmp_data_dir)
    res1 = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    schema_path = Path(res1["schema_path"])
    assert schema_path.exists()
    mtime_1 = schema_path.stat().st_mtime
    first_content = schema_path.read_text(encoding="utf-8")

    # Export a second date — SCHEMA.md must not be rewritten.
    res2 = export.write_export(settings.DB_PATH, date(2026, 4, 22))
    assert res2["schema_path"] == res1["schema_path"]
    assert schema_path.stat().st_mtime == mtime_1
    assert schema_path.read_text(encoding="utf-8") == first_content

    # Per-day directories must NOT contain their own schema.md.
    for res in (res1, res2):
        dir_path = Path(res["dir_path"])
        assert not (dir_path / "schema.md").exists()
        assert not (dir_path / "SCHEMA.md").exists()


async def test_legacy_flat_files_deleted(tmp_data_dir, monkeypatch):
    """Pre-v3 layout left ``YYYY-MM-DD.md`` + ``YYYY-MM-DD.json`` directly
    under exports/. On a successful v3 write those legacy files must be
    cleaned up — the new layout is self-contained."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed(tmp_data_dir)

    settings.ensure_exports_dir()
    legacy_md = settings.EXPORTS_DIR / "2026-04-21.md"
    legacy_json = settings.EXPORTS_DIR / "2026-04-21.json"
    legacy_md.write_text("legacy markdown", encoding="utf-8")
    legacy_json.write_text("{}", encoding="utf-8")

    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))

    # Legacy files gone; new layout present.
    assert not legacy_md.exists()
    assert not legacy_json.exists()
    dir_path = Path(res["dir_path"])
    assert dir_path.is_dir()
    for name in ("digest.md", "tweets.md", "activity.md", "timeline.md", "data.json"):
        assert (dir_path / name).exists(), f"missing {name}"


async def _seed_v2(tmp_data_dir):
    """Seed a session that exercises every v2 signal family + a revisit."""
    from backend.db import init_db, connect
    from backend.ingest import ingest_batch
    await init_db()
    db = await connect()
    alice = make_user("100", "alice", "Alice")
    bob = make_user("200", "bob", "Bob")
    t1 = make_tweet("1001", alice, "hello world", likes=10, views=500, conversation_id="1001")
    t2 = make_tweet("1002", bob, "hi alice",
                    reply_to_tweet_id="1001", reply_to_user_id="100", conversation_id="1001")
    await ingest_batch(db, [{"type": "graphql_payload", "operation_name": "HomeTimeline",
                             "payload": home_timeline_payload([t1, t2])}])
    base = "2026-04-21T"
    events = [
        {"type": "session_start", "session_id": "s1", "timestamp": base + "09:00:00+00:00"},
        # Impressions — tweet 1001 seen twice (revisit signal)
        {"type": "impression_end", "session_id": "s1", "tweet_id": "1001",
         "first_seen_at": base + "09:01:00+00:00", "dwell_ms": 3000, "feed_source": "for_you"},
        {"type": "impression_end", "session_id": "s1", "tweet_id": "1002",
         "first_seen_at": base + "09:02:00+00:00", "dwell_ms": 2000, "feed_source": "for_you"},
        {"type": "impression_end", "session_id": "s1", "tweet_id": "1001",
         "first_seen_at": base + "09:03:00+00:00", "dwell_ms": 1500, "feed_source": "for_you"},
        # Link click → external research domain
        {"type": "link_click", "session_id": "s1", "tweet_id": "1001",
         "url": "https://arxiv.org/abs/1706.03762", "domain": "arxiv.org",
         "link_kind": "external", "modifiers": "meta",
         "timestamp": base + "09:04:00+00:00"},
        # Media open on tweet 1002
        {"type": "media_open", "session_id": "s1", "tweet_id": "1002",
         "media_kind": "image", "media_index": 1,
         "timestamp": base + "09:05:00+00:00"},
        # Text selection from tweet 1001
        {"type": "text_selection", "session_id": "s1", "tweet_id": "1001",
         "text": "attention is all you need", "via": "copy",
         "timestamp": base + "09:06:00+00:00"},
        # Scroll burst
        {"type": "scroll_burst", "session_id": "s1", "feed_source": "for_you",
         "started_at": base + "09:06:30+00:00", "ended_at": base + "09:06:34+00:00",
         "duration_ms": 4000, "start_y": 0, "end_y": 5200, "delta_y": 5200,
         "reversals_count": 1,
         "timestamp": base + "09:06:30+00:00"},
        # Nav from home to alice profile
        {"type": "nav_change", "session_id": "s1",
         "from_path": "/home", "to_path": "/alice",
         "feed_source_before": "for_you", "feed_source_after": "profile",
         "timestamp": base + "09:07:00+00:00"},
        # Follow alice
        {"type": "relationship_change", "session_id": "s1",
         "target_user_id": "100", "action": "follow",
         "timestamp": base + "09:08:00+00:00"},
        {"type": "session_end", "session_id": "s1",
         "timestamp": base + "09:10:00+00:00",
         "total_dwell_ms": 10 * 60 * 1000, "tweet_count": 3,
         "feeds_visited": ["for_you", "profile"]},
    ]
    r = await ingest_batch(db, events)
    assert r["accepted"] == len(events), r
    await db.close()


async def test_v2_export_all_sections(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_v2(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))

    activity = Path(res["activity_path"]).read_text(encoding="utf-8")
    timeline = Path(res["timeline_path"]).read_text(encoding="utf-8")

    # Activity file covers link_outs, selections, media, sessions
    for heading in ("## Link-outs", "## Selections", "## Media", "## Sessions"):
        assert heading in activity, f"missing {heading!r} in activity.md"
    assert "arxiv.org" in activity
    assert "https://arxiv.org/abs/1706.03762" in activity
    assert "attention is all you need" in activity
    assert "image#1" in activity or "image" in activity
    assert "Nav path:" in activity
    assert "follow" in activity

    # Timeline file contains the chronological stream with every kind
    assert "## Timeline" in timeline
    link_pos = timeline.index("link")
    impr_pos = timeline.index("impression")
    assert link_pos > impr_pos
    for kind in ("impression", "link", "media:image", "select", "nav", "rel:follow"):
        assert kind in timeline, f"missing kind {kind} in timeline.md"

    # Revisit info lives in data.json (no more ## Impressions markdown)
    data = json.loads(Path(res["json_path"]).read_text(encoding="utf-8"))
    rvs = data.get("revisits") or []
    hits = [r for r in rvs if r.get("session_id") == "s1" and r.get("tweet_id") == "1001"]
    assert hits and hits[0]["count"] == 2


async def test_v2_revisits_query(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import queries, settings

    await _seed_v2(tmp_data_dir)
    db = queries.connect_ro(settings.DB_PATH)
    try:
        day_start, day_end = queries.day_window_utc(
            date(2026, 4, 21), settings.local_tz()
        )
        rv = queries.revisits(db, day_start, day_end)
        assert rv.get(("s1", "1001")) == 2
        # tweet 1002 seen once — not in revisits
        assert ("s1", "1002") not in rv
    finally:
        db.close()


async def test_v2_exclude_new_sections(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_v2(tmp_data_dir)
    res = export.write_export(
        settings.DB_PATH, date(2026, 4, 21),
        exclude=["link_outs", "selections", "media", "timeline"],
    )
    combined = _read_all_md(res)
    for heading in ("## Link-outs", "## Selections", "## Media", "## Timeline"):
        assert heading not in combined
    assert "## Sessions" in combined
    # Excluded keys drop from JSON too
    data = json.loads(Path(res["json_path"]).read_text(encoding="utf-8"))
    for key in ("link_outs", "selections", "media", "timeline"):
        assert key not in data


async def test_impressions_not_in_any_markdown(tmp_data_dir, monkeypatch):
    """No ``## Impressions`` heading may appear in any of the four per-day
    markdown files. Raw impressions live only in data.json."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    for key in ("digest_path", "tweets_path", "activity_path", "timeline_path"):
        text = Path(res[key]).read_text(encoding="utf-8")
        assert "## Impressions" not in text, f"{key} contains ## Impressions"
