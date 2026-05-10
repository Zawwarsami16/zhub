"""Phase 19.0 — built-in chat UI served at /chat."""

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
async def test_chat_ui_served_at_chat(hub):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{hub}/chat")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"].lower()
    body = r.text
    # Key UI structures must be present
    assert "zhub" in body.lower()
    for needle in ["base url", "api key", "/chat/completions", "input",
                   "messages"]:
        assert needle in body.lower(), f"missing {needle!r}"
