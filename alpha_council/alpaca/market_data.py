"""
Alpha Council v2.4 - market data normalization and persistence.

Two fixes that came directly out of the 2026-08-28 live test:

  1. RTH-ONLY BARS. Alpaca returned bars stamped 20:50Z against a 20:00Z
     close, roughly 83 bars per session where regular hours holds 78.
     The RVOL baseline compares the current 15-minute window against the
     median of the same window across 20 prior sessions; if some sessions
     carry extended-hours bars and others do not, that denominator is
     unstable and every RVOL score is quietly wrong.

  2. INVALID QUOTE REJECTION. AAPL came back bid=300.93 ask=0. Every
     normalization path here routes through safe_mid, which returns None
     for one-sided and crossed quotes.

Place at: alpha_council/alpaca/market_data.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Sequence

from alpha_council.alpaca.rest_client import AlpacaRestClient
from alpha_council.db.engine import Database
from alpha_council.models.market import Bar, QuoteObservation
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.math import median_or_none, safe_mid, spread_pct
from alpha_council.utils.time import (
    age_seconds,
    clock_window_index,
    is_rth,
    iso_utc,
    parse_alpaca_ts,
    previous_trading_days,
    session_bounds,
    to_et,
    utc_now,
)

SOURCE_IEX = "ALPACA_IEX"
BARS_TIMEFRAME = "5Min"
RTH_BARS_PER_SESSION = 78          # 09:30-16:00 in 5-minute bars


@dataclass(slots=True)
class SymbolSnapshot:
    """Normalized view of one Alpaca snapshot payload."""

    symbol: str
    observed_at: datetime
    quote: QuoteObservation
    last_trade: float | None
    last_trade_at: datetime | None
    minute_bar_close: float | None
    minute_bar_at: datetime | None
    daily_bar: dict[str, Any] | None
    prev_daily_bar: dict[str, Any] | None

    @property
    def mid(self) -> float | None:
        return self.quote.midpoint()

    @property
    def quote_age(self) -> float | None:
        return self.quote.quote_lag_seconds

    def signal_price(self,
                     prefer_last_above_spread_pct: float = 0.010
                     ) -> float | None:
        """Price for signal/exit evaluation.

        Delegates to the quote's wide-spread-aware logic, then falls back to
        the latest minute-bar close. The position monitor and the live mark
        source call this on the snapshot itself; without it every monitoring
        poll died with AttributeError and no exit ever fired.
        """
        price = self.quote.signal_price(prefer_last_above_spread_pct)
        if price is not None:
            return price
        if self.minute_bar_close and self.minute_bar_close > 0:
            return self.minute_bar_close
        return None

    @property
    def day_open(self) -> float | None:
        return (self.daily_bar or {}).get("o")

    @property
    def prev_close(self) -> float | None:
        return (self.prev_daily_bar or {}).get("c")

    def internal_divergence(self) -> float | None:
        """Max pairwise divergence between quote mid, last trade, and bar close.

        This is the §11.1 sanity check that replaces a second data provider.
        Three views of the same price that disagree by more than 1.5% mean
        one of them is stale or wrong.
        """
        prices = [p for p in (self.mid, self.last_trade, self.minute_bar_close)
                  if p is not None and p > 0]
        if len(prices) < 2:
            return None
        return (max(prices) - min(prices)) / min(prices)


# ----------------------------------------------------------------------
# normalization
# ----------------------------------------------------------------------

def normalize_snapshot(symbol: str, payload: dict[str, Any],
                       now: datetime | None = None) -> SymbolSnapshot | None:
    """Alpaca snapshot -> SymbolSnapshot. None when the payload is unusable."""
    if not payload:
        return None
    now = now or utc_now()

    q = payload.get("latestQuote") or {}
    t = payload.get("latestTrade") or {}
    mb = payload.get("minuteBar") or {}

    quote_ts = parse_alpaca_ts(q.get("t"))
    bid, ask = q.get("bp"), q.get("ap")

    quote = QuoteObservation(
        symbol=symbol,
        source=SOURCE_IEX,
        observed_at=now,
        source_timestamp=quote_ts,
        quote_lag_seconds=age_seconds(quote_ts, now),
        bid=bid if bid and bid > 0 else None,
        ask=ask if ask and ask > 0 else None,
        last=t.get("p") if t.get("p") else None,
        volume=mb.get("v"),
        raw={"bid_size": q.get("bs"), "ask_size": q.get("as")},
    )

    return SymbolSnapshot(
        symbol=symbol,
        observed_at=now,
        quote=quote,
        last_trade=t.get("p") if t.get("p") else None,
        last_trade_at=parse_alpaca_ts(t.get("t")),
        minute_bar_close=mb.get("c"),
        minute_bar_at=parse_alpaca_ts(mb.get("t")),
        daily_bar=payload.get("dailyBar"),
        prev_daily_bar=payload.get("prevDailyBar"),
    )


def normalize_bar(symbol: str, row: dict[str, Any]) -> Bar | None:
    """Alpaca bar row -> Bar. None when the row fails coherence checks."""
    ts = parse_alpaca_ts(row.get("t"))
    if ts is None:
        return None
    try:
        return Bar(
            symbol=symbol,
            source=SOURCE_IEX,
            timeframe=BARS_TIMEFRAME,
            timestamp=ts,
            open=float(row["o"]),
            high=float(row["h"]),
            low=float(row["l"]),
            close=float(row["c"]),
            volume=float(row.get("v", 0)),
            vwap=float(row["vw"]) if row.get("vw") else None,
            trade_count=int(row["n"]) if row.get("n") else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def filter_rth(bars: Iterable[Bar]) -> list[Bar]:
    """Drop every bar outside regular trading hours.

    This is the single most important line in this module for scoring
    correctness. Without it the RVOL baseline is computed over a mix of
    78-bar and 83-bar sessions.
    """
    return [b for b in bars if is_rth(b.timestamp)]


# ----------------------------------------------------------------------
# service
# ----------------------------------------------------------------------

class MarketDataService:
    def __init__(self, api: AlpacaRestClient, db: Database):
        self.api = api
        self.db = db

    # ---- snapshots ------------------------------------------------

    async def snapshots(self, symbols: Sequence[str],
                        persist: bool = False) -> dict[str, SymbolSnapshot]:
        raw = await self.api.get_stock_snapshots(list(symbols))
        now = utc_now()
        out: dict[str, SymbolSnapshot] = {}
        for sym in symbols:
            snap = normalize_snapshot(sym, raw.get(sym) or {}, now)
            if snap is not None:
                out[sym] = snap
        if persist and out:
            await self._persist_observations(out.values())
        return out

    async def _persist_observations(self,
                                    snaps: Iterable[SymbolSnapshot]) -> None:
        rows = []
        for s in snaps:
            rows.append((
                new_uuid(), s.symbol, "EQUITY", SOURCE_IEX,
                iso_utc(s.observed_at),
                iso_utc(s.quote.source_timestamp) if s.quote.source_timestamp else None,
                s.quote.quote_lag_seconds,
                s.quote.bid, s.quote.ask, s.last_trade, s.mid,
                (s.daily_bar or {}).get("v"),
                (s.daily_bar or {}).get("o"), (s.daily_bar or {}).get("h"),
                (s.daily_bar or {}).get("l"), (s.daily_bar or {}).get("c"),
                json.dumps({"spread_pct": spread_pct(s.quote.bid, s.quote.ask)}),
            ))
        await self.db.executemany(
            "INSERT OR IGNORE INTO market_observations("
            "observation_id, symbol, asset_type, source, observed_at, "
            "source_timestamp, quote_lag_seconds, bid, ask, last, mark, volume, "
            "open, high, low, close, raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    # ---- bars -----------------------------------------------------

    async def backfill_bars(self, symbols: Sequence[str], sessions: int = 20,
                            persist: bool = True) -> dict[str, int]:
        """Fetch and store RTH-only 5-minute bars for the last N sessions."""
        days = previous_trading_days(sessions)
        if not days:
            return {}
        start = session_bounds(days[0])[0]        # type: ignore[index]
        end = utc_now()

        raw = await self.api.get_stock_bars(list(symbols), BARS_TIMEFRAME,
                                            start, end)
        counts: dict[str, int] = {}
        for sym, rows in raw.items():
            bars = [b for b in (normalize_bar(sym, r) for r in rows) if b]
            kept = filter_rth(bars)
            counts[sym] = len(kept)
            if persist and kept:
                await self._persist_bars(kept)
        return counts

    async def backfill_missing(self, symbols: Sequence[str],
                               sessions: int = 20,
                               min_bars: int | None = None) -> dict[str, int]:
        """On-demand backfill for newly injected dynamic symbols.

        Discovery admits symbols mid-session, so their history has to arrive
        before they can be scored. Symbols already covered are skipped.
        """
        threshold = min_bars if min_bars is not None else int(
            RTH_BARS_PER_SESSION * sessions * 0.6)
        needed = []
        for sym in symbols:
            row = await self.db.fetchone(
                "SELECT COUNT(*) AS n, MAX(ts) AS last_ts FROM market_bars "
                "WHERE symbol=? AND timeframe=? AND source=?",
                (sym, BARS_TIMEFRAME, SOURCE_IEX),
            )
            have = (row or {}).get("n") or 0
            if have < threshold:
                needed.append(sym)
                continue

            # Enough history is not the same as current history. A symbol
            # backfilled on a previous day passes the count check and then
            # returns a stale RVOL forever, because the numerator needs
            # bars from the CURRENT session.
            last_ts = parse_alpaca_ts((row or {}).get("last_ts"))
            if last_ts is None:
                needed.append(sym)
                continue
            if to_et(last_ts).date() < to_et(utc_now()).date():
                needed.append(sym)
        return await self.backfill_bars(needed, sessions) if needed else {}

    async def _persist_bars(self, bars: Sequence[Bar]) -> None:
        await self.db.executemany(
            "INSERT OR REPLACE INTO market_bars("
            "symbol, source, timeframe, ts, open, high, low, close, volume, "
            "vwap, trade_count, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,'{}')",
            [(b.symbol, b.source, b.timeframe, iso_utc(b.timestamp),
              b.open, b.high, b.low, b.close, b.volume, b.vwap, b.trade_count)
             for b in bars],
        )

    async def load_bars(self, symbol: str, since: datetime | None = None,
                        limit: int = 5000) -> list[Bar]:
        sql = ("SELECT * FROM market_bars WHERE symbol=? AND timeframe=? "
               "AND source=?")
        params: list[Any] = [symbol, BARS_TIMEFRAME, SOURCE_IEX]
        if since is not None:
            sql += " AND ts >= ?"
            params.append(iso_utc(since))
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        rows = await self.db.fetchall(sql, params)
        bars = []
        for r in reversed(rows):
            ts = parse_alpaca_ts(r["ts"])
            if ts is None:
                continue
            bars.append(Bar(
                symbol=r["symbol"], source=r["source"], timeframe=r["timeframe"],
                timestamp=ts, open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], volume=r["volume"], vwap=r["vwap"],
                trade_count=r["trade_count"],
            ))
        return bars

    async def bar_coverage(self, symbol: str) -> dict[str, Any]:
        """Data-density check used by discovery eligibility."""
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
            "FROM market_bars WHERE symbol=? AND timeframe=? AND source=?",
            (symbol, BARS_TIMEFRAME, SOURCE_IEX),
        )
        n = (row or {}).get("n", 0) or 0
        return {
            "symbol": symbol,
            "bars": n,
            "sessions_equivalent": round(n / RTH_BARS_PER_SESSION, 1),
            "first_ts": (row or {}).get("first_ts"),
            "last_ts": (row or {}).get("last_ts"),
        }

    # ---- RVOL -----------------------------------------------------

    async def rvol(self, symbol: str, now: datetime | None = None,
                   window_minutes: int = 15,
                   lookback_sessions: int = 20) -> float | None:
        """§12.4 relative volume, same feed on both sides of the ratio.

        Current window volume divided by the median volume in the SAME clock
        window across prior sessions. Both terms come from IEX, so the ratio
        stays meaningful even though IEX carries only a fraction of
        consolidated volume. Never compare either term to a published
        consolidated figure.
        """
        now = now or utc_now()
        window = clock_window_index(now, window_minutes)
        if window is None:
            return None

        bars = await self.load_bars(
            symbol, since=now - timedelta(days=lookback_sessions * 2 + 10))
        if not bars:
            return None

        today = to_et(now).date()
        current = 0.0
        historical: dict[date, float] = {}

        for b in bars:
            et = to_et(b.timestamp)
            if clock_window_index(b.timestamp, window_minutes) != window:
                continue
            if et.date() == today:
                current += b.volume
            else:
                historical[et.date()] = historical.get(et.date(), 0.0) + b.volume

        if current <= 0 or len(historical) < 5:
            return None
        # Most recent N sessions, ordered by date. The previous
        # constant-key sort happened to preserve insertion order but read
        # as a no-op and depended on it silently.
        by_date = [volume for _, volume in sorted(historical.items())]
        baseline = median_or_none(by_date[-lookback_sessions:])
        if not baseline or baseline <= 0:
            return None
        return current / baseline

    async def session_volume_profile(self, symbol: str,
                                     window_minutes: int = 15,
                                     lookback_sessions: int = 20
                                     ) -> dict[int, float]:
        """Median volume per clock window, for diagnostics and the dashboard."""
        bars = await self.load_bars(symbol, limit=RTH_BARS_PER_SESSION *
                                    (lookback_sessions + 2))
        buckets: dict[int, dict[date, float]] = {}
        for b in bars:
            idx = clock_window_index(b.timestamp, window_minutes)
            if idx is None:
                continue
            d = to_et(b.timestamp).date()
            buckets.setdefault(idx, {}).setdefault(d, 0.0)
            buckets[idx][d] += b.volume
        return {idx: median_or_none(list(days.values())) or 0.0
                for idx, days in sorted(buckets.items())}

    # ---- pre-submit -----------------------------------------------

    async def fresh_quote(self, symbol: str,
                          max_age_seconds: float = 5.0
                          ) -> QuoteObservation | None:
        """§17.4 pre-submit refresh. None if not fresh enough to trade on."""
        raw = await self.api.get_latest_quotes([symbol])
        q = raw.get(symbol)
        if not q:
            return None
        now = utc_now()
        ts = parse_alpaca_ts(q.get("t"))
        age = age_seconds(ts, now)
        bid, ask = q.get("bp"), q.get("ap")
        if safe_mid(bid, ask) is None:
            return None
        if age is None or age > max_age_seconds:
            return None
        return QuoteObservation(
            symbol=symbol, source=SOURCE_IEX, observed_at=now,
            source_timestamp=ts, quote_lag_seconds=age,
            bid=bid, ask=ask,
        )

    async def underlying_at(self, symbol: str,
                            when: datetime) -> float | None:
        """Underlying price at an option quote's timestamp, from stored bars.

        Required by the §5.4 delta adjustment, which must not guess this
        value. Returns None when no bar within 10 minutes exists.
        """
        row = await self.db.fetchone(
            "SELECT close, ts FROM market_bars "
            "WHERE symbol=? AND timeframe=? AND source=? AND ts <= ? "
            "ORDER BY ts DESC LIMIT 1",
            (symbol, BARS_TIMEFRAME, SOURCE_IEX, iso_utc(when)),
        )
        if not row:
            return None
        ts = parse_alpaca_ts(row["ts"])
        if ts is None or (when - ts).total_seconds() > 600:
            return None
        return row["close"]
