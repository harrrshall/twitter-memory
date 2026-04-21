"""Markdown rendering for export_day."""
from __future__ import annotations

import json
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp_server import queries, settings


def _fmt_dwell_ms(ms: int | None) -> str:
    if not ms or ms <= 0:
        return "0s"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    r = s - m * 60
    return f"{m}m {int(r)}s"


def _fmt_count(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,}"


def _fmt_duration_ms(ms: int | None) -> str:
    if not ms or ms <= 0:
        return "0m"
    s = ms // 1000
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def _local_time(iso_utc: str | None, tz: ZoneInfo) -> str:
    if not iso_utc:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_utc)
        return dt.astimezone(tz).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return iso_utc


def _local_hm(iso_utc: str | None, tz: ZoneInfo) -> str:
    if not iso_utc:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_utc)
        return dt.astimezone(tz).strftime("%H:%M")
    except (TypeError, ValueError):
        return iso_utc


def _tweet_url(handle: str | None, tweet_id: str) -> str:
    h = handle or "i"
    return f"https://x.com/{h}/status/{tweet_id}"


def _preview(text: str | None, n: int = 80) -> str:
    if not text:
        return ""
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def render_summary(s: dict, tz: ZoneInfo) -> str:
    inter = s.get("interactions") or {}
    lines = [
        "## Summary",
        "",
        f"- **Total time on Twitter:** {_fmt_duration_ms(s.get('total_dwell_ms'))}",
        f"- **Sessions:** {s.get('sessions', 0)}",
        f"- **Tweets seen:** {s.get('tweets_seen', 0)} ({s.get('unique_authors', 0)} unique authors)",
        "- **Interactions:** "
        + " · ".join(
            f"{inter.get(a, 0)} {a}"
            for a in ("like", "retweet", "reply", "bookmark", "profile_click", "expand")
            if inter.get(a)
        )
        if any(inter.values()) else "- **Interactions:** none",
        f"- **Searches:** {s.get('searches', 0)}",
        "",
    ]
    return "\n".join(lines)


