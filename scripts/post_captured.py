"""POST the events that were captured from real x.com via the MCP browser tool.

Reconstructed from the MCP tool reads (data is authentic — captured from live
x.com home feed on 2026-04-21). This simulates what the Chrome extension's
service worker would POST in production.
"""
import json
import urllib.request

SESSION_ID = "sess-real-1776768148026"
FEED = "for_you"

IMPRESSIONS = [
    ("2046497873752358990", "2026-04-21T10:35:12.647Z", 11748),
    ("2044639593253798088", "2026-04-21T10:35:12.647Z", 0),
    ("2046456561573495158", "2026-04-21T10:35:12.647Z", 0),
    ("2046482518011248802", "2026-04-21T10:35:12.647Z", 1851),
    ("2046462041167372304", "2026-04-21T10:35:12.647Z", 0),
    ("2046487960791687256", "2026-04-21T10:35:24.651Z", 1769),
    ("2043727353919017010", "2026-04-21T10:35:26.266Z", 0),
    ("2046488992091427068", "2026-04-21T10:35:26.510Z", 1800),
    ("2046210417257824323", "2026-04-21T10:35:26.510Z", 0),
    ("2046487829677580328", "2026-04-21T10:35:28.322Z", 0),
    ("2046519982310473818", "2026-04-21T10:35:28.322Z", 1802),
    ("2041366944658661682", "2026-04-21T10:35:28.322Z", 0),
    ("2046330940847190269", "2026-04-21T10:35:30.179Z", 0),
    ("2046398130883809435", "2026-04-21T10:35:30.179Z", 1825),
    ("2046298611139670126", "2026-04-21T10:35:30.179Z", 0),
    ("2046235989614465084", "2026-04-21T10:35:31.905Z", 1744),
    ("2042094279829438889", "2026-04-21T10:35:31.905Z", 0),
    ("2046486811942199409", "2026-04-21T10:35:33.468Z", 0),
    ("2045906520672461309", "2026-04-21T10:35:33.713Z", 0),
    ("2041366944658661682", "2026-04-21T10:36:26.134Z", 0),
    ("2046330940847190269", "2026-04-21T10:36:26.134Z", 0),
    ("2046504434889638017", "2026-04-21T10:36:26.134Z", 0),
    ("2041366944658661682", "2026-04-21T10:37:36.164Z", 0),
    ("2046330940847190269", "2026-04-21T10:37:36.164Z", 0),
    ("2046504434889638017", "2026-04-21T10:37:36.164Z", 0),
]

DOM_TWEETS = [
    {
        "tweet_id": "2046398130883809435",
        "ah": "theblackmanda",
        "ad": "Chetan Manda",
        "text": "Met @sama today. Move to sf, it will change your life!",
        "created_at_iso": "Tue, 21 Apr 2026 01:18:29 GMT",
        "conversation_id": "2046398130883809435",
    },
    {
        "tweet_id": "2046298611139670126",
        "ah": "NimishaChanda",
        "ad": "Nimisha Chanda",
        "text": "gonna do multiple such retreats at @residencyBLR for folks across the globe\n\ndm me in case you want to get in!",
        "created_at_iso": "Mon, 20 Apr 2026 18:43:01 GMT",
        "conversation_id": "2046298611139670126",
    },
    {
        "tweet_id": "2046235989614465084",
        "ah": "cleoabram",
        "ad": "Cleo Abram",
        "text": (
            "The Einstein Test doesn't feel too far away. AI isn't just mimicking human knowledge.\n"
            "\n"
            "It's obvious when you look at AlphaZero. **By creating its own dataset through self-play (not human moves!)** AlphaZero went from:\n"
            "- Breakfast time: Playing chess randomly\n"
            "- Lunch time:"
        ),
        "created_at_iso": "Mon, 20 Apr 2026 14:34:11 GMT",
        "conversation_id": "2046235989614465084",
    },
    {
        "tweet_id": "2042094279829438889",
        "ah": "aisdkagents",
        "ad": "ai sdk agents",
        "text": "AI Agents built with shadcn",
        "created_at_iso": None,
        "conversation_id": "2042094279829438889",
    },
    {
        "tweet_id": "2046486811942199409",
        "ah": "icanvardar",
        "ad": "Can Vardar",
        "text": "this guy should've been apple's new ceo",
        "created_at_iso": "Tue, 21 Apr 2026 07:10:52 GMT",
        "conversation_id": "2046486811942199409",
    },
    {
        "tweet_id": "2045906520672461309",
        "ah": "RoundtableSpace",
        "ad": "0xMarioNawfal",
        "text": (
            "TOP FIVE GITHUB REPOSITORIES THIS WEEK\n"
            "\n"
            "* ANDREJ-KARPATHY-SKILLS: github.com/multica-ai/andrej-karpathy-skills\n"
            "* HERMES-AGENT: github.com/NousResearch/hermes-agent\n"
            "* CLAUDE-MEM: github.com/thedotmack/claude-mem\n"
            "* EVOLVER: github.com/EvoMap/evolver\n"
            "* GENERIC AGENT: github.com/lsdefine/GenericAgent"
        ),
        "created_at_iso": "Sun, 19 Apr 2026 16:45:00 GMT",
        "conversation_id": "2045906520672461309",
    },
]


def build_events() -> list[dict]:
    events: list[dict] = [{"type": "session_start", "s": SESSION_ID, "timestamp": "2026-04-21T10:35:12.633Z"}]
    for tid, fs, dwell in IMPRESSIONS:
        events.append({
            "type": "impression_end",
            "s": SESSION_ID,
            "tweet_id": tid,
            "first_seen_at": fs,
            "dwell_ms": dwell,
            "feed_source": FEED,
        })
    for t in DOM_TWEETS:
        events.append({"type": "dom_tweet", **t})
    events.append({
        "type": "session_end",
        "s": SESSION_ID,
        "timestamp": "2026-04-21T10:38:00.000Z",
        "total_dwell_ms": sum(d for _, _, d in IMPRESSIONS),
        "tweet_count": len(DOM_TWEETS),
        "feeds_visited": ["for_you"],
    })
    return events


def main() -> None:
    body = json.dumps({"events": build_events()}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8765/ingest",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        print(resp.status, resp.read().decode())


if __name__ == "__main__":
    main()
