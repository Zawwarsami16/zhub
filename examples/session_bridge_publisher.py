"""
session_bridge_publisher — wrap an interactive AI session (Claude Code,
Cursor's chat, an OpenAI Agents loop, anything with a human-in-loop
or tool-using runtime) as a zhub publisher.

The trick: every chat that lands gets serialized to a file. The session
on the other side watches that directory, reads the request, formulates
a response, writes it to an outbox file. The publisher polls the outbox,
returns the response to whoever called.

This lets you reach an *interactive* AI from Pocket / Cursor / curl /
Claude Desktop — same OpenAI Chat Completions surface as a normal
zhub publisher, but the brain is whatever's reading the inbox files.

Use cases:
  * "Talk to the Claude Code session on my laptop from Pocket on my phone"
  * Bridge a custom in-process LLM that doesn't have an HTTP API
  * Demo / debug — see exactly what messages your hub is receiving

Tradeoffs vs a normal brain-adapter publisher:
  * Latency: 0.4s polling interval + however long the human/agent takes
  * Throughput: one request at a time per session (the human/agent is
    the bottleneck)
  * Cost: bears whatever the underlying agent costs to think (e.g.,
    Claude Opus per-token > Sonnet > free local model)

For pure chat with Groq/Anthropic/Ollama, use multi_brain_publisher.py
instead — direct + cheap + fast. Use this script when you specifically
want the *agent* on the other side.

Run:
    1. python -m zhub.server --port 8080
    2. python examples/session_bridge_publisher.py
       → prints URL + KEY + the inbox/outbox paths
    3. In another terminal, run the watcher of your choice:
       a) Manual:  ls /tmp/zhub-inbox/  → cat the JSON, write reply to
                   /tmp/zhub-outbox/<same-id>.json as {"text": "..."}
       b) Claude Code session: open Claude Code with this repo, say
                   "watch /tmp/zhub-inbox for new JSON files, and for
                   each, read the messages, formulate a reply, and
                   write {\"text\": ...} to the matching path under
                   /tmp/zhub-outbox/"
       c) Custom script: see _example_handler() at the bottom for
                   a programmatic stub.
    4. Hit it from Pocket / curl / Claude Desktop. Pasted URL+KEY
       round-trips through the file bridge to your session.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

from zhub import publish

DEFAULT_INBOX = "/tmp/zhub-inbox"
DEFAULT_OUTBOX = "/tmp/zhub-outbox"
DEFAULT_TIMEOUT = 600  # 10 minutes — gives a human / slow agent time to reply


def make_chat_handler(inbox_dir: str, outbox_dir: str, timeout_s: int):
    """Build the async chat handler that owns the file-bridge contract.
    Pulled out of main() so test scripts can wire their own dirs."""
    os.makedirs(inbox_dir, exist_ok=True)
    os.makedirs(outbox_dir, exist_ok=True)

    async def chat_handler(messages, options):
        req_id = uuid.uuid4().hex[:12]
        inbox_path = os.path.join(inbox_dir, f"{req_id}.json")
        outbox_path = os.path.join(outbox_dir, f"{req_id}.json")

        last_user = next(
            (m.get("content", "") for m in reversed(messages)
             if m.get("role") == "user"),
            "",
        )
        payload = {
            "request_id": req_id,
            "ts": time.time(),
            "last_user_message": last_user,
            "messages": messages,
            "options": dict(options),
        }
        # atomic publish: write to .tmp then rename
        tmp = inbox_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.rename(tmp, inbox_path)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if os.path.exists(outbox_path):
                try:
                    with open(outbox_path) as f:
                        resp = json.load(f)
                except (json.JSONDecodeError, OSError):
                    await asyncio.sleep(0.2)
                    continue
                # cleanup both sides
                for p in (outbox_path, inbox_path):
                    try:
                        os.remove(p)
                    except FileNotFoundError:
                        pass
                return resp.get("text", "(empty reply)")
            await asyncio.sleep(0.4)

        # Timeout — clean up the inbox so we don't leak files
        try:
            os.remove(inbox_path)
        except FileNotFoundError:
            pass
        return ("(no response in {}s — the other side may be afk; "
                "send the message again to retry.)").format(timeout_s)

    return chat_handler


async def main():
    parser = argparse.ArgumentParser(
        description="Wrap an interactive session as a zhub publisher via "
                    "a file bridge."
    )
    parser.add_argument("--name", default=os.environ.get("ZHUB_NAME", "session"),
                        help="publisher name (becomes the AI's URL slug)")
    parser.add_argument("--hub", default=os.environ.get("ZHUB_HUB",
                                                          "ws://127.0.0.1:8080"))
    parser.add_argument("--inbox", default=DEFAULT_INBOX,
                        help="dir for incoming request files")
    parser.add_argument("--outbox", default=DEFAULT_OUTBOX,
                        help="dir for outgoing reply files")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="seconds to wait for a reply file (default 600)")
    args = parser.parse_args()

    handler = make_chat_handler(args.inbox, args.outbox, args.timeout)

    pub = publish(
        name=args.name,
        description=f"interactive session bridge ({args.inbox} → {args.outbox})",
        chat_handler=handler,
        hub_url=args.hub,
        public=True,
        operator=os.environ.get("USER", ""),
        api_key=os.environ.get("ZHUB_API_KEY") or None,
    )
    while not pub.api_key:
        await asyncio.sleep(0.05)

    # Discover hub's public URL (cloudflared etc) so the printed endpoint
    # is copy-paste-ready, not just name+key.
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

    print()
    print("=" * 64)
    print(f"  session bridge live")
    print(f"  name:        {pub.name}")
    print(f"  inbox:       {args.inbox}/")
    print(f"  outbox:      {args.outbox}/")
    print(f"  timeout:     {args.timeout}s per reply")
    print(f"  URL:         {public_endpoint}")
    print(f"  KEY:         {pub.api_key}")
    print("=" * 64)
    print()
    print("Watcher cheatsheet (in another terminal):")
    print()
    print(f"  # tail incoming requests")
    print(f"  watch -n 1 'ls -la {args.inbox}/'")
    print()
    print(f"  # reply to a request manually")
    print(f"  echo '{{\"text\": \"hi from the session\"}}' \\")
    print(f"    > {args.outbox}/<request_id>.json")
    print()
    print("Or point your agent (Claude Code / Cursor / a script) at the")
    print(f"  inbox dir to watch + reply automatically.")
    print()
    print("Ctrl+C to stop.")

    await asyncio.Event().wait()


# ----------------------------------------------------------------------
# Example: a tiny auto-replying watcher you can run as a sidecar to
# prove the loop works without any agent in the loop.
# ----------------------------------------------------------------------
async def _example_auto_watcher(inbox_dir: str, outbox_dir: str,
                                  reply_text: str = "auto-reply (echo-watcher)"):
    """Run alongside the publisher to make every request auto-reply.
    Not the real use case — just a way to smoke-test the bridge.

    Usage:
        # in one terminal
        python examples/session_bridge_publisher.py
        # in another, drive the auto-replier:
        python -c "import asyncio; from examples.session_bridge_publisher \\
                   import _example_auto_watcher; \\
                   asyncio.run(_example_auto_watcher('/tmp/zhub-inbox', \\
                                                     '/tmp/zhub-outbox'))"
    """
    import glob
    seen: set[str] = set()
    while True:
        for path in glob.glob(os.path.join(inbox_dir, "*.json")):
            if path in seen:
                continue
            seen.add(path)
            try:
                with open(path) as f:
                    req = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            req_id = req["request_id"]
            user_msg = req.get("last_user_message", "")
            out_path = os.path.join(outbox_dir, f"{req_id}.json")
            with open(out_path, "w") as f:
                json.dump({"text": f"{reply_text}: you said {user_msg!r}"}, f)
        await asyncio.sleep(0.4)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
