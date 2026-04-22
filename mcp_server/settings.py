"""MCP server settings. Mirrors backend.settings for paths."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import time

DATA_DIR = Path(os.environ.get("TWITTER_MEMORY_DATA", Path.home() / ".twitter-memory"))
DB_PATH = DATA_DIR / "db.sqlite"
EXPORTS_DIR = DATA_DIR / "exports"

# Cap for inline content returned to Claude in the tool response.
INLINE_CONTENT_CAP_BYTES = 200_000

ALL_SECTIONS = [
    "summary",
    "sessions",
    "searches",
    "interactions",
    "link_outs",
    "selections",
    "media",
    "top_authors",
    "threads",
    "timeline",
    "impressions",
]


def local_tz() -> ZoneInfo:
    name = os.environ.get("TWITTER_MEMORY_TZ") or time.tzname[0]
    try:
        return ZoneInfo(name)
    except Exception:
        # Fallback: use /etc/localtime via ZoneInfo('localtime') if available
        try:
            return ZoneInfo("localtime")
        except Exception:
            return ZoneInfo("UTC")


def ensure_exports_dir() -> None:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o755)
