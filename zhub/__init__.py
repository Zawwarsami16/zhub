"""
zhub — WiFi for AIs.

Drop-in skill that lets any AI publish a discoverable, controllable endpoint.
Bidirectional: the AI sees its connected clients and their capabilities, and
can invoke those capabilities back. Connected clients call the AI in
OpenAI-compatible format and don't need to know anything about how it's
implemented.

Two modes:

    publish() — installed in the AI. Opens a persistent connection to the
                hub, registers a manifest, awaits incoming chat requests.
    connect() — installed in clients (a generic device, Telegram bot, web chat, etc.).
                Registers the client's own capabilities back to the AI,
                listens for invocations.

Quickstart:

    # Inside your AI
    import asyncio
    from zhub import publish

    async def main():
        pub = publish(
            name="my-ai",
            description="A custom AI agent",
            hub_url="https://hub.example.com",
            chat_handler=lambda messages: "...",
        )
        await pub.run_forever()

    asyncio.run(main())

    # Inside your client (e.g., a generic device)
    import asyncio
    from zhub import connect

    async def main():
        conn = connect(
            endpoint="https://hub.example.com/my-ai",
            api_key="zk_...",
            capabilities={
                "send_whatsapp": (schema, handler),
            },
        )
        await conn.run_forever()

    asyncio.run(main())
"""

from .client import (
    publish, connect, expose, ZhubPublication, ZhubConnection, ZhubExposure,
)
from .manifest import Manifest, Capability
from .errors import ZhubError, AuthError, ConnectionError as ZhubConnectionError

# Signing API is optional — only available when 'cryptography' is installed.
try:
    from .signing import (
        generate_keypair, sign_manifest, verify_manifest, public_key_from_private,
    )
    _SIGNING_AVAILABLE = True
except SystemExit:
    _SIGNING_AVAILABLE = False

__version__ = "0.3.0"
__all__ = [
    "publish",
    "connect",
    "expose",
    "ZhubPublication",
    "ZhubConnection",
    "ZhubExposure",
    "Manifest",
    "Capability",
    "ZhubError",
    "AuthError",
    "ZhubConnectionError",
]
if _SIGNING_AVAILABLE:
    __all__ += ["generate_keypair", "sign_manifest", "verify_manifest", "public_key_from_private"]
