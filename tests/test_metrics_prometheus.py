"""Phase 18.0 — Prometheus exposition format for /metrics."""

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
from zhub import publish


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
async def test_prometheus_format_returns_text_exposition(hub):
    pub = publish(
        name="prom-bot", description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        for _ in range(3):
            await c.post(
                f"http://127.0.0.1:{hub}/{pub.name}/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": f"Bearer {pub.api_key}"},
            )
        r = await c.get(f"http://127.0.0.1:{hub}/metrics?format=prometheus")

    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"].lower()
    body = r.text

    # Hub-level counters
    assert "# HELP zhub_uptime_seconds" in body
    assert "# TYPE zhub_uptime_seconds gauge" in body
    assert "zhub_uptime_seconds " in body
    assert "zhub_publishers " in body

    # Per-AI metrics with labels
    assert "# HELP zhub_chat_requests_total" in body
    assert "# TYPE zhub_chat_requests_total counter" in body
    # Label format: zhub_chat_requests_total{ai="prom-bot"} N
    assert 'zhub_chat_requests_total{ai="prom-bot"}' in body
    assert 'zhub_avg_latency_ms{ai="prom-bot"}' in body
    assert 'zhub_p95_latency_ms{ai="prom-bot"}' in body


@pytest.mark.asyncio
async def test_metrics_default_still_json(hub):
    """Backwards compat: no `format` param returns the existing JSON shape."""
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{hub}/metrics")
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"].lower()
    d = r.json()
    assert "hub_id" in d
    assert "by_ai" in d
