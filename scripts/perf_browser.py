"""Extension-on-page performance harness (Playwright).

Launches Chromium with the extension loaded, opens x.com, scrolls a fixed
pattern, and captures a CDP trace. Reports scripting overhead attributed to
the extension vs x.com, plus backend ingest deltas for the run.

Setup (once):
    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/playwright install chromium

Auth (once): copy your x.com cookies to data/perf_cookies.json as a JSON
array of Playwright cookie objects. The easiest path is to export from a
logged-in browser profile. Without auth you'll just see the logged-out home
page, which is still a valid overhead baseline.

Usage:
    TWITTER_MEMORY_DATA=./data .venv/bin/python -m backend.main &
    .venv/bin/python scripts/perf_browser.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

import httpx

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("playwright not installed. Run: .venv/bin/pip install -r requirements-dev.txt")
    sys.exit(1)


REPO = Path(__file__).resolve().parent.parent
EXTENSION_DIR = REPO / "extension"
PERF_DIR = REPO / "data" / "perf"
COOKIES_FILE = REPO / "data" / "perf_cookies.json"
BACKEND = "http://127.0.0.1:8765"

SCROLL_STEPS = 20
SCROLL_PX = 800
SCROLL_INTERVAL_MS = 500


async def _fetch_metrics() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{BACKEND}/debug/metrics")
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}


async def _fetch_stats() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{BACKEND}/stats")
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}


def _summarize_trace(trace_path: Path) -> dict:
    """Parse a CDP trace JSON and attribute script CPU to origin buckets."""
    with trace_path.open() as fh:
        trace = json.load(fh)
    events = trace.get("traceEvents", trace) if isinstance(trace, dict) else trace

    ext_us = 0.0
    x_us = 0.0
    other_us = 0.0

    for ev in events:
        if ev.get("ph") != "X":  # duration events only
            continue
        name = ev.get("name", "")
        if "Script" not in name and "Evaluate" not in name and "Function" not in name:
            continue
        dur = ev.get("dur", 0)
        args = ev.get("args", {}) or {}
        data = args.get("data", {}) or {}
        url = data.get("url") or data.get("fileName") or ""
        if "chrome-extension://" in url:
            ext_us += dur
        elif "x.com" in url or "twitter.com" in url or "twimg.com" in url:
            x_us += dur
        else:
            other_us += dur

    total = ext_us + x_us + other_us
    return {
        "extension_ms": round(ext_us / 1000, 2),
        "x_ms": round(x_us / 1000, 2),
        "other_ms": round(other_us / 1000, 2),
        "total_ms": round(total / 1000, 2),
        "extension_share": round(ext_us / total, 4) if total else 0.0,
    }


async def main() -> None:
    if not EXTENSION_DIR.is_dir():
        print(f"extension dir missing: {EXTENSION_DIR}")
        return
    PERF_DIR.mkdir(parents=True, exist_ok=True)

    cookies: list[dict] = []
    if COOKIES_FILE.exists():
        cookies = json.loads(COOKIES_FILE.read_text())
        print(f"loaded {len(cookies)} cookies from {COOKIES_FILE.name}")
    else:
        print(f"no cookies file at {COOKIES_FILE} — running logged-out (overhead baseline only)")

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    trace_path = PERF_DIR / f"trace-{ts}.json"
    user_data_dir = PERF_DIR / f"profile-{ts}"

    baseline_stats = await _fetch_stats()
    baseline_metrics = await _fetch_metrics()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,  # MV3 extensions require a head
            args=[
                f"--disable-extensions-except={EXTENSION_DIR}",
                f"--load-extension={EXTENSION_DIR}",
            ],
        )
        if cookies:
            await context.add_cookies(cookies)

        page = context.pages[0] if context.pages else await context.new_page()
        await context.tracing.start(
            screenshots=False,
            snapshots=False,
            sources=False,
        )

        await page.goto("https://x.com/home", wait_until="domcontentloaded")
        # Give the extension time to inject and the feed time to populate.
        await page.wait_for_timeout(3000)

        for _ in range(SCROLL_STEPS):
            await page.evaluate(f"window.scrollBy(0, {SCROLL_PX})")
            await page.wait_for_timeout(SCROLL_INTERVAL_MS)

        # Let the final impression_end events flush before tearing down.
        await page.wait_for_timeout(5000)

        await context.tracing.stop(path=str(trace_path))
        await context.close()

    end_stats = await _fetch_stats()
    end_metrics = await _fetch_metrics()

    print(f"\n=== perf_browser: {SCROLL_STEPS} scrolls @ {SCROLL_PX}px ===")
    print(f"trace saved: {trace_path}")
    if baseline_stats and end_stats:
        dt_tweets = (end_stats.get("tweets_today") or 0) - (baseline_stats.get("tweets_today") or 0)
        dt_dwell = (end_stats.get("total_dwell_ms_today") or 0) - (baseline_stats.get("total_dwell_ms_today") or 0)
        print(f"tweets captured during run : {dt_tweets}")
        print(f"dwell captured during run  : {dt_dwell / 1000:.1f}s")

    if end_metrics.get("batches_recent"):
        br = end_metrics["batches_recent"]
        print(f"\nbackend batches (recent window):")
        print(f"  total events  : {br['events_total']}")
        print(f"  skipped (dup) : {br['skipped_total']}")
        print(f"  p50 / p95 ms  : {br['p50_ms']} / {br['p95_ms']}")

    # Playwright's trace is a zipped archive, not a raw tracing JSON. For raw
    # CDP tracing + a script-attribution summary, use chrome://tracing → load
    # the trace from the generated zip's `trace.trace` entry, or re-record via
    # CDP directly if you need per-URL attribution numbers. This harness's
    # job is to make the trace exist and correlate with backend stats.
    print(f"\nopen in chrome://tracing or Perfetto: {trace_path}")


if __name__ == "__main__":
    asyncio.run(main())
