"""Exercise the full HTTP surface via httpx + the ASGI transport."""
import pytest
import httpx

from tests.fixtures import home_timeline_payload, make_tweet, make_user


pytestmark = pytest.mark.asyncio


async def test_ingest_and_health(tmp_data_dir):
    # Import after env is set so settings pick up tmp_path
    import importlib
    import backend.settings, backend.db, backend.main
    importlib.reload(backend.settings)
    importlib.reload(backend.db)
    importlib.reload(backend.main)
    app = backend.main.app

    transport = httpx.ASGITransport(app=app)
    async with backend.main.lifespan(app):
     async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        # health before any data
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        alice = make_user("100", "alice")
        t = make_tweet("1001", alice, "hi", likes=5)
        body = {
            "events": [
                {"type": "graphql_payload", "operation_name": "HomeTimeline",
                 "payload": home_timeline_payload([t])},
                {"type": "impression_end", "session_id": None, "tweet_id": "1001",
                 "first_seen_at": "2026-04-21T09:14:00+00:00", "dwell_ms": 1000, "feed_source": "for_you"},
            ]
        }
        r = await client.post("/ingest", json=body)
        assert r.status_code == 200
        out = r.json()
        assert out["accepted"] == 2
        assert out["errors"] == []

        r = await client.get("/stats")
        s = r.json()
        # Stats are "today" based; seeded first_seen_at is 2026-04-21 which may not be today.
        # We just verify the shape here.
        assert set(s.keys()) == {"tweets_today", "sessions_today", "total_dwell_ms_today", "last_event_at"}
        assert s["last_event_at"] is not None


async def test_cors_preflight_accepts_extension_origin(tmp_data_dir):
    # Regression: /qa 2026-04-21. The service worker posts from
    # chrome-extension://<id>. CORS was narrowed to x.com/twitter.com, so the
    # preflight was rejected with 400 "Disallowed CORS origin" and nothing
    # ever reached /ingest. Backend binds to 127.0.0.1 only, so "*" is fine.
    import importlib
    import backend.settings, backend.db, backend.main
    importlib.reload(backend.settings)
    importlib.reload(backend.db)
    importlib.reload(backend.main)
    app = backend.main.app

    transport = httpx.ASGITransport(app=app)
    async with backend.main.lifespan(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            for origin in (
                "chrome-extension://mmgkhmdaeegmfcnmbkhnbmmjkhgedmkl",
                "https://x.com",
                "https://twitter.com",
            ):
                r = await client.options(
                    "/ingest",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": "content-type",
                    },
                )
                assert r.status_code == 200, f"preflight failed for {origin}: {r.status_code} {r.text}"
                assert "access-control-allow-origin" in r.headers
