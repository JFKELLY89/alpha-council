"""
Alpha Council v2.3 - Alpaca REST client verification.

Exercises every code path the scanner depends on: batch snapshots, bar
pagination, news, option contract pagination, option snapshots, and the
rate limiter under load. Safe to run with the market closed - it reports
staleness rather than failing on it.

Place at: scripts/test_alpaca_data.py

Usage:
    uv run python scripts/test_alpaca_data.py
    uv run python scripts/test_alpaca_data.py --full   # all universe symbols
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.alpaca.rest_client import (  # noqa: E402
    AlpacaRestClient,
    quote_age_seconds,
)
from alpha_council.settings import load_yaml  # noqa: E402

SMALL_SET = ["SPY", "QQQ", "NVDA", "AAPL", "JPM", "XOM"]


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 70)
    say(title)
    say("=" * 70)


async def run(full: bool) -> int:
    universe = load_yaml("universe")
    symbols = universe.get("core_symbols", SMALL_SET) if full else SMALL_SET
    excluded = {e["symbol"] for e in universe.get("exclusions", [])}
    symbols = [s for s in symbols if s not in excluded]

    failures: list[str] = []

    async with AlpacaRestClient() as api:
        # ---- clock and account -------------------------------------
        rule("1. CLOCK AND ACCOUNT")
        clock = await api.get_clock()
        acct = await api.get_account()
        is_open = bool(clock.get("is_open"))
        say(f"  market open : {is_open}")
        say(f"  equity      : ${float(acct.get('equity', 0)):,.2f}")
        say(f"  buying power: ${float(acct.get('buying_power', 0)):,.2f}")
        say(f"  opt level   : {acct.get('options_trading_level')}")
        if not is_open:
            say("  NOTE: market closed. Quote ages below reflect the last")
            say("        print before the close and are expected to be large.")

        # ---- batch snapshots ---------------------------------------
        rule(f"2. BATCH SNAPSHOTS ({len(symbols)} symbols)")
        snaps = await api.get_stock_snapshots(symbols)
        say(f"  returned {len(snaps)} of {len(symbols)}")
        missing = [s for s in symbols if s not in snaps]
        if missing:
            say(f"  MISSING: {missing}")
            failures.append(f"snapshots missing {len(missing)} symbols")

        widest = []
        for sym in symbols[:10]:
            s = snaps.get(sym) or {}
            q = s.get("latestQuote") or {}
            bid, ask = q.get("bp"), q.get("ap")
            age = quote_age_seconds(q.get("t"))
            if bid and ask and ask >= bid and (bid + ask) > 0:
                mid = (bid + ask) / 2
                spread = (ask - bid) / mid
                widest.append((spread, sym))
                say(f"  {sym:<5} bid={bid:<9.2f} ask={ask:<9.2f} "
                    f"spread={spread:>7.3%}  age={age}s")
            else:
                say(f"  {sym:<5} no valid two-sided quote (bid={bid} ask={ask})")
        if widest:
            widest.sort(reverse=True)
            say(f"  widest quoted spread: {widest[0][1]} at {widest[0][0]:.3%}")

        # ---- bar pagination ----------------------------------------
        rule("3. BAR HISTORY AND PAGINATION")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        bars = await api.get_stock_bars(SMALL_SET, "5Min", start, end)
        for sym in SMALL_SET:
            rows = bars.get(sym, [])
            if not rows:
                say(f"  {sym:<5} NO BARS")
                failures.append(f"no bars for {sym}")
                continue
            first, last = rows[0]["t"][:16], rows[-1]["t"][:16]
            say(f"  {sym:<5} {len(rows):>6} bars   {first} -> {last}")
        total = sum(len(v) for v in bars.values())
        say(f"  total {total} bars")
        if total < 10000:
            say("  NOTE: under 10,000 rows means pagination was not exercised.")
        else:
            say("  pagination exercised (>10,000 rows across pages)")

        # ---- news ---------------------------------------------------
        rule("4. NEWS")
        news = await api.get_news(SMALL_SET, end - timedelta(days=2), end, limit=50)
        say(f"  {len(news)} items in the last 48h")
        for item in news[:5]:
            headline = str(item.get("headline", ""))[:64]
            say(f"    [{item.get('created_at', '')[:16]}] "
                f"{','.join(item.get('symbols', []))[:20]:<20} {headline}")

        # ---- option contracts --------------------------------------
        rule("5. OPTION CONTRACTS (pagination)")
        spy = snaps.get("SPY") or {}
        q = spy.get("latestQuote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        spot = (bid + ask) / 2 if bid and ask else 0.0
        if not spot:
            trade = spy.get("latestTrade") or {}
            spot = float(trade.get("p") or 0)
        say(f"  SPY reference: {spot:.2f}")
        if not spot:
            failures.append("no SPY reference price")
            return finish(failures, api)

        today = datetime.now(timezone.utc).date()
        contracts = await api.get_option_contracts(
            "SPY",
            expiration_gte=(today + timedelta(days=7)).isoformat(),
            expiration_lte=(today + timedelta(days=21)).isoformat(),
            strike_gte=spot * 0.94, strike_lte=spot * 1.06,
        )
        say(f"  {len(contracts)} contracts in 7-21 DTE, +/-6% strikes")
        if len(contracts) <= 200:
            say("  NOTE: <=200 means one page; pagination not proven here.")
        with_oi = sum(1 for c in contracts if c.get("open_interest"))
        say(f"  {with_oi} carry open interest")
        expiries = sorted({c.get("expiration_date") for c in contracts})
        say(f"  expirations: {expiries}")

        # ---- option snapshots --------------------------------------
        rule("6. OPTION SNAPSHOTS AND GREEKS")
        picks = sorted(contracts,
                       key=lambda c: int(c.get("open_interest") or 0),
                       reverse=True)[:8]
        occ = [c["symbol"] for c in picks]
        osnaps = await api.get_option_snapshots(occ)
        say(f"  requested {len(occ)}, received {len(osnaps)}")
        greeks_ok = 0
        for sym, snap in osnaps.items():
            oq = snap.get("latestQuote") or {}
            g = snap.get("greeks") or {}
            age = quote_age_seconds(oq.get("t"))
            has_g = g.get("delta") is not None
            greeks_ok += 1 if has_g else 0
            delta = f"{g.get('delta'):.3f}" if has_g else "  --  "
            say(f"  {sym:<22} bid={str(oq.get('bp')):<7} ask={str(oq.get('ap')):<7} "
                f"delta={delta}  iv={snap.get('impliedVolatility')}  age={age}s")
        say(f"  greeks present on {greeks_ok}/{len(osnaps)}")
        if osnaps and greeks_ok < len(osnaps):
            failures.append("some option snapshots lack greeks")

        # ---- chain --------------------------------------------------
        rule("7. FULL CHAIN SNAPSHOT")
        chain = await api.get_option_chain(
            "SPY",
            expiration_gte=(today + timedelta(days=7)).isoformat(),
            expiration_lte=(today + timedelta(days=21)).isoformat(),
        )
        say(f"  {len(chain)} contracts in chain snapshot")
        if len(chain) > 1000:
            say("  chain pagination exercised")

        return finish(failures, api)


def finish(failures: list[str], api: AlpacaRestClient) -> int:
    rule("CLIENT STATS")
    for k, v in api.stats().items():
        say(f"  {k:<18}: {v}")

    rule("RESULT")
    if failures:
        for f in failures:
            say(f"  FAIL: {f}")
        return 1
    say("  all checks passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="use the whole universe.yaml symbol list")
    return asyncio.run(run(ap.parse_args().full))


if __name__ == "__main__":
    sys.exit(main())
