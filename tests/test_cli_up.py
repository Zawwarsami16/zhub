"""Test the `python -m zhub up` quickstart command.

The `up` command's job: in one shot, bring up a hub + (optionally) a
brain publisher, and print URL + key + ready-to-paste BYOK config so
any user (or AI installing zhub) can be reachable in one terminal.

For tests: invoke as a subprocess with --no-tunnel and a fake brain
registered via env-var injected into a shim, on a free port. Verify
stdout contains URL, key, and a BYOK summary.
"""

import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

try:
    import fastapi  # noqa
    import uvicorn  # noqa
    import httpx  # noqa
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_up_command_with_fake_brain_prints_url_and_key(tmp_path):
    """`python -m zhub up --port <free> --no-tunnel --name testpub --brain fake`
    should boot a hub, publish, and emit URL/key on stdout."""
    if not DEPS_AVAILABLE:
        pytest.skip("fastapi/uvicorn/httpx not installed")
    port = _free_port()
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
                yield ChatChunk(delta="ok", done=True, finish_reason="stop")

        _brains.REGISTRY = [FakeBrain]

        import runpy, sys as _sys
        _sys.argv = ["zhub", "up", "--port", "{port}",
                     "--no-tunnel", "--name", "testpub",
                     "--brain", "fake",
                     "--db", {str(tmp_path / "up.db")!r}]
        runpy.run_module("zhub", run_name="__main__")
    """))

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(shim),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    stdout_buf: list[str] = []
    api_key = None
    deadline = time.time() + 12.0
    try:
        while time.time() < deadline:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=4.0)
            if not line:
                break
            text = line.decode().rstrip()
            stdout_buf.append(text)
            if text.startswith("KEY:"):
                api_key = text.removeprefix("KEY:").strip()
            if api_key and any(s.startswith("URL:") for s in stdout_buf):
                break

        full = "\n".join(stdout_buf)
        assert any(s.startswith("URL:") for s in stdout_buf), \
            f"no URL line printed; stdout={full!r}"
        assert api_key and api_key.startswith("zk_"), \
            f"no key extracted; stdout={full!r}"

        # And the hub really should be reachable on that port now
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"http://127.0.0.1:{port}/healthz")
        assert r.status_code == 200
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
