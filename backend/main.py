"""FastAPI backend for twitter-memory ingest.

Binds 127.0.0.1:8765. No auth (local-only).
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

import aiosqlite
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend.db import connect, init_db
from backend.enrichment import REASON_TO_OPS, REPLAY_ALLOWLIST, sweep_loop
from backend.ingest import BATCH_METRICS, ingest_batch
from backend.retention import retention_loop
from backend.settings import DATA_DIR, DB_PATH, HOST, PARSER_VERSION, PORT


class State:
    db: aiosqlite.Connection | None = None
    retention_task: asyncio.Task | None = None
    retention_stop: asyncio.Event | None = None
    enrichment_task: asyncio.Task | None = None
    enrichment_stop: asyncio.Event | None = None
    started_at: float = 0.0


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    state.db = await connect()
    state.started_at = time.time()
    state.retention_stop = asyncio.Event()
    state.retention_task = asyncio.create_task(retention_loop(state.db, state.retention_stop))
    state.enrichment_stop = asyncio.Event()
    state.enrichment_task = asyncio.create_task(sweep_loop(state.db, state.enrichment_stop))
    try:
        yield
    finally:
        for stop in (state.retention_stop, state.enrichment_stop):
            if stop is not None:
                stop.set()
        for task in (state.retention_task, state.enrichment_task):
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=2)
                except asyncio.TimeoutError:
                    task.cancel()
        if state.db is not None:
            await state.db.close()
            state.db = None


app = FastAPI(title="twitter-memory backend", lifespan=lifespan)

# Permissive CORS: backend is bound to 127.0.0.1 only, so no non-local origin
# can reach the socket regardless. This lets the extension (origin
# chrome-extension://<id>) and the x.com page context both POST without a
# preflight failure. allow_credentials stays False so "*" is legal.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)


# Chrome Private Network Access (PNA): requests from a public origin to
# private network (127.0.0.1) require the server to advertise it opts in.
@app.middleware("http")
async def pna_middleware(request: Request, call_next):
    if request.method == "OPTIONS" and request.headers.get("access-control-request-private-network"):
        from starlette.responses import Response
        resp = Response(status_code=204)
        resp.headers["Access-Control-Allow-Origin"] = request.headers.get("origin", "*")
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = request.headers.get(
            "access-control-request-headers", "*"
        )
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        resp.headers["Access-Control-Max-Age"] = "600"
        return resp
    resp = await call_next(request)
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


@app.get("/health")
async def health():
    assert state.db is not None
    row = await (await state.db.execute(
        "SELECT MAX(first_seen_at) FROM impressions"
    )).fetchone()
    return {"status": "ok", "db": "ok", "last_event_at": row[0]}


@app.get("/stats")
async def stats():
    assert state.db is not None
    today = datetime.now(timezone.utc).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    end = (datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)).isoformat()

    async def count(sql: str) -> int:
        row = await (await state.db.execute(sql, (start, end))).fetchone()
        return int(row[0] or 0)

    tweets_today = await count(
        "SELECT COUNT(DISTINCT tweet_id) FROM impressions WHERE first_seen_at >= ? AND first_seen_at < ?"
    )
    sessions_today = await count(
        "SELECT COUNT(*) FROM sessions WHERE started_at >= ? AND started_at < ?"
    )
    total_dwell = await count(
        "SELECT COALESCE(SUM(dwell_ms),0) FROM impressions WHERE first_seen_at >= ? AND first_seen_at < ?"
    )
    last = await (await state.db.execute("SELECT MAX(first_seen_at) FROM impressions")).fetchone()
    return {
        "tweets_today": tweets_today,
        "sessions_today": sessions_today,
        "total_dwell_ms_today": total_dwell,
        "last_event_at": last[0],
    }


class IngestBody(BaseModel):
    events: list[dict]


@app.post("/ingest")
async def ingest(body: IngestBody, request: Request):
    assert state.db is not None
    # Defense in depth: only accept loopback
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1"):
        return {"accepted": 0, "skipped": 0, "errors": [{"error": "non-local request rejected"}]}
    return await ingest_batch(state.db, body.events)


def _require_loopback(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=404)


@app.get("/debug/config")
async def debug_config(request: Request):
    _require_loopback(request)
    return {
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_PATH),
        "parser_version": PARSER_VERSION,
    }


@app.post("/export/day")
async def export_day(request: Request, date: str | None = None):
    """Write a markdown export for one local calendar day to EXPORTS_DIR and
    return the file path. Date defaults to today (local tz). Reuses the same
    renderer as the MCP tool so the extension and Claude Desktop agree."""
    _require_loopback(request)
    from datetime import date as date_cls
    from mcp_server import export as mcp_export, settings as mcp_settings

    if date:
        try:
            target = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date '{date}' — expected YYYY-MM-DD")
    else:
        target = datetime.now(mcp_settings.local_tz()).date()

    if not mcp_settings.DB_PATH.exists():
        raise HTTPException(status_code=404, detail="database not found — nothing to export")

    result = mcp_export.write_export(mcp_settings.DB_PATH, target, [])
    # Trim the inline markdown from the response — callers just need the
    # paths. The files on disk are always complete.
    return {
        "dir_path": result["dir_path"],
        "digest_path": result["digest_path"],
        "tweets_path": result["tweets_path"],
        "activity_path": result["activity_path"],
        "timeline_path": result["timeline_path"],
        "json_path": result["json_path"],
        "schema_path": result["schema_path"],
        "date": target.isoformat(),
        "tweet_count": result["tweet_count"],
        "interaction_count": result["interaction_count"],
        "session_count": result["session_count"],
        "search_count": result["search_count"],
        "byte_size_digest": result["byte_size_digest"],
        "byte_size_total_md": result["byte_size_total_md"],
    }


class EnrichmentComplete(BaseModel):
    id: int
    result: str  # "ok" | "not_found" | "rate_limited" | "auth_failed" | "error"
    error: str | None = None


@app.get("/enrichment/next")
async def enrichment_next(request: Request, limit: int = 1):
    """Hand the SW the next N enrichment targets, each already joined with its
    replay template. Resolves each reason to the first operation in its
    preference list that has a captured template available. Skips rows
    attempted in the last 10 minutes or attempted >= 5 times."""
    _require_loopback(request)
    assert state.db is not None
    limit = max(1, min(limit, 10))

    # Find which ops we actually have templates for, then resolve each reason
    # to its highest-preference captured op. Operations must also be on the
    # allowlist — defense in depth against a template row for a mutation.
    template_rows = await (await state.db.execute(
        "SELECT operation_name FROM graphql_templates"
    )).fetchall()
    available_ops = {r["operation_name"] for r in template_rows} & REPLAY_ALLOWLIST
    reason_to_op: dict[str, str] = {}
    for reason, candidates in REASON_TO_OPS.items():
        for op in candidates:
            if op in available_ops:
                reason_to_op[reason] = op
                break
    if not reason_to_op:
        return {"items": []}

    # Build CASE expression for SQL — only for reasons with a resolved op.
    reason_case_parts = " ".join(
        f"WHEN '{r}' THEN '{op}'" for r, op in reason_to_op.items()
    )
    reason_list = tuple(reason_to_op.keys())
    reason_placeholders = ",".join(["?"] * len(reason_list))
    sql = f"""
        SELECT q.id, q.target_type, q.target_id, q.reason,
               t.operation_name, t.query_id, t.url_path,
               t.features_json, t.variables_json, t.bearer
        FROM enrichment_queue q
        JOIN graphql_templates t ON t.operation_name = CASE q.reason {reason_case_parts} END
        WHERE q.succeeded_at IS NULL
          AND q.reason IN ({reason_placeholders})
          AND (q.last_attempt_at IS NULL OR q.last_attempt_at < datetime('now','-10 minutes'))
          AND q.attempts < 5
        ORDER BY q.priority DESC, q.queued_at
        LIMIT ?
    """
    rows = await (await state.db.execute(sql, (*reason_list, limit))).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    items = []
    for r in rows:
        await state.db.execute(
            "UPDATE enrichment_queue SET last_attempt_at = ?, attempts = attempts + 1 WHERE id = ?",
            (now, r["id"]),
        )
        items.append({
            "id": r["id"],
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "reason": r["reason"],
            "template": {
                "operation_name": r["operation_name"],
                "query_id": r["query_id"],
                "url_path": r["url_path"],
                "features_json": r["features_json"],
                "variables_json": r["variables_json"],
                "bearer": r["bearer"],
            },
        })
    await state.db.commit()
    return {"items": items}


@app.post("/enrichment/complete")
async def enrichment_complete(body: EnrichmentComplete, request: Request):
    _require_loopback(request)
    assert state.db is not None
    now = datetime.now(timezone.utc).isoformat()
    if body.result == "ok":
        await state.db.execute(
            "UPDATE enrichment_queue SET succeeded_at = ?, last_error = NULL WHERE id = ?",
            (now, body.id),
        )
    else:
        await state.db.execute(
            "UPDATE enrichment_queue SET last_error = ? WHERE id = ?",
            (body.error or body.result, body.id),
        )
    await state.db.commit()
    return {"ok": True}


@app.get("/enrichment/stats")
async def enrichment_stats(request: Request):
    _require_loopback(request)
    assert state.db is not None
    rows = await (await state.db.execute(
        "SELECT reason, "
        "  SUM(CASE WHEN succeeded_at IS NULL THEN 1 ELSE 0 END) AS pending, "
        "  SUM(CASE WHEN succeeded_at IS NOT NULL THEN 1 ELSE 0 END) AS done "
        "FROM enrichment_queue GROUP BY reason"
    )).fetchall()
    by_reason = {r["reason"]: {"pending": r["pending"], "done": r["done"]} for r in rows}
    done_24h_row = await (await state.db.execute(
        "SELECT COUNT(*) FROM enrichment_queue WHERE succeeded_at > datetime('now','-1 day')"
    )).fetchone()
    errors_24h_row = await (await state.db.execute(
        "SELECT COUNT(*) FROM enrichment_queue "
        "WHERE last_error IS NOT NULL AND last_attempt_at > datetime('now','-1 day')"
    )).fetchone()
    templates_row = await (await state.db.execute(
        "SELECT COUNT(*) FROM graphql_templates"
    )).fetchone()
    return {
        "by_reason": by_reason,
        "done_24h": done_24h_row[0],
        "errors_24h": errors_24h_row[0],
        "templates_available": templates_row[0],
    }


@app.get("/debug/data-quality")
async def debug_data_quality(request: Request):
    _require_loopback(request)
    assert state.db is not None

    async def one(sql: str) -> int:
        row = await (await state.db.execute(sql)).fetchone()
        return int(row[0] or 0)

    return {
        "tweets_total": await one("SELECT COUNT(*) FROM tweets"),
        "tweets_with_text": await one("SELECT COUNT(*) FROM tweets WHERE text IS NOT NULL"),
        "tweets_without_text": await one("SELECT COUNT(*) FROM tweets WHERE text IS NULL"),
        "authors_total": await one("SELECT COUNT(*) FROM authors"),
        "authors_cold": await one(
            "SELECT COUNT(*) FROM authors WHERE follower_count IS NULL AND user_id NOT LIKE 'dom-%'"
        ),
        "engagement_coverage_tweets": await one(
            "SELECT COUNT(DISTINCT tweet_id) FROM engagement_snapshots"
        ),
        "impressions_last_7d": await one(
            "SELECT COUNT(*) FROM impressions WHERE first_seen_at > datetime('now','-7 days')"
        ),
        "graphql_templates": await one("SELECT COUNT(*) FROM graphql_templates"),
        "enrichment_pending": await one(
            "SELECT COUNT(*) FROM enrichment_queue WHERE succeeded_at IS NULL"
        ),
    }


@app.get("/debug/metrics")
async def debug_metrics(request: Request):
    _require_loopback(request)
    batches = list(BATCH_METRICS)
    if batches:
        durations = sorted(b["duration_ms"] for b in batches)
        n = len(durations)

        def pct(p: float) -> float:
            idx = max(0, min(n - 1, int(round(p * (n - 1)))))
            return round(durations[idx], 3)

        summary = {
            "count": n,
            "events_total": sum(b["events"] for b in batches),
            "accepted_total": sum(b["accepted"] for b in batches),
            "skipped_total": sum(b["skipped"] for b in batches),
            "p50_ms": pct(0.50),
            "p95_ms": pct(0.95),
            "p99_ms": pct(0.99),
        }
    else:
        summary = {
            "count": 0,
            "events_total": 0,
            "accepted_total": 0,
            "skipped_total": 0,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
        }
    return {
        "batches_recent": summary,
        "db_path": str(DB_PATH),
        "uptime_s": round(time.time() - state.started_at, 1) if state.started_at else 0,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
