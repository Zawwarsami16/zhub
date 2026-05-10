"""HTTP-level tests for entity extensions: add / list / delete + merge into /entity."""

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
def hub(tmp_path):
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    db_path = str(tmp_path / "ext.db")
    app = create_app(db_path=db_path)

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
async def test_extend_unauthorized_without_bearer(hub):
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/entity/extend",
            json={"section": "patterns", "title": "x", "body": "y"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_extend_with_valid_publisher_key_succeeds(hub):
    pub = publish(
        name="ext-bot",
        description="extender",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/entity/extend",
            json={
                "section": "patterns",
                "title": "loki-whatsapp",
                "body": "Call /v1/invoke directly when sending whatsapp.",
            },
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["id"], int)
    assert body["section"] == "patterns"
    assert body["title"] == "loki-whatsapp"
    assert body["added_by"] == "ext-bot"


@pytest.mark.asyncio
async def test_extension_surfaces_in_entity_full_and_section(hub):
    pub = publish(
        name="ext-bot-2",
        description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        await c.post(
            f"http://127.0.0.1:{hub}/entity/extend",
            json={"section": "patterns",
                  "title": "loki-shortcut",
                  "body": "Use /v1/invoke for known calls."},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )

        full = (await c.get(f"http://127.0.0.1:{hub}/entity")).text
        assert "loki-shortcut" in full
        assert "Use /v1/invoke for known calls." in full
        assert "user-added by ext-bot-2" in full

        section = (await c.get(f"http://127.0.0.1:{hub}/entity/patterns")).text
        assert "loki-shortcut" in section
        assert "## extensions" not in section.split("loki-shortcut")[0]


@pytest.mark.asyncio
async def test_extension_to_errors_section_surfaces_in_error_lookup(hub):
    pub = publish(
        name="ext-bot-3",
        description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        await c.post(
            f"http://127.0.0.1:{hub}/entity/extend",
            json={"section": "errors", "title": "401",
                  "body": "Also check that the publisher restarted with --db."},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )

        recipe = (await c.get(f"http://127.0.0.1:{hub}/entity/errors/401")).text
        # shipped recipe still present
        assert "Bearer key" in recipe or "api key" in recipe.lower()
        # user extension appended
        assert "Also check that the publisher restarted with --db." in recipe


@pytest.mark.asyncio
async def test_list_and_delete_extensions(hub):
    pub = publish(
        name="ext-bot-4",
        description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    auth = {"Authorization": f"Bearer {pub.api_key}"}

    async with httpx.AsyncClient(timeout=5.0) as c:
        e1 = (await c.post(
            f"http://127.0.0.1:{hub}/entity/extend",
            json={"section": "patterns", "title": "a", "body": "b"},
            headers=auth,
        )).json()
        e2 = (await c.post(
            f"http://127.0.0.1:{hub}/entity/extend",
            json={"section": "debug", "title": "c", "body": "d"},
            headers=auth,
        )).json()

        listing = (await c.get(f"http://127.0.0.1:{hub}/entity/extend",
                               headers=auth)).json()["extensions"]
        ids = sorted(e["id"] for e in listing)
        assert ids == sorted([e1["id"], e2["id"]])

        # delete the first
        d = await c.delete(f"http://127.0.0.1:{hub}/entity/extend/{e1['id']}",
                           headers=auth)
        assert d.status_code == 200
        assert d.json()["deleted"] is True

        listing2 = (await c.get(f"http://127.0.0.1:{hub}/entity/extend",
                                headers=auth)).json()["extensions"]
        assert [e["id"] for e in listing2] == [e2["id"]]


@pytest.mark.asyncio
async def test_extend_rejects_oversized_body(hub):
    pub = publish(
        name="ext-bot-5", description="x",
        chat_handler=lambda m, o: "ok",
        hub_url=f"ws://127.0.0.1:{hub}",
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(
            f"http://127.0.0.1:{hub}/entity/extend",
            json={"section": "patterns", "title": "huge", "body": "x" * 9000},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert r.status_code == 413
