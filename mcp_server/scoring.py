"""Per-tweet importance scoring for the v2 export.

One pure function. All inputs are plain floats/ints/bools so it's
trivial to unit-test. The score is a weighted sum documented in
`## Schema` of the export:

    importance = 0.40·dwell_norm
               + 0.30·eng_pct_norm
               + 0.20·impression_bonus
               + 0.10·interaction_flag

where `impression_bonus` only kicks in above a single impression — a tweet
seen once isn't "algorithmic pressure", it's the normal case.

See mcp_server/export.py for how the inputs get assembled from the
aggregated tweet row.
"""
from __future__ import annotations

import bisect
from typing import Iterable


_DWELL_CAP_MS = 5000          # 5s of dwell = full credit on this axis
_IMPRESSION_BASELINE = 1      # first impression is the baseline — zero bonus
_IMPRESSION_SATURATION = 5    # 5 impressions of the same tweet = full credit


def _percentile(value: float, sorted_values: list[float]) -> float:
    """Return the 0..1 percentile rank of ``value`` within ``sorted_values``.

    Returns 0.0 when the sample is empty or the value is None/zero. Uses the
    right-insertion point so ties rank at the top of their cohort — a tweet
    tied with the day's max views gets 1.0, not something less.
    """
    if not sorted_values or value <= 0:
        return 0.0
    idx = bisect.bisect_right(sorted_values, value)
    return idx / len(sorted_values)


def importance(
    total_dwell_ms: int | None,
    views: int | None,
    impressions_count: int,
    has_interaction: bool,
    day_view_distribution: list[float],
) -> float:
    """Compute an importance score in [0, 1] for a single unique tweet.

    Arguments:
        total_dwell_ms: sum of dwell_ms across all impressions of this tweet
            today. ``None`` / ``0`` → dwell contributes nothing.
        views: latest captured view count for the tweet. ``None`` / ``0`` →
            engagement contributes nothing.
        impressions_count: how many impression rows reference this tweet today.
        has_interaction: True if the user liked/rt/reply/bookmarked this tweet.
        day_view_distribution: sorted list of view counts across all today's
            unique tweets — used to compute the percentile rank. Sort is the
            caller's responsibility (one sort per day, not per tweet).
    """
    dwell_norm = min((total_dwell_ms or 0) / _DWELL_CAP_MS, 1.0)
    eng_pct_norm = _percentile(views or 0, day_view_distribution)
    extra_impressions = max((impressions_count or 0) - _IMPRESSION_BASELINE, 0)
    impression_bonus = min(
        extra_impressions / (_IMPRESSION_SATURATION - _IMPRESSION_BASELINE),
        1.0,
    )
    interaction_flag = 1.0 if has_interaction else 0.0
    raw = (
        0.40 * dwell_norm
        + 0.30 * eng_pct_norm
        + 0.20 * impression_bonus
        + 0.10 * interaction_flag
    )
    return round(raw, 2)


def view_distribution(view_counts: Iterable[int | None]) -> list[float]:
    """Helper: build the sorted day_view_distribution argument from raw rows.

    Nones and zeros are filtered out so percentile calculations aren't
    dragged toward the low end by tweets we haven't captured engagement for
    yet. Stable output — same input, same list.
    """
    cleaned = [float(v) for v in view_counts if v]
    cleaned.sort()
    return cleaned
