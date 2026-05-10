"""multi_brain_publisher — pick a brain, publish to a zhub hub.

Usage:
    # auto-detect — uses the first available brain in priority order
    python examples/multi_brain_publisher.py

    # explicit brain
    python examples/multi_brain_publisher.py --brain ollama --name my-ai
    python examples/multi_brain_publisher.py --brain groq  --name my-ai

    # see what brains are available right now
    python examples/multi_brain_publisher.py --list

Env (override CLI defaults):
    ZHUB_HUB        ws hub url           (default ws://127.0.0.1:8080)
    ZHUB_NAME       publisher name        (default 'me')
    ZHUB_API_KEY    re-register with this existing zk_ key
    OLLAMA_HOST / GROQ_API_KEY / OPENAI_API_KEY / CEREBRAS_API_KEY
                    standard credentials for each brain
"""

import argparse
import asyncio
import os
import sys

from zhub import publish
from zhub.brains import detect, list_available, REGISTRY


def _resolve_brain(name: str):
    """Return an initialized brain adapter or exit with a helpful message."""
    if name == "auto":
        brain = detect()
        if brain is None:
            raise SystemExit(
                "no brains available. set OLLAMA_HOST or one of "
                "GROQ_API_KEY / OPENAI_API_KEY / CEREBRAS_API_KEY, "
                "or pass --brain explicitly."
            )
        return brain
    match = next((cls for cls in REGISTRY if cls.name == name), None)
    if match is None:
        raise SystemExit(f"unknown brain: {name!r}. "
                         f"choices: {[c.name for c in REGISTRY]} or 'auto'")
    brain = match.try_init()
    if brain is None:
        raise SystemExit(
            f"{name!r} not available — check creds (env vars) and "
            "that the upstream service is reachable."
        )
    return brain


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a zhub endpoint backed by any of four brains.",
    )
    parser.add_argument("--brain", default="auto",
                        help="ollama | groq | openai | cerebras | auto")
    parser.add_argument("--name", default=os.environ.get("ZHUB_NAME", "me"))
    parser.add_argument("--hub",
                        default=os.environ.get("ZHUB_HUB",
                                               "ws://127.0.0.1:8080"))
    parser.add_argument("--list", action="store_true",
                        help="list available brains and exit")
    parser.add_argument("--system", default=os.environ.get("ZHUB_SYSTEM", ""),
                        help="extra server-side system prompt prepended to "
                             "every chat (on top of the zhub-aware default). "
                             "Use this for persona / role / domain context.")
    parser.add_argument("--no-zhub-context", action="store_true",
                        help="don't auto-inject the default 'you are served "
                             "via zhub' awareness preamble. Use if you want "
                             "the brain to answer as if zhub didn't exist.")
    args = parser.parse_args()

    if args.list:
        avail = list_available()
        if not avail:
            print("no brains available", file=sys.stderr)
            return
        for a in avail:
            print(f"{a.name:10}  {a.label}")
        return

    brain = _resolve_brain(args.brain)
    print(f"using brain: {brain.label}", flush=True)

    # Zhub-aware preamble — auto-injected so the brain naturally knows
    # it's part of the substrate, without the operator having to spell
    # it out. Disable with --no-zhub-context when you want a "naked" brain.
    zhub_preamble = (
        f"You are an AI agent published via zhub — an open-source substrate "
        f"that wraps any AI as a single OpenAI-compatible endpoint reachable "
        f"from any client (Pocket, curl, PowerShell, Claude Desktop, Cursor, "
        f"and anywhere else). You are running on the {brain.label} backend "
        f"right now, but the substrate could swap that brain underneath "
        f"without changing your URL or key. The hub operator can see live "
        f"traffic and observability on a dashboard, and the same endpoint "
        f"may be hit from multiple surfaces simultaneously. When asked about "
        f"yourself, zhub, or what's powering you, answer naturally with "
        f"this context — don't over-explain or sound like a marketing page."
    )

    async def chat_handler(messages, options):
        # Fold any system messages into a single system prompt; keep the
        # rest of the conversation intact. Order: zhub preamble (unless
        # disabled) → operator's --system → any system messages from the
        # client. Position-wise, server-side identity wins.
        system_parts = []
        if not args.no_zhub_context:
            system_parts.append(zhub_preamble)
        if args.system:
            system_parts.append(args.system)
        system_parts += [m.get("content", "") for m in messages
                         if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        system = "\n\n".join(p for p in system_parts if p) or None
        async for chunk in brain.stream(
            non_system,
            system=system,
            temperature=float(options.get("temperature", 0.7)),
            max_tokens=int(options.get("max_tokens", 2048)),
            tools=options.get("tools"),
        ):
            if chunk.delta:
                yield chunk.delta

    pub = publish(
        name=args.name,
        description=f"served by {brain.label}",
        chat_handler=chat_handler,
        hub_url=args.hub,
        public=True,
        api_key=os.environ.get("ZHUB_API_KEY") or None,
    )
    while not pub.api_key:
        await asyncio.sleep(0.05)

    # Try to discover the hub's public URL so we can print a copy-paste-
    # ready endpoint. Falls back to the local hub URL when the hub has no
    # tunnel up. Best-effort — if /hub/identity fails or httpx isn't
    # installed, we just print local. Either way the publisher is ready.
    public_endpoint = None
    try:
        import httpx
        http_hub = args.hub.replace("ws://", "http://").replace("wss://", "https://")
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(http_hub.rstrip("/") + "/hub/identity")
            if r.status_code == 200:
                pub_url = r.json().get("public_url") or http_hub
                public_endpoint = pub_url.rstrip("/") + f"/{pub.name}/v1"
    except Exception:
        public_endpoint = args.hub.replace("ws://", "http://") \
            .replace("wss://", "https://").rstrip("/") + f"/{pub.name}/v1"

    # Machine-readable lines (parseable by scripts / supervisord / tests)
    # come first; the pretty operator box follows underneath.
    print(f"name={pub.name}", flush=True)
    print(f"key={pub.api_key}", flush=True)
    print(f"url={public_endpoint}", flush=True)
    print("ready", flush=True)

    bar = "=" * 64
    print()
    print(bar, flush=True)
    print(f"  zhub publisher live  ·  brain: {brain.label}")
    print(bar)
    print(f"  URL:  {public_endpoint}")
    print(f"  KEY:  {pub.api_key}")
    print(bar)
    print(f"  curl example:")
    print(f"    curl -X POST {public_endpoint}/chat/completions \\")
    print(f"      -H 'Authorization: Bearer {pub.api_key}' \\")
    print(f"      -H 'Content-Type: application/json' \\")
    print(f"      -d '{{\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}]}}'")
    print(bar)
    print(f"  paste URL+KEY into Pocket / Cursor / Claude Desktop.")
    print(f"  ctrl+c to stop.")
    print()
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
