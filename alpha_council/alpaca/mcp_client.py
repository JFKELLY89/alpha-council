"""
Alpha Council v2.5 - Alpaca MCP control plane.

The hackathon requires projects to use Alpaca's Trading API together with
its MCP server or CLI. This routes the control plane — account state,
clock, positions, and optionally order submission — through MCP tools,
while the high-throughput data plane stays on REST because scanning 250
symbols through stdio would be slow and pointless.

Three design decisions worth knowing:

  TOOL NAMES ARE DISCOVERED, NOT ASSUMED. The server's exact tool names
  are not something this code can know in advance, so it lists them at
  startup, matches against alias sets, and persists the full inventory to
  system_events. A renamed tool degrades to a logged miss, not a crash.

  REST IS THE FALLBACK, NEVER THE DEFAULT. Every method tries MCP first
  and records which path served the call, so the demo can state
  truthfully how many operations went through MCP.

  A DEGRADED CONTROL PLANE DOES NOT STOP TRADING. If the MCP server will
  not start, the system logs it loudly and continues on REST. Losing a
  transport is not a reason to stop managing live positions.

Place at: alpha_council/alpaca/mcp_client.py
"""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from alpha_council.alpaca.rest_client import AlpacaRestClient
from alpha_council.db.engine import Database
from alpha_council.settings import Settings

# Candidate names for each logical operation. The first match wins.
TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "account": ("get_account_info", "get_account", "account_info"),
    "clock": ("get_clock", "clock", "get_market_clock"),
    "positions": ("get_all_positions", "get_positions", "list_positions"),
    "position": ("get_open_position", "get_position"),
    "option_contracts": ("get_option_contracts", "list_option_contracts"),
    "option_snapshot": ("get_option_snapshot", "get_option_snapshots"),
    "option_latest_quote": ("get_option_latest_quote",),
    "stock_latest_quote": ("get_stock_latest_quote",),
    "stock_snapshot": ("get_stock_snapshot", "get_stock_snapshots"),
    "news": ("get_news",),
    "place_option_order": ("place_option_order",
                           "place_option_market_order",
                           "place_multi_leg_order", "submit_order"),
    "order_by_id": ("get_order_by_id", "get_order"),
    "order_by_client_id": ("get_order_by_client_id",),
    "orders": ("get_orders", "list_orders"),
    "cancel_order": ("cancel_order_by_id", "cancel_order"),
    "activities": ("get_account_activities",),
}

# Without these the control plane is not meaningfully using MCP.
REQUIRED_LOGICAL_TOOLS = ("account", "clock", "positions")


@dataclass(slots=True)
class MCPStats:
    mcp_calls: int = 0
    rest_fallbacks: int = 0
    errors: int = 0
    by_tool: dict[str, int] = field(default_factory=dict)

    def note_mcp(self, tool: str) -> None:
        self.mcp_calls += 1
        self.by_tool[tool] = self.by_tool.get(tool, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        total = self.mcp_calls + self.rest_fallbacks
        return {
            "mcp_calls": self.mcp_calls,
            "rest_fallbacks": self.rest_fallbacks,
            "errors": self.errors,
            "mcp_share": round(self.mcp_calls / total, 3) if total else 0.0,
            "by_tool": dict(sorted(self.by_tool.items(),
                                   key=lambda kv: -kv[1])),
        }


class AlpacaMCPClient:
    """Long-lived stdio session against the Alpaca MCP server."""

    def __init__(self, settings: Settings, db: Database | None = None,
                 command: str = "uvx",
                 args: tuple[str, ...] = ("alpaca-mcp-server",)):
        self.settings = settings
        self.db = db
        self.command = command
        self.args = list(args)
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self.available: bool = False
        self.start_error: str | None = None
        self.tools: dict[str, dict[str, Any]] = {}
        self.resolved: dict[str, str] = {}
        self.stats = MCPStats()

    # ---- lifecycle ---------------------------------------------------

    async def connect(self) -> bool:
        """Start the server and take an inventory of its tools.

        Returns False rather than raising: a control plane that cannot
        start must not prevent the system from running on REST.
        """
        self.settings.assert_paper_only()
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            self.start_error = f"mcp package unavailable: {exc}"
            await self._log("ERROR", "MCP_UNAVAILABLE", self.start_error)
            return False

        env = {
            **os.environ,
            "ALPACA_API_KEY": self.settings.alpaca_api_key.get_secret_value(),
            "ALPACA_SECRET_KEY":
                self.settings.alpaca_secret_key.get_secret_value(),
            "ALPACA_PAPER_TRADE": "true",
            "ALPACA_TOOLSETS": ("account,trading,assets,stock-data,"
                                "options-data,news"),
        }

        try:
            self._stack = AsyncExitStack()
            params = StdioServerParameters(command=self.command,
                                           args=self.args, env=env)
            read, write = await self._stack.enter_async_context(
                stdio_client(params))
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write))
            await self._session.initialize()
        except Exception as exc:  # noqa: BLE001
            self.start_error = f"{type(exc).__name__}: {exc}"[:300]
            await self._log("ERROR", "MCP_START_FAILED", self.start_error)
            await self.close()
            return False

        await self._inventory()
        self.available = True
        return True

    async def _inventory(self) -> None:
        """List tools, resolve aliases, and persist the full inventory.

        The inventory in system_events is the audit record that this
        system genuinely spoke to the MCP server.
        """
        try:
            listing = await self._session.list_tools()
        except Exception as exc:  # noqa: BLE001
            self.start_error = f"list_tools failed: {exc}"[:300]
            await self._log("ERROR", "MCP_LIST_TOOLS_FAILED",
                            self.start_error)
            return

        for tool in getattr(listing, "tools", []) or []:
            name = getattr(tool, "name", "")
            if not name:
                continue
            self.tools[name] = {
                "description": (getattr(tool, "description", "") or "")[:300],
                "schema": getattr(tool, "inputSchema", None),
            }

        for logical, aliases in TOOL_ALIASES.items():
            match = next((a for a in aliases if a in self.tools), None)
            if match:
                self.resolved[logical] = match

        missing = [t for t in REQUIRED_LOGICAL_TOOLS
                   if t not in self.resolved]
        await self._log(
            "INFO" if not missing else "WARN",
            "MCP_TOOLS_DISCOVERED",
            f"{len(self.tools)} tools, {len(self.resolved)} resolved",
            {"tool_names": sorted(self.tools),
             "resolved": self.resolved, "missing_required": missing})

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._stack = None
        self._session = None
        self.available = False

    async def __aenter__(self) -> "AlpacaMCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- invocation --------------------------------------------------

    def has(self, logical: str) -> bool:
        return self.available and logical in self.resolved

    async def call(self, logical: str,
                   arguments: dict[str, Any] | None = None) -> Any:
        """Invoke a logical tool. Raises on failure; callers decide policy."""
        if not self.has(logical):
            raise RuntimeError(f"MCP tool unavailable: {logical}")

        name = self.resolved[logical]
        result = await self._session.call_tool(name, arguments or {})
        self.stats.note_mcp(name)

        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool {name} returned an error")
        return parse_tool_result(result)

    async def _log(self, level: str, event: str, message: str,
                   context: dict[str, Any] | None = None) -> None:
        if self.db is None:
            return
        try:
            await self.db.log_event(level, "mcp_client", event, message,
                                    context or {})
        except Exception:  # noqa: BLE001
            pass


