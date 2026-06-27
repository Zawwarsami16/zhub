"""Guard against non-dict items in manifest resources/prompts/messages lists.

A publisher manifest is user-supplied JSON. Any of the list fields
(resources, prompts, messages) could contain a string/number/null instead
of the expected dict, triggering AttributeError inside _list_resources /
_read_resource / _list_prompts / _get_prompt. These tests confirm the guard
(isinstance check + continue) is in place across all four paths.

All tests mock _fetch_manifest directly — no subprocess or live hub needed.
"""

from unittest.mock import AsyncMock, patch

import pytest

from zhub.mcp_server import ZhubMCPServer


@pytest.fixture
def server():
    return ZhubMCPServer(hub="http://hub.local", ai="bot", key="zk_test")


# ---------------------------------------------------------------------------
# _list_resources
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_resources_skips_non_dict_entries(server):
    """A string in the resources list must be silently skipped, not crash."""
    manifest = {
        "resources": [
            "not-a-dict",             # should be skipped
            42,                        # should be skipped
            {"uri": "zhub://bot/ok", "name": "ok"},  # valid entry
        ]
    }
    with patch.object(server, "_fetch_manifest", new=AsyncMock(return_value=manifest)):
        result = await server._list_resources()
    assert len(result) == 1
    assert result[0]["uri"] == "zhub://bot/ok"


# ---------------------------------------------------------------------------
# _read_resource
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_resource_skips_non_dict_entries(server):
    """Non-dict entries in resources must not crash _read_resource."""
    manifest = {
        "resources": [
            "garbage",
            {"uri": "zhub://bot/data", "content": "hello"},
        ]
    }
    with patch.object(server, "_fetch_manifest", new=AsyncMock(return_value=manifest)):
        result = await server._read_resource("zhub://bot/data")
    assert result is not None
    assert result["contents"][0]["text"] == "hello"


# ---------------------------------------------------------------------------
# _list_prompts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_prompts_skips_non_dict_entries(server):
    """A string in the prompts list must be silently skipped, not crash."""
    manifest = {
        "prompts": [
            "bad-entry",
            None,
            {"name": "good-prompt", "description": "a real prompt"},
        ]
    }
    with patch.object(server, "_fetch_manifest", new=AsyncMock(return_value=manifest)):
        result = await server._list_prompts()
    assert len(result) == 1
    assert result[0]["name"] == "good-prompt"


# ---------------------------------------------------------------------------
# _get_prompt — non-dict in prompts list + non-dict in messages list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_prompt_skips_non_dict_prompt_entries(server):
    """A non-dict entry in the prompts list must not crash _get_prompt."""
    manifest = {
        "prompts": [
            "trash",
            {"name": "real", "messages": [{"role": "user", "content": "hi"}]},
        ]
    }
    with patch.object(server, "_fetch_manifest", new=AsyncMock(return_value=manifest)):
        result, err = await server._get_prompt("real", {})
    assert err is None
    assert result["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_get_prompt_skips_non_dict_message_entries(server):
    """Non-dict entries in a prompt's messages list must be skipped, not crash."""
    manifest = {
        "prompts": [
            {
                "name": "mixed",
                "messages": [
                    "just a string",            # should be skipped
                    {"role": "user", "content": "real message"},
                ],
            },
        ]
    }
    with patch.object(server, "_fetch_manifest", new=AsyncMock(return_value=manifest)):
        result, err = await server._get_prompt("mixed", {})
    assert err is None
    # Only the dict message should survive
    assert len(result["messages"]) == 1
    assert result["messages"][0]["content"]["text"] == "real message"
