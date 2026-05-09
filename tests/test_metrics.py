"""Phase 2.0a — Hub observability via /metrics.

Operators need to see what's happening on their hub: how many chats per
AI, how many rate-limit rejections, how many tool calls auto-resolved,
how many cross-hub proxies. /metrics returns a JSON snapshot.
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
def metrics_port():
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    app = create_app()

    def run():
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
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
async def test_metrics_counts_chat_requests_per_ai(metrics_port):
    """Each /<ai>/v1/chat/completions hit increments a per-AI chat counter."""
    hub_ws = f"ws://127.0.0.1:{metrics_port}"
    hub_http = f"http://127.0.0.1:{metrics_port}"

    pub = publish(
        name="metrics-bot",
        description="metrics test",
        chat_handler=lambda m, o: "ok",
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    async with httpx.AsyncClient(timeout=5.0) as client:
        for _ in range(3):
            await client.post(
                f"{hub_http}/{pub.name}/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": f"Bearer {pub.api_key}"},
            )
        resp = await client.get(f"{hub_http}/metrics")

    assert resp.status_code == 200
    data = resp.json()
    assert data["uptime_seconds"] >= 0
    assert data["publishers"] >= 1
    by_ai = data["by_ai"]
    assert pub.name in by_ai
    assert by_ai[pub.name]["chat_requests"] >= 3


@pytest.mark.asyncio
async def test_metrics_counts_rate_limit_rejections(metrics_port):
    """A 429 response increments the rate_limited counter for that AI."""
    hub_ws = f"ws://127.0.0.1:{metrics_port}"
    hub_http = f"http://127.0.0.1:{metrics_port}"

    pub = publish(
        name="rl-metrics-bot",
        description="rl metrics",
        chat_handler=lambda m, o: "ok",
        hub_url=hub_ws,
        rate_limit="2/min",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as client:
        # Two ok, third 429
        for _ in range(2):
            await client.post(
                f"{hub_http}/{pub.name}/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "x"}]},
                headers={"Authorization": f"Bearer {pub.api_key}"},
            )
        rejected = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
        assert rejected.status_code == 429
        resp = await client.get(f"{hub_http}/metrics")

    data = resp.json()
    assert data["by_ai"][pub.name]["rate_limited"] >= 1
