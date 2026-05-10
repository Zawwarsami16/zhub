"""Phase 6.0 — per-AI request latency surfaced in /metrics.

Each /<ai>/v1/* request bumps a per-AI rolling latency counter so
operators can see how long the publisher is taking to respond.
/metrics returns avg_latency_ms and max_latency_ms per AI.
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
from zhub import publish


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def lat_hub():
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
async def test_per_ai_latency_in_metrics(lat_hub):
    pub = publish(
        name="lat-bot",
        description="latency test",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{lat_hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        for _ in range(3):
            await c.post(
                f"http://127.0.0.1:{lat_hub}/{pub.name}/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": f"Bearer {pub.api_key}"},
            )
        m = (await c.get(f"http://127.0.0.1:{lat_hub}/metrics")).json()

    by_ai = m["by_ai"]
    assert pub.name in by_ai
    e = by_ai[pub.name]
    assert "avg_latency_ms" in e and e["avg_latency_ms"] >= 0
    assert "max_latency_ms" in e and e["max_latency_ms"] >= e["avg_latency_ms"]
    assert "request_count" in e and e["request_count"] >= 3
