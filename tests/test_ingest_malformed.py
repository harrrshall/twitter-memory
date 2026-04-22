"""Regression: malformed events must land in errors[], not accepted.

Before this fix, handlers silently `return` when required fields were missing
but ingest_batch still counted the event as accepted, so /ingest responses
lied about how many events were actually persisted. A service worker using
accepted to drive retry/queue logic would think data was saved when it
wasn't.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def _ingest(db, events):
    from backend.ingest import ingest_batch
    return await ingest_batch(db, events)


async def _fresh_db():
    from backend.db import init_db, connect
    await init_db()
    return await connect()


@pytest.mark.parametrize(
    "event,expected_msg_prefix",
    [
        ({"type": "impression_end"}, "impression_end missing"),
        ({"type": "interaction", "tweet_id": "t1"}, "interaction missing"),
        ({"type": "interaction", "action": "like"}, "interaction missing"),
        ({"type": "dom_tweet", "tweet_id": "t1"}, "dom_tweet missing"),
        ({"type": "dom_tweet", "author_handle": "alice"}, "dom_tweet missing"),
        ({"type": "session_start"}, "session_start missing"),
        ({"type": "session_end"}, "session_end missing"),
        ({"type": "search"}, "search missing"),
    ],
)
async def test_malformed_events_go_to_errors_not_accepted(tmp_data_dir, event, expected_msg_prefix):
    db = await _fresh_db()
    try:
        r = await _ingest(db, [event])
        assert r["accepted"] == 0, f"expected 0 accepted for {event!r}, got {r}"
        assert r["skipped"] == 0
        assert len(r["errors"]) == 1, f"expected 1 error, got {r}"
        assert expected_msg_prefix in r["errors"][0]["error"]
        assert r["errors"][0]["index"] == 0
    finally:
        await db.close()


async def test_mixed_batch_accepts_valid_and_errors_malformed(tmp_data_dir):
    db = await _fresh_db()
    try:
        r = await _ingest(db, [
            {"type": "session_start", "session_id": "s1",
             "timestamp": "2026-04-22T09:10:00+00:00"},
            {"type": "interaction"},  # malformed
            {"type": "session_end", "session_id": "s1",
             "timestamp": "2026-04-22T09:31:00+00:00"},
        ])
        assert r["accepted"] == 2
        assert r["skipped"] == 0
        assert len(r["errors"]) == 1
        assert r["errors"][0]["index"] == 1
    finally:
        await db.close()
