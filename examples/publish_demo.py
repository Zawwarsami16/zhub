"""
Minimal publisher example — a fake AI that just echoes the user's message.

Run the hub in one terminal:
    python -m zhub.server --port 8080

Run this in another terminal:
    python examples/publish_demo.py

The script will print the assigned URL + API key. Hit it from a third
terminal with curl or the OpenAI Python client (see examples/curl_test.sh).
"""

import asyncio
import logging

from zhub import publish


def echo_handler(messages, options):
    """Toy chat handler — pulls the last user message and echoes it back."""
    last_user = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        "(no user message)",
    )
    return f"Echo: {last_user}"


async def main():
    logging.basicConfig(level=logging.INFO)

    pub = publish(
        name="echo",
        description="Toy echo AI for zhub Phase 0 demo.",
        chat_handler=echo_handler,
        hub_url="ws://localhost:8080",
        public=True,
        operator="demo",
    )

    # wait for the registration ack
    while not pub.api_key:
        await asyncio.sleep(0.1)

    print("=" * 60)
    print(f"  AI registered as: {pub.name}")
    print(f"  Base URL:         http://localhost:8080{pub.base_url}")
    print(f"  Manifest:         http://localhost:8080{pub.base_url}/manifest.json")
    print(f"  API key:          {pub.api_key}")
    print("=" * 60)
    print()
    print("  test it:")
    print(f'    curl http://localhost:8080{pub.base_url}/v1/chat/completions \\')
    print(f"      -H 'Authorization: Bearer {pub.api_key}' \\")
    print(f"      -H 'Content-Type: application/json' \\")
    print(f"      -d '{{\"messages\": [{{\"role\": \"user\", \"content\": \"hi\"}}]}}'")
    print()
    print("  Ctrl-C to stop.")

    # block forever
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
