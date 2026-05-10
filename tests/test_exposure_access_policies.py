"""Phase 15.0 — per-exposure access policies.

Devices can restrict which AIs are allowed to invoke their capabilities
by passing `allow_publishers=["zai", "claude-here"]` to expose(). When
unset, the existing "any registered publisher can invoke" behavior is
preserved (backwards compatible).
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
def hub(tmp_path):
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    app = create_app(db_path=str(tmp_path / "policy.db"))

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
async def test_no_policy_means_any_publisher_can_invoke(hub):
    """Backwards compat: when allow_publishers isn't set, any registered
    publisher's bearer key authorizes the invoke."""
    e = expose(
        name="open-cap",
        capabilities={"do_x": ({"type": "object"}, lambda a: {"ok": True})},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    pub = publish(name="any-ai", description="x",
                  chat_handler=lambda m, o: "ok",
                  hub_url=f"ws://127.0.0.1:{hub}")
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "do_x", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_allow_list_permits_listed_publisher(hub):
    """Exposure declares allow_publishers=['allowed-ai']. That AI's key
    invokes successfully."""
    invoke_n = {"n": 0}

    def handler(args):
        invoke_n["n"] += 1
        return {"ok": True}

    e = expose(
        name="restricted-cap",
        capabilities={"do_x": ({"type": "object"}, handler)},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
        allow_publishers=["allowed-ai"],
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    pub = publish(name="allowed-ai", description="x",
                  chat_handler=lambda m, o: "ok",
                  hub_url=f"ws://127.0.0.1:{hub}")
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "do_x", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 200, r.text
    assert invoke_n["n"] == 1


@pytest.mark.asyncio
async def test_allow_list_denies_unlisted_publisher(hub):
    """Same exposure, a different publisher's key — denied with 403."""
    invoke_n = {"n": 0}

    def handler(args):
        invoke_n["n"] += 1
        return {"ok": True}

    e = expose(
        name="locked-cap",
        capabilities={"do_x": ({"type": "object"}, handler)},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
        allow_publishers=["only-this-one"],
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    pub = publish(name="other-ai", description="not allowed",
                  chat_handler=lambda m, o: "ok",
                  hub_url=f"ws://127.0.0.1:{hub}")
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "do_x", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 403, r.text
    assert invoke_n["n"] == 0


@pytest.mark.asyncio
async def test_empty_allow_list_means_no_publisher_can_invoke(hub):
    """Edge case: allow_publishers=[] (explicit empty list) is a kill
    switch — nobody can invoke. Distinct from `not set` (which means
    everyone can)."""
    invoke_n = {"n": 0}

    def handler(args):
        invoke_n["n"] += 1
        return {"ok": True}

    e = expose(
        name="kill-switch-cap",
        capabilities={"do_x": ({"type": "object"}, handler)},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
        allow_publishers=[],
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    pub = publish(name="any-ai", description="x",
                  chat_handler=lambda m, o: "ok",
                  hub_url=f"ws://127.0.0.1:{hub}")
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "do_x", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 403
    assert invoke_n["n"] == 0


@pytest.mark.asyncio
async def test_policy_visible_in_get_exposures(hub):
    """The allow_publishers list (if set) shows up in /exposures so other
    AIs can discover whether they're permitted before attempting."""
    e_open = expose(
        name="open", capabilities={"x": ({"type": "object"}, lambda a: 1)},
        hub_url=f"ws://127.0.0.1:{hub}", public=True,
    )
    e_locked = expose(
        name="locked", capabilities={"x": ({"type": "object"}, lambda a: 1)},
        hub_url=f"ws://127.0.0.1:{hub}", public=True,
        allow_publishers=["only-zai"],
    )
    for _ in range(50):
        if e_open.exposure_id and e_locked.exposure_id:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        listing = (await c.get(f"http://127.0.0.1:{hub}/exposures")).json()

    by_name = {item["name"]: item for item in listing}
    assert "allow_publishers" not in by_name["open"] or by_name["open"].get("allow_publishers") in (None, [])
    assert by_name["locked"].get("allow_publishers") == ["only-zai"]