def parse_tool_result(result: Any) -> Any:
    """Extract usable data from an MCP tool result.

    Servers return a list of content blocks. Text blocks are commonly JSON
    but not always, so JSON is attempted and raw text kept on failure —
    never silently discarded.
    """
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured

    payloads: list[Any] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            payloads.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            payloads.append(text)

    if not payloads:
        return None
    return payloads[0] if len(payloads) == 1 else payloads


# ======================================================================


class ControlPlane:
    """Account, clock and position reads. MCP first, REST as fallback.

    Every method records which transport served it, so the share of
    operations that genuinely went through MCP is a measured number rather
    than a claim.
    """

    def __init__(self, mcp: AlpacaMCPClient, rest: AlpacaRestClient,
                 db: Database | None = None):
        self.mcp = mcp
        self.rest = rest
        self.db = db

    async def _try(self, logical: str, rest_call: Any,
                   arguments: dict[str, Any] | None = None) -> Any:
        if self.mcp.has(logical):
            try:
                return await self.mcp.call(logical, arguments)
            except Exception as exc:  # noqa: BLE001
                self.mcp.stats.errors += 1
                if self.db is not None:
                    try:
                        await self.db.log_event(
                            "WARN", "mcp_client", "MCP_CALL_FAILED",
                            f"{logical} fell back to REST: {exc}"[:300],
                            {"logical": logical})
                    except Exception:  # noqa: BLE001
                        pass
        self.mcp.stats.rest_fallbacks += 1
        return await rest_call()

    async def get_account(self) -> dict[str, Any]:
        return await self._try("account", self.rest.get_account)

    async def get_clock(self) -> dict[str, Any]:
        return await self._try("clock", self.rest.get_clock)

    async def get_positions(self) -> list[dict[str, Any]]:
        async def rest_positions() -> list[dict[str, Any]]:
            payload = await self.rest._get(
                f"{self.rest.trade_base}/v2/positions")
            return payload if isinstance(payload, list) else []

        result = await self._try("positions", rest_positions)
        if isinstance(result, dict):
            # Some servers wrap the list; accept either shape.
            for key in ("positions", "data", "result"):
                if isinstance(result.get(key), list):
                    return result[key]
            return []
        return result if isinstance(result, list) else []

    async def get_option_positions(self) -> list[dict[str, Any]]:
        positions = await self.get_positions()
        return [p for p in positions
                if str(p.get("asset_class", "")).lower() == "us_option"]

    def summary(self) -> dict[str, Any]:
        return {
            "mcp_available": self.mcp.available,
            "tools_discovered": len(self.mcp.tools),
            "tools_resolved": len(self.mcp.resolved),
            "start_error": self.mcp.start_error,
            **self.mcp.stats.as_dict(),
        }
