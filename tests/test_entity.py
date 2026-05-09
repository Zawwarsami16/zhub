"""Phase 3.0 — entity (zhub's self-knowledge layer).

GET /entity returns the full master file. Sectioned access at
/entity/<section> and /entity/errors/<code>. Useful for any AI attached
to the hub to learn zhub's surface fluently in one fetch.
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def entity_hub_port():
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
async def test_entity_full_returns_markdown(entity_hub_port):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{entity_hub_port}/entity")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    body = r.text
    # All major sections should appear
    for section in ["## architecture", "## routes", "## errors",
                    "## patterns", "## debug", "## perf"]:
        assert section in body.lower(), f"missing {section!r}"


@pytest.mark.asyncio
async def test_entity_section_routes(entity_hub_port):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{entity_hub_port}/entity/routes")
    assert r.status_code == 200
    body = r.text.lower()
    assert "## routes" in body
    assert "/v1/chat/completions" in body
    assert "/v1/invoke" in body
    # Should NOT contain other top-level sections
    assert "## errors" not in body
    assert "## perf" not in body


@pytest.mark.asyncio
async def test_entity_unknown_section_404(entity_hub_port):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{entity_hub_port}/entity/nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_entity_error_lookup(entity_hub_port):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{entity_hub_port}/entity/errors/401")
    assert r.status_code == 200
    body = r.text.lower()
    assert "401" in body
    assert "bearer" in body or "api key" in body


@pytest.mark.asyncio
async def test_entity_error_unknown_code_404(entity_hub_port):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"http://127.0.0.1:{entity_hub_port}/entity/errors/9999")
    assert r.status_code == 404
