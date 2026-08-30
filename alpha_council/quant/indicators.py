"""
Alpha Council v2.4 - technical indicators.

Everything here is computed from RTH-only stored bars. No indicator reaches
outside the bar table, so replay against frozen rows produces identical
numbers to a live scan.

Place at: alpha_council/quant/indicators.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from alpha_council.models.market import Bar
from alpha_council.utils.math import safe_div
from alpha_council.utils.time import to_et, utc_now

BARS_PER_5MIN = 1
BARS_PER_15MIN = 3
BARS_PER_60MIN = 12


@dataclass(slots=True)
class IndicatorSet:
    """Everything the scanner needs from price history for one symbol."""

    symbol: str
    as_of: datetime
    last_price: float

    r5: float = 0.0
    r15: float = 0.0
    r60: float = 0.0
    r_day: float = 0.0

    ema9: float | None = None
    ema20: float | None = None
    vwap: float | None = None
    day_open: float | None = None
    prev_close: float | None = None

    session_volume: float = 0.0
    window_volume: float = 0.0
    rvol: float | None = None

    bars_used: int = 0
    session_bars: int = 0
    data_gaps: int = 0
    key_metrics: dict[str, float] = field(default_factory=dict)

    # ---- derived flags ------------------------------------------------

    @property
    def above_vwap(self) -> bool:
        return self.vwap is not None and self.last_price > self.vwap

    @property
    def ema_aligned_bullish(self) -> bool:
        return (self.ema9 is not None and self.ema20 is not None
                and self.ema9 > self.ema20)

    @property
    def above_day_open(self) -> bool:
        return self.day_open is not None and self.last_price > self.day_open

    @property
    def sufficient_history(self) -> bool:
        """Momentum over 60 minutes needs 12 bars; anything less is noise."""
        return self.bars_used >= BARS_PER_60MIN + 1


def _pct_return(bars: Sequence[Bar], lookback: int) -> float:
    """Close-to-close return over `lookback` bars."""
    if len(bars) < lookback + 1:
        return 0.0
    return safe_div(bars[-1].close - bars[-1 - lookback].close,
                    bars[-1 - lookback].close)


def ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    out = sum(values[:period]) / period
    for v in values[period:]:
        out = v * k + out * (1 - k)
    return out


def session_vwap(bars: Sequence[Bar]) -> float | None:
    """Volume-weighted average of typical price across the current session."""
    total_pv = total_v = 0.0
    for b in bars:
        price = b.vwap if b.vwap is not None else b.typical_price
        total_pv += price * b.volume
        total_v += b.volume
    return safe_div(total_pv, total_v, default=0.0) or None


def split_session(bars: Sequence[Bar],
                  now: datetime | None = None) -> tuple[list[Bar], list[Bar]]:
    """(current session bars, all bars). Session is the latest ET date present.

    Uses the latest date in the data rather than today's date, so the same
    code path works after hours and in replay.
    """
    if not bars:
        return [], []
    latest_date = to_et(bars[-1].timestamp).date()
    session = [b for b in bars if to_et(b.timestamp).date() == latest_date]
    return session, list(bars)


def compute(symbol: str, bars: Sequence[Bar],
            rvol: float | None = None,
            now: datetime | None = None) -> IndicatorSet | None:
    """Build an IndicatorSet from ordered ascending RTH bars."""
    if not bars:
        return None
    now = now or utc_now()
    session_bars, all_bars = split_session(bars, now)
    last = all_bars[-1]

    closes = [b.close for b in all_bars]
    session_vol = sum(b.volume for b in session_bars)

    # Missing 5-minute windows inside the session indicate thin IEX quoting.
    expected = len(session_bars)
    gaps = 0
    for prev, nxt in zip(session_bars, session_bars[1:]):
        delta_min = (nxt.timestamp - prev.timestamp).total_seconds() / 60
        if delta_min > 5.5:
            gaps += int(delta_min // 5) - 1

    ind = IndicatorSet(
        symbol=symbol,
        as_of=now,
        last_price=last.close,
        r5=_pct_return(all_bars, BARS_PER_5MIN),
        r15=_pct_return(all_bars, BARS_PER_15MIN),
        r60=_pct_return(all_bars, BARS_PER_60MIN),
        ema9=ema(closes, 9),
        ema20=ema(closes, 20),
        vwap=session_vwap(session_bars),
        day_open=session_bars[0].open if session_bars else None,
        session_volume=session_vol,
        window_volume=sum(b.volume for b in session_bars[-BARS_PER_15MIN:]),
        rvol=rvol,
        bars_used=len(all_bars),
        session_bars=expected,
        data_gaps=gaps,
    )
    if ind.day_open:
        ind.r_day = safe_div(ind.last_price - ind.day_open, ind.day_open)
    ind.key_metrics = {
        "r5": round(ind.r5, 6),
        "r15": round(ind.r15, 6),
        "r60": round(ind.r60, 6),
        "r_day": round(ind.r_day, 6),
        "session_volume": ind.session_volume,
        "data_gaps": ind.data_gaps,
    }
    return ind


def relative_strength(candidate: IndicatorSet,
                      benchmark: IndicatorSet) -> tuple[float, float]:
    """(rs15, rs60): candidate return minus benchmark return."""
    return (candidate.r15 - benchmark.r15, candidate.r60 - benchmark.r60)


def benchmark_aligned(candidate_direction: int,
                      benchmark: IndicatorSet) -> bool:
    """Does the benchmark move the same way the candidate wants to?"""
    bench_sign = 1 if benchmark.r15 >= 0 else -1
    return bench_sign == candidate_direction
