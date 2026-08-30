"""
Alpha Council v2.4 - scoring math.

Every score in the system lands on 0-100 and every formula in the spec is
implemented once, here, so the scanner, the options engine, and the tests
cannot drift apart.

Place at: alpha_council/utils/math.py
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence


def clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def safe_div(numerator: float, denominator: float,
             default: float = 0.0, epsilon: float = 1e-9) -> float:
    if abs(denominator) < epsilon:
        return default
    return numerator / denominator


def safe_mid(bid: float | None, ask: float | None) -> float | None:
    """Midpoint, or None for a one-sided or crossed quote.

    Alpaca returned AAPL bid=300.93 ask=0 after the close on 2026-08-28.
    A naive midpoint yields 150.47 from that.
    """
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2


def spread_pct(bid: float | None, ask: float | None,
               floor: float = 0.01) -> float | None:
    mid = safe_mid(bid, ask)
    if mid is None:
        return None
    return (ask - bid) / max(mid, floor)  # type: ignore[operator]


def pct_change(new: float, old: float) -> float:
    return safe_div(new - old, abs(old))


# ----------------------------------------------------------------------
# score primitives
# ----------------------------------------------------------------------

def tanh_score(value: float, scale: float, center: float = 50.0,
               amplitude: float = 50.0) -> float:
    """center +/- amplitude * tanh(value / scale), clipped to 0-100."""
    return clip(center + amplitude * math.tanh(safe_div(value, scale)))


def log_score(value: float, saturation: float) -> float:
    """0-100 on a log1p curve that reaches 100 at `saturation`."""
    if value <= 0:
        return 0.0
    return 100.0 * clip(math.log1p(value) / math.log1p(saturation), 0.0, 1.0)


def linear_decay_score(value: float, max_value: float) -> float:
    """100 at zero, 0 at max_value."""
    return 100.0 * clip(1.0 - safe_div(value, max_value), 0.0, 1.0)


def proximity_score(value: float, target: float, tolerance: float) -> float:
    """100 at the target, 0 once `tolerance` away."""
    return 100.0 * clip(1.0 - safe_div(abs(value - target), tolerance), 0.0, 1.0)


def weighted_sum(components: dict[str, float],
                 weights: dict[str, float],
                 require_complete: bool = True) -> float:
    """Weighted score. Missing components are an error, not a silent zero.

    A silently-zeroed component looks like a legitimately weak score, which
    is exactly the failure mode that hides a broken feature calculation.
    """
    missing = set(weights) - set(components)
    if missing and require_complete:
        raise KeyError(f"missing score components: {sorted(missing)}")
    total_weight = sum(w for k, w in weights.items() if k in components)
    if total_weight <= 0:
        return 0.0
    score = sum(components[k] * w for k, w in weights.items() if k in components)
    return clip(score / total_weight)


# ----------------------------------------------------------------------
# spec formulas (§12)
# ----------------------------------------------------------------------

def momentum_score(d: int, r5: float, r15: float, r60: float) -> float:
    """§12.3. d is +1 bullish, -1 bearish."""
    return clip(
        50.0
        + 20.0 * math.tanh(safe_div(d * r5, 0.004))
        + 18.0 * math.tanh(safe_div(d * r15, 0.008))
        + 12.0 * math.tanh(safe_div(d * r60, 0.015))
    )


def relative_volume_score(rvol: float) -> float:
    """§12.4. ~1x -> 40, 2x -> 70, 4x -> 100."""
    return clip(40.0 + 30.0 * math.log2(max(rvol, 0.25)))


def relative_strength_score(d: int, rs15: float, rs60: float) -> float:
    return clip(
        50.0
        + 25.0 * math.tanh(safe_div(d * rs15, 0.005))
        + 25.0 * math.tanh(safe_div(d * rs60, 0.010))
    )


def trend_regime_score(above_vwap: bool, ema_aligned: bool,
                       above_open: bool, benchmark_aligned: bool,
                       bearish: bool = False) -> float:
    """Four equally weighted components. Conditions invert for bearish."""
    flags = [above_vwap, ema_aligned, above_open, benchmark_aligned]
    if bearish:
        flags = [not f for f in flags]
    return 25.0 * sum(flags)


def technical_direction(r5: float, r15: float, r60: float,
                        rs15: float, rs60: float,
                        above_vwap: bool, ema_aligned: bool,
                        above_open: bool, benchmark_aligned: bool) -> float:
    """§12.3. Signed in [-1, +1]."""
    mom = math.tanh(safe_div(0.40 * r5 + 0.35 * r15 + 0.25 * r60, 0.01))
    rs = math.tanh(safe_div(0.50 * rs15 + 0.50 * rs60, 0.01))
    trend = statistics.mean([
        1.0 if above_vwap else -1.0,
        1.0 if ema_aligned else -1.0,
        1.0 if above_open else -1.0,
        1.0 if benchmark_aligned else -1.0,
    ])
    return max(-1.0, min(1.0, 0.50 * mom + 0.30 * rs + 0.20 * trend))


def combined_direction(tech: float, catalyst_signed: float | None,
                       catalyst_weight: float = 0.35) -> float:
    """EVENT blends in the catalyst. MOMENTUM passes tech straight through.

    catalyst_signed of None means MOMENTUM: no catalyst exists and none is
    invented (spec §12.3).
    """
    if catalyst_signed is None:
        return max(-1.0, min(1.0, tech))
    blended = (1.0 - catalyst_weight) * tech + catalyst_weight * catalyst_signed
    return max(-1.0, min(1.0, blended))


def fast_score(momentum: float, relative_volume: float,
               relative_strength: float, trend_regime: float,
               discovery_boost: float,
               weights: dict[str, float] | None = None) -> float:
    """§12.1 Stage-0 FastScore.

    The first term rewards directional conviction in either direction:
    a momentum score of 10 is as interesting as one of 90.
    """
    w = weights or {
        "directional_conviction": 0.30,
        "relative_volume": 0.25,
        "relative_strength": 0.20,
        "trend_regime": 0.15,
        "discovery_boost": 0.10,
    }
    conviction = clip(abs(momentum - 50.0) * 2.0)
    return clip(
        w["directional_conviction"] * conviction
        + w["relative_volume"] * clip(relative_volume)
        + w["relative_strength"] * clip(relative_strength)
        + w["trend_regime"] * clip(trend_regime)
        + w["discovery_boost"] * clip(discovery_boost)
    )


def freshness_score(age_minutes: float, half_life_minutes: float) -> float:
    """§10.2. 100 * 2 ** (-age / half_life)."""
    if half_life_minutes <= 0:
        return 0.0
    return clip(100.0 * (2.0 ** (-max(0.0, age_minutes) / half_life_minutes)))


def surprise_score(actual: float, expected: float,
                   sigma: float | None = None) -> float:
    """§10.6. Never fabricates a consensus; caller passes 50 if none exists."""
    if sigma is not None and sigma > 1e-9:
        z = abs(actual - expected) / sigma
        return clip(100.0 * math.tanh(z / 2.0))
    denom = max(abs(expected), 1e-9)
    return clip(100.0 * math.tanh((abs(actual - expected) / denom) / 0.05))


# ----------------------------------------------------------------------
# options math (§13)
# ----------------------------------------------------------------------

def reward_risk_from_cost_width(cost_to_width: float) -> float:
    """RR = (1 - c/w) / (c/w).

    Reward/risk is derived, never configured. Constraining cost/width is the
    same constraint in a form that maps onto what a chain can actually
    produce; v2.2's hard RR >= 1.20 eliminated nearly every compliant
    0.60/0.33 delta vertical before any other gate was consulted.
    """
    if cost_to_width <= 0 or cost_to_width >= 1:
        raise ValueError(f"cost_to_width must be in (0,1), got {cost_to_width}")
    return (1.0 - cost_to_width) / cost_to_width


def cost_width_from_reward_risk(rr: float) -> float:
    if rr <= 0:
        raise ValueError(f"reward_risk must be positive, got {rr}")
    return 1.0 / (1.0 + rr)


def delta_adjust(raw_mid: float, delta: float, underlying_move: float) -> float:
    """§5.4. Gamma is ignored deliberately; document the approximation."""
    return max(0.0, raw_mid + delta * underlying_move)


def dte_fit_score(dte: int, target: int = 14, floor: float = 60.0) -> float:
    return max(floor, 100.0 - 5.0 * abs(dte - target))


def delta_fit_score(long_delta: float, short_delta: float,
                    long_target: float = 0.60, short_target: float = 0.33,
                    long_tol: float = 0.20, short_tol: float = 0.18) -> float:
    return (proximity_score(abs(long_delta), long_target, long_tol)
            + proximity_score(abs(short_delta), short_target, short_tol)) / 2.0


def rr_score(reward_risk: float) -> float:
    return clip(60.0 * reward_risk)


def freshness_bucket_score(lag_seconds: float) -> float:
    """§13.5 discrete freshness buckets."""
    if lag_seconds <= 60:
        return 100.0
    if lag_seconds <= 300:
        return 80.0
    if lag_seconds <= 900:
        return 60.0
    if lag_seconds <= 1200:
        return 30.0
    return 0.0


# ----------------------------------------------------------------------
# statistics
# ----------------------------------------------------------------------

def median_or_none(values: Sequence[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.median(clean) if clean else None


def percentile(values: Sequence[float], q: float) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    idx = min(len(clean) - 1, int(round(q * (len(clean) - 1))))
    return clean[idx]


def rolling_median(values: Sequence[float], window: int) -> float | None:
    return median_or_none(list(values)[-window:]) if values else None
