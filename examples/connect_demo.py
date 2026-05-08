"""
Minimal connect-mode example — a fake "Loki" client that exposes a couple
of capabilities back to the AI it connects to, AND can chat with the AI.

Run order:
    1. python -m zhub.server --port 8080
    2. python examples/publish_demo.py        (note the API key)
    3. AI_NAME=echo API_KEY=zk_... python examples/connect_demo.py

The client will:
    - Register itself as a connection to the AI
    - Expose 'send_whatsapp' and 'get_battery' capabilities (stubbed)
    - Send a chat to the AI and print the response
    - Idle, waiting for the AI to invoke its capabilities

To trigger the AI invoking capabilities, see examples/orchestrate_demo.py.
"""

import asyncio
import logging
import os

from zhub import connect


def send_whatsapp_stub(args):
    """Pretend to send WhatsApp."""
    return {
        "ok": True,
        "to": args.get("to", "?"),
        "message_id": "wa_" + os.urandom(4).hex(),
        "delivered": True,
    }


def get_battery_stub(args):
    return {"level": 78, "charging": False, "temperature_c": 31}


WHATSAPP_SCHEMA = {
    "type": "object",
    "required": ["to", "message"],
    "properties": {
        "to": {"type": "string", "description": "phone number or contact name"},
        "message": {"type": "string"},
    },
}

BATTERY_SCHEMA = {"type": "object", "properties": {}}


async def main():
    logging.basicConfig(level=logging.INFO)
    ai_name = os.environ.get("AI_NAME", "echo")
    api_key = os.environ.get("API_KEY")
    if not api_key:
        raise SystemExit("set API_KEY env var (run publish_demo.py first to get one)")

    conn = connect(
        ai_name=ai_name,
        api_key=api_key,
        hub_url="ws://localhost:8080",
        description="fake Loki — phone control surface",
        operator="demo",
        capabilities={
            "send_whatsapp": (WHATSAPP_SCHEMA, send_whatsapp_stub),
            "get_battery": (BATTERY_SCHEMA, get_battery_stub),
        },
    )

    # give the registration a moment
    await asyncio.sleep(0.5)
    print(f"connected to {ai_name} as a client")
    print("exposed capabilities: send_whatsapp, get_battery")
    print()

    # send a chat request
    print("--- sending chat request ---")
    resp = await conn.chat(messages=[{"role": "user", "content": "hello from fake-loki"}])
    print(f"AI replied: {resp.get('text')}")
    print()
    print("now idling — AI can invoke send_whatsapp / get_battery via the hub")
    print("Ctrl-C to stop.")

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
