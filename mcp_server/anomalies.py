"""Anomaly detection over a day's sessions and impressions.

Purely functional — takes structured rows, returns a list of short
human-readable strings. The export's TL;DR section renders them as
bullets. If no anomaly fires, returns an empty list.

Four rules (see mcp_server/export.py `## Schema` for the user-facing
copy):

- **back-to-back sessions** (<3 min gap between prev.ended_at and next.started_at)
- **doomscroll** (impressions ≥ 20 with median dwell < 500ms)
- **late-night** (any impression between 23:00-04:00 local)
- **topic drift** (10-impression sliding window spanning ≥3 topics)

The input shape is whatever the queries module returns — keep the
field names stable and this module doesn't care about SQL.
"""
from __future__ import annotations

import statistics
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo


_BACK_TO_BACK_GAP_S = 180
_DOOMSCROLL_MIN_IMPRESSIONS = 20
_DOOMSCROLL_MAX_MEDIAN_MS = 500
_LATE_NIGHT_START_HOUR = 23
_LATE_NIGHT_END_HOUR = 4
_TOPIC_DRIFT_WINDOW = 10
_TOPIC_DRIFT_THRESHOLD = 3


def _parse_iso(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def detect_back_to_back(sessions: list[dict]) -> list[str]:
    out: list[str] = []
    prev_end = None
    prev_idx = 0
    for i, s in enumerate(sessions, 1):
        end = _parse_iso(s.get("ended_at"))
        start = _parse_iso(s.get("started_at"))
        if prev_end and start:
            gap = (start - prev_end).total_seconds()
            if 0 <= gap < _BACK_TO_BACK_GAP_S:
                out.append(
                    f"Session {i} started {int(gap)}s after session {prev_idx} "
                    "ended — likely continuation of the same scroll."
                )
        if end:
            prev_end = end
            prev_idx = i
    return out


def detect_doomscroll(sessions: list[dict], impressions: list[dict]) -> list[str]:
    # Group impressions by session_id
    by_session: dict[str, list[int]] = {}
    for im in impressions:
        sid = im.get("session_id")
        if not sid:
            continue
        by_session.setdefault(sid, []).append(im.get("dwell_ms") or 0)

    out: list[str] = []
    for i, s in enumerate(sessions, 1):
        dwells = by_session.get(s.get("session_id"), [])
        if len(dwells) < _DOOMSCROLL_MIN_IMPRESSIONS:
            continue
        median = statistics.median(dwells)
        if median < _DOOMSCROLL_MAX_MEDIAN_MS:
            median_s = median / 1000
            out.append(
                f"Session {i}: {len(dwells)} impressions with {median_s:.1f}s "
                "median dwell — classic doomscroll pattern."
            )
    return out


def detect_late_night(impressions: list[dict], tz: ZoneInfo) -> list[str]:
    hits = []
    for im in impressions:
        t = _parse_iso(im.get("first_seen_at"))
        if not t:
            continue
        local_hour = t.astimezone(tz).hour
        if local_hour >= _LATE_NIGHT_START_HOUR or local_hour < _LATE_NIGHT_END_HOUR:
            hits.append(t.astimezone(tz))
    if not hits:
        return []
    hits.sort()
    return [
        f"{len(hits)} impressions between "
        f"{hits[0].strftime('%H:%M')} and {hits[-1].strftime('%H:%M')} — late-night window."
    ]


def detect_topic_drift(impressions_with_topics: list[tuple[str, list[str]]]) -> list[str]:
    # impressions_with_topics is [(timestamp_iso, ["ai-tooling", "meme"]), ...]
    # in chronological order. We check a sliding window of N impressions and
    # flag spans that span >=K distinct topics.
    n = len(impressions_with_topics)
    if n < _TOPIC_DRIFT_WINDOW:
        return []
    out: list[str] = []
    reported_windows: set[tuple[int, int]] = set()
    for i in range(n - _TOPIC_DRIFT_WINDOW + 1):
        window = impressions_with_topics[i : i + _TOPIC_DRIFT_WINDOW]
        topics = set()
        for _, tags in window:
            for t in tags:
                if t != "untagged":
                    topics.add(t)
        if len(topics) >= _TOPIC_DRIFT_THRESHOLD:
            # Dedupe overlapping windows: only report the first in each run
            key = (i // _TOPIC_DRIFT_WINDOW, len(topics))
            if key in reported_windows:
                continue
            reported_windows.add(key)
            start_ts, _ = window[0]
            end_ts, _ = window[-1]
            start_hm = start_ts[11:16] if len(start_ts) >= 16 else start_ts
            end_hm = end_ts[11:16] if len(end_ts) >= 16 else end_ts
            out.append(
                f"{start_hm}-{end_hm} — topic drift across "
                f"{', '.join(sorted(topics))} ({len(topics)} topics in a "
                f"{_TOPIC_DRIFT_WINDOW}-impression window)."
            )
    return out


def detect(
    sessions: list[dict],
    impressions: list[dict],
    impressions_with_topics: list[tuple[str, list[str]]] | None,
    tz: ZoneInfo,
) -> list[str]:
    """Run every rule in order. Returns flat list of anomaly strings."""
    out: list[str] = []
    out.extend(detect_back_to_back(sessions))
    out.extend(detect_doomscroll(sessions, impressions))
    out.extend(detect_late_night(impressions, tz))
    if impressions_with_topics:
        out.extend(detect_topic_drift(impressions_with_topics))
    return out
