from datetime import datetime, timezone, timedelta

import pytest

async def test_retention_deletes_only_expired(tmp_data_dir):
    from backend.db import init_db, connect
    from backend.retention import run_once

    await init_db()
    db = await connect()
    # Seed rows: one old (impression 70 days ago) and one fresh (today).
    old = (datetime.now(timezone.utc) - timedelta(days=70)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    # Stub tweets so FK is satisfied
    for tid in ("t_old", "t_new"):
        await db.execute(
            "INSERT INTO tweets (tweet_id, captured_at, last_updated_at) VALUES (?, ?, ?)",
            (tid, fresh, fresh),
        )
    await db.execute(
        "INSERT INTO impressions (tweet_id, session_id, first_seen_at, dwell_ms, feed_source) VALUES (?, ?, ?, ?, ?)",
        ("t_old", None, old, 1000, "for_you"),
    )
    await db.execute(
        "INSERT INTO impressions (tweet_id, session_id, first_seen_at, dwell_ms, feed_source) VALUES (?, ?, ?, ?, ?)",
        ("t_new", None, fresh, 1000, "for_you"),
    )
    # Raw payload older than 30d gets trimmed
    await db.execute(
        "INSERT INTO raw_payloads (operation_name, payload_json, captured_at, parser_version) VALUES (?, ?, ?, ?)",
        ("HomeTimeline", "{}", (datetime.now(timezone.utc) - timedelta(days=35)).isoformat(), "1"),
    )
    await db.commit()

    deleted = await run_once(db)
    assert deleted["impressions"] == 1
    assert deleted["raw_payloads"] == 1

    row = await (await db.execute("SELECT tweet_id FROM impressions")).fetchone()
    assert row[0] == "t_new"
    await db.close()


def test_seconds_until_3am_is_positive():
    from backend.retention import _seconds_until_next_3am
    now = datetime.now().astimezone()
    s = _seconds_until_next_3am(now)
    assert 0 < s <= 24 * 3600
