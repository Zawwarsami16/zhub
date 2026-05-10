"""Phase 6.0 — structured request logging middleware.

Every HTTP request to the hub is logged at INFO with a single line
containing: method, path, status, latency_ms, ai_name (when path
matches /<ai>/...). Logs go to stderr via the zhub.access logger so
operators can tail them or pipe to a structured collector.
"""

import asyncio
import logging
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
def hub_with_log_capture(caplog):
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
    caplog.set_level(logging.INFO, logger="zhub.access")
    yield port


@pytest.mark.asyncio
async def test_each_request_logged_with_status_and_latency(hub_with_log_capture, caplog):
    port = hub_with_log_capture
    async with httpx.AsyncClient(timeout=5.0) as c:
        await c.get(f"http://127.0.0.1:{port}/healthz")
    # Log message: "200 GET /healthz <ms>ms"
    msgs = [r.message for r in caplog.records if r.name == "zhub.access"]
    assert any("/healthz" in m and "200" in m and "ms" in m for m in msgs), \
        f"no access log line found: {msgs!r}"


@pytest.mark.asyncio
async def test_ai_path_logs_include_ai_name(hub_with_log_capture, caplog):
    port = hub_with_log_capture
    pub = publish(
        name="logbot",
        description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{port}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    caplog.clear()
    async with httpx.AsyncClient(timeout=5.0) as c:
        await c.post(
            f"http://127.0.0.1:{port}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    msgs = [r.message for r in caplog.records if r.name == "zhub.access"]
    assert any("logbot" in m and "200" in m for m in msgs), \
        f"no AI-tagged access log: {msgs!r}"
