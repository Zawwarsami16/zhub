"""Unit coverage for ZhubMCPServer._get_prompt edge cases.

Tests the required-arg check path with a mocked _fetch_manifest so there is
no need for a running hub or subprocess.  The specific regression tested here:
_get_prompt previously accessed arg["name"] with bracket notation after safely
calling arg.get("required").  A publisher that sent a prompt argument object
missing the "name" field (malformed manifest) caused an uncaught KeyError
instead of returning a clean error tuple.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from zhub.mcp_server import ZhubMCPServer


def _make_server() -> ZhubMCPServer:
    s = ZhubMCPServer(hub="http://fake", ai="fake", key="zk_fake")
    s._http = object()  # prevent start() needing a real httpx client
    return s


def _manifest_with_prompts(prompts: list) -> dict:
    return {"name": "fake", "prompts": prompts}


@pytest.mark.asyncio
async def test_get_prompt_nameless_required_arg_does_not_crash():
    """A required arg with no 'name' field must not raise KeyError."""
    server = _make_server()
    manifest = _manifest_with_prompts([
        {
            "name": "greet",
            "arguments": [{"required": True}],  # <-- no "name" key
            "messages": [{"role": "user", "content": "Hello!"}],
        },
    ])
    with patch.object(server, "_fetch_manifest", new=AsyncMock(return_value=manifest)):
        result, err = await server._get_prompt("greet", {})
    # nameless required arg is unenforced (can't be checked) — returns the rendered prompt
    assert err is None
    assert result is not None
    assert result["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_get_prompt_named_required_arg_blocks_when_absent():
    """A required arg with a name still blocks if missing from the call."""
    server = _make_server()
    manifest = _manifest_with_prompts([
        {
            "name": "summarize",
            "arguments": [{"name": "text", "required": True}],
            "messages": [{"role": "user", "content": "Summarize: {text}"}],
        },
    ])
    with patch.object(server, "_fetch_manifest", new=AsyncMock(return_value=manifest)):
        result, err = await server._get_prompt("summarize", {})
    assert result is None
    assert err is not None
    assert "text" in err


@pytest.mark.asyncio
async def test_get_prompt_named_required_arg_passes_when_provided():
    """A required arg with a name passes when the arg is provided."""
    server = _make_server()
    manifest = _manifest_with_prompts([
        {
            "name": "summarize",
            "arguments": [{"name": "text", "required": True}],
            "messages": [{"role": "user", "content": "Summarize: {text}"}],
        },
    ])
    with patch.object(server, "_fetch_manifest", new=AsyncMock(return_value=manifest)):
        result, err = await server._get_prompt("summarize", {"text": "hello world"})
    assert err is None
    assert result is not None
    content = result["messages"][0]["content"]
    text = content if isinstance(content, str) else content.get("text", "")
    assert "hello world" in text
