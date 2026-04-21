"""Defensive parser for Twitter GraphQL payloads.

Extracts tweet, author, engagement, and conversation_id records out of the many
response shapes that X.com's GraphQL endpoints return. Never raises on missing
fields - returns what it found and logs nothing (callers handle logging).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _iter_entries(payload: dict) -> Iterable[dict]:
    """Walk every dict node anywhere in the payload once."""
    stack: list[Any] = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _is_tweet_result(node: dict) -> bool:
    # Canonical shape: {"__typename":"Tweet","rest_id":"...","legacy":{...},"core":{...}}
    if node.get("__typename") in ("Tweet", "TweetWithVisibilityResults"):
        return True
    # itemContent.tweet_results.result wrapper
    return False


def _unwrap_tweet(node: dict) -> dict | None:
    # TweetWithVisibilityResults wraps the real tweet
    if node.get("__typename") == "TweetWithVisibilityResults":
        inner = node.get("tweet") or {}
        return inner if inner.get("__typename") == "Tweet" else None
    if node.get("__typename") == "Tweet":
        return node
    return None


def _parse_user_from_user_results(user_results: dict) -> dict | None:
    result = _get(user_results, "result") or user_results
    if not isinstance(result, dict):
        return None
    legacy = result.get("legacy") or {}
    core = result.get("core") or {}
    user_id = result.get("rest_id") or legacy.get("id_str")
    if not user_id:
        return None
    # Newer payloads moved some fields under "core"
    handle = (
        core.get("screen_name")
        or legacy.get("screen_name")
        or result.get("screen_name")
    )
    display_name = (
        core.get("name")
        or legacy.get("name")
        or result.get("name")
    )
    return {
        "user_id": str(user_id),
        "handle": handle or "",
        "display_name": display_name,
        "bio": legacy.get("description"),
        "verified": int(bool(legacy.get("verified") or result.get("is_blue_verified"))),
        "follower_count": legacy.get("followers_count"),
        "following_count": legacy.get("friends_count"),
    }


def _iso(s: str | None) -> str | None:
    if not s:
        return None
    # Twitter timestamps: "Wed Oct 10 20:19:24 +0000 2018"
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return s


def _parse_tweet_node(tweet: dict) -> tuple[dict | None, dict | None, dict | None]:
    """Return (author, tweet_row, engagement_row) or Nones."""
    tweet = _unwrap_tweet(tweet) or {}
    if tweet.get("__typename") != "Tweet":
        return None, None, None
    rest_id = tweet.get("rest_id")
    legacy = tweet.get("legacy") or {}
    if not rest_id and not legacy.get("id_str"):
        return None, None, None
    tweet_id = str(rest_id or legacy.get("id_str"))

    # Author
    core = tweet.get("core") or {}
    user_results = core.get("user_results") or {}
    author = _parse_user_from_user_results(user_results)

    author_id = author["user_id"] if author else (legacy.get("user_id_str") and str(legacy["user_id_str"])) or None

    # Quoted / retweeted references
    quoted_tweet_id = legacy.get("quoted_status_id_str") or _get(
        tweet, "quoted_status_result", "result", "rest_id"
    )
    retweeted_tweet_id = _get(legacy, "retweeted_status_result", "result", "rest_id") or _get(
        tweet, "retweeted_status_result", "result", "rest_id"
    )

    media_entities = _get(legacy, "entities", "media") or _get(legacy, "extended_entities", "media")
    media_json = json.dumps(media_entities) if media_entities else None

    tweet_row = {
        "tweet_id": tweet_id,
        "author_id": author_id,
        "text": legacy.get("full_text") or legacy.get("text"),
        "created_at": _iso(legacy.get("created_at")),
        "lang": legacy.get("lang"),
        "conversation_id": legacy.get("conversation_id_str"),
        "reply_to_tweet_id": legacy.get("in_reply_to_status_id_str"),
        "reply_to_user_id": legacy.get("in_reply_to_user_id_str"),
        "quoted_tweet_id": str(quoted_tweet_id) if quoted_tweet_id else None,
        "retweeted_tweet_id": str(retweeted_tweet_id) if retweeted_tweet_id else None,
        "media_json": media_json,
    }

    engagement_row = {
        "tweet_id": tweet_id,
        "likes": legacy.get("favorite_count"),
        "retweets": legacy.get("retweet_count"),
        "replies": legacy.get("reply_count"),
        "quotes": legacy.get("quote_count"),
        "views": _get(tweet, "views", "count") and int(tweet["views"]["count"]),
        "bookmarks": legacy.get("bookmark_count"),
    }

    # Also recurse into quoted tweet result if embedded so we capture it
    # (callers handle nested discovery via extract_from_payload walking the tree)

    return author, tweet_row, engagement_row


def extract_from_payload(payload: dict) -> dict:
    """Scan an arbitrary GraphQL response and pull every tweet + author we find.

    Returns dict of lists: {"authors": [...], "tweets": [...], "engagements": [...]}.
    Dedupes by primary key.
    """
    authors: dict[str, dict] = {}
    tweets: dict[str, dict] = {}
    engagements: dict[str, dict] = {}

    for node in _iter_entries(payload):
        if not isinstance(node, dict):
            continue
        if node.get("__typename") in ("Tweet", "TweetWithVisibilityResults"):
            author, tweet_row, eng_row = _parse_tweet_node(node)
            if author:
                authors[author["user_id"]] = author
            if tweet_row:
                tweets[tweet_row["tweet_id"]] = tweet_row
            if eng_row:
                engagements[eng_row["tweet_id"]] = eng_row
        elif node.get("__typename") == "User":
            # Standalone user result (UserByScreenName, etc.)
            au = _parse_user_from_user_results({"result": node})
            if au and au["user_id"]:
                authors.setdefault(au["user_id"], au)

    return {
        "authors": list(authors.values()),
        "tweets": list(tweets.values()),
        "engagements": list(engagements.values()),
    }
