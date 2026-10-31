"""
Alpha Council v2.7 - ETF sleeve rebalance, from a cold start.

The scheduler runs the same check daily at sleeves.etf.rebalance_time_et;
this script is the standalone path - used for the initial establishment
of the sleeve and for any operator-driven rebalance while the desk is
down. Dry run by default: prints the full plan and submits nothing
without --execute.

Usage:
    uv run python scripts/sleeve_rebalance.py              # plan only
    uv run python scripts/sleeve_rebalance.py --execute    # trade the plan
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.alpaca.market_data import MarketDataService  # noqa: E402
from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.execution.order_manager import OrderManager  # noqa: E402
from alpha_council.sleeves import SleeveManager  # noqa: E402
from alpha_council.settings import get_settings, load_yaml  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


async def run(execute: bool) -> int:
    settings = get_settings()
    settings.assert_paper_only()
    scoring = load_yaml("scoring")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:
        identity = await api.assert_account_identity(
            settings.alpaca_expected_account_last4)
        say(f"account identity : {identity}")

        market = MarketDataService(api, db)
        orders = OrderManager(api, db)
        sleeves = SleeveManager(orders, market, db, scoring)
        await sleeves.ensure_schema()
        if not (sleeves.enabled and sleeves.etf.enabled):
            say("ETF sleeve is disabled in config/scoring.yaml; nothing to do.")
            return 1

        clock = await api.get_clock()
        if not clock.get("is_open") and execute:
            say("Market is CLOSED. Marketable limits priced off stale "
                "quotes gap-risk the open; run during RTH.")
            return 2

        account = await api.get_account()
        equity = float(account.get("equity", 0) or 0)
        say(f"account equity   : ${equity:,.2f}")
        say(f"sleeve target    : {sleeves.etf.target_pct:.0f}% = "
            f"${equity * sleeves.etf.target_pct / 100:,.2f}")

        plan = await sleeves.etf.plan(equity)
        say(f"current sleeve MV: ${plan.current_value:,.2f}")
        say("")
        if not plan.orders:
            say("Every leg is inside its band. Nothing to trade.")
        for order in plan.orders:
            say(f"  {'EXECUTE' if execute else 'would'} {order.side:<4} "
                f"{order.qty:>4} {order.symbol:<5} @ "
                f"{order.reference_price:>9.2f}  "
                f"(${order.current_value:>9,.0f} -> "
                f"${order.target_value:>9,.0f})")
        for note in plan.skipped:
            say(f"  skipped: {note}")

        if not execute or not plan.orders:
            return 0

        say("")
        summary = await sleeves.etf.rebalance(equity, execute=True)
        for fill in summary["fills"]:
            say(f"  FILLED  {fill}")
        for miss in summary["unfilled"]:
            say(f"  UNFILLED {miss} - re-run to retry")

        acct = await sleeves.accounting()
        say("")
        say(f"sleeve MV        : ${acct.sleeve_market_value:,.2f}")
        say(f"net invested     : ${acct.net_invested:,.2f}")
        say(f"sleeve P&L       : ${acct.sleeve_pnl_total:,.2f}")
        await sleeves.etf.snapshot()
        await db.log_event(
            "INFO", "sleeve_rebalance", "ETF_REBALANCE_MANUAL",
            f"{len(summary['fills'])} fill(s), "
            f"{len(summary['unfilled'])} unfilled", summary)
        return 0 if not summary["unfilled"] else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="actually submit orders; default is a dry run")
    return asyncio.run(run(ap.parse_args().execute))


if __name__ == "__main__":
    sys.exit(main())
