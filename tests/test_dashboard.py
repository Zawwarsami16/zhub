"""Phase 8.0 — Hub UI dashboard.

`GET /` serves an HTML page with live operator visibility.
`GET /api/dashboard` returns the JSON snapshot the page polls.
The hub maintains a recent_requests ring buffer (last 50) populated
by the access-log middleware so the dashboard can show recent traffic.
"""

import asyncio
import socket
import threading
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    import httpx  # noqa
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

if DEPS_AVAILABLE:
    from zhub.server import create_app
from zhub import publish, expose


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture
def hub():
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    app = create_app()

    def run():
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning")
        asyncio.run(uvicorn.Server(config).serve())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    yield port


@pytest.mark.asyncio
async def test_dashboard_html_served_at_root(hub):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{hub}/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"].lower()
    body = r.text
    # Page must reference the data endpoint and have basic identity
    assert "/api/dashboard" in body
    assert "zhub" in body.lower()


@pytest.mark.asyncio
async def test_api_dashboard_returns_full_snapshot(hub):
    pub = publish(
        name="dash-bot",
        description="dashboard test",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    e = expose(
        name="dash-cap",
        capabilities={"thing": ({"type": "object"}, lambda a: {"ok": True})},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{hub}/api/dashboard")
    assert r.status_code == 200
    data = r.json()
    # Top-level keys
    for k in ("hub_id", "uptime_seconds", "publishers", "exposures",
             "by_ai", "recent_requests", "peers"):
        assert k in data, f"missing top-level key {k!r}: {data.keys()}"
    # Publishers must include ours
    pub_names = {p["name"] for p in data["publishers"]}
    assert "dash-bot" in pub_names
    # Exposures must include ours
    exp_names = {e_["name"] for e_ in data["exposures"]}
    assert "dash-cap" in exp_names


@pytest.mark.asyncio
async def test_recent_requests_captured_in_dashboard(hub):
    async with httpx.AsyncClient(timeout=5.0) as c:
        # Generate a few hits
        await c.get(f"http://127.0.0.1:{hub}/healthz")
        await c.get(f"http://127.0.0.1:{hub}/registry")
        await c.get(f"http://127.0.0.1:{hub}/healthz")
        data = (await c.get(f"http://127.0.0.1:{hub}/api/dashboard")).json()

    rr = data["recent_requests"]
    assert isinstance(rr, list)
    assert len(rr) >= 3
    # Each entry has the access-log fields
    sample = rr[-1]
    for k in ("ts", "method", "path", "status", "latency_ms"):
        assert k in sample, f"missing {k!r} in {sample!r}"
    paths = [r["path"] for r in rr]
    assert "/healthz" in paths
    assert "/registry" in paths
