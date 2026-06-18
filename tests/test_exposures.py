"""Phase 7.0 — capability-only exposures end-to-end.

A device registers via /ws/expose without being paired to any AI.
Any registered publisher's bearer key can invoke its capabilities.
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
    db_path = str(tmp_path / "exp.db")
    app = create_app(db_path=db_path)

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
async def test_register_exposure_returns_id_and_key(hub):
    """expose() registers the device and the hub returns an exposure_id
    + dx_ device key."""
    exp = expose(
        name="weather-sensor",
        capabilities={
            "weather_lookup": (
                {"type": "object",
                 "properties": {"city": {"type": "string"}},
                 "required": ["city"]},
                lambda args: {"temp": 22, "city": args["city"]},
            ),
        },
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if exp.exposure_id and exp.device_key:
            break
        await asyncio.sleep(0.1)
    assert exp.exposure_id.startswith("ex_"), f"got {exp.exposure_id!r}"
    assert exp.device_key.startswith("dx_"), f"got {exp.device_key!r}"


@pytest.mark.asyncio
async def test_get_exposures_lists_public_only(hub):
    e_pub = expose(
        name="weather-public",
        capabilities={"weather_lookup": ({"type": "object"}, lambda a: {})},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    e_priv = expose(
        name="secret-camera",
        capabilities={"take_photo": ({"type": "object"}, lambda a: {})},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=False,
    )
    for _ in range(50):
        if e_pub.exposure_id and e_priv.exposure_id:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        listing = (await c.get(f"http://127.0.0.1:{hub}/exposures")).json()

    names = {e["name"] for e in listing}
    assert "weather-public" in names
    assert "secret-camera" not in names


@pytest.mark.asyncio
async def test_invoke_exposure_via_publisher_key(hub):
    """A registered publisher's zk_ key authorizes invoking any exposure."""
    captured: dict = {}
    e = expose(
        name="weather",
        capabilities={
            "weather_lookup": (
                {"type": "object", "required": ["city"],
                 "properties": {"city": {"type": "string"}}},
                lambda a: {"city": a["city"], "temp": 18,
                           "_captured": captured.update(a) or True},
            ),
        },
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    pub = publish(
        name="some-ai",
        description="any AI",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "weather_lookup", "args": {"city": "Mississauga"}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["result"]["city"] == "Mississauga"
    assert body["result"]["temp"] == 18
    assert captured == {"city": "Mississauga"}


@pytest.mark.asyncio
async def test_invoke_schemaless_capability(hub):
    """A no-arg capability that declares no schema (schema is None/absent —
    the idiomatic shape for a non-Python publisher) must still be invokable.
    Regression: the schema lookup conflated 'capability not found' with
    'capability has no schema', 404-ing a legitimately-exposed capability."""
    e = expose(
        name="pinger",
        capabilities={"ping": (None, lambda a: {"pong": True})},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    pub = publish(
        name="ping-caller",
        description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        resp = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "ping", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"pong": True}


@pytest.mark.asyncio
async def test_invoke_unknown_capability_on_known_exposure(hub):
    """An exposure that exists but doesn't declare the requested capability
    still 404s — the fix must not weaken the not-found guard."""
    e = expose(
        name="lonely",
        capabilities={"real_cap": ({"type": "object"}, lambda a: {"ok": True})},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    pub = publish(
        name="probe",
        description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "ghost_cap", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 404
    assert "ghost_cap" in r.text


@pytest.mark.asyncio
async def test_invoke_rejects_without_publisher_key(hub):
    e = expose(
        name="x",
        capabilities={"do_x": ({"type": "object"}, lambda a: {"ok": True})},
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        # No bearer
        r = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "do_x", "args": {}},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_invoke_404_for_unknown_exposure(hub):
    pub = publish(
        name="auth-source",
        description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/exposures/ex_nope/invoke",
            json={"capability": "x", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invoke_validates_args_against_schema(hub):
    e = expose(
        name="strict",
        capabilities={
            "strict_op": (
                {"type": "object", "required": ["needed"],
                 "properties": {"needed": {"type": "string"}}},
                lambda a: {"got": a},
            ),
        },
        hub_url=f"ws://127.0.0.1:{hub}",
        public=True,
    )
    for _ in range(50):
        if e.exposure_id:
            break
        await asyncio.sleep(0.1)

    pub = publish(
        name="caller",
        description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/exposures/{e.exposure_id}/invoke",
            json={"capability": "strict_op", "args": {}},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 400
    assert "needed" in r.text.lower()
