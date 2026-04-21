"""Enrichment queue + template upsert + allowlist enforcement."""
from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.asyncio


async def _ingest(db, events):
    from backend.ingest import ingest_batch
    return await ingest_batch(db, events)


async def _seed_stubs(db, now_iso: str, tweet_ids: list[str]) -> None:
    # Create stub tweet rows (text NULL) + an impression for each so the sweep
    # has something to enqueue.
    for tid in tweet_ids:
        await db.execute(
            "INSERT OR IGNORE INTO tweets (tweet_id, captured_at, last_updated_at) VALUES (?, ?, ?)",
            (tid, now_iso, now_iso),
        )
        await db.execute(
            "INSERT INTO impressions (tweet_id, session_id, first_seen_at, dwell_ms, feed_source) "
            "VALUES (?, NULL, ?, 1000, 'for_you')",
            (tid, now_iso),
        )
    await db.commit()


async def test_graphql_template_upsert(tmp_data_dir):
    from backend.db import init_db, connect
    await init_db()
    db = await connect()

    url = (
        "/i/api/graphql/ABC123xyz/TweetDetail"
        "?variables=%7B%22focalTweetId%22%3A%22111%22%7D"
        "&features=%7B%22f1%22%3Atrue%7D"
    )
    await _ingest(db, [
        {"type": "graphql_template", "event_id": "t1",
         "operation_name": "TweetDetail", "url": url, "auth": "Bearer AAA"},
    ])

    row = await (await db.execute(
        "SELECT operation_name, query_id, url_path, features_json, variables_json, bearer "
        "FROM graphql_templates"
    )).fetchone()
    assert row["operation_name"] == "TweetDetail"
    assert row["query_id"] == "ABC123xyz"
    assert row["url_path"] == "/i/api/graphql/ABC123xyz/TweetDetail"
    assert json.loads(row["variables_json"])["focalTweetId"] == "111"
    assert json.loads(row["features_json"])["f1"] is True
    assert row["bearer"] == "Bearer AAA"

    # Second template without auth must NOT blank the existing bearer.
    url2 = (
        "/i/api/graphql/DEF456/TweetDetail"
        "?variables=%7B%22focalTweetId%22%3A%22222%22%7D"
        "&features=%7B%7D"
    )
    await _ingest(db, [
        {"type": "graphql_template", "event_id": "t2",
         "operation_name": "TweetDetail", "url": url2, "auth": None},
    ])
    row = await (await db.execute(
        "SELECT query_id, bearer FROM graphql_templates WHERE operation_name='TweetDetail'"
    )).fetchone()
    assert row["query_id"] == "DEF456"
    assert row["bearer"] == "Bearer AAA", "null auth should not clobber existing bearer"
    await db.close()


async def test_queue_population_stub_tweets(tmp_data_dir):
    from backend.db import init_db, connect
    from backend.enrichment import populate_queue
    from datetime import datetime, timezone
    await init_db()
    db = await connect()

    now_iso = datetime.now(timezone.utc).isoformat()
    await _seed_stubs(db, now_iso, ["100", "200", "300"])

    added = await populate_queue(db)
    assert added.get("stub_tweet") == 3

    rows = await (await db.execute(
        "SELECT target_id, reason, priority FROM enrichment_queue ORDER BY target_id"
    )).fetchall()
    assert [r["target_id"] for r in rows] == ["100", "200", "300"]
    assert all(r["reason"] == "stub_tweet" for r in rows)
    assert all(r["priority"] == 100 for r in rows)

    # Re-running the sweep must NOT duplicate rows (UNIQUE constraint).
    added = await populate_queue(db)
    assert added.get("stub_tweet") == 0
    count = await (await db.execute(
        "SELECT COUNT(*) FROM enrichment_queue"
    )).fetchone()
    assert count[0] == 3
    await db.close()


async def test_queue_excludes_tweets_with_text(tmp_data_dir):
    # Tweets with text should not get a stub_tweet entry.
    from backend.db import init_db, connect
    from backend.enrichment import populate_queue
    from datetime import datetime, timezone
    await init_db()
    db = await connect()

    now_iso = datetime.now(timezone.utc).isoformat()
    # Complete tweet
    await db.execute(
        "INSERT INTO tweets (tweet_id, text, captured_at, last_updated_at) VALUES ('999', 'hello', ?, ?)",
        (now_iso, now_iso),
    )
    # Stub with no impression either — shouldn't queue (no evidence user saw it)
    await db.execute(
        "INSERT INTO tweets (tweet_id, captured_at, last_updated_at) VALUES ('888', ?, ?)",
        (now_iso, now_iso),
    )
    # Stub WITH impression — should queue
    await _seed_stubs(db, now_iso, ["777"])

    await populate_queue(db)
    rows = await (await db.execute(
        "SELECT target_id FROM enrichment_queue WHERE reason='stub_tweet'"
    )).fetchall()
    assert [r["target_id"] for r in rows] == ["777"]
    await db.close()


async def test_replay_allowlist_never_contains_mutations():
    # Structural safety: the allowlist must be read-only GraphQL queries.
    from backend.enrichment import REPLAY_ALLOWLIST
    forbidden_prefixes = ("Create", "Delete", "Favorite", "Unfavorite", "Follow", "Unfollow",
                          "Retweet", "Unretweet", "Block", "Mute", "Bookmark", "Send", "DM")
    for op in REPLAY_ALLOWLIST:
        for prefix in forbidden_prefixes:
            assert not op.startswith(prefix), f"{op} looks like a mutation"


async def test_reason_to_op_maps_to_allowlisted(tmp_data_dir):
    from backend.enrichment import REASON_TO_OPS, REPLAY_ALLOWLIST
    for reason, candidates in REASON_TO_OPS.items():
        assert candidates, f"reason {reason} has no candidates"
        for op in candidates:
            assert op in REPLAY_ALLOWLIST, f"reason {reason} candidate {op} not on allowlist"
