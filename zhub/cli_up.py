"""`python -m zhub up` — one-shot start.

Goal: a brand-new user (or a brand-new AI installing zhub) runs ONE
command and gets back a URL + key they can paste into anything that
speaks OpenAI Chat Completions. Hub, optional Cloudflare Tunnel, and a
brain publisher all run inside a single Python process; Ctrl-C tears
the lot down cleanly.

Usage:
    python -m zhub up [--port 8080] [--no-tunnel] [--name me]
                      [--brain auto|ollama|groq|openai|cerebras]
                      [--db zhub.db]

Defaults are sane for a fresh install:
  * port 8080 (or first free if taken)
  * cloudflared tunnel ON (if cloudflared is installed)
  * brain auto (uses OLLAMA_HOST or first env-set brain key)
  * persistence at ./zhub.db so the zk_ key survives restarts
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import sys
from typing import Optional

from . import publish
from .brains import detect, REGISTRY


def _free_port_from(start: int) -> int:
    """Return `start` if free, otherwise the OS-allocated free port."""
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", start))
            return start
    except OSError:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _resolve_brain(name: str):
    if name == "auto":
        b = detect()
        if b is None:
            return None
        return b
    cls = next((c for c in REGISTRY if c.name == name), None)
    if cls is None:
        return None
    return cls.try_init()


async def _start_tunnel(port: int) -> tuple[Optional[asyncio.subprocess.Process], Optional[str]]:
    """Spawn cloudflared --url http://localhost:<port>; wait for the
    public URL to appear in stderr. Returns (proc, url) or (None, None)
    if cloudflared isn't installed."""
    import shutil
    if not shutil.which("cloudflared"):
        return None, None
    proc = await asyncio.create_subprocess_exec(
        "cloudflared", "tunnel", "--url", f"http://localhost:{port}",
        "--no-autoupdate",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    import re
    pat = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    deadline = asyncio.get_event_loop().time() + 30.0
    url: Optional[str] = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        if not line:
            break
        text = line.decode("utf-8", errors="replace")
        m = pat.search(text)
        if m:
            url = m.group(0)
            break
    return proc, url


async def _run(args: argparse.Namespace) -> int:
    import uvicorn
    from .server import create_app

    port = _free_port_from(args.port)
    if port != args.port:
        print(f"[zhub up] port {args.port} taken, using {port}", file=sys.stderr)

    db_path: Optional[str] = args.db if args.db else None
    app = create_app(db_path=db_path)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # wait until accepting
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            await asyncio.sleep(0.1)

    public_url = f"http://127.0.0.1:{port}"
    tunnel_proc: Optional[asyncio.subprocess.Process] = None
    if not args.no_tunnel:
        tunnel_proc, t_url = await _start_tunnel(port)
        if t_url is None:
            print("[zhub up] cloudflared unavailable or slow to start; "
                  "falling back to local URL", file=sys.stderr)
        else:
            public_url = t_url

    brain = _resolve_brain(args.brain)
    pub = None
    if brain is None:
        print(f"[zhub up] no brain available (asked for {args.brain!r}); "
              "the hub is up but nothing is published. set OLLAMA_HOST or "
              "GROQ_API_KEY / OPENAI_API_KEY / CEREBRAS_API_KEY and re-run.",
              file=sys.stderr, flush=True)
    else:
        async def chat_handler(messages, options):
            system_parts = [m.get("content", "") for m in messages
                            if m.get("role") == "system"]
            non_system = [m for m in messages if m.get("role") != "system"]
            system = "\n\n".join(p for p in system_parts if p) or None
            async for chunk in brain.stream(
                non_system, system=system,
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
            hub_url=f"ws://127.0.0.1:{port}",
            public=True,
            api_key=os.environ.get("ZHUB_API_KEY") or None,
        )
        # wait for registration
        deadline = asyncio.get_event_loop().time() + 6.0
        while not pub.api_key and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)

    print()
    print("=" * 64, flush=True)
    if pub and pub.api_key:
        print(f"  brain:    {brain.label}", flush=True)
        print(f"  URL:      {public_url}/{pub.name}/v1", flush=True)
        print(f"KEY: {pub.api_key}", flush=True)
        print("  paste both into Pocket / openai-py / curl / Claude Desktop", flush=True)
    else:
        print(f"  hub up at {public_url}", flush=True)
        print("  no publisher yet — start one with:", flush=True)
        print(f"    python examples/multi_brain_publisher.py --hub ws://127.0.0.1:{port}", flush=True)
    print("=" * 64, flush=True)
    print(flush=True)
    print(f"URL: {public_url}/{pub.name}/v1" if pub and pub.api_key else f"URL: {public_url}",
          flush=True)
    if pub and pub.api_key:
        # second redundant marker line some scripts grep for
        print(f"KEY: {pub.api_key}", flush=True)

    # park forever, until Ctrl-C
    stop = asyncio.Event()

    def _stop_handler(*_a):
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop_handler)
        except NotImplementedError:
            pass

    try:
        await stop.wait()
    finally:
        server.should_exit = True
        if tunnel_proc and tunnel_proc.returncode is None:
            tunnel_proc.terminate()
            try:
                await asyncio.wait_for(tunnel_proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                tunnel_proc.kill()
        try:
            await asyncio.wait_for(server_task, timeout=3.0)
        except asyncio.TimeoutError:
            pass

    return 0


def run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m zhub up",
        description="One-shot: hub + tunnel + brain publisher.",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-tunnel", action="store_true",
                        help="skip cloudflared, use local URL only")
    parser.add_argument("--name", default=os.environ.get("ZHUB_NAME", "me"))
    parser.add_argument("--brain", default="auto",
                        help="auto | ollama | groq | openai | cerebras")
    parser.add_argument("--db", default="zhub.db",
                        help="SQLite path for persistence (empty to disable)")
    args = parser.parse_args(argv)
    try:
        rc = asyncio.run(_run(args))
        sys.exit(rc)
    except KeyboardInterrupt:
        sys.exit(0)