def render_sessions(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "## Sessions\n\n_no sessions_\n\n"
    out = ["## Sessions", ""]
    for i, r in enumerate(rows, 1):
        feeds = []
        try:
            feeds = json.loads(r.get("feeds_visited") or "[]")
        except (TypeError, ValueError):
            pass
        out.append(
            f"### Session {i} — {_local_hm(r.get('started_at'), tz)} to "
            f"{_local_hm(r.get('ended_at'), tz)} ({_fmt_duration_ms(r.get('total_dwell_ms'))})"
        )
        if feeds:
            out.append(f"- Feeds: {', '.join(feeds)}")
        out.append(f"- Tweets seen: {r.get('tweet_count') or 0}")
        out.append("")
    return "\n".join(out)


def render_searches(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "## Searches\n\n_no searches_\n\n"
    out = ["## Searches", ""]
    for r in rows:
        out.append(f"- {_local_hm(r.get('timestamp'), tz)} — `{r.get('query','')}`")
    out.append("")
    return "\n".join(out)


def render_interactions(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "## Interactions\n\n_none_\n\n"
    out = ["## Interactions", ""]
    verbs = {
        "like": "liked",
        "retweet": "retweeted",
        "reply": "replied to",
        "bookmark": "bookmarked",
        "profile_click": "clicked profile of",
        "expand": "expanded",
    }
    for r in rows:
        tid = r.get("tweet_id") or ""
        handle = r.get("handle") or ""
        verb = verbs.get(r.get("action") or "", r.get("action") or "")
        txt = _preview(r.get("text"), 80)
        url = _tweet_url(handle, tid)
        handle_fmt = f"@{handle}" if handle else "(unknown)"
        out.append(
            f"- {_local_hm(r.get('timestamp'), tz)} · **{verb}** {handle_fmt}"
            + (f" — \"{txt}\"" if txt else "")
            + f" · [link]({url})"
        )
    out.append("")
    return "\n".join(out)


def render_top_authors(by_impr: list[dict], by_dwell: list[dict]) -> str:
    out = ["## Top authors", ""]
    out.append("### By impressions")
    if by_impr:
        for i, r in enumerate(by_impr, 1):
            out.append(f"{i}. @{r.get('handle','')} — {r.get('n',0)} tweets seen")
    else:
        out.append("_none_")
    out.append("")
    out.append("### By dwell time")
    if by_dwell:
        for i, r in enumerate(by_dwell, 1):
            out.append(f"{i}. @{r.get('handle','')} — {_fmt_dwell_ms(r.get('dwell_ms'))}")
    else:
        out.append("_none_")
    out.append("")
    return "\n".join(out)


def render_threads(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "## Threads\n\n_no threads_\n\n"
    out = ["## Threads", ""]
    # Group rows by conversation_id preserving order.
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["conversation_id"], []).append(r)
    for i, (conv_id, items) in enumerate(groups.items(), 1):
        first_handle = items[0].get("handle") or ""
        out.append(
            f"### Thread {i} — {len(items)} tweets from @{first_handle} (conversation_id={conv_id})"
        )
        out.append("")
        for it in items:
            url = _tweet_url(it.get("handle"), it["tweet_id"])
            out.append(f"> **@{it.get('handle','')}** {_local_hm(it.get('created_at'), tz)} · [link]({url})")
            txt = (it.get("text") or "").replace("\n", " ")
            out.append(f"> {txt}")
            out.append(">")
        out.append("")
    return "\n".join(out)


def render_impressions(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "## Impressions\n\n_none_\n\n"
    out = ["## Impressions", ""]
    # Group by session, then feed_source.
    current_key: tuple[str | None, str | None] | None = None
    session_idx: dict[str | None, int] = {}
    for r in rows:
        key = (r.get("session_id"), r.get("feed_source"))
        if key != current_key:
            sid = r.get("session_id")
            if sid not in session_idx:
                session_idx[sid] = len(session_idx) + 1
            idx = session_idx[sid]
            feed = r.get("feed_source") or "unknown"
            out.append(f"### Session {idx} · {feed}")
            out.append("")
            current_key = key
        handle = r.get("handle") or ""
        tid = r.get("tweet_id") or ""
        url = _tweet_url(handle, tid)
        text = (r.get("text") or "").strip()
        eng_bits = []
        if r.get("likes") is not None:
            eng_bits.append(f"likes: {_fmt_count(r.get('likes'))}")
        if r.get("retweets") is not None:
            eng_bits.append(f"retweets: {_fmt_count(r.get('retweets'))}")
        if r.get("replies") is not None:
            eng_bits.append(f"replies: {_fmt_count(r.get('replies'))}")
        if r.get("views"):
            eng_bits.append(f"views: {_fmt_count(r.get('views'))}")
        out.append(
            f"**@{handle}** · {_local_time(r.get('first_seen_at'), tz)} · "
            f"dwell {_fmt_dwell_ms(r.get('dwell_ms'))} · [link]({url})"
        )
        if text:
            out.append(text)
        if eng_bits:
            out.append(f"_{' · '.join(eng_bits)}_")
        out.append("")
        out.append("---")
        out.append("")
    return "\n".join(out)


def build_markdown(
    db_path: Path,
    target_date: date_cls,
    exclude: list[str] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    """Build the markdown for a single local calendar day.

    Returns (markdown, sections_included, stats) where stats has tweet_count,
    interaction_count, etc.
    """
    exclude = exclude or []
    unknown = [s for s in exclude if s not in settings.ALL_SECTIONS]
    if unknown:
        raise ValueError(f"unknown section(s) in exclude: {unknown}. Allowed: {settings.ALL_SECTIONS}")
    included = [s for s in settings.ALL_SECTIONS if s not in exclude]

    tz = settings.local_tz()
    day_start, day_end = queries.day_window_utc(target_date, tz)

    db = queries.connect_ro(db_path)
    try:
        blocks: list[str] = []
        blocks.append(f"# Twitter — {target_date.isoformat()}\n")
        blocks.append(
            f"_Generated {datetime.now(tz).isoformat(timespec='seconds')} · Local timezone: {str(tz)}_\n"
        )

        summary_data: dict[str, Any] = {}
        if "summary" in included:
            summary_data = queries.summary(db, day_start, day_end)
            blocks.append(render_summary(summary_data, tz))

        if "sessions" in included:
            rows = queries.sessions_rows(db, day_start, day_end)
            blocks.append(render_sessions(rows, tz))

        if "searches" in included:
            rows = queries.searches_rows(db, day_start, day_end)
            blocks.append(render_searches(rows, tz))

        if "interactions" in included:
            rows = queries.interactions_rows(db, day_start, day_end)
            blocks.append(render_interactions(rows, tz))

        if "top_authors" in included:
            by_i = queries.top_authors_by_impressions(db, day_start, day_end)
            by_d = queries.top_authors_by_dwell(db, day_start, day_end)
            blocks.append(render_top_authors(by_i, by_d))

        if "threads" in included:
            rows = queries.threads_rows(db, day_start, day_end)
            blocks.append(render_threads(rows, tz))

        if "impressions" in included:
            rows = queries.impressions_rows(db, day_start, day_end)
            blocks.append(render_impressions(rows, tz))

        if not summary_data:
            summary_data = queries.summary(db, day_start, day_end)

        stats = {
            "tweet_count": summary_data.get("tweets_seen", 0),
            "interaction_count": sum((summary_data.get("interactions") or {}).values()),
            "session_count": summary_data.get("sessions", 0),
            "search_count": summary_data.get("searches", 0),
        }
        markdown = "\n".join(blocks)
        return markdown, included, stats
    finally:
        db.close()


def write_export(
    db_path: Path,
    target_date: date_cls,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    settings.ensure_exports_dir()
    markdown, included, stats = build_markdown(db_path, target_date, exclude)

    file_path = settings.EXPORTS_DIR / f"{target_date.isoformat()}.md"
    file_path.write_text(markdown, encoding="utf-8")

    byte_size = len(markdown.encode("utf-8"))
    truncated = byte_size > settings.INLINE_CONTENT_CAP_BYTES
    return {
        "file_path": str(file_path),
        "sections_included": included,
        "tweet_count": stats["tweet_count"],
        "interaction_count": stats["interaction_count"],
        "session_count": stats["session_count"],
        "search_count": stats["search_count"],
        "byte_size": byte_size,
        "content": "" if truncated else markdown,
        "truncated": truncated,
    }
