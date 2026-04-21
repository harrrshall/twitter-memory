"""Synthetic GraphQL-ish payloads for tests and seed data.

Shapes match the essentials of HomeTimeline / TweetDetail responses from x.com
GraphQL circa 2025-2026. Not every field is present - the parser must tolerate that.
"""
from __future__ import annotations


def make_user(user_id: str, handle: str, name: str | None = None, followers: int = 1000) -> dict:
    return {
        "__typename": "User",
        "rest_id": user_id,
        "core": {"screen_name": handle, "name": name or handle},
        "legacy": {
            "screen_name": handle,
            "name": name or handle,
            "description": f"bio for {handle}",
            "verified": False,
            "followers_count": followers,
            "friends_count": 100,
        },
    }


def make_tweet(
    tweet_id: str,
    user: dict,
    text: str,
    created_at: str = "Mon Apr 20 10:00:00 +0000 2026",
    conversation_id: str | None = None,
    reply_to_tweet_id: str | None = None,
    reply_to_user_id: str | None = None,
    quoted_tweet_id: str | None = None,
    likes: int = 0,
    retweets: int = 0,
    replies: int = 0,
    views: int = 0,
) -> dict:
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {"user_results": {"result": user}},
        "legacy": {
            "id_str": tweet_id,
            "user_id_str": user["rest_id"],
            "full_text": text,
            "created_at": created_at,
            "lang": "en",
            "conversation_id_str": conversation_id or tweet_id,
            "in_reply_to_status_id_str": reply_to_tweet_id,
            "in_reply_to_user_id_str": reply_to_user_id,
            "quoted_status_id_str": quoted_tweet_id,
            "favorite_count": likes,
            "retweet_count": retweets,
            "reply_count": replies,
            "quote_count": 0,
            "bookmark_count": 0,
        },
        "views": {"count": str(views)} if views else {},
    }


def home_timeline_payload(tweets: list[dict]) -> dict:
    """Wrap tweets in a HomeTimeline-ish envelope so extract_from_payload walks them."""
    return {
        "data": {
            "home": {
                "home_timeline_urt": {
                    "instructions": [
                        {
                            "type": "TimelineAddEntries",
                            "entries": [
                                {
                                    "entryId": f"tweet-{t['rest_id']}",
                                    "content": {
                                        "itemContent": {
                                            "tweet_results": {"result": t}
                                        }
                                    },
                                }
                                for t in tweets
                            ],
                        }
                    ]
                }
            }
        }
    }
