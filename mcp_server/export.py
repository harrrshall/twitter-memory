"""Markdown rendering for export_day."""
from __future__ import annotations

import html
import json
import statistics
from datetime import date as date_cls, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp_server import anomalies, queries, scoring, settings, topics


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
    t = " ".join(_clean_text(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _clean_text(text: str | None) -> str:
    """Unescape HTML entities (``&gt;`` → ``>``, ``&amp;`` → ``&`` …) so the
    LLM consumer doesn't have to. Applied on every path that surfaces tweet
    text to the markdown."""
    if not text:
        return ""
    return html.unescape(text)


def _has_media(media_json: str | None) -> bool:
    if not media_json:
        return False
    try:
        parsed = json.loads(media_json)
    except (TypeError, ValueError):
        return False
    if isinstance(parsed, list):
        return len(parsed) > 0
    if isinstance(parsed, dict):
        return bool(parsed)
    return False


def _stub_free(tweets: list[dict]) -> list[dict]:
    """Filter out tweets with no handle AND no text — pure stubs that add
    noise to human-readable sections. Stubs still appear in the raw
    impressions/timeline output so the full log stays complete."""
    out = []
    for t in tweets:
        handle = (t.get("handle") or "").strip()
        text = _clean_text(t.get("text")).strip()
        if not handle and not text:
            continue
        out.append(t)
    return out


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
            txt = _clean_text(it.get("text")).replace("\n", " ")
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
        text = _clean_text(r.get("text")).strip()
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
        text = _clean_text(r.get("text")).replace("\n", " ").strip()
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
            bits.append(f"\"{_clean_text(p['text'])}\"")
        return " · ".join(bits)
    if kind.startswith("interaction:"):
        return handle_bit + (f" (tweet {tweet})" if tweet else "")
    if kind == "search":
        return f"\"{_clean_text(p.get('query',''))}\""
    if kind == "link":
        lk = p.get("link_kind") or ""
        mods = p.get("modifiers") or ""
        suffix = f" [{mods}]" if mods else ""
        return f"{p.get('url','')} ({lk}){suffix}"
    if kind.startswith("media:"):
        return f"tweet {tweet} idx {p.get('media_index')}"
    if kind == "select":
        via = p.get("via") or "select"
        txt = _clean_text(p.get("text")).replace("\n", " ")
        return f"({via}) \"{txt[:100]}\""
    if kind == "nav":
        return f"{p.get('from_path','?')} → {p.get('to_path','?')}"
    if kind.startswith("rel:"):
        return handle_bit or p.get("target_user_id") or ""
    return json.dumps(p, separators=(",", ":"))


def _enrich_with_topics(unique_tweets: list[dict]) -> list[dict]:
    """Tag every unique tweet with its topic buckets (see mcp_server.topics).
    Adds a ``topics`` key; does not mutate the caller's list."""
    out = []
    for t in unique_tweets:
        enriched = dict(t)
        enriched["topics"] = topics.tag_tweet(_clean_text(t.get("text")), t.get("handle"))
        out.append(enriched)
    return out


def _score_tweets(unique_tweets: list[dict]) -> list[dict]:
    """Add an ``importance`` float to every unique tweet row."""
    view_dist = scoring.view_distribution(t.get("views") for t in unique_tweets)
    out = []
    for t in unique_tweets:
        enriched = dict(t)
        enriched["importance"] = scoring.importance(
            total_dwell_ms=t.get("total_dwell_ms"),
            views=t.get("views"),
            impressions_count=t.get("impressions_count") or 0,
            has_interaction=bool(t.get("user_had_interaction")),
            day_view_distribution=view_dist,
        )
        out.append(enriched)
    return out


def _read_speed_summary(unique_tweets: list[dict], impressions: list[dict]) -> str | None:
    """Compute a human summary of reading speed for the TL;DR.

    WPM per impression = (char_count / 5) / (dwell_ms / 60000), using
    impressions with ``dwell_ms >= 2000`` so division noise from zero-dwell
    scroll-bys doesn't dominate. Needs at least 5 qualifying impressions to
    surface (otherwise the number is too noisy to be useful).

    Returns e.g. "median 320 WPM across 42 real reads · 87% fast-scroll (<500ms)"
    or ``None`` when there aren't enough samples.
    """
    if not impressions:
        return None
    char_by_id = {
        t.get("tweet_id"): len(_clean_text(t.get("text")) or "")
        for t in unique_tweets
    }
    wpms: list[float] = []
    fast_scroll = 0
    for im in impressions:
        dwell = im.get("dwell_ms") or 0
        if dwell < 500:
            fast_scroll += 1
        if dwell < 2000:
            continue
        chars = char_by_id.get(im.get("tweet_id"))
        if not chars:
            continue
        words = chars / 5.0
        minutes = dwell / 60000.0
        if minutes <= 0:
            continue
        wpm = words / minutes
        # Implausible WPM (>2000) = likely empty text or stub row; drop.
        if wpm > 2000:
            continue
        wpms.append(wpm)
    if len(wpms) < 3:
        return None
    median_wpm = int(sorted(wpms)[len(wpms) // 2])
    fast_pct = round(100 * fast_scroll / len(impressions)) if impressions else 0
    return (
        f"median {median_wpm} WPM across {len(wpms)} real read{'s' if len(wpms) != 1 else ''} "
        f"· {fast_pct}% fast-scroll (<500ms)"
    )


def render_tldr(
    summary: dict,
    unique_tweets: list[dict],
    sessions: list[dict],
    impressions: list[dict],
    tz: ZoneInfo,
) -> str:
    """LLM-first digest — everything worth knowing in six bullets.

    Collapses topic rollups, the tweets that actually got read (dwell ≥3s),
    algorithmic-pressure tweets (seen ≥3×), and anomaly signals into a
    single block at the top of the export. The consuming LLM can answer
    most 'what did I read today?' questions without scrolling past this.
    """
    total_impr = summary.get("tweets_seen", 0)
    unique_count = summary.get("unique_tweets", 0)
    sessions_n = summary.get("sessions", 0)
    authors_n = summary.get("unique_authors", 0)
    total_dwell = _fmt_duration_ms(summary.get("total_dwell_ms"))

    # Topic rollup
    topic_counts: dict[str, int] = {}
    for t in unique_tweets:
        for tag in t.get("topics") or ["untagged"]:
            topic_counts[tag] = topic_counts.get(tag, 0) + 1
    top_topics = sorted(
        ((k, v) for k, v in topic_counts.items() if k != "untagged"),
        key=lambda kv: -kv[1],
    )[:4]

    # Actually read
    read = [
        t for t in unique_tweets
        if (t.get("total_dwell_ms") or 0) >= 3000
    ]
    read.sort(key=lambda t: -(t.get("total_dwell_ms") or 0))

    # Algorithmic pressure
    pressure = [
        t for t in unique_tweets
        if (t.get("impressions_count") or 0) >= 3
    ]
    pressure.sort(key=lambda t: -(t.get("impressions_count") or 0))

    # Actions
    inter = summary.get("interactions") or {}
    action_bits = [f"{v} {k}" for k, v in inter.items() if v]
    searches = summary.get("searches", 0)
    if searches:
        action_bits.append(f"{searches} search{'es' if searches != 1 else ''}")

    # Scroll intensity: fraction of impressions with dwell=0
    zero_dwell = sum(1 for i in impressions if not (i.get("dwell_ms") or 0))
    total_imp_rows = len(impressions)
    zero_pct = round(100 * zero_dwell / total_imp_rows) if total_imp_rows else 0

    # Anomalies (topic drift requires per-impression topic tagging)
    imp_topics: list[tuple[str, list[str]]] = []
    tweet_topic_map = {t.get("tweet_id"): t.get("topics") for t in unique_tweets}
    for im in impressions:
        tid = im.get("tweet_id")
        tags = tweet_topic_map.get(tid) or ["untagged"]
        imp_topics.append((im.get("first_seen_at") or "", tags))
    anomaly_hits = anomalies.detect(sessions, impressions, imp_topics, tz)

    lines = ["## TL;DR", ""]
    headline = (
        f"- **{total_dwell} across {sessions_n} session{'s' if sessions_n != 1 else ''}, "
        f"{total_impr} impressions of {unique_count} unique tweet{'s' if unique_count != 1 else ''} "
        f"from {authors_n} author{'s' if authors_n != 1 else ''}.**"
    )
    if action_bits:
        headline = headline[:-3] + f". Actions: {', '.join(action_bits)}.**"
    else:
        headline = headline[:-3] + ". No interactions, no searches.**"
    lines.append(headline)

    if top_topics:
        topic_str = ", ".join(f"`{k}` ({v} tweet{'s' if v != 1 else ''})" for k, v in top_topics)
        lines.append(f"- **Topics that dominated:** {topic_str}.")

    if read:
        read_bits = ", ".join(
            f"@{t.get('handle')} ({_fmt_dwell_ms(t.get('total_dwell_ms'))})"
            for t in read[:5]
        )
        lines.append(f"- **Actually read (dwell ≥3s):** {len(read)} tweet{'s' if len(read) != 1 else ''} — {read_bits}.")
    else:
        lines.append("- **Actually read (dwell ≥3s):** none — every tweet flew by.")

    if pressure:
        pr_bits = ", ".join(
            f"@{t.get('handle')} ×{t.get('impressions_count')}"
            for t in pressure[:6]
        )
        lines.append(f"- **Algorithmic pressure (seen ≥3×):** {pr_bits}.")

    if zero_pct >= 50 and total_imp_rows:
        lines.append(f"- **Scroll intensity:** {zero_pct}% of impressions had 0s dwell (very fast scroll).")

    wpm_summary = _read_speed_summary(unique_tweets, impressions)
    if wpm_summary:
        lines.append(f"- **Read speed:** {wpm_summary}.")

    if anomaly_hits:
        lines.append("- **Anomalies:**")
        for a in anomaly_hits:
            lines.append(f"  - {a}")

    lines.append("")
    return "\n".join(lines)


def render_tweets_ranked(unique_tweets: list[dict], tz: ZoneInfo) -> str:
    """Deduplicated unique-tweet table sorted by importance score.

    Compact table — one row per unique tweet, ranked by the scoring
    module's weighted signal. This is the section an LLM should read
    first when asked 'show me the most notable tweets'.
    """
    filtered = _stub_free(unique_tweets)
    if not filtered:
        return "## Tweets (ranked by importance)\n\n_no tweets_\n\n"
    ranked = sorted(
        filtered,
        key=lambda t: (
            -(t.get("importance") or 0),
            -(t.get("impressions_count") or 0),
            -(t.get("views") or 0),
        ),
    )
    out = [
        "## Tweets (ranked by importance)",
        "",
        f"> {len(ranked)} unique tweets. Columns: `rank | importance | handle | tid | ×seen | total dwell | engagement (l/rt/rp/v) | topics | text`",
        "",
        "| # | imp | handle | tid | × | dwell | engagement | act | topics | text |",
        "|--:|---:|:---|:---|--:|---:|:---|:---|:---|:---|",
    ]
    for rank, t in enumerate(ranked, 1):
        imp = f"{t.get('importance', 0):.2f}"
        handle = t.get("handle") or "(stub)"
        tid = t.get("tweet_id") or ""
        impressions_count = t.get("impressions_count") or 1
        dwell = _fmt_dwell_ms(t.get("total_dwell_ms"))
        eng_parts = [
            _fmt_count(t.get("likes")),
            _fmt_count(t.get("retweets")),
            _fmt_count(t.get("replies")),
            _fmt_count(t.get("views")),
        ]
        eng = "/".join(eng_parts)
        act = "✓" if t.get("user_had_interaction") else "—"
        tag_str = ",".join(f"`{x}`" for x in (t.get("topics") or []))
        text = _preview(t.get("text"), 100)
        if _has_media(t.get("media_json")):
            text = (text + " [media]").strip()
        # Escape pipes so table doesn't break
        text = text.replace("|", "\\|")
        out.append(
            f"| {rank} | {imp} | @{handle} | t{tid} | {impressions_count} | "
            f"{dwell} | {eng} | {act} | {tag_str} | {text} |"
        )
    out.append("")
    return "\n".join(out)


def render_repeat_exposure(unique_tweets: list[dict], tz: ZoneInfo) -> str:
    """Tweets the algorithm showed ≥3 times today — surfaced explicitly so an
    LLM can reason about what the feed is pushing at the user."""
    filtered = _stub_free(unique_tweets)
    pressured = [
        t for t in filtered if (t.get("impressions_count") or 0) >= 3
    ]
    if not pressured:
        return "## Repeat-exposure (algorithmic pressure)\n\n_no tweet was shown to you 3+ times today._\n\n"
    pressured.sort(key=lambda t: -(t.get("impressions_count") or 0))
    out = [
        "## Repeat-exposure (algorithmic pressure)",
        "",
        "> Tweets the feed showed you ≥3× today. The dwell column is total across all impressions.",
        "",
    ]
    for t in pressured:
        tid = t.get("tweet_id") or ""
        handle = t.get("handle") or "(stub)"
        imps = t.get("impressions_count") or 0
        dwell = _fmt_dwell_ms(t.get("total_dwell_ms"))
        sess_csv = t.get("sessions_hit_csv") or ""
        sess_count = len([s for s in sess_csv.split(",") if s])
        preview = _preview(t.get("text"), 120)
        tag_str = ", ".join(f"`{x}`" for x in (t.get("topics") or []))
        out.append(
            f"- `t{tid}` **@{handle}** — ×{imps} across {sess_count} session{'s' if sess_count != 1 else ''} · "
            f"total dwell {dwell} · {tag_str}"
        )
        if preview:
            out.append(f"  > {preview}")
    out.append("")
    return "\n".join(out)


def render_topics(unique_tweets: list[dict], tz: ZoneInfo) -> str:
    """Per-topic rollup: how many tweets hit each bucket, how much dwell
    they collectively got, and the notable handles involved."""
    filtered = _stub_free(unique_tweets)
    if not filtered:
        return "## Topics\n\n_no tweets to tag_\n\n"
    buckets: dict[str, list[dict]] = {}
    for t in filtered:
        for tag in t.get("topics") or ["untagged"]:
            buckets.setdefault(tag, []).append(t)
    # Rank buckets by tweet count desc; keep 'untagged' last.
    ordered = sorted(
        buckets.items(),
        key=lambda kv: (kv[0] == "untagged", -len(kv[1]), kv[0]),
    )
    out = [
        "## Topics",
        "",
        "> Heuristic tagging (keyword + hashtag rules — not ML). Multi-label allowed. See `## Schema` for bucket definitions.",
        "",
        "| Topic | Tweets | Total dwell | Notable handles |",
        "|:---|---:|---:|:---|",
    ]
    for tag, tweets_in in ordered:
        total_dwell = sum(t.get("total_dwell_ms") or 0 for t in tweets_in)
        handles_sorted = sorted(
            tweets_in,
            key=lambda t: -(t.get("total_dwell_ms") or 0) - (t.get("impressions_count") or 0),
        )
        notable = ", ".join(
            f"@{t.get('handle')}" for t in handles_sorted[:4] if t.get("handle")
        )
        out.append(
            f"| `{tag}` | {len(tweets_in)} | {_fmt_dwell_ms(total_dwell)} | {notable} |"
        )
    out.append("")
    return "\n".join(out)


def render_authors_v2(rows: list[dict], tz: ZoneInfo) -> str:
    """Top authors with follower_count + verified context. Replaces the
    separate by-impressions / by-dwell ranked lists with one table."""
    if not rows:
        return "## Authors\n\n_no authors_\n\n"
    out = [
        "## Authors",
        "",
        "> Top authors seen today. Followers and verified come from whatever GraphQL responses we captured during the day — blank when unknown.",
        "",
        "| Handle | Impr | Unique | Total dwell | Followers | Verified | Display name |",
        "|:---|---:|---:|---:|---:|:---:|:---|",
    ]
    for r in rows[:15]:
        handle = r.get("handle") or "(unknown)"
        impr = r.get("impressions_count") or 0
        unique = r.get("unique_tweets") or 0
        dwell = _fmt_dwell_ms(r.get("total_dwell_ms"))
        followers = _fmt_count(r.get("follower_count"))
        verified = "✓" if r.get("verified") else "—"
        name = _clean_text(r.get("display_name") or "").replace("|", "\\|")
        out.append(
            f"| @{handle} | {impr} | {unique} | {dwell} | {followers} | {verified} | {name} |"
        )
    out.append("")
    return "\n".join(out)


def render_schema() -> str:
    """Interpretive guide for the LLM reader. Token-light but high-leverage:
    documents the score formula, dwell semantics, tid prefix, topic rules
    disclaimer, and anomaly rules so the consuming LLM stops guessing."""
    return (
        "## Schema (v2)\n"
        "\n"
        "Tagging rules, scoring formulas, field semantics — tokens well spent so the consuming LLM interprets correctly.\n"
        "\n"
        "- **importance ∈ [0, 1]** = `0.40·dwell_norm + 0.30·eng_pct_norm + 0.20·impression_bonus + 0.10·interaction_flag`.\n"
        "  - `dwell_norm` = min(total_dwell_ms / 5000, 1).\n"
        "  - `eng_pct_norm` = percentile of `views` within today's sample (0 when unknown).\n"
        "  - `impression_bonus` = max(impressions_count - 1, 0) / 4, capped at 1. A tweet seen once is the baseline.\n"
        "  - `interaction_flag` = 1 if you liked/retweeted/replied/bookmarked.\n"
        "- **dwell semantics:** dwell=0 does **not** mean 'didn't see' — the impression was recorded but no scroll-pause. Total dwell (sum across impressions of the same tweet) is on the Tweets and Repeat-exposure rows; per-impression dwell is on the raw Impressions section.\n"
        "- **×N / impressions_count:** number of distinct impression events for the same tweet_id today. ≥3 is 'algorithmic pressure'.\n"
        "- **tid:** prefixed with `t` to keep it a clean token in tables (`t2046506730624852316`). Strip the `t` to build `https://x.com/<handle>/status/<id>`.\n"
        "- **engagement l/rt/rp/v:** latest captured snapshot of likes, retweets, replies, views. Human-formatted (K/M) — precise integers live in `data/db.sqlite` if needed.\n"
        "- **topic rules:** curated keyword + hashtag table in `mcp_server/topics.py`. Multi-label. `untagged` catches zero-hit tweets. Explicit heuristic, recall > precision — don't treat as ground truth for high-stakes inference.\n"
        "- **anomaly rules:** (1) back-to-back sessions (<3 min gap); (2) doomscroll (session with ≥20 impressions and median dwell <500ms); (3) late-night (23:00-04:00 local); (4) topic drift (10-impression window spanning ≥3 distinct topics).\n"
        "- **stubs:** tweets with empty text AND missing handle are dropped from human-readable sections. They still appear in the raw Impressions and Timeline output so the impression log stays complete.\n"
        "\n"
    )


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

        # Summary is needed for TL;DR + stats return value, so compute once.
        summary_data = queries.summary(db, day_start, day_end)

        # Unique-tweet aggregate — input for TL;DR / Tweets-ranked /
        # Repeat-exposure / Topics. Only fetch when at least one of those
        # sections will render.
        needs_unique = any(
            s in included for s in ("tldr", "tweets_ranked", "repeat_exposure", "topics")
        )
        unique_tweets: list[dict] = []
        if needs_unique:
            raw = queries.unique_tweets_with_engagement(db, day_start, day_end)
            unique_tweets = _score_tweets(_enrich_with_topics(raw))

        # Sessions are also a TL;DR dependency (anomaly detection).
        session_rows = queries.sessions_rows(db, day_start, day_end)
        impression_rows_for_tldr: list[dict] = []
        if "tldr" in included:
            impression_rows_for_tldr = queries.impressions_rows(db, day_start, day_end)

        if "tldr" in included:
            blocks.append(render_tldr(summary_data, unique_tweets, session_rows, impression_rows_for_tldr, tz))

        if "summary" in included:
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
            blocks.append(render_sessions(session_rows, tz, nav_by_session, rel_by_session))

        if "tweets_ranked" in included:
            blocks.append(render_tweets_ranked(unique_tweets, tz))

        if "repeat_exposure" in included:
            blocks.append(render_repeat_exposure(unique_tweets, tz))

        if "topics" in included:
            blocks.append(render_topics(unique_tweets, tz))

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

        if "authors" in included:
            rows = queries.author_context_rows(db, day_start, day_end)
            blocks.append(render_authors_v2(rows, tz))

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

        if "schema" in included:
            blocks.append(render_schema())

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


def _eng_from_row(r: dict) -> dict:
    return {
        "likes": r.get("likes"),
        "retweets": r.get("retweets"),
        "replies": r.get("replies"),
        "views": r.get("views"),
    }


def build_json(
    db_path: Path,
    target_date: date_cls,
) -> dict[str, Any]:
    """Structured companion to build_markdown. Same data, machine-parseable.

    Agents should prefer this over regex-ing the markdown. Shape is flat
    arrays with stable keys. Fields that only appear in certain tables
    (e.g., quoted_tweet_id, verified) are always present in the row with
    ``null`` when not applicable.
    """
    tz = settings.local_tz()
    day_start, day_end = queries.day_window_utc(target_date, tz)
    db = queries.connect_ro(db_path)
    try:
        summary_data = queries.summary(db, day_start, day_end)
        raw_unique = queries.unique_tweets_with_engagement(db, day_start, day_end)
        unique_tweets = _score_tweets(_enrich_with_topics(raw_unique))
        session_rows = queries.sessions_rows(db, day_start, day_end)
        impression_rows = queries.impressions_rows(db, day_start, day_end)

        # Tweets ranked (sorted by importance DESC, same rule as the md section)
        tweets_ranked = sorted(
            _stub_free(unique_tweets),
            key=lambda t: (
                -(t.get("importance") or 0),
                -(t.get("impressions_count") or 0),
                -(t.get("views") or 0),
            ),
        )
        tweets_ranked_out = [
            {
                "rank": rank,
                "tweet_id": t.get("tweet_id"),
                "importance": t.get("importance"),
                "handle": t.get("handle"),
                "display_name": t.get("display_name"),
                "verified": bool(t.get("verified")) if t.get("verified") is not None else None,
                "text": _clean_text(t.get("text")),
                "created_at": t.get("created_at"),
                "conversation_id": t.get("conversation_id"),
                "quoted_tweet_id": t.get("quoted_tweet_id"),
                "retweeted_tweet_id": t.get("retweeted_tweet_id"),
                "reply_to_tweet_id": t.get("reply_to_tweet_id"),
                "has_media": _has_media(t.get("media_json")),
                "media_json": t.get("media_json"),
                "impressions_count": t.get("impressions_count") or 0,
                "total_dwell_ms": t.get("total_dwell_ms") or 0,
                "sessions_hit": [
                    s for s in (t.get("sessions_hit_csv") or "").split(",") if s
                ],
                "engagement": _eng_from_row(t),
                "topics": t.get("topics") or [],
                "user_had_interaction": bool(t.get("user_had_interaction")),
                "first_seen_at": t.get("first_seen_at"),
            }
            for rank, t in enumerate(tweets_ranked, 1)
        ]

        # Topic rollup
        buckets: dict[str, list[dict]] = {}
        for t in _stub_free(unique_tweets):
            for tag in t.get("topics") or ["untagged"]:
                buckets.setdefault(tag, []).append(t)
        topics_out = [
            {
                "name": tag,
                "tweet_count": len(tws),
                "total_dwell_ms": sum(t.get("total_dwell_ms") or 0 for t in tws),
                "notable_handles": [
                    t.get("handle")
                    for t in sorted(
                        tws,
                        key=lambda t: -(t.get("total_dwell_ms") or 0)
                        - (t.get("impressions_count") or 0),
                    )[:4]
                    if t.get("handle")
                ],
            }
            for tag, tws in sorted(
                buckets.items(),
                key=lambda kv: (kv[0] == "untagged", -len(kv[1]), kv[0]),
            )
        ]

        # Anomalies (same function as render_tldr)
        tweet_topic_map = {t.get("tweet_id"): t.get("topics") for t in unique_tweets}
        imp_topics = [
            (im.get("first_seen_at") or "", tweet_topic_map.get(im.get("tweet_id")) or ["untagged"])
            for im in impression_rows
        ]
        anomaly_hits = anomalies.detect(session_rows, impression_rows, imp_topics, tz)

        interactions_out = [
            {
                "tweet_id": r.get("tweet_id"),
                "action": r.get("action"),
                "timestamp": r.get("timestamp"),
                "handle": r.get("handle"),
                "text_preview": _preview(r.get("text"), 80),
            }
            for r in queries.interactions_rows(db, day_start, day_end)
        ]

        sessions_out = [
            {
                "session_id": r.get("session_id"),
                "started_at": r.get("started_at"),
                "ended_at": r.get("ended_at"),
                "tweet_count": r.get("tweet_count") or 0,
                "total_dwell_ms": r.get("total_dwell_ms") or 0,
                "feeds_visited": _parse_json_list(r.get("feeds_visited")),
            }
            for r in session_rows
        ]

        authors_out = [
            {
                "handle": r.get("handle"),
                "user_id": r.get("user_id"),
                "display_name": r.get("display_name"),
                "verified": bool(r.get("verified")) if r.get("verified") is not None else None,
                "follower_count": r.get("follower_count"),
                "impressions_count": r.get("impressions_count") or 0,
                "unique_tweets": r.get("unique_tweets") or 0,
                "total_dwell_ms": r.get("total_dwell_ms") or 0,
            }
            for r in queries.author_context_rows(db, day_start, day_end)
        ]

        link_outs = [
            {
                "url": r.get("url"),
                "domain": r.get("domain"),
                "link_kind": r.get("link_kind"),
                "modifiers": r.get("modifiers"),
                "timestamp": r.get("timestamp"),
                "tweet_id": r.get("tweet_id"),
                "handle": r.get("handle"),
            }
            for r in queries.link_clicks_rows(db, day_start, day_end)
        ]

        selections = [
            {
                "tweet_id": r.get("tweet_id"),
                "text": r.get("text"),
                "via": r.get("via"),
                "timestamp": r.get("timestamp"),
                "handle": r.get("handle"),
            }
            for r in queries.text_selections_rows(db, day_start, day_end)
        ]

        media = [
            {
                "tweet_id": r.get("tweet_id"),
                "media_kind": r.get("media_kind"),
                "media_index": r.get("media_index"),
                "timestamp": r.get("timestamp"),
                "handle": r.get("handle"),
            }
            for r in queries.media_events_rows(db, day_start, day_end)
        ]

        searches = [
            {
                "query": r.get("query"),
                "timestamp": r.get("timestamp"),
                "session_id": r.get("session_id"),
            }
            for r in queries.searches_rows(db, day_start, day_end)
        ]

        return {
            "date": target_date.isoformat(),
            "generated_at": datetime.now(tz).isoformat(timespec="seconds"),
            "timezone": str(tz),
            "summary": {
                "sessions": summary_data.get("sessions", 0),
                "impressions": summary_data.get("tweets_seen", 0),
                "unique_tweets": summary_data.get("unique_tweets", 0),
                "unique_authors": summary_data.get("unique_authors", 0),
                "total_dwell_ms": summary_data.get("total_dwell_ms", 0),
                "searches": summary_data.get("searches", 0),
                "interactions_by_action": summary_data.get("interactions") or {},
            },
            "anomalies": anomaly_hits,
            "tweets_ranked": tweets_ranked_out,
            "topics": topics_out,
            "authors": authors_out,
            "sessions": sessions_out,
            "interactions": interactions_out,
            "searches": searches,
            "link_outs": link_outs,
            "selections": selections,
            "media": media,
        }
    finally:
        db.close()


def _parse_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def write_export(
    db_path: Path,
    target_date: date_cls,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    settings.ensure_exports_dir()
    markdown, included, stats = build_markdown(db_path, target_date, exclude)

    md_path = settings.EXPORTS_DIR / f"{target_date.isoformat()}.md"
    md_path.write_text(markdown, encoding="utf-8")

    # JSON companion — always written alongside the markdown. Ignores
    # ``exclude`` (the JSON is structural, not presentational, so callers
    # that want a subset should filter keys themselves).
    json_data = build_json(db_path, target_date)
    json_path = settings.EXPORTS_DIR / f"{target_date.isoformat()}.json"
    json_path.write_text(
        json.dumps(json_data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    byte_size = len(markdown.encode("utf-8"))
    truncated = byte_size > settings.INLINE_CONTENT_CAP_BYTES
    return {
        "file_path": str(md_path),
        "json_path": str(json_path),
        "sections_included": included,
        "tweet_count": stats["tweet_count"],
        "interaction_count": stats["interaction_count"],
        "session_count": stats["session_count"],
        "search_count": stats["search_count"],
        "byte_size": byte_size,
        "content": "" if truncated else markdown,
        "truncated": truncated,
    }
