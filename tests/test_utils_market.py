"""
Alpha Council v2.4 - utility and market-data tests.

Focused on the two live-test findings: extended-hours bar contamination
and invalid quote shapes.

Place at: tests/test_utils_market.py

Run:
    uv run pytest tests/test_utils_market.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from alpha_council.alpaca.market_data import (
    RTH_BARS_PER_SESSION,
    filter_rth,
    normalize_bar,
    normalize_snapshot,
)
from alpha_council.utils.ids import (
    client_order_id,
    content_hash,
    decision_fragment,
    is_valid_client_order_id,
    occ_key,
)
from alpha_council.utils.math import (
    cost_width_from_reward_risk,
    fast_score,
    freshness_score,
    momentum_score,
    relative_volume_score,
    reward_risk_from_cost_width,
    safe_mid,
    spread_pct,
    weighted_sum,
)
from alpha_council.utils.time import (
    ET,
    age_seconds,
    clock_window_index,
    clock_window_label,
    is_rth,
    is_trading_day,
    parse_alpaca_ts,
    previous_trading_days,
    sessions_remaining,
    windows_per_session,
)

# Monday 2026-08-31, 10:07 ET
MON_1007_ET = datetime(2026, 8, 31, 10, 7, tzinfo=ET)


# ======================================================================
# RTH detection - the extended-hours bug
# ======================================================================

def test_rth_boundaries():
    assert not is_rth(datetime(2026, 8, 31, 9, 29, tzinfo=ET))
    assert is_rth(datetime(2026, 8, 31, 9, 30, tzinfo=ET))
    assert is_rth(datetime(2026, 8, 31, 15, 59, tzinfo=ET))
    assert not is_rth(datetime(2026, 8, 31, 16, 0, tzinfo=ET))


def test_the_2050z_bar_is_rejected():
    """Alpaca returned a QQQ bar stamped 20:50Z on 2026-08-28 against a
    20:00Z close. That is 16:50 ET, after the bell."""
    late = datetime(2026, 8, 28, 20, 50, tzinfo=timezone.utc)
    assert not is_rth(late)


def test_premarket_and_afterhours_rejected():
    assert not is_rth(datetime(2026, 8, 31, 7, 15, tzinfo=ET))
    assert not is_rth(datetime(2026, 8, 31, 18, 45, tzinfo=ET))


def test_weekend_and_holiday_are_not_trading_days():
    assert not is_trading_day(date(2026, 8, 29))     # Saturday
    assert not is_trading_day(date(2026, 8, 30))     # Sunday
    assert not is_trading_day(date(2026, 9, 7))      # Labor Day
    assert is_trading_day(date(2026, 8, 31))
    assert is_trading_day(date(2026, 9, 3))


def test_competition_sessions_remaining():
    assert sessions_remaining(datetime(2026, 8, 31, 10, 0, tzinfo=ET)) == 4
    assert sessions_remaining(datetime(2026, 9, 3, 10, 0, tzinfo=ET)) == 1
    assert sessions_remaining(datetime(2026, 9, 4, 10, 0, tzinfo=ET)) == 0


def test_previous_trading_days_skips_the_weekend():
    days = previous_trading_days(3, before=date(2026, 8, 31))
    assert days == [date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]


# ======================================================================
# clock windows - the RVOL baseline
# ======================================================================

def test_clock_window_index_counts_from_the_open():
    assert clock_window_index(datetime(2026, 8, 31, 9, 30, tzinfo=ET)) == 0
    assert clock_window_index(datetime(2026, 8, 31, 9, 44, tzinfo=ET)) == 0
    assert clock_window_index(datetime(2026, 8, 31, 9, 45, tzinfo=ET)) == 1
    assert clock_window_index(MON_1007_ET) == 2


def test_clock_window_outside_rth_is_none():
    assert clock_window_index(datetime(2026, 8, 31, 8, 0, tzinfo=ET)) is None
    assert clock_window_index(datetime(2026, 8, 31, 16, 30, tzinfo=ET)) is None


def test_full_session_has_26_windows():
    assert windows_per_session(date(2026, 8, 31)) == 26
    assert clock_window_label(2) == "10:00-10:15"


# ======================================================================
# timestamps
# ======================================================================

def test_nanosecond_timestamps_parse():
    ts = parse_alpaca_ts("2026-08-31T14:30:00.123456789Z")
    assert ts is not None and ts.tzinfo is not None
    assert ts.microsecond == 123456


def test_negative_age_clamps_to_zero():
    """Observed -0.08s against Alpaca: local clock skew, not a future quote."""
    now = datetime(2026, 8, 31, 14, 30, 0, tzinfo=timezone.utc)
    future = datetime(2026, 8, 31, 14, 30, 5, tzinfo=timezone.utc)
    assert age_seconds(future, now) == 0.0


# ======================================================================
# quote validity
# ======================================================================

def test_safe_mid_rejects_the_aapl_shape():
    assert safe_mid(300.93, 0.0) is None
    assert safe_mid(0.0, 300.93) is None
    assert safe_mid(101.0, 99.0) is None
    assert safe_mid(100.0, 100.10) == pytest.approx(100.05)


def test_spread_pct_none_on_invalid_quote():
    assert spread_pct(300.93, 0.0) is None
    assert spread_pct(318.02, 319.69) == pytest.approx(0.005238, abs=1e-5)


def test_snapshot_with_zero_ask_yields_no_midpoint():
    snap = normalize_snapshot("AAPL", {
        "latestQuote": {"bp": 300.93, "ap": 0.0, "t": "2026-08-28T20:00:00Z"},
        "latestTrade": {"p": 301.10, "t": "2026-08-28T20:00:00Z"},
    })
    assert snap is not None
    assert snap.quote.ask is None
    assert snap.mid == pytest.approx(301.10)   # falls through to last


def test_internal_divergence_detects_a_stale_leg():
    snap = normalize_snapshot("XYZ", {
        "latestQuote": {"bp": 100.00, "ap": 100.10, "t": "2026-08-31T14:00:00Z"},
        "latestTrade": {"p": 103.50, "t": "2026-08-31T14:00:00Z"},
        "minuteBar": {"c": 100.05, "t": "2026-08-31T14:00:00Z"},
    })
    assert snap is not None
    assert snap.internal_divergence() > 0.015


# ======================================================================
# bars
# ======================================================================

def _bar_row(hour_utc: int, minute: int = 0) -> dict:
    return {"t": f"2026-08-31T{hour_utc:02d}:{minute:02d}:00Z",
            "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.5, "v": 5000, "n": 40}


def test_filter_rth_drops_extended_hours_bars():
    rows = [_bar_row(12, 0),   # 08:00 ET  pre-market
            _bar_row(14, 0),   # 10:00 ET  RTH
            _bar_row(19, 55),  # 15:55 ET  RTH
            _bar_row(20, 50)]  # 16:50 ET  after hours
    bars = [b for b in (normalize_bar("SPY", r) for r in rows) if b]
    assert len(bars) == 4
    kept = filter_rth(bars)
    assert len(kept) == 2


def test_incoherent_bar_is_dropped_not_raised():
    bad = {"t": "2026-08-31T14:00:00Z", "o": 100.0, "h": 98.0,
           "l": 99.0, "c": 100.0, "v": 10}
    assert normalize_bar("SPY", bad) is None


def test_expected_bars_per_session():
    assert RTH_BARS_PER_SESSION == 78


# ======================================================================
# scoring math
# ======================================================================

def test_reward_risk_is_derived_from_cost_width():
    """Tier 1 caps cost/width at 0.55, which implies RR >= 0.82.
    v2.2's hard RR >= 1.20 required cost/width <= 0.4545, which eliminates
    nearly every compliant 0.60/0.33 delta vertical."""
    assert reward_risk_from_cost_width(0.55) == pytest.approx(0.8182, abs=1e-4)
    assert reward_risk_from_cost_width(0.50) == pytest.approx(1.0)
    assert reward_risk_from_cost_width(0.45) == pytest.approx(1.2222, abs=1e-4)
    assert cost_width_from_reward_risk(1.20) == pytest.approx(0.4545, abs=1e-4)


@pytest.mark.parametrize("bad", [0.0, 1.0, 1.5, -0.2])
def test_invalid_cost_width_rejected(bad):
    with pytest.raises(ValueError):
        reward_risk_from_cost_width(bad)


def test_rvol_score_anchors():
    assert relative_volume_score(1.0) == pytest.approx(40.0)
    assert relative_volume_score(2.0) == pytest.approx(70.0)
    assert relative_volume_score(4.0) == pytest.approx(100.0)


def test_momentum_is_symmetric_around_50():
    up = momentum_score(1, 0.004, 0.008, 0.015)
    down = momentum_score(-1, -0.004, -0.008, -0.015)
    assert up == pytest.approx(down)
    assert up > 80


def test_bearish_move_scores_low_for_a_bullish_candidate():
    assert momentum_score(1, -0.006, -0.010, -0.020) < 20


def test_freshness_halves_at_the_half_life():
    assert freshness_score(0, 120) == pytest.approx(100.0)
    assert freshness_score(120, 120) == pytest.approx(50.0)
    assert freshness_score(240, 120) == pytest.approx(25.0)


def test_fast_score_rewards_conviction_in_either_direction():
    """A momentum score of 10 is as interesting as one of 90 at Stage 0."""
    bullish = fast_score(90, 70, 65, 75, 80)
    bearish = fast_score(10, 70, 65, 75, 80)
    assert bullish == pytest.approx(bearish)
    flat = fast_score(50, 70, 65, 75, 80)
    assert flat < bullish


def test_weighted_sum_refuses_to_silently_zero_a_missing_component():
    weights = {"a": 0.5, "b": 0.5}
    assert weighted_sum({"a": 80, "b": 60}, weights) == pytest.approx(70.0)
    with pytest.raises(KeyError, match="missing score components"):
        weighted_sum({"a": 80}, weights)
    assert weighted_sum({"a": 80}, weights, require_complete=False) == 80.0


# ======================================================================
# identifiers
# ======================================================================

def test_client_order_id_shape_and_length():
    cid = client_order_id("dec_abc123def456", revision=0)
    assert is_valid_client_order_id(cid)
    assert len(cid) <= 48
    assert cid.startswith("ac_")
    assert "_r0_" in cid


def test_client_order_id_is_stable_in_its_decision_fragment():
    a = client_order_id("dec_same", 0)
    b = client_order_id("dec_same", 0)
    assert a != b                                   # unique per submission
    assert decision_fragment(a) == decision_fragment(b)


def test_revision_must_be_zero_or_one():
    with pytest.raises(ValueError):
        client_order_id("dec_x", revision=2)


def test_content_hash_ignores_formatting_noise():
    assert content_hash("NVDA  beats\n Q2") == content_hash("nvda beats q2")
    assert content_hash("NVDA beats") != content_hash("NVDA misses")


def test_occ_key_is_provider_independent():
    assert occ_key("spy", "2026-09-18", "call", 750.0) == \
        "SPY|2026-09-18|CALL|750.000"
    assert occ_key("SPY", "2026-09-18", "CALL", 750.0) == \
        occ_key("spy", "2026-09-18", "call", 750.000)
