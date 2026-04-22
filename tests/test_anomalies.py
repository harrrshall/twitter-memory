"""Anomaly detection — pure function rules over sessions + impressions.

Four rules: back-to-back sessions, doomscroll, late-night, topic drift.
"""
from zoneinfo import ZoneInfo

import pytest

from mcp_server.anomalies import (
    detect,
    detect_back_to_back,
    detect_doomscroll,
    detect_late_night,
    detect_topic_drift,
)


IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


class TestBackToBack:
    def test_short_gap_flagged(self):
        sessions = [
            {"session_id": "a", "started_at": "2026-04-21T14:30:00+00:00", "ended_at": "2026-04-21T14:39:00+00:00"},
            {"session_id": "b", "started_at": "2026-04-21T14:41:00+00:00", "ended_at": "2026-04-21T14:50:00+00:00"},
        ]
        out = detect_back_to_back(sessions)
        assert len(out) == 1
        assert "Session 2 started 120s after session 1" in out[0]

    def test_long_gap_not_flagged(self):
        sessions = [
            {"session_id": "a", "started_at": "2026-04-21T10:00:00+00:00", "ended_at": "2026-04-21T10:05:00+00:00"},
            {"session_id": "b", "started_at": "2026-04-21T15:00:00+00:00", "ended_at": "2026-04-21T15:05:00+00:00"},
        ]
        assert detect_back_to_back(sessions) == []

    def test_no_ended_at_skipped(self):
        # Session without ended_at can't feed the next comparison
        sessions = [
            {"session_id": "a", "started_at": "2026-04-21T10:00:00+00:00", "ended_at": None},
            {"session_id": "b", "started_at": "2026-04-21T10:02:00+00:00", "ended_at": None},
        ]
        assert detect_back_to_back(sessions) == []


class TestDoomscroll:
    def test_high_impressions_low_dwell_flagged(self):
        sessions = [{"session_id": "s1", "started_at": "2026-04-21T14:00:00+00:00"}]
        impressions = [{"session_id": "s1", "dwell_ms": 100} for _ in range(25)]
        out = detect_doomscroll(sessions, impressions)
        assert len(out) == 1
        assert "doomscroll" in out[0].lower()
        assert "25 impressions" in out[0]

    def test_few_impressions_not_flagged(self):
        sessions = [{"session_id": "s1", "started_at": "2026-04-21T14:00:00+00:00"}]
        impressions = [{"session_id": "s1", "dwell_ms": 0}] * 10
        assert detect_doomscroll(sessions, impressions) == []

    def test_long_dwell_not_flagged(self):
        # 25 impressions but median dwell is 5s — that's engaged reading
        sessions = [{"session_id": "s1", "started_at": "2026-04-21T14:00:00+00:00"}]
        impressions = [{"session_id": "s1", "dwell_ms": 5000} for _ in range(25)]
        assert detect_doomscroll(sessions, impressions) == []


class TestLateNight:
    def test_late_night_flagged(self):
        # 23:47 IST = 18:17 UTC
        impressions = [
            {"first_seen_at": "2026-04-21T18:17:00+00:00"},
            {"first_seen_at": "2026-04-21T19:22:00+00:00"},  # 00:52 IST
        ]
        out = detect_late_night(impressions, IST)
        assert len(out) == 1
        assert "2 impressions between 23:47 and 00:52" in out[0]

    def test_daytime_not_flagged(self):
        # 14:00 IST = 08:30 UTC
        impressions = [{"first_seen_at": "2026-04-21T08:30:00+00:00"}]
        assert detect_late_night(impressions, IST) == []


class TestTopicDrift:
    def test_drift_flagged(self):
        # 10 impressions spanning 4 distinct topics
        imps = [
            ("2026-04-21T19:42:00+00:00", ["ai-tooling"]),
            ("2026-04-21T19:42:30+00:00", ["ai-tooling"]),
            ("2026-04-21T19:43:00+00:00", ["politics"]),
            ("2026-04-21T19:43:30+00:00", ["politics"]),
            ("2026-04-21T19:44:00+00:00", ["meme"]),
            ("2026-04-21T19:44:30+00:00", ["meme"]),
            ("2026-04-21T19:45:00+00:00", ["startup"]),
            ("2026-04-21T19:45:30+00:00", ["startup"]),
            ("2026-04-21T19:46:00+00:00", ["ai-tooling"]),
            ("2026-04-21T19:46:30+00:00", ["meme"]),
        ]
        out = detect_topic_drift(imps)
        assert len(out) == 1
        assert "topic drift" in out[0]
        assert "4 topics" in out[0]

    def test_consistent_topic_not_flagged(self):
        imps = [("2026-04-21T19:42:00+00:00", ["ai-tooling"])] * 10
        assert detect_topic_drift(imps) == []

    def test_below_window_size_not_flagged(self):
        imps = [("2026-04-21T19:42:00+00:00", ["ai-tooling", "meme", "politics"])] * 5
        assert detect_topic_drift(imps) == []

    def test_untagged_ignored(self):
        # untagged tweets don't count toward topic diversity
        imps = [("2026-04-21T19:42:00+00:00", ["untagged"])] * 10
        assert detect_topic_drift(imps) == []


class TestIntegration:
    def test_detect_wires_all_rules(self):
        sessions = [
            {"session_id": "a", "started_at": "2026-04-21T14:30:00+00:00", "ended_at": "2026-04-21T14:39:00+00:00"},
            {"session_id": "b", "started_at": "2026-04-21T14:41:00+00:00", "ended_at": "2026-04-21T14:50:00+00:00"},
        ]
        impressions = [{"session_id": "b", "first_seen_at": "2026-04-21T14:42:00+00:00", "dwell_ms": 100}] * 25
        topics = [("2026-04-21T14:42:00+00:00", ["ai-tooling"])] * 10
        out = detect(sessions, impressions, topics, IST)
        # back-to-back + doomscroll should both fire
        assert any("continuation" in s for s in out)
        assert any("doomscroll" in s for s in out)

    def test_empty_input(self):
        assert detect([], [], [], IST) == []
