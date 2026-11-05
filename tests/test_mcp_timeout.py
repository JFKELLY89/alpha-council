"""
Alpha Council - F11 MCP deadline tests.

The stdio transport has no deadline of its own; a wedged server must
become a raised error (which the control plane turns into the WARN +
REST-fallback path) rather than a coroutine that never returns.

Run:
    uv run pytest tests/test_mcp_timeout.py -v
"""

from __future__ import annotations

import asyncio

import pytest

import alpha_council.alpaca.mcp_client as mcp_mod
from alpha_council.alpaca.mcp_client import AlpacaMCPClient, MCPStats


class _HangingSession:
    async def call_tool(self, name, arguments):
        await asyncio.sleep(3600)


def _client_with(session) -> AlpacaMCPClient:
    client = AlpacaMCPClient.__new__(AlpacaMCPClient)
    client._session = session
    client.available = True
    client.resolved = {"account": "get_account_info"}
    client.stats = MCPStats()
    return client


@pytest.mark.asyncio
async def test_a_hung_tool_call_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(mcp_mod, "MCP_CALL_TIMEOUT_S", 0.05)
    client = _client_with(_HangingSession())
    with pytest.raises(RuntimeError, match="timed out"):
        await client.call("account")
    assert client.stats.mcp_calls == 0  # never counted as served


@pytest.mark.asyncio
async def test_a_fast_call_is_unaffected():
    class _Session:
        async def call_tool(self, name, arguments):
            class _Result:
                isError = False
                structuredContent = {"status": "ACTIVE"}
            return _Result()

    client = _client_with(_Session())
    result = await client.call("account")
    assert result == {"status": "ACTIVE"}
    assert client.stats.mcp_calls == 1
