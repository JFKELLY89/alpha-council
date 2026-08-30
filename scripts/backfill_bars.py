"""
Alpha Council v2.4 - 20-day RTH bar backfill.

Loads regular-trading-hours 5-minute IEX bars for the Core Universe and
persists them to market_bars. Verifies that the extended-hours filter is
actually working by comparing bars-per-session against the expected 78.

Place at: scripts/backfill_bars.py

Usage:
    uv run python scripts/backfill_bars.py
    uv run python scripts/backfill_bars.py --sessions 20 --verify-only
    uv run python scripts/backfill_bars.py --symbols SPY,QQQ,NVDA
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_council.alpaca.market_data import (  # noqa: E402
    RTH_BARS_PER_SESSION,
    MarketDataService,
)
from alpha_council.alpaca.rest_client import AlpacaRestClient  # noqa: E402
from alpha_council.db.engine import Database  # noqa: E402
from alpha_council.settings import get_settings, load_yaml  # noqa: E402
from alpha_council.utils.time import to_et  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 70)
    say(title)
    say("=" * 70)


async def verify_rth(db: Database, symbols: list[str]) -> list[str]:
    """Confirm no stored bar falls outside regular trading hours."""
    problems = []
    rows = await db.fetchall(
        "SELECT symbol, ts FROM market_bars WHERE timeframe='5Min' "
        "ORDER BY ts DESC LIMIT 5000"
    )
    from alpha_council.utils.time import is_rth, parse_alpaca_ts

    offenders: dict[str, int] = {}
    for r in rows:
        ts = parse_alpaca_ts(r["ts"])
        if ts and not is_rth(ts):
            offenders[r["symbol"]] = offenders.get(r["symbol"], 0) + 1

    if offenders:
        total = sum(offenders.values())
        problems.append(
            f"{total} extended-hours bars stored across "
            f"{len(offenders)} symbol(s): {dict(list(offenders.items())[:5])}"
        )
    return problems


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        universe = load_yaml("universe")
        excluded = {e["symbol"] for e in universe.get("exclusions", [])}
        symbols = [s for s in universe.get("core_symbols", []) if s not in excluded]

    rule("ALPHA COUNCIL - RTH BAR BACKFILL")
    say(f"  symbols  : {len(symbols)}")
    say(f"  sessions : {args.sessions}")
    say(f"  timeframe: 5Min, regular trading hours only")
    say(f"  expected : ~{RTH_BARS_PER_SESSION} bars per session per symbol")

    async with Database(settings.database_path) as db:
        if args.verify_only:
            rule("VERIFY ONLY")
            problems = await verify_rth(db, symbols)
            svc = MarketDataService(None, db)  # type: ignore[arg-type]
            for sym in symbols[:12]:
                cov = await svc.bar_coverage(sym)
                say(f"  {sym:<6} {cov['bars']:>6} bars  "
                    f"~{cov['sessions_equivalent']:>5} sessions  "
                    f"last={str(cov['last_ts'])[:16]}")
            if problems:
                for p in problems:
                    say(f"  FAIL: {p}")
                return 1
            say("  no extended-hours bars found")
            return 0

        async with AlpacaRestClient(settings) as api:
            svc = MarketDataService(api, db)

            rule("FETCHING")
            counts = await svc.backfill_bars(symbols, sessions=args.sessions)

            total = sum(counts.values())
            say(f"  {total:,} RTH bars stored across {len(counts)} symbols")
            say("")

            thin = []
            for sym in symbols:
                n = counts.get(sym, 0)
                sessions_eq = n / RTH_BARS_PER_SESSION
                flag = ""
                if n == 0:
                    flag = "  <-- NO DATA"
                    thin.append(sym)
                elif sessions_eq < args.sessions * 0.5:
                    flag = "  <-- THIN"
                    thin.append(sym)
                say(f"  {sym:<6} {n:>6} bars  ~{sessions_eq:>5.1f} sessions{flag}")

            rule("RTH FILTER VERIFICATION")
            problems = await verify_rth(db, symbols)
            if problems:
                for p in problems:
                    say(f"  FAIL: {p}")
                return 1
            say("  every stored bar falls inside regular trading hours")

            sample = next((s for s in symbols if counts.get(s)), None)
            if sample:
                bars = await svc.load_bars(sample, limit=RTH_BARS_PER_SESSION)
                if bars:
                    first, last = to_et(bars[0].timestamp), to_et(bars[-1].timestamp)
                    say(f"  sample {sample}: {first:%Y-%m-%d %H:%M} ET "
                        f"-> {last:%Y-%m-%d %H:%M} ET")

                profile = await svc.session_volume_profile(sample)
                say(f"  {sample} volume profile: {len(profile)} clock windows "
                    f"(expect 26 for a full session at 15-minute buckets)")

            rule("RESULT")
            if thin:
                say(f"  {len(thin)} symbol(s) with thin or missing data: "
                    f"{thin[:10]}")
                say("  These will fail the discovery data-density check.")
            say(f"  client: {api.stats()}")
            say("  backfill complete")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=20)
    ap.add_argument("--symbols", type=str, default="")
    ap.add_argument("--verify-only", action="store_true")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
