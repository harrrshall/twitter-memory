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


def render_sessions(
    rows: list[dict],
    tz: ZoneInfo,
    nav_by_session: dict[str, list[dict]] | None = None,
    rel_by_session: dict[str, list[dict]] | None = None,
) -> str:
    if not rows:
        return "## Sessions\n\n_no sessions_\n\n"
    nav_by_session = nav_by_session or {}
    rel_by_session = rel_by_session or {}
    out = ["## Sessions", ""]
    for i, r in enumerate(rows, 1):
        feeds = []
        try:
            feeds = json.loads(r.get("feeds_visited") or "[]")
        except (TypeError, ValueError):
            pass
        sid = r.get("session_id")
        out.append(
            f"### Session {i} — {_local_hm(r.get('started_at'), tz)} to "
            f"{_local_hm(r.get('ended_at'), tz)} ({_fmt_duration_ms(r.get('total_dwell_ms'))})"
        )
        if feeds:
            out.append(f"- Feeds: {', '.join(feeds)}")
        out.append(f"- Tweets seen: {r.get('tweet_count') or 0}")
        # Nav path — compact chain of feed_source transitions.
        navs = nav_by_session.get(sid) or []
        if navs:
            chain: list[str] = []
            for n in navs:
                before = n.get("feed_source_before") or "?"
                after = n.get("feed_source_after") or "?"
                if not chain:
                    chain.append(before)
                if chain[-1] != after:
                    chain.append(after)
            if len(chain) > 1:
                out.append(f"- Nav path: {' → '.join(chain)}")
        # Relationship changes in this session.
        rels = rel_by_session.get(sid) or []
        if rels:
            bits = []
            for rr in rels:
                h = rr.get("handle")
                who = f"@{h}" if h else rr.get("target_user_id") or "?"
                bits.append(f"{rr.get('action','?')} {who}")
            out.append(f"- Relationship changes: {', '.join(bits)}")
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


