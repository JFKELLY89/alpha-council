"""
Alpha Council v2.5 - live spread marking.

Supplies current spread values to the shadow book. Every counterfactual
number in the system traces back to this file, so two rules govern it:

  ONE METHOD PER CYCLE. The method is chosen by the caller and applied
  identically to every variant. Marking the executed variant at the mid and
  a shadow at the bid would manufacture an edge out of nothing.

  RETURN None RATHER THAN GUESS. A missing or unusable quote yields None,
  and the shadow book skips that variant for that cycle. A mark of zero
  would read as a total loss, which is a much worse error than a gap.

Place at: alpha_council/journal/marks.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alpha_council.alpaca.market_data import MarketDataService
from alpha_council.alpaca.rest_client import AlpacaError, AlpacaRestClient
from alpha_council.models.enums import MarkMethod
from alpha_council.models.trading import OptionStructure
from alpha_council.utils.math import delta_adjust, safe_mid
from alpha_council.utils.time import age_seconds, parse_alpaca_ts, utc_now


@dataclass(slots=True)
class MarkStats:
    requested: int = 0
    marked: int = 0
    skipped_no_quote: int = 0
    skipped_stale: int = 0
    skipped_no_reference: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "requested": self.requested, "marked": self.marked,
            "no_quote": self.skipped_no_quote, "stale": self.skipped_stale,
            "no_reference": self.skipped_no_reference, "errors": self.errors,
        }


class LiveMarkSource:
    """Prices a two-leg spread from the current Indicative option chain."""

    def __init__(self, api: AlpacaRestClient, market: MarketDataService,
                 fresh_quote_seconds: float = 60.0,
                 max_quote_lag_seconds: float = 1200.0,
                 max_underlying_drift_pct: float = 0.010):
        self.api = api
        self.market = market
        self.fresh_quote_seconds = fresh_quote_seconds
        self.max_quote_lag_seconds = max_quote_lag_seconds
        self.max_underlying_drift_pct = max_underlying_drift_pct
        self.stats = MarkStats()

    async def spread_mark(self, structure: OptionStructure,
                          method: MarkMethod) -> float | None:
        """Current value of one spread, or None when it cannot be priced."""
        self.stats.requested += 1
        long_leg, short_leg = structure.long_leg, structure.short_leg

        try:
            snapshots = await self.api.get_option_snapshots(
                [long_leg.symbol, short_leg.symbol])
        except AlpacaError:
            self.stats.errors += 1
            return None

        long_snap = snapshots.get(long_leg.symbol)
        short_snap = snapshots.get(short_leg.symbol)
        if not long_snap or not short_snap:
            self.stats.skipped_no_quote += 1
            return None

        now = utc_now()

        if method is MarkMethod.CONSERVATIVE:
            # Exit value if you crossed both spreads: sell the long at the
            # bid, buy back the short at the ask. Raw, never adjusted.
            long_bid = (long_snap.get("latestQuote") or {}).get("bp")
            short_ask = (short_snap.get("latestQuote") or {}).get("ap")
            if not long_bid or not short_ask or long_bid <= 0 or short_ask <= 0:
                self.stats.skipped_no_quote += 1
                return None
            value = float(long_bid) - float(short_ask)
            if value < 0:
                value = 0.0
            self.stats.marked += 1
            return round(value, 4)

        long_value = await self._leg_value(long_snap, structure, now)
        short_value = await self._leg_value(short_snap, structure, now)
        if long_value is None or short_value is None:
            return None

        value = long_value - short_value
        if value < 0:
            # A debit spread cannot be worth less than nothing. A negative
            # result means a bad quote pair, not a negative value.
            self.stats.skipped_no_quote += 1
            return None

        self.stats.marked += 1
        return round(min(value, structure.width), 4)

    async def _leg_value(self, snapshot: dict[str, Any],
                         structure: OptionStructure,
                         now: datetime) -> float | None:
        quote = snapshot.get("latestQuote") or {}
        bid, ask = quote.get("bp"), quote.get("ap")
        raw_mid = safe_mid(bid, ask)
        if raw_mid is None:
            self.stats.skipped_no_quote += 1
            return None

        quote_ts = parse_alpaca_ts(quote.get("t"))
        lag = age_seconds(quote_ts, now) or 0.0
        if lag > self.max_quote_lag_seconds:
            self.stats.skipped_stale += 1
            return None
        if lag <= self.fresh_quote_seconds or quote_ts is None:
            return raw_mid

        # Stale: adjust for underlying movement since the quote timestamp,
        # exactly as the options engine does at entry. The same method must
        # apply at entry and at every mark, or the P&L is not comparable.
        underlying_now = await self._underlying_now(structure.symbol)
        underlying_then = await self.market.underlying_at(structure.symbol,
                                                          quote_ts)
        if underlying_now is None or underlying_then is None:
            self.stats.skipped_no_reference += 1
            return None

        move = underlying_now - underlying_then
        if abs(move / underlying_now) > self.max_underlying_drift_pct:
            self.stats.skipped_stale += 1
            return None

        delta = (snapshot.get("greeks") or {}).get("delta")
        if delta is None:
            self.stats.skipped_no_quote += 1
            return None
        return delta_adjust(raw_mid, float(delta), move)

    async def _underlying_now(self, symbol: str) -> float | None:
        snapshots = await self.market.snapshots([symbol])
        snap = snapshots.get(symbol)
        return snap.signal_price() if snap else None

    def reset_stats(self) -> None:
        self.stats = MarkStats()
