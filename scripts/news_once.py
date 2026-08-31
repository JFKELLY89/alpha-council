"""
Alpha Council v2.5 - news intelligence check.

Fetches, scores and prints today's news events. No LLM calls, no orders.
Run it to see whether the EVENT track has anything to work with before
committing a council session to it.

Place at: scripts/news_once.py

Usage:
    uv run python scripts/news_once.py
    uv run python scripts/news_once.py --symbols NVDA,AMD,RIVN --hours 12
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
from alpha_council.intelligence.news import NewsIntelligence  # noqa: E402
from alpha_council.quant.scoring import summarize_intel  # noqa: E402
from alpha_council.settings import get_settings, load_yaml  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str) -> None:
    say("")
    say("=" * 76)
    say(title)
    say("=" * 76)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.assert_paper_only()
    scoring = load_yaml("scoring")
    universe = load_yaml("universe")

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")
                   if s.strip()]
    else:
        excluded = {e["symbol"] for e in universe.get("exclusions", [])}
        symbols = [s for s in universe.get("core_symbols", [])
                   if s not in excluded]

    rule("ALPHA COUNCIL - NEWS INTELLIGENCE")
    say(f"  symbols  : {len(symbols)}")
    say(f"  lookback : {args.hours}h")

    async with Database(settings.database_path) as db, \
            AlpacaRestClient(settings, scoring) as api:

        market = MarketDataService(api, db)
        news = NewsIntelligence(api, db, scoring)

        # Price response is what separates a story the tape believes from
        # one it is ignoring, so it has to be fetched before scoring.
        rule("1. PRICE RESPONSE")
        snapshots = await market.snapshots(symbols)
        returns: dict[str, float] = {}
        for symbol, snap in snapshots.items():
            prev_close = snap.prev_close
            price = snap.quote.signal_price() or snap.mid
            if prev_close and price and prev_close > 0:
                returns[symbol] = (price - prev_close) / prev_close
        say(f"  {len(returns)} symbols with a usable day return")

        movers = sorted(returns.items(), key=lambda kv: -abs(kv[1]))[:8]
        for symbol, move in movers:
            say(f"    {symbol:<6} {move:+.2%}")

        rule("2. COLLECTING")
        events = await news.collect(symbols, lookback_hours=args.hours,
                                    price_returns=returns)
        for key, value in news.stats.as_dict().items():
            say(f"  {key:<22}: {value}")

        if not events:
            rule("NO EVENTS")
            say("  Nothing published for these symbols in the window.")
            say("  Every candidate will run on the MOMENTUM track with a")
            say("  null catalyst, which is honest but gives the PM little")
            say("  to work with.")
            return 0

        rule("3. EVENTS BY CATALYST SCORE")
        ranked = sorted(
            ((symbol, event) for symbol, evs in events.items() for event in evs),
            key=lambda pair: pair[1].catalyst_score, reverse=True)

        say(f"  {'SYM':<7}{'CAT':>6}{'DIR':>9}{'CONF':>6}{'MATL':>6}"
            f"{'FRESH':>7}{'NOV':>6}{'CORR':>6}{'MKT':>6}  TYPE")
        say("  " + "-" * 74)
        for symbol, event in ranked[:20]:
            say(f"  {symbol:<7}{event.catalyst_score:>6.1f}"
                f"{str(event.direction)[:8]:>9}{event.direction_confidence:>6.2f}"
                f"{event.materiality_score:>6.0f}{event.freshness_score:>7.0f}"
                f"{event.novelty_score:>6.0f}{event.corroboration_score:>6.0f}"
                f"{event.market_confirmation_score:>6.0f}  {event.event_type}")

        rule("4. EVENT-TRACK CANDIDATES")
        say("  Symbols whose intelligence clears the materiality bar and")
        say("  would therefore score on the EVENT track rather than MOMENTUM.")
        say("")
        qualified = 0
        for symbol, symbol_events in sorted(
                events.items(),
                key=lambda kv: -max(e.catalyst_score for e in kv[1])):
            summary = summarize_intel(symbol_events)
            if not summary.has_material_catalyst:
                continue
            qualified += 1
            say(f"  {symbol:<7} catalyst {summary.catalyst_score:>5.1f}  "
                f"{summary.direction} @ {summary.direction_confidence:.2f}  "
                f"{summary.event_count} event(s)")
            say(f"          {symbol_events[0].extracted_facts[0][:90]}")

        if qualified == 0:
            say("  None. All events fall below the materiality or freshness")
            say("  floor, so every candidate stays on the MOMENTUM track.")

        rule("5. HEADLINE VERSUS TAPE")
        say("  Stories the price response contradicts. Direction is taken")
        say("  from the tape and confidence is cut.")
        say("")
        contradicted = [(s, e) for s, e in ranked
                        if any("direction taken from price" in f
                               for f in e.extracted_facts)]
        if contradicted:
            for symbol, event in contradicted[:8]:
                say(f"  {symbol:<7} {event.extracted_facts[-1][:80]}")
        else:
            say("  None.")

        rule("SUMMARY")
        say(f"  {news.stats.events} events across "
            f"{news.stats.symbols_with_events} symbols")
        say(f"  {qualified} would qualify for the EVENT track")
        say(f"  {news.stats.duplicates} duplicate copies clustered")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=str, default="")
    ap.add_argument("--hours", type=int, default=24)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
