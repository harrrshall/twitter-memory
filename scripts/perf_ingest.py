"""Backend throughput benchmark.

Sends a realistic event mix to POST /ingest at configurable concurrency and
reports throughput + latency percentiles + backend-reported batch timings.

Usage:
    TWITTER_MEMORY_DATA=./data .venv/bin/python -m backend.main &
    .venv/bin/python scripts/perf_ingest.py --events 10000 --concurrency 4

Writes no data outside the backend it talks to. Stop the backend when done.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
import uuid

import httpx


BACKEND = "http://127.0.0.1:8765"
BATCH_SIZE = 50

# Rough x.com session mix: most events are impressions, with occasional
# interactions and searches. GraphQL payloads are heavier but rarer.
EVENT_MIX = [
    ("impression_end", 0.70),
    ("graphql_payload", 0.15),
    ("interaction", 0.10),
    ("search", 0.05),
]


def _sample_event_type() -> str:
    r = random.random()
    cum = 0.0
    for name, p in EVENT_MIX:
        cum += p
        if r < cum:
            return name
    return EVENT_MIX[-1][0]


def _make_event(session_id: str | None) -> dict:
    etype = _sample_event_type()
    ev: dict = {"type": etype, "event_id": str(uuid.uuid4())}
    if etype == "impression_end":
        ev.update({
            "tweet_id": f"perf-{random.randint(1, 200000)}",
            "session_id": session_id,
            "first_seen_at": "2026-04-21T09:14:00+00:00",
            "dwell_ms": random.randint(200, 12000),
            "feed_source": random.choice(["for_you", "following", "search"]),
        })
    elif etype == "interaction":
        ev.update({
            "tweet_id": f"perf-{random.randint(1, 200000)}",
            "action": random.choice(["like", "retweet", "reply", "bookmark"]),
            "timestamp": "2026-04-21T09:14:00+00:00",
        })
    elif etype == "search":
        ev.update({
            "query": random.choice(["rust async", "llm eval", "claude code", "attention"]),
            "timestamp": "2026-04-21T09:14:00+00:00",
        })
    elif etype == "graphql_payload":
        # Small synthetic payload. Real HomeTimeline bodies are ~100KB, but
        # this benchmark is about the write path, not JSON parse cost.
        ev.update({
            "operation_name": "HomeTimeline",
            "payload": {"data": {"home": {"home_timeline_urt": {"instructions": []}}}},
        })
    return ev


async def _post_batch(
    client: httpx.AsyncClient, batch: list[dict], timings: list[float]
) -> tuple[int, int]:
    t0 = time.perf_counter()
    r = await client.post(f"{BACKEND}/ingest", json={"events": batch})
    timings.append((time.perf_counter() - t0) * 1000)
    r.raise_for_status()
    body = r.json()
    return body.get("accepted", 0), body.get("skipped", 0)


async def _worker(
    client: httpx.AsyncClient,
    batches: asyncio.Queue[list[dict] | None],
    timings: list[float],
    totals: dict[str, int],
) -> None:
    while True:
        batch = await batches.get()
        if batch is None:
            batches.task_done()
            return
        try:
            accepted, skipped = await _post_batch(client, batch, timings)
            totals["accepted"] += accepted
            totals["skipped"] += skipped
        except Exception as exc:
            totals["errors"] += 1
            print(f"batch failed: {type(exc).__name__}: {exc}")
        finally:
            batches.task_done()


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[idx]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=5000)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    session_id: str | None = None  # keep None so FK to sessions table doesn't fire

    batches: asyncio.Queue[list[dict] | None] = asyncio.Queue(maxsize=args.concurrency * 2)
    timings: list[float] = []
    totals = {"accepted": 0, "skipped": 0, "errors": 0}

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Sanity check the backend is up before starting the storm.
        try:
            r = await client.get(f"{BACKEND}/health")
            r.raise_for_status()
        except Exception as exc:
            print(f"backend not reachable at {BACKEND}: {exc}")
            return

        start_metrics = await client.get(f"{BACKEND}/debug/metrics")
        baseline = start_metrics.json() if start_metrics.status_code == 200 else None

        workers = [
            asyncio.create_task(_worker(client, batches, timings, totals))
            for _ in range(args.concurrency)
        ]

        wall_start = time.perf_counter()
        sent = 0
        while sent < args.events:
            take = min(args.batch_size, args.events - sent)
            batch = [_make_event(session_id) for _ in range(take)]
            await batches.put(batch)
            sent += take

        for _ in workers:
            await batches.put(None)
        await asyncio.gather(*workers)
        wall = time.perf_counter() - wall_start

        end_metrics = await client.get(f"{BACKEND}/debug/metrics")
        end = end_metrics.json() if end_metrics.status_code == 200 else None

    print(f"\n=== perf_ingest: {args.events} events @ concurrency={args.concurrency} ===")
    print(f"wall time        : {wall:.2f}s")
    print(f"throughput       : {args.events / wall:,.0f} events/sec")
    print(f"batches sent     : {len(timings)}")
    print(f"accepted / skipped / errors : "
          f"{totals['accepted']} / {totals['skipped']} / {totals['errors']}")
    if timings:
        print(f"POST latency p50 : {_pct(timings, 0.50):.2f} ms")
        print(f"POST latency p95 : {_pct(timings, 0.95):.2f} ms")
        print(f"POST latency p99 : {_pct(timings, 0.99):.2f} ms")
        print(f"POST latency max : {max(timings):.2f} ms")
    if end and end.get("batches_recent"):
        br = end["batches_recent"]
        print(f"\nbackend ring buffer (latest {br['count']} batches):")
        print(f"  events_total   : {br['events_total']}")
        print(f"  p50 / p95 / p99: {br['p50_ms']} / {br['p95_ms']} / {br['p99_ms']} ms")


if __name__ == "__main__":
    asyncio.run(main())
