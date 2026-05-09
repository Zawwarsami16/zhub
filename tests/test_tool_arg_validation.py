"""Phase 2.2 — JSON-Schema validation of tool args.

When an LLM emits a tool_call with arguments that don't match the
capability's declared schema (missing a required field, wrong type), the
hub catches it before invoking the capability. The connected client never
sees garbage. The validation error is fed back to the LLM as the tool
result so it can retry with corrected args.
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
def val_hub_port():
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
async def test_missing_required_field_short_circuits_invoke(val_hub_port):
    """LLM emits tool_call with no args; schema requires `city`.
    Hub should return a validation error as the tool result and the
    capability handler should NOT be invoked."""
    hub_ws = f"ws://127.0.0.1:{val_hub_port}"
    hub_http = f"http://127.0.0.1:{val_hub_port}"
    invoke_count = {"n": 0}
    call_n = {"n": 0}

    def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{
                    "id": "c_bad",
                    "type": "function",
                    "function": {"name": "weather_lookup", "arguments": "{}"},
                }],
                "finish_reason": "tool_calls",
            }
        # second call: tool result is in messages
        last_tool = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
        return f"tool said: {last_tool['content']}"

    def cap_handler(args):
        invoke_count["n"] += 1
        return {"city": args.get("city"), "temp": 22}

    pub = publish(
        name="val-bot",
        description="validation test",
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
        capabilities={
            "weather_lookup": (
                {
                    "type": "object",
                    "required": ["city"],
                    "properties": {"city": {"type": "string"}},
                },
                cap_handler,
            ),
        },
    )
    await asyncio.sleep(0.6)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "weather?"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    audit = body["usage"]["tool_results"]
    assert len(audit) == 1
    result = audit[0]["result"]
    assert "error" in result, f"expected validation error, got {result!r}"
    assert "city" in result["error"].lower(), f"error should mention missing field: {result!r}"
    assert invoke_count["n"] == 0, "capability must NOT be invoked when args fail validation"


@pytest.mark.asyncio
async def test_wrong_type_short_circuits_invoke(val_hub_port):
    """Schema says city: string. LLM emits city: 42. Hub blocks the invoke."""
    hub_ws = f"ws://127.0.0.1:{val_hub_port}"
    hub_http = f"http://127.0.0.1:{val_hub_port}"
    invoke_count = {"n": 0}
    call_n = {"n": 0}

    def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{
                    "id": "c_t",
                    "type": "function",
                    "function": {
                        "name": "weather_t",
                        "arguments": json.dumps({"city": 42}),
                    },
                }],
                "finish_reason": "tool_calls",
            }
        return "fallback"

    def cap_handler(args):
        invoke_count["n"] += 1
        return {"ok": True}

    pub = publish(
        name="val-type-bot",
        description="type validation",
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
        capabilities={
            "weather_t": (
                {
                    "type": "object",
                    "required": ["city"],
                    "properties": {"city": {"type": "string"}},
                },
                cap_handler,
            ),
        },
    )
    await asyncio.sleep(0.6)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    body = resp.json()
    audit = body["usage"]["tool_results"]
    result = audit[0]["result"]
    assert "error" in result, f"got {result!r}"
    err_lower = result["error"].lower()
    assert "city" in err_lower and ("string" in err_lower or "type" in err_lower)
    assert invoke_count["n"] == 0


@pytest.mark.asyncio
async def test_valid_args_pass_through_to_handler(val_hub_port):
    """Sanity: well-formed args reach the handler unchanged."""
    hub_ws = f"ws://127.0.0.1:{val_hub_port}"
    hub_http = f"http://127.0.0.1:{val_hub_port}"
    captured = {}
    call_n = {"n": 0}

    def chat_handler(messages, options):
        call_n["n"] += 1
        if call_n["n"] == 1:
            return {
                "text": "",
                "tool_calls": [{
                    "id": "c_ok",
                    "type": "function",
                    "function": {
                        "name": "weather_ok",
                        "arguments": json.dumps({"city": "Mississauga"}),
                    },
                }],
                "finish_reason": "tool_calls",
            }
        return "got it"

    def cap_handler(args):
        captured.update(args)
        return {"city": args["city"], "temp": 14}

    pub = publish(
        name="val-ok-bot",
        description="happy path",
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
        capabilities={
            "weather_ok": (
                {"type": "object", "required": ["city"],
                 "properties": {"city": {"type": "string"}}},
                cap_handler,
            ),
        },
    )
    await asyncio.sleep(0.6)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{hub_http}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
    body = resp.json()
    audit = body["usage"]["tool_results"]
    assert "error" not in audit[0]["result"], f"unexpected error: {audit[0]!r}"
    assert captured == {"city": "Mississauga"}
