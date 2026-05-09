"""
Tool-call demo — the OpenAI function-calling story end-to-end through zhub.

The publisher (LLM) emits an OpenAI `tool_calls` response. The hub looks at
its connected clients, finds one that exposes a capability matching the
tool name, invokes it via the bidirectional channel, gets the result, and
feeds it back to the LLM as a `role: tool` message. The LLM then returns a
plain text answer that already incorporates the tool result.

This shows phases 1.8 + 1.9 working together: the LLM doesn't have to be
told what tools exist — the hub injects connected capabilities as OpenAI
function-tool entries automatically.

Run:
    1. python -m zhub.server --port 8080
    2. python examples/tool_demo.py
"""

import asyncio
import json
import logging
import os

import httpx

from zhub import publish, connect


HUB_HOST = os.environ.get("ZHUB_HOST", "127.0.0.1")
HUB_PORT = int(os.environ.get("ZHUB_PORT", "8080"))
HUB_WS = f"ws://{HUB_HOST}:{HUB_PORT}"
HUB_HTTP = f"http://{HUB_HOST}:{HUB_PORT}"


def stub_llm(messages, options):
    """A pretend LLM. Looks at the most recent user/tool message and decides:
    if there's already a tool result in the conversation, write a final
    answer; otherwise emit a tool_call. Real LLMs do this automatically."""
    tools = options.get("tools") or []
    tool_msg = next(
        (m for m in reversed(messages) if m.get("role") == "tool"),
        None,
    )
    if tool_msg is not None:
        try:
            result = json.loads(tool_msg.get("content") or "null")
        except (TypeError, ValueError):
            result = tool_msg.get("content")
        return f"Looked up the weather: {result}. Looks good outside."

    # Pick the first tool the hub injected (capability auto-injection)
    tool = next(
        (t for t in tools if t.get("function", {}).get("name") == "weather_lookup"),
        None,
    )
    if tool is None:
        return "I don't have a weather tool available."

    return {
        "text": "",
        "tool_calls": [{
            "id": "call_w1",
            "type": "function",
            "function": {
                "name": "weather_lookup",
                "arguments": json.dumps({"city": "Mississauga"}),
            },
        }],
        "finish_reason": "tool_calls",
    }


def weather_stub(args):
    """Capability handler exposed by the connected client."""
    city = args.get("city", "?")
    return {"city": city, "temp_c": 14, "condition": "partly cloudy"}


async def main():
    logging.basicConfig(level=logging.INFO)
    pub = publish(
        name="tool-demo-bot",
        description="phase 1.8/1.9 demo",
        chat_handler=stub_llm,
        hub_url=HUB_WS,
        public=True,
    )
    while not pub.api_key:
        await asyncio.sleep(0.1)
    print(f"published 'tool-demo-bot'  key={pub.api_key[:14]}…")

    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=HUB_WS,
        description="weather-client",
        capabilities={
            "weather_lookup": (
                {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
                weather_stub,
            ),
        },
    )
    await asyncio.sleep(0.6)
    print("client connected, exposing weather_lookup")
    print()

    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(
            f"{HUB_HTTP}/{pub.name}/v1/chat/completions",
            json={
                "model": "demo",
                "messages": [{"role": "user", "content": "what's the weather like?"}],
            },
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )

    body = resp.json()
    print(">>> what's the weather like?")
    print(f"<<< {body['choices'][0]['message']['content']}")
    audit = body.get("usage", {}).get("tool_results")
    if audit:
        print()
        print("hub auto-resolved these tool calls:")
        for entry in audit:
            print(f"  • {entry['name']}({entry['args']}) → {entry['result']}")


if __name__ == "__main__":
    asyncio.run(main())
