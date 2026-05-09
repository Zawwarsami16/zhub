"""Federation demo — two hubs, one AI, three different ways to reach it.

Spins up two zhub hubs in this same Python process (different ports). Hub A
peers Hub B. An AI is published on Hub B only. Then we drive three flows
that should all succeed:

  1. HTTP chat through Hub A      — phase 1.1 (HTTP cross-hub proxy)
  2. WebSocket connect via Hub A   — phase 1.1b (WS tunnel)
  3. /registry/global on Hub A     — phase 1.0b (federated discovery)

Run:
    python examples/federation_demo.py

No external services needed.
"""

import asyncio
import os
import socket
import threading
import time

import httpx
import uvicorn

from zhub import publish, connect
from zhub.server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _start_hub(port: int, peers: str = "", hub_id: str = "") -> None:
    if peers:
        os.environ["ZHUB_PEERS"] = peers
    else:
        os.environ.pop("ZHUB_PEERS", None)
    if hub_id:
        os.environ["ZHUB_HUB_ID"] = hub_id
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning",
    )
    asyncio.run(uvicorn.Server(config).serve())


def _wait(port: int) -> None:
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.1)


async def main() -> None:
    port_a, port_b = _free_port(), _free_port()
    print(f"hub A on 127.0.0.1:{port_a}  (peers → B)")
    print(f"hub B on 127.0.0.1:{port_b}  (no peers)")
    print()

    threading.Thread(target=_start_hub, args=(port_b, "", "hub-b"), daemon=True).start()
    _wait(port_b)
    threading.Thread(
        target=_start_hub,
        args=(port_a, f"http://127.0.0.1:{port_b}", "hub-a"),
        daemon=True,
    ).start()
    _wait(port_a)

    pub = publish(
        name="echo-fed",
        description="lives only on B, reachable from A",
        chat_handler=lambda m, o: f"echo from B: {m[-1]['content']}",
        hub_url=f"ws://127.0.0.1:{port_b}",
        public=True,
    )
    while not pub.api_key:
        await asyncio.sleep(0.05)
    print(f"published 'echo-fed' on B  key={pub.api_key[:14]}…")
    print()

    # 1. /registry/global on A should show B's listing
    async with httpx.AsyncClient(timeout=5.0) as http:
        r = await http.get(f"http://127.0.0.1:{port_a}/registry/global")
        names = [(e["name"], e.get("origin")) for e in r.json()]
        print(f"[1.0b] /registry/global on A  →  {names}")

    # 2. HTTP chat through A — A proxies to B (phase 1.1)
    async with httpx.AsyncClient(timeout=5.0) as http:
        r = await http.post(
            f"http://127.0.0.1:{port_a}/{pub.name}/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "from-http"}]},
            headers={"Authorization": f"Bearer {pub.api_key}"},
        )
        body = r.json()
        print(f"[1.1 ] HTTP chat via A     →  {body['choices'][0]['message']['content']!r}")
        print(f"        X-Zhub-Origin       =  {r.headers.get('x-zhub-origin')}")

    # 3. WebSocket connect via A — A tunnels to B (phase 1.1b)
    conn = connect(
        ai_name=pub.name,
        api_key=pub.api_key,
        hub_url=f"ws://127.0.0.1:{port_a}",
        capabilities={},
    )
    for _ in range(50):
        if conn._ws is not None:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    resp = await conn.chat(messages=[{"role": "user", "content": "from-ws"}], timeout=5.0)
    print(f"[1.1b] WS chat via A        →  {resp.get('text')!r}")

    print()
    print("done — same publisher reached three different ways across two hubs.")


if __name__ == "__main__":
    asyncio.run(main())
