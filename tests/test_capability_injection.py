"""Phase 1.9 — Auto-inject connected-client capabilities as OpenAI tools.

When a chat-request reaches the hub for an AI that has at least one
connected client with capabilities, the hub turns each capability into an
OpenAI-style `tool` entry and adds it to the chat-request envelope. The
publisher (LLM) sees the runtime tools and can decide to call them.

Client-supplied `tools` in the request body are preserved; hub-supplied
tools append to that list (deduplicated by function.name).
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
from zhub import publish, connect


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def cap_hub_port():
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
async def test_connected_capabilities_injected_as_tools(cap_hub_port):
    """The publisher's chat_handler should see a `tools` entry per connected
    client capability, formatted in OpenAI function-tool shape."""
    hub_ws = f"ws://127.0.0.1:{cap_hub_port}"
    hub_http = f"http://127.0.0.1:{cap_hub_port}"
    captured = {"options": None}

    def chat_handler(messages, options):
        captured["options"] = options
        return "ok"

    pub = publish(
        name="cap-bot",
        description="cap inject test",
        chat_handler=chat_handler,
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_ws,
        capabilities={"weather_lookup": (schema, lambda a: {"temp": 22})},
    )
    await asyncio.sleep(0.8)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200

    tools = captured["options"].get("tools")
    assert tools, f"no tools injected: {captured['options']!r}"
    matching = [t for t in tools if t.get("function", {}).get("name") == "weather_lookup"]
    assert len(matching) == 1, f"expected one weather_lookup tool, got {tools!r}"
    fn = matching[0]["function"]
    assert fn["parameters"] == schema
    assert matching[0]["type"] == "function"


@pytest.mark.asyncio
async def test_client_supplied_tools_merged_with_injected(cap_hub_port):
    """If the caller supplies their own `tools` array, the hub merges its
    auto-injected tools with the caller's. Caller-supplied wins on name
    collision (caller is closer to the user)."""
    hub_ws = f"ws://127.0.0.1:{cap_hub_port}"
    hub_http = f"http://127.0.0.1:{cap_hub_port}"
    captured = {"options": None}

    def chat_handler(messages, options):
        captured["options"] = options
        return "ok"

    pub = publish(
        name="cap-merge-bot",
        description="merge test",
        chat_handler=chat_handler,
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_ws,
        capabilities={"local_thing": ({"type": "object"}, lambda a: 1)},
    )
    await asyncio.sleep(0.8)

    user_tool = {
        "type": "function",
        "function": {"name": "user_thing", "description": "from user",
                     "parameters": {"type": "object"}},
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [user_tool],
            },
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    tools = captured["options"].get("tools") or []
    names = sorted(t["function"]["name"] for t in tools)
    assert names == ["local_thing", "user_thing"], f"got {names!r}"
