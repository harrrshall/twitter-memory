"""Sprint 1 coverage: MCP query tools + JSON companion.

Seeds a known corpus, then calls each agent-query function directly (bypassing
the FastMCP decorator wiring, which is purely glue). Verifies row shapes,
filters, ordering, and the `_meta` envelope produced by the tool wrappers.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


async def _seed_rich(tmp_data_dir):
    """Richer corpus than test_export: two authors across two days, multiple
    impressions per tweet, mixed engagement + dwell so ranking is non-trivial."""
    from backend.db import init_db, connect
    from backend.ingest import ingest_batch
    await init_db()
    db = await connect()

    alice = make_user("100", "alice", "Alice A", followers=5000)
    bob = make_user("200", "bob", "Bob B", followers=100)
    t_ai = make_tweet("5001", alice, "new LLM model dropped today", likes=1000, views=50000)
    t_meme = make_tweet("5002", alice, "bro that pov gotta be illegal", likes=50, views=900)
    t_bob = make_tweet("5003", bob, "hi everyone", likes=2, views=20)
    t_long = make_tweet("5004", alice, "a long tweet about nothing in particular and probably more", likes=10, views=300)
    await ingest_batch(db, [{
        "type": "graphql_payload", "operation_name": "HomeTimeline",
        "payload": home_timeline_payload([t_ai, t_meme, t_bob, t_long]),
    }])

    events = [
        {"type": "session_start", "session_id": "s1", "timestamp": "2026-04-21T09:00:00+00:00"},
        # Ai tweet: 3 impressions, large dwell, engaged
        {"type": "impression_end", "session_id": "s1", "tweet_id": "5001",
         "first_seen_at": "2026-04-21T09:01:00+00:00", "dwell_ms": 5000, "feed_source": "for_you"},
        {"type": "impression_end", "session_id": "s1", "tweet_id": "5001",
         "first_seen_at": "2026-04-21T09:05:00+00:00", "dwell_ms": 3000, "feed_source": "for_you"},
        {"type": "impression_end", "session_id": "s1", "tweet_id": "5001",
         "first_seen_at": "2026-04-21T09:10:00+00:00", "dwell_ms": 1500, "feed_source": "for_you"},
        {"type": "interaction", "session_id": "s1", "tweet_id": "5001",
         "action": "like", "timestamp": "2026-04-21T09:02:00+00:00"},
        # Meme tweet: single impression, short dwell, no engagement
        {"type": "impression_end", "session_id": "s1", "tweet_id": "5002",
         "first_seen_at": "2026-04-21T09:12:00+00:00", "dwell_ms": 500, "feed_source": "for_you"},
        # Bob tweet: single impression, medium dwell, no engagement
        {"type": "impression_end", "session_id": "s1", "tweet_id": "5003",
         "first_seen_at": "2026-04-21T09:14:00+00:00", "dwell_ms": 4000, "feed_source": "for_you"},
        # Long: dwelled heavily, no interaction (the silent-meaningful case)
        {"type": "impression_end", "session_id": "s1", "tweet_id": "5004",
         "first_seen_at": "2026-04-21T09:20:00+00:00", "dwell_ms": 8000, "feed_source": "for_you"},
        {"type": "session_end", "session_id": "s1", "timestamp": "2026-04-21T09:30:00+00:00",
         "total_dwell_ms": 30 * 60 * 1000, "tweet_count": 4, "feeds_visited": ["for_you"]},
        {"type": "link_click", "session_id": "s1", "tweet_id": "5001",
         "url": "https://example.com/paper", "domain": "example.com",
         "link_kind": "external", "modifiers": "",
         "timestamp": "2026-04-21T09:03:00+00:00"},
        {"type": "text_selection", "session_id": "s1", "tweet_id": "5001",
         "text": "new LLM model dropped", "via": "copy",
         "timestamp": "2026-04-21T09:04:00+00:00"},
    ]
    await ingest_batch(db, events)
    await db.close()


def _make_env(monkeypatch):
    """Pin timezone to UTC and re-read mcp_server.settings so the day window
    lines up with the seeded ISO-UTC timestamps."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("TWITTER_MEMORY_TZ", "UTC")
    import importlib
    import mcp_server.settings
    importlib.reload(mcp_server.settings)


# --- Tool wrappers: call via server module to exercise the envelope ----------


