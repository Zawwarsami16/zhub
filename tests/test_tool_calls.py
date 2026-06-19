"""Phase 1.8 — Tool calls (function calling) end-to-end.

Standard OpenAI-style function calling works through zhub. The AI's
tool_calls in a chat response automatically map to invoke-request envelopes
against connected clients' capabilities. Tool result returns to the AI as a
role=tool message.
"""

import asyncio
import json
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
def tool_hub_port():
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
async def test_tool_call_auto_resolved(tool_hub_port):
    """Publisher emits tool_calls on the first turn, plain text on the second.
    The hub auto-invokes the connected client's capability and feeds the
    result back as a role=tool message. Final response should be the
    publisher's plain text — no tool_calls visible to the HTTP caller."""
    hub_ws = f"ws://127.0.0.1:{tool_hub_port}"
    hub_http = f"http://127.0.0.1:{tool_hub_port}"
    call_counter = {"n": 0}
    invoke_counter = {"n": 0, "args": None}

    def chat_handler(messages, options):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{
                    "id": "call_send_1",
                    "type": "function",
                    "function": {
                        "name": "send_whatsapp",
                        "arguments": json.dumps({"to": "Ammi", "message": "ok"}),
                    },
                }],
                "finish_reason": "tool_calls",
            }
        # Second call should now have the tool result in messages.
        last_tool = next(
            (m for m in reversed(messages) if m.get("role") == "tool"),
            None,
        )
        return f"delivered={last_tool['content'] if last_tool else 'none'}"

    def whatsapp_handler(args):
        invoke_counter["n"] += 1
        invoke_counter["args"] = dict(args)
        return {"ok": True, "delivered": True}

    pub = publish(
        name="ammi-bot",
        description="tool test",
        chat_handler=chat_handler,
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_ws,
        capabilities={
            "send_whatsapp": (
                {"type": "object", "properties": {"to": {"type": "string"},
                                                  "message": {"type": "string"}}},
                whatsapp_handler,
            ),
        },
    )
    for _ in range(60):
        if pub.find_capability("send_whatsapp") is not None:
            break
        await asyncio.sleep(0.1)
    assert pub.find_capability("send_whatsapp") is not None, "connection never established"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"model": "test",
                  "messages": [{"role": "user", "content": "tell ammi"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    assert content.startswith("delivered="), f"unexpected content: {content!r}"
    assert "ok" in content.lower() or "true" in content.lower(), \
        f"tool result missing from final text: {content!r}"
    assert invoke_counter["n"] == 1, \
        f"capability should have been invoked exactly once, got {invoke_counter['n']}"
    assert invoke_counter["args"] == {"to": "Ammi", "message": "ok"}
    assert call_counter["n"] == 2, "publisher should have been called twice"
    # The hub records what tools it auto-resolved in the response usage block
    audit = body.get("usage", {}).get("tool_results")
    assert audit, f"expected usage.tool_results audit, got {body.get('usage')!r}"
    assert audit[0]["name"] == "send_whatsapp"
    assert audit[0]["args"] == {"to": "Ammi", "message": "ok"}
    assert audit[0]["result"]["delivered"] is True


@pytest.mark.asyncio
async def test_tool_call_pass_through_with_header(tool_hub_port):
    """With X-Zhub-Tool-Resolve: client, the hub returns tool_calls verbatim
    instead of auto-resolving them. The connected client's capability is
    NOT invoked."""
    hub_ws = f"ws://127.0.0.1:{tool_hub_port}"
    hub_http = f"http://127.0.0.1:{tool_hub_port}"
    invoke_counter = {"n": 0}

    def chat_handler(messages, options):
        return {
            "text": "",
            "tool_calls": [{
                "id": "call_x_1",
                "type": "function",
                "function": {"name": "do_thing", "arguments": "{}"},
            }],
            "finish_reason": "tool_calls",
        }

    def thing_handler(args):
        invoke_counter["n"] += 1
        return {"never": "called"}

    pub = publish(
        name="passthrough-bot",
        description="passthrough test",
        chat_handler=chat_handler,
        hub_url=hub_ws,
    )
    for _ in range(50):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    assert pub.api_key

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=hub_ws,
        capabilities={"do_thing": ({"type": "object"}, thing_handler)},
    )
    for _ in range(60):
        if pub.find_capability("do_thing") is not None:
            break
        await asyncio.sleep(0.1)
    assert pub.find_capability("do_thing") is not None, "connection never established"

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}]},
            headers={
                "Authorization": f"Bearer {pub.api_key}",
                "X-Zhub-Tool-Resolve": "client",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    msg = body["choices"][0]["message"]
    assert msg.get("tool_calls"), \
        f"expected tool_calls passed through, got {msg!r}"
    assert msg["tool_calls"][0]["function"]["name"] == "do_thing"
    assert invoke_counter["n"] == 0, \
        f"capability should NOT have been invoked, got {invoke_counter['n']}"
