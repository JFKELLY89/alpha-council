"""
Alpha Council v2.4 §18.2 - flatten everything, from a cold start.

The automatic competition flatten lives inside the scheduler process. This
script is the insurance policy the spec requires alongside it: runnable by
hand with no scheduler, no MCP, and no assumptions about local state being
intact. It reconciles THREE sources of truth and closes what it finds:

  1. working orders at the broker        -> canceled first
  2. positions the journal knows about   -> closed through the full path
     (credit-side limit walk, close-side calibration, journal close,
      shadow-variant freeze + final attribution)
  3. positions ONLY the broker knows     -> named loudly, then closed
     leg-by-leg with marketable limits (the Aug 31 lesson: fills that
     bypass the books still exist and still expire)

Dry run by default: prints exactly what it would do. Nothing is submitted
without --execute. Exit code is non-zero if anything remains open, so it
can gate a shutdown script.

Usage:
    uv run python scripts/close_all.py                # reconcile + report
    uv run python scripts/close_all.py --execute      # flatten (MANUAL)
    uv run python scripts/close_all.py --execute --competition
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.alpaca.market_data import MarketDataService  # noqa: E402
from alpha_council.alpaca.rest_client import AlpacaError, AlpacaRestClient  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.execution.order_manager import OrderManager  # noqa: E402
from alpha_council.execution.position_monitor import (  # noqa: E402
    ExitDecision,
    PositionMonitor,
)
from alpha_council.journal.marks import LiveMarkSource  # noqa: E402
from alpha_council.journal.shadow_book import ShadowBook  # noqa: E402
from alpha_council.journal.trade_journal import TradeJournal  # noqa: E402
from alpha_council.models.enums import ExitReason, MarkMethod  # noqa: E402
from alpha_council.settings import get_settings, load_yaml  # noqa: E402
from alpha_council.utils.math import safe_mid  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 72)
    say(title)
    say("=" * 72)


async def cancel_working_orders(orders: OrderManager,
                                execute: bool) -> int:
    working = await orders.working_orders()
    if not working:
        say("  no working orders")
        return 0
    for order in working:
        cid = (order.get("client_order_id") or "")[:24]
        say(f"  {'CANCELING' if execute else 'would cancel'} "
            f"{order.get('id', '')[:12]} {order.get('symbol', '')} "
            f"{order.get('status')} cid={cid}")
        if execute:
            await orders.cancel(order.get("id", ""))
    return len(working)


async def close_orphan_leg(api: AlpacaRestClient, orders: OrderManager,
                           position: dict[str, Any], execute: bool) -> bool:
    """Close one broker-only option position with a marketable limit.

    A long leg sells at the bid, a short leg buys back at the ask -
    conservative crossing prices on a paper account, chosen to fill.
    """
    occ = position.get("symbol", "")
    qty = abs(int(float(position.get("qty", 0))))
    long_side = str(position.get("side", "")).lower() == "long"

    snaps = await api.get_option_snapshots([occ])
    quote = (snaps.get(occ) or {}).get("latestQuote") or {}
    bid, ask = quote.get("bp"), quote.get("ap")
    if safe_mid(bid, ask) is None:
        say(f"    {occ}: no usable quote; cannot price a close. SKIPPED - "
            "close manually in the Alpaca UI.")
        return False

    side = "sell" if long_side else "buy"
    limit = float(bid) if long_side else float(ask)
    say(f"    {occ}: {'CLOSING' if execute else 'would close'} "
        f"{side} {qty} @ limit {limit:.2f}")
    if not execute:
        return True
    try:
        await orders._post("/v2/orders", {
            "symbol": occ, "qty": str(qty), "side": side, "type": "limit",
            "limit_price": f"{limit:.2f}", "time_in_force": "day",
        })
        return True
    except AlpacaError as exc:
        say(f"    {occ}: close submission failed: {exc}")
        return False


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()
    scoring = load_yaml("scoring")
    risk_cfg = load_yaml("risk_constitution")
    options_cfg = scoring.get("options", {})
    reason = (ExitReason.COMPETITION_FLATTEN if args.competition
              else ExitReason.MANUAL)

    rule("ALPHA COUNCIL - CLOSE ALL")
    say(f"  mode   : {'EXECUTE' if args.execute else 'DRY RUN - nothing will be submitted'}")
    say(f"  reason : {reason}")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:
        market = MarketDataService(api, db)
        orders = OrderManager(api, db)
        journal = TradeJournal(db)
        marks = LiveMarkSource(
            api, market,
            fresh_quote_seconds=float(options_cfg.get("fresh_quote_seconds", 60)),
            max_quote_lag_seconds=float(
                options_cfg.get("max_quote_lag_seconds", 1200)),
            max_underlying_drift_pct=float(
                options_cfg.get("max_underlying_drift_pct", 0.010)))
        shadows = ShadowBook(db, marks, MarkMethod.ADJUSTED_MID)
        await shadows.restore()
        monitor = PositionMonitor(db, market, orders, journal, scoring,
                                  risk_cfg, marks=marks, shadows=shadows)
        restored = await monitor.restore()

        # ---- 1. working orders ------------------------------------
        rule("1. WORKING ORDERS")
        await cancel_working_orders(orders, args.execute)

        # ---- 2. reconcile broker vs books -------------------------
        rule("2. RECONCILIATION")
        broker_positions = await orders.open_option_positions()
        tracked = {leg.symbol for p in monitor.tracked
                   for leg in p.structure.legs}
        say(f"  journaled open positions : {restored}")
        say(f"  broker option positions  : {len(broker_positions)}")
        orphans = [p for p in broker_positions
                   if p.get("symbol") not in tracked]
        if orphans:
            say(f"  ORPHANS (broker-only, unknown to the books): "
                f"{len(orphans)}")
            for p in orphans:
                say(f"    {p.get('symbol')} qty={p.get('qty')} "
                    f"side={p.get('side')} mv={p.get('market_value')}")

        if not monitor.tracked and not orphans:
            say("  Nothing to close. Flat on both books and broker.")
            return 0

        # ---- 3. journaled closes ----------------------------------
        if monitor.tracked:
            rule("3. JOURNALED CLOSES (full path)")
            for position in list(monitor.tracked):
                say(f"  {position.symbol} x{position.qty} "
                    f"@ {position.entry_debit:.2f} "
                    f"({position.decision_id})")
                if not args.execute:
                    continue
                outcome = await monitor.close(
                    position, ExitDecision(True, reason, "close_all"))
                if outcome.filled and outcome.fill_debit is not None:
                    say(f"    CLOSED @ credit {outcome.fill_debit:.2f}")
                else:
                    say(f"    NOT FILLED ({outcome.final_status}) - "
                        "still open, retry or close in the Alpaca UI")

        # ---- 4. orphan closes -------------------------------------
        if orphans:
            rule("4. ORPHAN CLOSES (leg-by-leg)")
            for position in orphans:
                await close_orphan_leg(api, orders, position, args.execute)

        # ---- 5. verify --------------------------------------------
        rule("5. VERIFICATION")
        if not args.execute:
            say("  dry run - no verification pass")
            return 0
        await asyncio.sleep(5)
        remaining = await orders.open_option_positions()
        open_journal = await journal.open_trades()
        say(f"  broker option positions remaining : {len(remaining)}")
        say(f"  journaled OPEN trades remaining   : {len(open_journal)}")
        for p in remaining:
            say(f"    STILL OPEN: {p.get('symbol')} qty={p.get('qty')}")
        if remaining or open_journal:
            say("  NOT FLAT. Re-run, or close the remainder in the "
                "Alpaca UI and reconcile the journal by hand.")
            return 2
        say("  FLAT. Books and broker agree.")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--execute", action="store_true",
                    help="actually cancel and close; default is a dry run")
    ap.add_argument("--competition", action="store_true",
                    help="journal closes as COMPETITION_FLATTEN instead of "
                         "MANUAL")
    try:
        return asyncio.run(run(ap.parse_args()))
    except KeyboardInterrupt:
        say("\nInterrupted mid-flatten. RE-RUN THIS SCRIPT: orders may be "
            "live and positions may remain.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
