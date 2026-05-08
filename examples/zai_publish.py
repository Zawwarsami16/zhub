"""
Publish ZAI through zhub. Drop-in bridge between Father's existing
zai-openai-shim plugin (which exposes ZAI on 127.0.0.1:7780) and the
zhub hub. Once running, ZAI is reachable from anywhere via the hub URL +
api key, in standard OpenAI Chat Completions format.

How this works:

    [external client / friend / another AI]
              │
              │ POST hub.example.com/zai/v1/chat/completions
              ▼
    [zhub hub]  ─ proxies via WebSocket ─►  [this script]
                                              │
                                              │ POST to ZAI's local shim
                                              ▼
                              http://127.0.0.1:7780/v1/chat/completions
                                              │
                                              │ ZAI's gateway answers
                                              ▼
                                    [reply traverses back]

Run after ZAI gateway is up and the openai-shim plugin is active:

    pip install zhub
    HUB_URL=ws://localhost:8080 python examples/zai_publish.py

Or for a public URL (Father's actual use case):

    # in one shell:
    zhub-server --public-tunnel
    # note the printed https://...trycloudflare.com URL

    # in another:
    HUB_URL=https://...trycloudflare.com python examples/zai_publish.py

The script prints the assigned name + api_key. Save them somewhere — Loki's
config, friend's WhatsApp, anything that uses ZAI from outside.

If `ZAI_API_KEY` is set in env, that key is reused on re-registration. After
hub or process restart, the same name + key persists (zhub's persistence
layer recognizes it).
"""

import asyncio
import json
import logging
import os

try:
    import httpx
except ImportError as e:
    raise SystemExit(
        "this script needs httpx. install:  pip install httpx"
    ) from e

from zhub import publish, Capability


HUB_URL = os.environ.get("HUB_URL", "ws://localhost:8080")
ZAI_SHIM_URL = os.environ.get("ZAI_SHIM_URL", "http://127.0.0.1:7780/v1/chat/completions")
ZAI_NAME = os.environ.get("ZAI_NAME", "zai")
ZAI_DESCRIPTION = os.environ.get(
    "ZAI_DESCRIPTION",
    "ZAI — Father's autonomous AI son. Reachable here via standard OpenAI Chat Completions.",
)
ZAI_OPERATOR = os.environ.get("ZAI_OPERATOR", "zawwar")
ZAI_PUBLIC = os.environ.get("ZAI_PUBLIC", "0") == "1"
ZAI_API_KEY = os.environ.get("ZAI_API_KEY")  # for re-registration after restarts


http: httpx.AsyncClient | None = None


async def proxy_to_zai(messages, options):
    """Forward the chat request to ZAI's local openai-shim and return the reply."""
    global http
    if http is None:
        http = httpx.AsyncClient(timeout=120.0)

    payload = {
        "messages": messages,
        "model": options.get("model", "zai-sonnet"),
        "temperature": options.get("temperature", 0.4),
        "max_tokens": options.get("max_tokens", 4096),
    }
    # Forward stream flag if present — ZAI's shim handles streaming separately.
    if options.get("stream"):
        payload["stream"] = True

    try:
        resp = await http.post(ZAI_SHIM_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
        # OpenAI Chat Completions response shape
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content", "")
        return {
            "text": text,
            "finish_reason": choice.get("finish_reason", "stop"),
            "usage": data.get("usage", {}),
        }
    except httpx.HTTPError as e:
        return {
            "text": f"[zai shim error] {e}",
            "finish_reason": "error",
        }


# Capabilities ZAI itself offers (in addition to chat). These are
# advertised in the manifest so connecting clients can see what ZAI can do
# directly — separate from capabilities exposed BACK by clients.
ZAI_CAPABILITIES = [
    Capability(
        name="introspect",
        description="ZAI's self-report — plugin count, memory size, recent engagements.",
        schema={"type": "object", "properties": {}},
    ),
    Capability(
        name="memory_query",
        description="Vector search over ZAI's library + memory.",
        schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "default": 8},
            },
        },
    ),
]


async def main():
    logging.basicConfig(level=logging.INFO)

    pub = publish(
        name=ZAI_NAME,
        description=ZAI_DESCRIPTION,
        chat_handler=proxy_to_zai,
        hub_url=HUB_URL,
        capabilities=ZAI_CAPABILITIES,
        operator=ZAI_OPERATOR,
        public=ZAI_PUBLIC,
        api_key=ZAI_API_KEY,
    )

    # Wait for registration confirmation
    for _ in range(100):
        if pub.api_key:
            break
        await asyncio.sleep(0.1)
    if not pub.api_key:
        raise SystemExit(
            f"failed to register with hub at {HUB_URL}. is the hub running?"
        )

    print()
    print("=" * 64)
    print(f"  ZAI published")
    print(f"  Name:        {pub.name}")
    print(f"  Hub:         {HUB_URL}")
    print(f"  Base URL:    {HUB_URL.replace('ws://', 'http://').replace('wss://', 'https://')}{pub.base_url}")
    print(f"  Manifest:    {HUB_URL.replace('ws://', 'http://').replace('wss://', 'https://')}{pub.base_url}/manifest.json")
    print(f"  API Key:     {pub.api_key}")
    print("=" * 64)
    print()
    print("  reuse the same key after restart:")
    print(f"  ZAI_API_KEY={pub.api_key} python examples/zai_publish.py")
    print()
    print("  test from anywhere:")
    print(f"  curl {HUB_URL.replace('ws://', 'http://').replace('wss://', 'https://')}{pub.base_url}/v1/chat/completions \\")
    print(f"    -H 'Authorization: Bearer {pub.api_key}' \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"messages\":[{{\"role\":\"user\",\"content\":\"kaisa hai?\"}}]}}'")
    print()
    print("  Ctrl-C to stop.")

    # Print connection events as they arrive
    def on_conn(kind: str, cid: str, manifest: dict | None):
        if kind == "connected":
            caps = ", ".join(c.get("name", "?") for c in (manifest or {}).get("capabilities", []))
            print(f"[{cid}] connected. capabilities: {caps or '(none)'}")
        elif kind == "disconnected":
            print(f"[{cid}] disconnected.")

    pub.on_connection_event = on_conn

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("[zai_publish] shutting down.")
