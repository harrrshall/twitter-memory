"""Topic tagging — heuristic keyword/hashtag bucketing.

Purely a recall-focused sanity check. Parametrized positive + negative cases
per bucket, plus untagged and multi-label.
"""
import pytest

from mcp_server.topics import TOPIC_RULES, tag_tweet


@pytest.mark.parametrize(
    "text,expected_bucket",
    [
        # ai-tooling — real tweets from 2026-04-21
        ("I found a GitHub repo that gives Claude Code the ability to fully reverse engineer any Android app.", "ai-tooling"),
        ("how to generate unlimited tweet ideas using GPT", "ai-tooling"),
        # crypto
        ("Bitcoin's market cap exceeds $1 trillion. < 0.5% of it is used in DeFi.", "crypto"),
        ("new BTC ETF launched today", "crypto"),
        # startup
        ("₹100 crore is a life-changing exit for a bootstrapped founder.", "startup"),
        ("YC S26 applications now open", "startup"),
        # personal-philosophy
        ("the highest form of peace is to have zero desire to be understood", "personal-philosophy"),
        ("a quote from Marcus Aurelius on wisdom", "personal-philosophy"),
        # meme
        ("POV: your code actually works on the first try", "meme"),
        ("when she asks what you're thinking 😭", "meme"),
        # politics
        ("Trump signs new executive order", "politics"),
        # self-promotion
        ("Buy Amul Peanut Butter Spread on shop.amul app today!", "self-promotion"),
        ("Join the waitlist", "self-promotion"),
        # lifestyle
        ("need a summer camp concept for adults with arts and crafts", "lifestyle"),
        ("morning routine for peak productivity", "lifestyle"),
    ],
)
def test_positive_bucket_hits(text: str, expected_bucket: str) -> None:
    tags = tag_tweet(text)
    assert expected_bucket in tags, f"{expected_bucket!r} not in {tags} for {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "just saw a beautiful sunset",
        "random thought about nothing",
        "the weather today",
    ],
)
def test_plain_text_is_untagged(text: str) -> None:
    assert tag_tweet(text) == ["untagged"]


def test_empty_or_none_text_is_untagged() -> None:
    assert tag_tweet("") == ["untagged"]
    assert tag_tweet(None) == ["untagged"]


def test_multi_label() -> None:
    # AI + startup: a tweet can hit multiple buckets
    tags = tag_tweet("the founders who raise from YC are shipping LLM agents")
    assert "ai-tooling" in tags
    assert "startup" in tags


def test_every_rule_bucket_is_tested() -> None:
    # Guard against rot: if someone adds a new bucket without a test
    # above, this will fail.
    tested_buckets = {
        "ai-tooling", "crypto", "startup", "personal-philosophy",
        "meme", "politics", "self-promotion", "lifestyle",
    }
    assert set(TOPIC_RULES.keys()) == tested_buckets
