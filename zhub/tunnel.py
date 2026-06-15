"""
Cloudflare Tunnel auto-config helper for the hub.

If `cloudflared` is on the PATH and `--public-tunnel` is passed to the hub,
the hub spawns an ephemeral cloudflared tunnel on startup and prints the
public URL to stdout. No Cloudflare account required for ephemeral tunnels.

For production deployment with a stable URL, use a named cloudflared tunnel
(see the cloudflared docs) and front the hub behind it the normal way —
this helper is for laptop / phone-side dev, not production.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from typing import Optional

log = logging.getLogger("zhub.tunnel")

URL_RE = re.compile(rb"https://[A-Za-z0-9.-]+\.trycloudflare\.com")


class CloudflareTunnel:
    """Wraps a cloudflared subprocess. Starts on entry, terminates on close()."""

    def __init__(self, local_port: int, binary: Optional[str] = None) -> None:
        self.local_port = local_port
        self.binary = binary or shutil.which("cloudflared")
        self.process: Optional[asyncio.subprocess.Process] = None
        self.public_url: Optional[str] = None

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which("cloudflared") is not None

    async def start(self, timeout: float = 30.0) -> str:
        """Start the tunnel. Returns the public URL when it's ready."""
        if not self.binary:
            raise RuntimeError(
                "cloudflared not found on PATH. install from "
                "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
            )
        cmd = [self.binary, "tunnel", "--url", f"http://localhost:{self.local_port}", "--no-autoupdate"]
        log.info("starting cloudflared: %s", " ".join(cmd))
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # Read stdout/stderr until we see the URL or timeout
        url_future: asyncio.Future = asyncio.get_running_loop().create_future()

        async def consume():
            assert self.process and self.process.stdout
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                m = URL_RE.search(line)
                if m and not url_future.done():
                    url = m.group(0).decode()
                    url_future.set_result(url)
                # Keep draining so the buffer doesn't fill up

        consumer = asyncio.create_task(consume())
        try:
            self.public_url = await asyncio.wait_for(url_future, timeout=timeout)
            log.info("tunnel up at %s", self.public_url)
            return self.public_url
        except asyncio.TimeoutError:
            consumer.cancel()
            await self.close()
            raise RuntimeError(
                f"cloudflared did not produce a public URL within {timeout}s — "
                f"check `cloudflared tunnel --url http://localhost:{self.local_port}` manually"
            )

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
