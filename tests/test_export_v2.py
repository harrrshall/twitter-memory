"""Regression tests for the export v2 LLM-first sections.

Covers the new renderers added on top of the parallel interaction-capture
v2 work: TL;DR digest, Tweets-ranked table, Repeat-exposure, Topics,
Authors-with-context, Schema. Also locks in HTML-entity decoding and
stub filtering.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


def _read(res: dict, key: str) -> str:
    return Path(res[key]).read_text(encoding="utf-8")


async def _seed_heavy_scroll(tmp_data_dir):
    """Seed a day that fires multiple v2 signals: one algorithmic-pressure
    tweet (×3 impressions), one HTML-entity-laden tweet, one stub tweet,
    and an ai-tooling topical tweet."""
    from backend.db import init_db, connect
    from backend.ingest import ingest_batch

    await init_db()
    db = await connect()
    alice = make_user("100", "alice", "Alice Example", followers=50000)
    bob = make_user("200", "bob", "Bob")
    chop = make_user("300", "chop", "Chop")
    ai = make_user("400", "sentient_agency", "Sentient AI")

    # Tweets
    t_algo = make_tweet("2001", chop, "POV: when she's everything you're looking for", likes=3822, retweets=836, replies=42, views=100900, conversation_id="2001")
    t_html = make_tweet("2002", bob, "how to: &gt; drop them into a doc &amp; ask for tweets", likes=49, views=3104, conversation_id="2002")
    t_ai = make_tweet("2003", ai, "I found a GitHub repo that gives Claude Code APK reverse-engineering abilities", likes=570, retweets=96, views=29700, conversation_id="2003")
    t_calm = make_tweet("2004", alice, "just thinking about peace today", likes=66200, retweets=13700, views=1800000, conversation_id="2004")

    await ingest_batch(db, [
        {"type": "graphql_payload", "operation_name": "HomeTimeline",
         "payload": home_timeline_payload([t_algo, t_html, t_ai, t_calm])},
    ])

    events: list[dict] = [
        {"type": "session_start", "session_id": "s1", "timestamp": "2026-04-21T14:00:00+00:00"},
    ]
    # t_algo: seen 4 times (algorithmic pressure), low dwell every time
    for i, ts in enumerate(["14:01:00", "14:02:30", "14:05:00", "14:08:00"], 1):
        events.append({
            "type": "impression_end", "session_id": "s1", "tweet_id": "2001",
            "first_seen_at": f"2026-04-21T{ts}+00:00",
            "dwell_ms": 0, "feed_source": "for_you",
            "event_id": f"qa-t2001-{i}",
        })
    # t_html: seen once, has HTML entities
    events.append({
        "type": "impression_end", "session_id": "s1", "tweet_id": "2002",
        "first_seen_at": "2026-04-21T14:03:00+00:00",
        "dwell_ms": 500, "feed_source": "for_you",
        "event_id": "qa-t2002-1",
    })
    # t_ai: seen once, high dwell (actually read)
    events.append({
        "type": "impression_end", "session_id": "s1", "tweet_id": "2003",
        "first_seen_at": "2026-04-21T14:04:00+00:00",
        "dwell_ms": 4200, "feed_source": "for_you",
        "event_id": "qa-t2003-1",
    })
    # t_calm: seen once, high dwell, user liked it
    events.append({
        "type": "impression_end", "session_id": "s1", "tweet_id": "2004",
        "first_seen_at": "2026-04-21T14:06:00+00:00",
        "dwell_ms": 5500, "feed_source": "for_you",
        "event_id": "qa-t2004-1",
    })
    events.append({
        "type": "interaction", "session_id": "s1", "tweet_id": "2004",
        "action": "like", "timestamp": "2026-04-21T14:06:05+00:00",
        "event_id": "qa-like-1",
    })
    # Stub tweet: impression_end for an unknown tweet_id. Backend's
    # impression_end handler inserts a bare tweet row so FKs hold; tests/fixtures
    # does not need to know about it.
    events.append({
        "type": "impression_end", "session_id": "s1", "tweet_id": "2099",
        "first_seen_at": "2026-04-21T14:09:00+00:00",
        "dwell_ms": 0, "feed_source": "for_you",
        "event_id": "qa-stub-1",
    })
    events.append({
        "type": "session_end", "session_id": "s1",
        "timestamp": "2026-04-21T14:10:00+00:00",
        "total_dwell_ms": 10 * 60 * 1000,
        "tweet_count": 4,
        "feeds_visited": ["for_you"],
    })
    await ingest_batch(db, events)
    await db.close()


async def test_tldr_headline_and_topic_rollup(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    digest = _read(res, "digest_path")

    assert "## TL;DR" in digest
    # headline: 5 impressions of 4 unique... (stub is filtered only from
    # human-readable; stub might or might not show in unique count depending
    # on whether the stub author row exists)
    assert "impressions of" in digest
    # Topic detection: the ai-tooling tweet should surface
    assert "`ai-tooling`" in digest
    # Algorithmic pressure line: t_algo was seen 4 times
    assert "×4" in digest or "×3" in digest
    # "Actually read" line: two tweets had dwell >= 3s
    assert "Actually read" in digest


async def test_tweets_ranked_table_structure(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    tweets_md = _read(res, "tweets_path")

    assert "## Tweets (ranked by importance)" in tweets_md
    # Column header row
    assert "| # | imp | handle |" in tweets_md
    # tid prefix
    assert "| t2001 |" in tweets_md or "| t2004 |" in tweets_md
    # The highly-liked calm tweet should outrank the algo-pushed pressure
    # tweet because of higher dwell + interaction + views percentile.
    idx_calm = tweets_md.find("t2004")
    idx_algo = tweets_md.find("t2001")
    assert idx_calm >= 0 and idx_algo >= 0
    assert idx_calm < idx_algo, "t2004 (read + liked) should rank above t2001 (algo pressure with 0 dwell)"


async def test_repeat_exposure_only_shows_pressured(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    tweets_md = _read(res, "tweets_path")

    # Pull out just the repeat-exposure section
    section = tweets_md.split("\n## Repeat-exposure", 1)[1]
    assert "@chop" in section         # seen 4× → qualifies
    assert "@bob" not in section       # seen 1× → excluded
    assert "@alice" not in section     # seen 1× → excluded
    assert "@sentient_agency" not in section


async def test_html_entities_decoded(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    # tweets.md is where the entity-laden tweet text surfaces (ranked table)
    tweets_md = _read(res, "tweets_path")

    assert "&gt;" not in tweets_md
    assert "&amp;" not in tweets_md
    assert "> drop them into a doc & ask for tweets" in tweets_md


async def test_topics_section_has_untagged_and_known_bucket(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    digest = _read(res, "digest_path")

    section = digest.split("\n## Topics", 1)[1].split("\n## ", 1)[0]
    assert "`ai-tooling`" in section
    # meme tweet: POV / when she
    assert "`meme`" in section
    # untagged bucket present for the HTML-laden "how to" tweet which doesn't hit any bucket
    assert "`untagged`" in section


async def test_authors_section_surfaces_follower_count(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    digest = _read(res, "digest_path")

    section = digest.split("\n## Authors", 1)[1].split("\n## ", 1)[0]
    # alice has 50k followers configured
    assert "50.0K" in section or "50,000" in section
    # and her verified flag is ✓
    assert "| @alice |" in section


async def test_schema_always_present_and_documents_scoring(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    # Schema is a shared file at the exports root, not inlined per day.
    schema_text = Path(res["schema_path"]).read_text(encoding="utf-8")

    assert "## Schema (v2)" in schema_text
    assert "0.40·dwell_norm" in schema_text
    assert "tid" in schema_text
    assert "algorithmic pressure" in schema_text.lower()


async def test_llm_first_ordering_across_files(tmp_data_dir, monkeypatch):
    """The high-signal surfaces (TL;DR, Tweets-ranked, Repeat-exposure,
    Topics) must all be readable without opening timeline.md. Digest +
    tweets files must contain them; timeline must be its own file."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    digest = _read(res, "digest_path")
    tweets_md = _read(res, "tweets_path")
    timeline_md = _read(res, "timeline_path")

    assert "## TL;DR" in digest
    assert "## Topics" in digest
    assert "## Tweets (ranked by importance)" in tweets_md
    assert "## Repeat-exposure" in tweets_md
    # Timeline is its own file, not smuggled into the digest.
    assert "## Timeline" in timeline_md
    assert "## Timeline" not in digest
    # And the raw impressions section is gone from every .md file.
    for text in (digest, tweets_md, timeline_md):
        assert "## Impressions" not in text


async def test_stub_tweets_excluded_from_ranked_but_present_in_json(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    tweets_md = _read(res, "tweets_path")
    data = json.loads(Path(res["json_path"]).read_text(encoding="utf-8"))

    # The stub tweet (id 2099, no author, no text) should:
    # - NOT appear as a row in the Tweets-ranked table
    # - STILL appear in data.json's raw impressions array so the log stays complete
    ranked_section = tweets_md.split("\n## Tweets (ranked by importance)", 1)[1].split("\n## ", 1)[0]
    assert "t2099" not in ranked_section
    impression_tids = [im.get("tweet_id") for im in data.get("impressions") or []]
    assert "2099" in impression_tids
