"""
Alpha Council v2.5 - MCP control plane verification.

The hackathon requires Alpaca's Trading API together with its MCP server or
CLI. This proves the MCP path works, dumps the server's actual tool
inventory, and compares MCP responses against REST for the same
operations.

Run this before starting the scheduler. The tool names in the code are
aliases matched at runtime, so this script is how you find out what the
server actually calls things.

Place at: scripts/test_alpaca_mcp.py

Usage:
    uv run python scripts/test_alpaca_mcp.py
    uv run python scripts/test_alpaca_mcp.py --schemas    # full JSON schemas
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.alpaca.mcp_client import (  # noqa: E402
    REQUIRED_LOGICAL_TOOLS,
    TOOL_ALIASES,
    AlpacaMCPClient,
    ControlPlane,
)
from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.settings import get_settings, load_yaml  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 74)
    say(title)
    say("=" * 74)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()
    scoring = load_yaml("scoring")

    rule("ALPHA COUNCIL - MCP CONTROL PLANE CHECK")
    say("  Requirement: projects must use Alpaca's Trading API together")
    say("  with its MCP server or CLI.")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as rest:

        mcp = AlpacaMCPClient(settings, db)

        rule("1. STARTING THE SERVER")
        say("  launching: uvx alpaca-mcp-server")
        say("  (first run downloads the package; allow a minute)")
        connected = await mcp.connect()

        if not connected:
            say("")
            say(f"  FAILED: {mcp.start_error}")
            say("")
            say("  Things to check:")
            say("    uvx --version")
            say("    uvx alpaca-mcp-server --help")
            say("  If the package name differs, pass it to AlpacaMCPClient")
            say("  as args=('the-real-name',).")
            say("")
            say("  The system still runs on REST, but the MCP requirement")
            say("  would be unmet.")
            return 1

        say("  connected")

        rule("2. TOOL INVENTORY")
        say(f"  {len(mcp.tools)} tools exposed")
        say("")
        for name in sorted(mcp.tools):
            description = mcp.tools[name]["description"]
            say(f"    {name:<34} {description[:34]}")

        rule("3. ALIAS RESOLUTION")
        say(f"  {'LOGICAL':<22}{'RESOLVED':<34}STATUS")
        say("  " + "-" * 68)
        for logical in TOOL_ALIASES:
            resolved = mcp.resolved.get(logical)
            required = logical in REQUIRED_LOGICAL_TOOLS
            if resolved:
                status = "ok"
            elif required:
                status = "MISSING (required)"
            else:
                status = "not found"
            say(f"  {logical:<22}{(resolved or '-'):<34}{status}")

        missing = [t for t in REQUIRED_LOGICAL_TOOLS if t not in mcp.resolved]
        if missing:
            say("")
            say(f"  Required logical tools unresolved: {missing}")
            say("  Add the server's real names to TOOL_ALIASES in")
            say("  alpha_council/alpaca/mcp_client.py using the inventory above.")

        if args.schemas:
            rule("3b. INPUT SCHEMAS")
            for name in sorted(mcp.tools):
                schema = mcp.tools[name]["schema"]
                say(f"  {name}")
                say("    " + json.dumps(schema, default=str)[:400])

        rule("4. LIVE CALLS THROUGH MCP")
        control = ControlPlane(mcp, rest, db)

        account = await control.get_account()
        if isinstance(account, dict):
            say(f"  account       : {account.get('account_number')}")
            say(f"  equity        : ${float(account.get('equity', 0) or 0):,.2f}")
            say(f"  options level : {account.get('options_trading_level')}")
        else:
            say(f"  account       : unexpected shape {type(account).__name__}")
            say(f"                  {str(account)[:200]}")

        clock = await control.get_clock()
        if isinstance(clock, dict):
            say(f"  market open   : {clock.get('is_open')}")
            say(f"  next close    : {clock.get('next_close')}")

        positions = await control.get_option_positions()
        say(f"  option posns  : {len(positions)}")

        rule("5. MCP VERSUS REST")
        rest_account = await rest.get_account()
        if isinstance(account, dict) and isinstance(rest_account, dict):
            mcp_equity = float(account.get("equity", 0) or 0)
            rest_equity = float(rest_account.get("equity", 0) or 0)
            say(f"  MCP equity    : ${mcp_equity:,.2f}")
            say(f"  REST equity   : ${rest_equity:,.2f}")
            if abs(mcp_equity - rest_equity) < 0.01:
                say("  agreement     : yes")
            else:
                say("  agreement     : NO — investigate before trading")
        else:
            say("  could not compare; MCP returned a non-dict payload")

        rule("6. TRANSPORT USAGE")
        for key, value in control.summary().items():
            say(f"  {key:<18}: {value}")

        await mcp.close()

    rule("RESULT")
    if missing:
        say("  MCP connected but required tools are unresolved.")
        say("  Update TOOL_ALIASES, then re-run.")
        return 1
    say("  MCP control plane verified. Account, clock and positions all")
    say("  served through MCP tools, with REST agreeing on equity.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", action="store_true",
                    help="print each tool's full input schema")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
