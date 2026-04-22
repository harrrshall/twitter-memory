"""Heuristic topic tagging for tweets.

Not ML. A small curated keyword + hashtag table. Multi-label: a tweet can
carry several tags; a tweet with zero matches gets ["untagged"].

Consumers (LLMs reading the export) should treat these tags as a retrieval
hint, not a reliable classifier.
"""
from __future__ import annotations

import re

# Each bucket: set of lowercase substrings matched against the tweet text.
# Hashtags are matched with leading "#" included; cashtags with leading "$".
# Keep rules narrow — a tiny false-positive rate is the goal.
TOPIC_RULES: dict[str, set[str]] = {
    "ai-tooling": {
        "claude", "gpt", "llm", "llms", "openai", "anthropic", "prompt",
        "apk", "reverse engineer", "agent", "rag", " model ", "sft",
        "finetune", "cursor", "github copilot", "mcp", "embedding",
    },
    "crypto": {
        "bitcoin", "btc", "ethereum", " eth ", "solana", "sui", "defi",
        "$btc", "$eth", "stablecoin", "onchain", "wallet",
    },
    "startup": {
        "yc", "y combinator", "founders", "bootstrapped", " raise ", "seed round",
        "series a", "exit", "acquisition", "₹100 crore", "vc-backed",
    },
    "personal-philosophy": {
        "peace", "philosophy", "wisdom", "life is", "highest form",
        "desire to be understood",
    },
    "meme": {
        "pov", "when she", "when you", " bro ", "😭", "💀", " nah ",
        " ong ", "aura", "vibe",
    },
    "politics": {
        "trump", "biden", "harris", " gop ", " dems ", "congress",
        "elon on", "election",
    },
    "self-promotion": {
        "buy now", "waitlist", "launching", "discount", "use code",
        "dm us", "link in bio", "shop.", " app today",
    },
    "lifestyle": {
        "fitness", " gym ", "morning routine", " cook ", "travel",
        "summer camp", "arts and crafts",
    },
}

_WS_RE = re.compile(r"\s+")


def tag_tweet(text: str | None, handle: str | None = None) -> list[str]:
    """Return the list of topic buckets a tweet matches.

    Matching is case-insensitive substring over the tweet text with whitespace
    normalized. Returns ``["untagged"]`` when no bucket matches.

    ``handle`` is currently unused but kept in the signature so we can add
    author-based rules later without breaking callers.
    """
    if not text:
        return ["untagged"]
    norm = " " + _WS_RE.sub(" ", text.lower()) + " "
    matched: list[str] = []
    for bucket, needles in TOPIC_RULES.items():
        for needle in needles:
            if needle in norm:
                matched.append(bucket)
                break
    return matched or ["untagged"]
