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
                        help="server-side system prompt prepended to every "
                             "chat (lets the brain know it lives behind zhub, "
                             "what its name/persona is, etc). Clients don't "
                             "have to send any context.")
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

    async def chat_handler(messages, options):
        # Fold any system messages into a single system prompt; keep the
        # rest of the conversation intact. The publisher-level --system
        # always wins position-wise (prepended), so server-side identity
        # survives even if a client also sends a system message.
        system_parts = []
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
    print(f"name={pub.name}", flush=True)
    print(f"key={pub.api_key}", flush=True)
    print("ready", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
