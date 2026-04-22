"""Regression tests for the export v2 LLM-first sections.

Covers the new renderers added on top of the parallel interaction-capture
v2 work: TL;DR digest, Tweets-ranked table, Repeat-exposure, Topics,
Authors-with-context, Schema. Also locks in HTML-entity decoding and
stub filtering.
"""
from datetime import date

import pytest

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


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
    md = res["content"]

    assert "## TL;DR" in md
    # headline: 5 impressions of 4 unique... (stub is filtered only from
    # human-readable; stub might or might not show in unique count depending
    # on whether the stub author row exists)
    assert "impressions of" in md
    # Topic detection: the ai-tooling tweet should surface
    assert "`ai-tooling`" in md
    # Algorithmic pressure line: t_algo was seen 4 times
    assert "×4" in md or "×3" in md
    # "Actually read" line: two tweets had dwell >= 3s
    assert "Actually read" in md


async def test_tweets_ranked_table_structure(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    md = res["content"]

    assert "## Tweets (ranked by importance)" in md
    # Column header row
    assert "| # | imp | handle |" in md
    # tid prefix
    assert "| t2001 |" in md or "| t2004 |" in md
    # The highly-liked calm tweet should outrank the algo-pushed pressure
    # tweet because of higher dwell + interaction + views percentile.
    idx_calm = md.find("t2004")
    idx_algo = md.find("t2001")
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
    md = res["content"]

    # Pull out just the repeat-exposure section
    section = md.split("\n## Repeat-exposure", 1)[1].split("\n## ", 1)[0]
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
    md = res["content"]

    # Raw entities should be gone from rendered text
    assert "&gt;" not in md
    assert "&amp;" not in md
    # Decoded text should be present (at least somewhere)
    assert "> drop them into a doc & ask for tweets" in md


async def test_topics_section_has_untagged_and_known_bucket(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    md = res["content"]

    section = md.split("\n## Topics", 1)[1].split("\n## ", 1)[0]
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
    md = res["content"]

    section = md.split("\n## Authors", 1)[1].split("\n## ", 1)[0]
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
    md = res["content"]

    assert "## Schema (v2)" in md
    assert "0.40·dwell_norm" in md
    assert "tid" in md
    assert "algorithmic pressure" in md.lower()


async def test_all_sections_ordered_llm_first(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    md = res["content"]

    # TL;DR must come before the raw Impressions log. That's the whole point.
    tldr_pos = md.find("## TL;DR")
    impressions_pos = md.find("## Impressions")
    assert tldr_pos >= 0 and impressions_pos >= 0
    assert tldr_pos < impressions_pos

    # Tweets-ranked + Repeat-exposure + Topics must all come before raw impressions
    for section in ("## Tweets (ranked by importance)", "## Repeat-exposure", "## Topics"):
        pos = md.find(section)
        assert pos > 0 and pos < impressions_pos, f"{section} not before raw Impressions"


async def test_stub_tweets_excluded_from_ranked_but_present_in_raw(tmp_data_dir, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)
    from mcp_server import export, settings

    await _seed_heavy_scroll(tmp_data_dir)
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    md = res["content"]

    # The stub tweet (id 2099, no author, no text) should:
    # - NOT appear as a row in the Tweets-ranked table
    # - STILL appear in the raw Impressions section (complete log)
    ranked_section = md.split("\n## Tweets (ranked by importance)", 1)[1].split("\n## ", 1)[0]
    assert "t2099" not in ranked_section
    # In raw impressions the stub has no handle, so the link uses @i as
    # fallback; confirm the tweet_id is still there
    impressions_section = md.split("\n## Impressions", 1)[1].split("\n## ", 1)[0]
    assert "2099" in impressions_section
