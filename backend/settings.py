import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("TWITTER_MEMORY_DATA", Path.home() / ".twitter-memory"))
DB_PATH = DATA_DIR / "db.sqlite"
EXPORTS_DIR = DATA_DIR / "exports"
BACKUPS_DIR = DATA_DIR / "backups"

HOST = "127.0.0.1"
PORT = int(os.environ.get("TWITTER_MEMORY_PORT", "8765"))

PARSER_VERSION = "1"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o755)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True, mode=0o755)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True, mode=0o755)
