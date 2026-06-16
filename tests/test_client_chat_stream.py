"""Unit coverage for ZhubConnection.chat_stream chunk handling.

Drives chat_stream directly against a fake websocket so we can feed exact
chunk envelopes — in particular a *combined* final chunk that carries both
`delta` text and `done=True` in one envelope. The HTTP SSE consumer emits
text before honoring the finish flag; the Python client must do the same or
it silently drops a publisher's last words.
"""

import json

import pytest

from zhub.client import ZhubConnection
from zhub.manifest import Manifest


class _FakeWS:
    """Captures the outgoing chat-request and replays a scripted chunk
    sequence onto the connection's stream queue for that request_id."""

    def __init__(self, conn: ZhubConnection, script: list[dict]) -> None:
        self._conn = conn
        self._script = script

    async def send(self, text: str) -> None:
        env = json.loads(text)
        rid = env["request_id"]
        queue = self._conn._streams[rid]
        for chunk in self._script:
            queue.put_nowait(chunk)


def _make_conn(script: list[dict]) -> ZhubConnection:
    conn = ZhubConnection(
        ai_name="ai",
        api_key="zk_test",
        hub_url="ws://localhost",
        client_manifest=Manifest(name="ai-client"),
        capabilities={},
    )
    conn._ws = _FakeWS(conn, script)
    return conn


async def _drain(conn: ZhubConnection) -> list[str]:
    out = []
    async for chunk in conn.chat_stream(
        messages=[{"role": "user", "content": "hi"}],
        timeout_per_chunk=1.0,
    ):
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_combined_final_chunk_keeps_its_text():
    # Final chunk flags done=True AND carries content in the same envelope.
    conn = _make_conn([
        {"delta": "hello ", "done": False},
        {"delta": "world", "done": True},
    ])
    chunks = await _drain(conn)
    assert "".join(chunks) == "hello world"
    assert "world" in chunks


@pytest.mark.asyncio
async def test_separate_empty_done_chunk_terminates_cleanly():
    # Framework-default shape: content chunks, then a separate empty done.
    conn = _make_conn([
        {"delta": "a", "done": False},
        {"delta": "b", "done": False},
        {"delta": "", "done": True},
    ])
    chunks = await _drain(conn)
    assert "".join(chunks) == "ab"
    # The empty terminator must not surface as a yielded chunk.
    assert "" not in chunks


@pytest.mark.asyncio
async def test_explicit_none_terminates_without_yielding():
    conn = _make_conn([
        {"delta": "x", "done": False},
        None,
    ])
    chunks = await _drain(conn)
    assert chunks == ["x"]
