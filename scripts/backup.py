"""Daily hot backup via VACUUM INTO. Safe while backend is running (SQLite
coordinates via WAL). Drop into cron or systemd.timer."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from backend.settings import BACKUPS_DIR, DB_PATH, ensure_dirs


def main() -> None:
    ensure_dirs()
    target = BACKUPS_DIR / f"{date.today().isoformat()}.db"
    if target.exists():
        target.unlink()
    c = sqlite3.connect(DB_PATH)
    try:
        c.execute(f"VACUUM INTO '{target.as_posix()}'")
    finally:
        c.close()
    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"wrote {target} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
