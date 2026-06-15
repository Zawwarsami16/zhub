"""CloudflareTunnel — ephemeral `--public-tunnel` helper.

These pin two contracts that broke (or could silently break) in messages
the user actually reads when a tunnel fails to come up:

* missing binary -> a RuntimeError pointing at the install docs
* startup timeout -> a RuntimeError that names the *actual* local port so the
  user can copy-paste the manual command (the port was once emitted as the
  literal text ``{self.local_port}`` because the line wasn't an f-string).
"""

import shutil
import stat
import sys

import pytest

from zhub.tunnel import CloudflareTunnel


def test_is_available_reflects_path(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/cloudflared")
    assert CloudflareTunnel.is_available() is True
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert CloudflareTunnel.is_available() is False


async def test_start_without_binary_points_at_install_docs(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    t = CloudflareTunnel(local_port=8787)
    assert t.binary is None
    with pytest.raises(RuntimeError) as ei:
        await t.start()
    assert "cloudflared not found" in str(ei.value)
    assert "downloads" in str(ei.value)


@pytest.fixture
def fake_cloudflared(tmp_path):
    """A stand-in binary that never prints a trycloudflare URL, so start() times out."""
    script = tmp_path / "cloudflared"
    script.write_text("#!/bin/sh\nwhile true; do echo 'still booting'; sleep 0.05; done\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
async def test_start_timeout_reports_real_port(fake_cloudflared):
    port = 54321
    t = CloudflareTunnel(local_port=port, binary=fake_cloudflared)
    with pytest.raises(RuntimeError) as ei:
        await t.start(timeout=0.3)
    msg = str(ei.value)
    # The actual port must be interpolated, not the literal placeholder.
    assert str(port) in msg
    assert "{self.local_port}" not in msg
    # And the process must be reaped, not left running.
    assert t.process is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
async def test_close_is_idempotent_and_safe_when_never_started(fake_cloudflared):
    t = CloudflareTunnel(local_port=1234, binary=fake_cloudflared)
    # never started -> no process -> close is a no-op
    await t.close()
    assert t.process is None
