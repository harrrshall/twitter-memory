"""Nightly retention + maintenance. Runs inside the FastAPI process as an
asyncio task started in the app's lifespan.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

log = logging.getLogger("twitter_memory.retention")


RETENTION_TABLES = {
    "impressions": ("first_seen_at", 60),
    "engagement_snapshots": ("captured_at", 60),
    "sessions": ("ended_at", 60),
    "searches": ("timestamp", 60),
    "raw_payloads": ("captured_at", 30),
    "link_clicks": ("timestamp", 60),
    "media_events": ("timestamp", 60),
    "scroll_bursts": ("started_at", 60),
    "nav_events": ("timestamp", 60),
    "relationship_changes": ("timestamp", 60),
    # Sensitive — shorter retention to match raw_payloads.
    "text_selections": ("timestamp", 30),
}


async def run_once(db: aiosqlite.Connection) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    deleted: dict[str, int] = {}
    for table, (col, days) in RETENTION_TABLES.items():
        cutoff = (now - timedelta(days=days)).isoformat()
        cur = await db.execute(f"DELETE FROM {table} WHERE {col} < ?", (cutoff,))
        deleted[table] = cur.rowcount or 0
    await db.commit()
    # WAL checkpoint (truncate) to bound WAL file size.
    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Monthly VACUUM on the first of the month.
    if now.day == 1:
        await db.execute("VACUUM")
    log.info("retention: %s", deleted)
    return deleted


def _seconds_until_next_3am(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc).astimezone()
    target = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


async def retention_loop(db: aiosqlite.Connection, stop: asyncio.Event) -> None:
    while not stop.is_set():
        delay = _seconds_until_next_3am()
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return  # stop set
        except asyncio.TimeoutError:
            pass
        try:
            await run_once(db)
        except Exception:
            log.exception("retention pass failed")
