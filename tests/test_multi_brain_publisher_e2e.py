"""End-to-end test for the multi_brain_publisher example.

Spins up a hub in this process, then spawns the example as a subprocess
with a fake brain registered via env-var injection. Drives a chat through
the hub's HTTP endpoint and checks that the canned reply comes back.
"""

import asyncio
import os
import socket
import sys
import textwrap
import threading
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    import httpx  # noqa
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

if DEPS_AVAILABLE:
    from zhub.server import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def hub_port():
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
    app = create_app()

    def run():
        config = uvicorn.Config(app, host="127.0.0.1", port=port,
                                log_level="warning")
        asyncio.run(uvicorn.Server(config).serve())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.1)
    yield port


@pytest.mark.asyncio
async def test_publisher_with_explicit_fake_brain(hub_port, tmp_path):
    """Spawn the example with a tiny fake-brain shim that streams a fixed
    text. POST a chat to the hub; expect the canned text back."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    shim = tmp_path / "fake_brain_shim.py"
    shim.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {repo_root!r})
        from zhub.brains.base import BrainAdapter, ChatChunk
        import zhub.brains as _brains

        class FakeBrain(BrainAdapter):
            name = "fake"
            label = "fake test brain"

            @classmethod
            def try_init(cls):
                return cls()

            async def stream(self, messages, *, system=None, temperature=0.7,
                             max_tokens=2048, tools=None):
                yield ChatChunk(delta="hello ")
                yield ChatChunk(delta="from fake")
                yield ChatChunk(delta="", done=True, finish_reason="stop")

        _brains.REGISTRY = [FakeBrain]

        import runpy
        sys.argv = ["multi_brain_publisher.py", "--brain", "auto",
                    "--name", "fake-pub"]
        runpy.run_path({os.path.join(repo_root, "examples", "multi_brain_publisher.py")!r},
                       run_name="__main__")
    """))

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["ZHUB_HUB"] = f"ws://127.0.0.1:{hub_port}"

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(shim),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    api_key = None
    deadline = time.time() + 25.0
    try:
        while time.time() < deadline:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=4.0)
            if not line:
                break
            text = line.decode().strip()
            if text.startswith("key="):
                api_key = text.removeprefix("key=").strip()
                break
        assert api_key, "publisher never printed key="

        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{hub_port}/fake-pub/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        assert content == "hello from fake", f"got {content!r}"

    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
