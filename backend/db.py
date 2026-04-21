import aiosqlite
from pathlib import Path

from backend.settings import DB_PATH, ensure_dirs

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_PRAGMAS = [
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA wal_autocheckpoint = 1000",
    "PRAGMA mmap_size = 268435456",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA cache_size = -64000",
    "PRAGMA foreign_keys = ON",
]


async def connect(path: Path | None = None) -> aiosqlite.Connection:
    ensure_dirs()
    db = await aiosqlite.connect(path or DB_PATH)
    db.row_factory = aiosqlite.Row
    for p in _PRAGMAS:
        await db.execute(p)
    await db.commit()
    return db


async def init_db(path: Path | None = None) -> None:
    db = await connect(path)
    try:
        schema = SCHEMA_PATH.read_text()
        await db.executescript(schema)
        await db.commit()
    finally:
        await db.close()
