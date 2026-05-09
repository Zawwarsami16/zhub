"""Phase 3.0b — entity hint header on error responses.

Every 4xx/5xx response from the hub carries an X-Zhub-Entity-Hint header
pointing at the relevant /entity/errors/<code> recipe — *if* such a
recipe exists in entity.md. Any AI hitting the hub can self-debug by
following the hint.
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
def hint_hub_port():
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
async def test_401_carries_entity_hint(hint_hub_port):
    """Wrong bearer key → 401 → response should include the entity hint
    header pointing at /entity/errors/401."""
    hub_ws = f"ws://127.0.0.1:{hint_hub_port}"
    hub_http = f"http://127.0.0.1:{hint_hub_port}"

    pub = publish(
        name="hint-bot",
        description="hint test",
        chat_handler=lambda m, o: "ok",
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer zk_definitely_wrong"},
        )
    assert resp.status_code == 401
    assert resp.headers.get("x-zhub-entity-hint") == "/entity/errors/401", \
        f"missing/bad hint header: {dict(resp.headers)!r}"


@pytest.mark.asyncio
async def test_404_carries_entity_hint(hint_hub_port):
    """Unknown AI → 404 → hint should point at /entity/errors/404."""
    hub_http = f"http://127.0.0.1:{hint_hub_port}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/ghost/v1/chat/completions",
            json={"messages": []},
            headers={"Authorization": "Bearer zk_x"},
        )
    assert resp.status_code == 404
    assert resp.headers.get("x-zhub-entity-hint") == "/entity/errors/404"


@pytest.mark.asyncio
async def test_200_carries_no_hint(hint_hub_port):
    """Successful responses don't get a hint header."""
    hub_http = f"http://127.0.0.1:{hint_hub_port}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{hub_http}/healthz")
    assert resp.status_code == 200
    assert "x-zhub-entity-hint" not in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_unknown_error_code_skips_hint(hint_hub_port):
    """Hitting a path that triggers an error code with no entity entry
    (e.g. 405 method-not-allowed) returns no hint — graceful degradation,
    not a stale pointer."""
    hub_http = f"http://127.0.0.1:{hint_hub_port}"
    async with httpx.AsyncClient(timeout=5.0) as client:
        # GET on a POST-only endpoint → 405 (no entry in entity for 405)
        resp = await client.get(f"{hub_http}/x/v1/chat/completions")
    assert resp.status_code == 405
    assert "x-zhub-entity-hint" not in {k.lower() for k in resp.headers}, \
        f"unexpected hint header for unmapped code: {dict(resp.headers)!r}"