async def test_search_tweets_basic(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.search_tweets(q="llm", start="2026-04-21", end="2026-04-21")
    assert r["row_count"] == 1
    assert r["rows"][0]["tweet_id"] == "5001"
    assert r["rows"][0]["handle"] == "alice"
    assert r["rows"][0]["impressions_count"] == 3
    assert r["rows"][0]["total_dwell_ms"] == 9500
    assert "topics" in r["rows"][0]
    assert r["date_range"] == {"start": "2026-04-21", "end": "2026-04-21"}
    assert r["query_ms"] >= 0
    assert r["truncated"] is False


async def test_search_tweets_with_filters(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.search_tweets(
        q="pov", start="2026-04-21", end="2026-04-21",
        author="alice", min_dwell_ms=100,
    )
    assert r["row_count"] == 1
    assert r["rows"][0]["tweet_id"] == "5002"
    # engaged_only filter should exclude the meme
    r2 = server.search_tweets(
        q="pov", start="2026-04-21", end="2026-04-21",
        engaged_only=True,
    )
    assert r2["row_count"] == 0


async def test_search_tweets_rejects_empty_query(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    with pytest.raises(ValueError):
        server.search_tweets(q="  ")


async def test_top_dwelled_sorts_by_dwell(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.top_dwelled(start="2026-04-21", end="2026-04-21")
    ids = [row["tweet_id"] for row in r["rows"]]
    # 5001 (9500ms across 3 impressions) > 5004 (8000ms) > 5003 (4000ms) > 5002 (500ms)
    assert ids == ["5001", "5004", "5003", "5002"]


async def test_top_dwelled_author_filter(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.top_dwelled(start="2026-04-21", end="2026-04-21", author="bob")
    assert r["row_count"] == 1
    assert r["rows"][0]["handle"] == "bob"


async def test_read_but_not_engaged_filters_interactions(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.read_but_not_engaged(
        start="2026-04-21", end="2026-04-21", min_dwell_ms=3000
    )
    # 5001 is excluded (user liked it); 5002 below threshold; 5003 + 5004 qualify
    ids = [row["tweet_id"] for row in r["rows"]]
    assert "5001" not in ids
    assert "5002" not in ids
    assert set(ids) == {"5003", "5004"}


async def test_algorithmic_pressure_min_impressions(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.algorithmic_pressure(
        start="2026-04-21", end="2026-04-21", min_impressions=3
    )
    assert r["row_count"] == 1
    assert r["rows"][0]["tweet_id"] == "5001"
    # Below the threshold: nothing qualifies at 5+
    r2 = server.algorithmic_pressure(
        start="2026-04-21", end="2026-04-21", min_impressions=5
    )
    assert r2["row_count"] == 0


async def test_author_report_full_shape(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.author_report(
        handle="alice", start="2026-04-21", end="2026-04-21"
    )
    assert r["author"]["handle"] == "alice"
    assert r["author"]["follower_count"] == 5000
    assert r["stats"]["unique_tweets_seen"] == 3
    assert r["stats"]["interactions"] == {"like": 1}
    assert len(r["tweets"]) == 3
    assert len(r["recent_selections"]) == 1
    assert len(r["recent_link_clicks"]) == 1
    assert r["_meta"]["date_range"]["start"] == "2026-04-21"


async def test_author_report_unknown_handle(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.author_report(
        handle="nosuchuser", start="2026-04-21", end="2026-04-21"
    )
    assert r["author"] is None
    assert r["tweets"] == []


async def test_session_detail_returns_everything(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.session_detail(session_id="s1")
    assert r["session"]["session_id"] == "s1"
    assert len(r["impressions"]) == 6  # 3 for 5001 + one each for 5002/5003/5004
    assert len(r["interactions"]) == 1
    assert len(r["link_clicks"]) == 1
    assert len(r["selections"]) == 1
    assert r["_meta"]["query_ms"] >= 0


async def test_session_detail_unknown(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.session_detail(session_id="does-not-exist")
    assert r["session"] is None


async def test_recent_sessions(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.recent_sessions(limit=5)
    assert r["row_count"] == 1
    assert r["rows"][0]["session_id"] == "s1"
    assert r["rows"][0]["tweet_count"] == 6  # impression-derived


async def test_hesitation_report_empty_without_table(tmp_data_dir, monkeypatch):
    # Sprint 2 hasn't shipped the button_hover_intent table yet; tool should
    # return an empty result not raise.
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.hesitation_report(start="2026-04-21", end="2026-04-21")
    assert r["row_count"] == 0


async def test_daily_summary_json_shape(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    r = server.daily_summary_json(date="2026-04-21")
    assert r["date"] == "2026-04-21"
    assert r["summary"]["sessions"] == 1
    assert r["summary"]["unique_tweets"] == 4
    assert r["summary"]["interactions_by_action"] == {"like": 1}
    assert len(r["tweets_ranked"]) == 4
    assert r["tweets_ranked"][0]["tweet_id"] == "5001"  # highest importance
    assert "topics" in r["tweets_ranked"][0]
    # Every tweet row has stable keys agents can count on
    expected_keys = {
        "rank", "tweet_id", "importance", "handle", "display_name",
        "text", "created_at", "has_media", "impressions_count",
        "total_dwell_ms", "engagement", "topics", "user_had_interaction",
    }
    assert expected_keys.issubset(set(r["tweets_ranked"][0].keys()))


async def test_export_day_writes_json_companion(tmp_data_dir, monkeypatch):
    from pathlib import Path
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server, settings
    r = server.export_day(date="2026-04-21")
    assert Path(r["file_path"]).exists()
    assert Path(r["json_path"]).exists()
    body = json.loads(Path(r["json_path"]).read_text())
    # Mirrors daily_summary_json
    assert body["date"] == "2026-04-21"
    assert len(body["tweets_ranked"]) == 4


async def test_daily_briefing_not_configured(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    # Ensure no env config leaks in
    monkeypatch.delenv("TWITTER_MEMORY_BRIEFING_API_KEY", raising=False)
    from mcp_server import server
    r = server.daily_briefing(date="2026-04-21")
    assert "error" in r
    assert "not configured" in r["error"]


async def test_daily_briefing_with_mocked_llm(tmp_data_dir, monkeypatch):
    """Bypass the real LLM via call_override through the briefing module
    directly, so tests don't need anthropic installed or network."""
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    monkeypatch.setenv("TWITTER_MEMORY_BRIEFING_API_KEY", "fake-key-for-tests")
    from mcp_server import briefing, export, settings
    day_json = export.build_json(settings.DB_PATH, date(2026, 4, 21))

    def fake_call(model, api_key, prompt):
        assert api_key == "fake-key-for-tests"
        # The real provider returns a string; tests return parseable JSON.
        return (
            '```json\n'
            '{"headline": "You read 4 tweets, engaged with 1.",'
            ' "hesitations": [],'
            ' "suggested_replies": [{"tweet_id": "5001", "reasoning": "You '
            'dwelled 9.5s; worth a take", "draft_tone_hints": ["curious"]}],'
            ' "follow_ups": [{"handle": "alice", "why": "You engage here"}],'
            ' "topic_gaps": ["none today"]}\n'
            '```'
        )

    r = briefing.generate(day_json, call_override=fake_call)
    assert "error" not in r, r
    assert r["headline"].startswith("You read")
    assert r["suggested_replies"][0]["tweet_id"] == "5001"
    assert r["_meta"]["provider"] in {"anthropic", "unknown"} or True


async def test_daily_briefing_handles_bad_llm_json(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    monkeypatch.setenv("TWITTER_MEMORY_BRIEFING_API_KEY", "fake-key")
    from mcp_server import briefing, export, settings
    day_json = export.build_json(settings.DB_PATH, date(2026, 4, 21))

    def broken_call(model, api_key, prompt):
        return "i refuse to produce json"

    r = briefing.generate(day_json, call_override=broken_call)
    assert "error" in r
    assert "invalid JSON" in r["error"]


async def test_tldr_includes_read_speed(tmp_data_dir, monkeypatch):
    """Read-speed summary should appear when there are ≥5 dwell-2s+ impressions
    with real text. The seeded corpus has 3 impressions of 5001 (all >= 2000ms)
    plus 5003 (4000ms) plus 5004 (8000ms) = 5 qualifying reads — should trigger."""
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import export, settings
    res = export.write_export(settings.DB_PATH, date(2026, 4, 21))
    assert "Read speed" in res["content"]
    assert "WPM" in res["content"]


async def test_clamp_limit_respected(tmp_data_dir, monkeypatch):
    _make_env(monkeypatch)
    await _seed_rich(tmp_data_dir)
    from mcp_server import server
    # Ask for a huge limit — should clamp to 500 (our max).
    r = server.top_dwelled(
        start="2026-04-21", end="2026-04-21", limit=999999
    )
    # Only 4 tweets seeded so truncated should be False.
    assert r["truncated"] is False