def render_impressions(
    rows: list[dict],
    tz: ZoneInfo,
    revisits: dict[tuple[str, str], int] | None = None,
) -> str:
    if not rows:
        return "## Impressions\n\n_none_\n\n"
    revisits = revisits or {}
    # Collapse duplicate rows per (session, tweet_id) so the section is readable
    # — we keep the first row and decorate with ×N where N > 1. Duplicates are
    # still visible in the raw `impressions` table and in the timeline.
    seen: set[tuple[str | None, str | None]] = set()
    deduped: list[dict] = []
    for r in rows:
        key = (r.get("session_id"), r.get("tweet_id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    out = ["## Impressions", ""]
    current_key: tuple[str | None, str | None] | None = None
    session_idx: dict[str | None, int] = {}
    for r in deduped:
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
        revisit_count = revisits.get((r.get("session_id"), r.get("tweet_id")), 0)
        revisit_suffix = f" · **×{revisit_count}** (revisited)" if revisit_count > 1 else ""
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
            f"dwell {_fmt_dwell_ms(r.get('dwell_ms'))}{revisit_suffix} · [link]({url})"
        )
        if text:
            out.append(text)
        if eng_bits:
            out.append(f"_{' · '.join(eng_bits)}_")
        out.append("")
        out.append("---")
        out.append("")
    return "\n".join(out)


def render_link_outs(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "## Link-outs\n\n_no links clicked_\n\n"
    out = ["## Link-outs", ""]
    # Group by domain so agents can see what sources the user kept returning to.
    by_domain: dict[str, list[dict]] = {}
    for r in rows:
        d = r.get("domain") or "(unknown)"
        by_domain.setdefault(d, []).append(r)
    # Order domains by frequency desc, then name.
    ordered = sorted(by_domain.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for domain, items in ordered:
        out.append(f"### {domain} ({len(items)})")
        for r in items:
            kind = r.get("link_kind") or "?"
            mods = r.get("modifiers") or ""
            mods_suffix = f" · `{mods}`" if mods else ""
            src_bits = ""
            handle = r.get("handle")
            tweet_id = r.get("tweet_id")
            if handle and tweet_id:
                src_bits = f" · from [@{handle}]({_tweet_url(handle, tweet_id)}): \"{_preview(r.get('text'), 60)}\""
            elif tweet_id:
                src_bits = f" · from tweet {tweet_id}"
            out.append(
                f"- {_local_hm(r.get('timestamp'), tz)} · `{kind}`{mods_suffix} · "
                f"{r.get('url','')}{src_bits}"
            )
        out.append("")
    return "\n".join(out)


def render_selections(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "## Selections\n\n_no text selected_\n\n"
    out = ["## Selections", ""]
    for r in rows:
        handle = r.get("handle") or ""
        tid = r.get("tweet_id") or ""
        via = r.get("via") or "select"
        text = (r.get("text") or "").replace("\n", " ").strip()
        src = ""
        if handle and tid:
            src = f" · from [@{handle}]({_tweet_url(handle, tid)})"
        elif tid:
            src = f" · from tweet {tid}"
        out.append(
            f"- {_local_hm(r.get('timestamp'), tz)} · `{via}`{src}"
        )
        if text:
            out.append(f"  > {text}")
    out.append("")
    return "\n".join(out)


def render_media(rows: list[dict], tz: ZoneInfo) -> str:
    if not rows:
        return "## Media\n\n_no media opened_\n\n"
    out = ["## Media", ""]
    # Group by tweet.
    by_tweet: dict[str, list[dict]] = {}
    for r in rows:
        by_tweet.setdefault(r.get("tweet_id") or "(unknown)", []).append(r)
    for tid, items in by_tweet.items():
        handle = items[0].get("handle") or ""
        url = _tweet_url(handle, tid) if tid != "(unknown)" else ""
        header = f"- [@{handle}]({url})" if handle and url else f"- tweet {tid}"
        kinds = ", ".join(
            f"{it.get('media_kind','?')}#{it.get('media_index','?')} @ {_local_hm(it.get('timestamp'), tz)}"
            for it in items
        )
        preview = _preview(items[0].get("text"), 80)
        out.append(f"{header} · {kinds}" + (f" — \"{preview}\"" if preview else ""))
    out.append("")
    return "\n".join(out)


def render_timeline(rows: list[dict], tz: ZoneInfo) -> str:
    """The centerpiece: every event in chronological order, grouped by session.
    Each line is `HH:MM:SS · kind · compact payload` so an agent reading the
    markdown can reconstruct the user's journey without additional joins."""
    if not rows:
        return "## Timeline\n\n_no events_\n\n"
    out = ["## Timeline", ""]
    current_sid: str | None = object()  # sentinel: nothing yet
    session_idx: dict[str | None, int] = {}
    for r in rows:
        sid = r.get("session_id")
        if sid != current_sid:
            if sid not in session_idx:
                session_idx[sid] = len(session_idx) + 1
            label = f"Session {session_idx[sid]}" if sid else "Unsessioned"
            out.append(f"### {label}")
            out.append("")
            current_sid = sid
        t = _local_time(r.get("ts"), tz)
        kind = r.get("kind") or ""
        payload = r.get("payload") or "{}"
        try:
            p = json.loads(payload)
        except (TypeError, ValueError):
            p = {}
        out.append(f"- `{t}` **{kind}** {_timeline_compact(kind, p)}")
    out.append("")
    return "\n".join(out)


def _timeline_compact(kind: str, p: dict) -> str:
    """Render the payload compactly for timeline lines. Keeps agent-readable
    text next to semantic keys; avoids dumping raw JSON."""
    h = p.get("handle")
    tweet = p.get("tweet_id")
    handle_bit = f"@{h}" if h else ""
    if kind == "impression":
        bits = [handle_bit] if handle_bit else []
        if p.get("feed_source"):
            bits.append(f"feed:{p['feed_source']}")
        if p.get("dwell_ms") is not None:
            bits.append(f"dwell:{p['dwell_ms']}ms")
        if p.get("text"):
            bits.append(f"\"{p['text']}\"")
        return " · ".join(bits)
    if kind.startswith("interaction:"):
        return handle_bit + (f" (tweet {tweet})" if tweet else "")
    if kind == "search":
        return f"\"{p.get('query','')}\""
    if kind == "link":
        lk = p.get("link_kind") or ""
        mods = p.get("modifiers") or ""
        suffix = f" [{mods}]" if mods else ""
        return f"{p.get('url','')} ({lk}){suffix}"
    if kind.startswith("media:"):
        return f"tweet {tweet} idx {p.get('media_index')}"
    if kind == "select":
        via = p.get("via") or "select"
        txt = (p.get("text") or "").replace("\n", " ")
        return f"({via}) \"{txt[:100]}\""
    if kind == "nav":
        return f"{p.get('from_path','?')} → {p.get('to_path','?')}"
    if kind.startswith("rel:"):
        return handle_bit or p.get("target_user_id") or ""
    return json.dumps(p, separators=(",", ":"))


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

        # Preload nav + relationship rows once — used to enrich the sessions
        # section as well as being standalone timeline inputs.
        nav_rows = queries.nav_events_rows(db, day_start, day_end)
        rel_rows = queries.relationship_changes_rows(db, day_start, day_end)
        nav_by_session: dict[str, list[dict]] = {}
        for n in nav_rows:
            nav_by_session.setdefault(n.get("session_id") or "", []).append(n)
        rel_by_session: dict[str, list[dict]] = {}
        for rr in rel_rows:
            rel_by_session.setdefault(rr.get("session_id") or "", []).append(rr)

        if "sessions" in included:
            rows = queries.sessions_rows(db, day_start, day_end)
            blocks.append(render_sessions(rows, tz, nav_by_session, rel_by_session))

        if "searches" in included:
            rows = queries.searches_rows(db, day_start, day_end)
            blocks.append(render_searches(rows, tz))

        if "interactions" in included:
            rows = queries.interactions_rows(db, day_start, day_end)
            blocks.append(render_interactions(rows, tz))

        if "link_outs" in included:
            rows = queries.link_clicks_rows(db, day_start, day_end)
            blocks.append(render_link_outs(rows, tz))

        if "selections" in included:
            rows = queries.text_selections_rows(db, day_start, day_end)
            blocks.append(render_selections(rows, tz))

        if "media" in included:
            rows = queries.media_events_rows(db, day_start, day_end)
            blocks.append(render_media(rows, tz))

        if "top_authors" in included:
            by_i = queries.top_authors_by_impressions(db, day_start, day_end)
            by_d = queries.top_authors_by_dwell(db, day_start, day_end)
            blocks.append(render_top_authors(by_i, by_d))

        if "threads" in included:
            rows = queries.threads_rows(db, day_start, day_end)
            blocks.append(render_threads(rows, tz))

        if "timeline" in included:
            rows = queries.session_timeline(db, day_start, day_end)
            blocks.append(render_timeline(rows, tz))

        if "impressions" in included:
            rows = queries.impressions_rows(db, day_start, day_end)
            revisits = queries.revisits(db, day_start, day_end)
            blocks.append(render_impressions(rows, tz, revisits))

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
