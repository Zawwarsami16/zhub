"""Full-stack zhub demo in one runnable script.

Spins up a hub, a publisher (fake brain), an exposure (a "weather sensor"
device), and walks through:

  1. The publisher receives a chat asking about weather
  2. The publisher emits a tool_call for `weather_lookup`
  3. The hub auto-resolves the tool_call via the connected exposure
  4. The exposure returns weather data
  5. The publisher returns the final assembled answer
  6. We print the whole flow with timestamps

No external services needed — this runs end-to-end in one Python process.

Run:
    python examples/full_stack_demo.py
"""

import asyncio
import json
import os
import socket
import threading
import time

import httpx
import uvicorn

from zhub import publish, expose
from zhub.server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# A "fake brain" — emits a tool_call on first turn, plain text on second.
# A real brain (Anthropic / Groq / Ollama / etc.) would do this naturally.
_call_n = {"n": 0}


def fake_brain(messages, options):
    _call_n["n"] += 1
    if _call_n["n"] == 1:
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
    # Second turn: tool result is in messages
    last_tool = next(
        (m for m in reversed(messages) if m.get("role") == "tool"),
        None,
    )
    if last_tool:
        result = json.loads(last_tool["content"])
        return (
            f"Mississauga abhi {result['temp_c']}°C "
            f"({result['condition']}). {result.get('hint', '')}"
        )
    return "(no tool result available)"


# A "device" — exposes weather_lookup as a capability any AI can invoke
def weather_handler(args):
    print(f"  ⚡ device fired: weather_lookup({args})")
    return {
        "city": args["city"],
        "temp_c": 14,
        "condition": "partly cloudy",
        "hint": "Light jacket recommended.",
    }


async def main() -> None:
    port = _free_port()
    app = create_app()

    def _serve():
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning",
        )
        asyncio.run(uvicorn.Server(config).serve())

    threading.Thread(target=_serve, daemon=True).start()
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    print(f"[{time.strftime('%H:%M:%S')}] hub up on 127.0.0.1:{port}")

    # 1. publish an AI
    pub = publish(
        name="demo-ai",
        description="full-stack demo AI",
        chat_handler=fake_brain,
        hub_url=f"ws://127.0.0.1:{port}",
        public=True,
    )
    while not pub.api_key:
        await asyncio.sleep(0.05)
    print(f"[{time.strftime('%H:%M:%S')}] publisher 'demo-ai' registered")
    print(f"        URL: http://127.0.0.1:{port}/{pub.name}/v1")
    print(f"        KEY: {pub.api_key}")

    # 2. plug in a device exposure
    e = expose(
        name="weather-sensor",
        capabilities={
            "weather_lookup": (
                {
                    "type": "object",
                    "required": ["city"],
                    "properties": {"city": {"type": "string"}},
                },
                weather_handler,
            ),
        },
        hub_url=f"ws://127.0.0.1:{port}",
        public=True,
    )
    while not e.exposure_id:
        await asyncio.sleep(0.05)
    print(f"[{time.strftime('%H:%M:%S')}] exposure '{e.name}' registered "
          f"({e.exposure_id})")

    await asyncio.sleep(0.4)  # let the connection-event propagate

    # 3. drive a chat that needs the tool
    print()
    print(f"[{time.strftime('%H:%M:%S')}] >>> user: 'Mississauga ka weather batao?'")
    print()
    async with httpx.AsyncClient(timeout=10.0) as c:
        # First inject the exposure as a tool the AI can see
        # (zhub's Phase 1.9 auto-injects connected-client capabilities;
        # exposures need the publisher to opt in via use_global_exposures
        # in a future phase. for this demo we pass tools explicitly.)
        resp = await c.post(
            f"http://127.0.0.1:{port}/{pub.name}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user",
                     "content": "Mississauga ka weather batao?"},
                ],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "weather_lookup",
                        "description": "Look up the weather in a city",
                        "parameters": {
                            "type": "object",
                            "required": ["city"],
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }],
            },
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    final = body["choices"][0]["message"]["content"]
    audit = body.get("usage", {}).get("tool_results", [])

    print(f"[{time.strftime('%H:%M:%S')}] <<< AI: {final}")
    print()
    if audit:
        print(f"[{time.strftime('%H:%M:%S')}] hub auto-resolved tool calls:")
        for entry in audit:
            print(f"        • {entry['name']}({entry['args']}) → "
                  f"{entry['result']}")

    # 4. show the live state
    print()
    print(f"[{time.strftime('%H:%M:%S')}] hub state snapshot:")
    async with httpx.AsyncClient(timeout=5.0) as c:
        d = (await c.get(f"http://127.0.0.1:{port}/api/dashboard")).json()
    print(f"        publishers: {len(d['publishers'])}")
    print(f"        exposures:  {len(d['exposures'])}")
    print(f"        recent reqs: {len(d['recent_requests'])}")
    if d["by_ai"].get(pub.name):
        m = d["by_ai"][pub.name]
        print(f"        {pub.name}: {m.get('chat_requests', 0)} chats, "
              f"{m.get('tool_calls_resolved', 0)} tool calls resolved")
    print()
    print(f"open http://127.0.0.1:{port}/ in a browser to see the live")
    print("operator dashboard with traffic flowing through the SVG.")
    print("(Ctrl+C to stop)")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
