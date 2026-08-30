"""
Alpha Council v2.4 - options engine tests.

The calibration story lives here: the cost/width constraint must admit
realistic 0.60/0.33 delta verticals, which v2.2's hard RR >= 1.20 did not.

Place at: tests/test_options_engine.py

Run:
    uv run pytest tests/test_options_engine.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from alpha_council.models.enums import Direction, StrategyType
from alpha_council.models.trading import OptionLeg
from alpha_council.options_engine.chain import (
    ChainFilters,
    ChainResult,
    parse_occ_symbol,
)
from alpha_council.options_engine.spreads import (
    SpreadBuilder,
    SpreadFilters,
    leg_liquidity_score,
    round_to_tick,
)
from alpha_council.utils.math import reward_risk_from_cost_width

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)


def _leg(strike: float, delta: float, bid: float, ask: float,
         opt_type: str = "CALL", lag: float = 2.0, oi: int = 5000,
         vol: int = 500) -> OptionLeg:
    mid = (bid + ask) / 2
    letter = "C" if opt_type == "CALL" else "P"
    return OptionLeg(
        symbol=f"SPY260918{letter}{int(strike * 1000):08d}",
        underlying="SPY", expiration=EXP, option_type=opt_type,
        strike=strike, side="BUY", position_intent="buy_to_open",
        bid=bid, ask=ask, raw_mid=mid, adjusted_mid=mid,
        quote_lag_seconds=lag, delta=delta,
        open_interest=oi, volume=vol,
    )


def _chain(legs: list[OptionLeg], spot: float = 769.30) -> ChainResult:
    calls = [leg for leg in legs if leg.option_type == "CALL"]
    puts = [leg for leg in legs if leg.option_type == "PUT"]
    return ChainResult(symbol="SPY", underlying_price=spot, fetched_at=NOW,
                       calls=calls, puts=puts)


def _builder(**kw) -> SpreadBuilder:
    return SpreadBuilder(SpreadFilters(**kw))


# ======================================================================
# OCC parsing
# ======================================================================

def test_parse_occ_symbol():
    parsed = parse_occ_symbol("SPY260918C00750000")
    assert parsed == ("SPY", date(2026, 9, 18), "CALL", 750.0)


def test_parse_occ_handles_variable_length_underlying():
    assert parse_occ_symbol("A260918P00035500") == (
        "A", date(2026, 9, 18), "PUT", 35.5)
    assert parse_occ_symbol("GOOGL260918C00200000") == (
        "GOOGL", date(2026, 9, 18), "CALL", 200.0)


def test_parse_occ_rejects_garbage():
    assert parse_occ_symbol("NOTANOPTION") is None
    assert parse_occ_symbol("SPY261332C00750000") is None   # month 13


# ======================================================================
# the calibration story
# ======================================================================

def test_realistic_vertical_survives_tier1_but_fails_v22():
    """A 0.60/0.33 delta SPY vertical costing ~52% of width.

    Tier 1 (cost/width <= 0.55) admits it. v2.2's RR >= 1.20 required
    cost/width <= 0.4545 and would have rejected it. This single case is
    why the anti-zero-trade fix was necessary.
    """
    long_leg = _leg(750.0, 0.60, 23.30, 23.70)
    short_leg = _leg(760.0, 0.33, 18.20, 18.60)
    res = _builder().build(_chain([long_leg, short_leg]), Direction.BULLISH)

    assert res.structures, res.rejection_counts()
    s = res.best
    assert 0.45 <= s.cost_to_width_ratio <= 0.55
    assert s.reward_risk_ratio < 1.20
    assert s.reward_risk_ratio == pytest.approx(
        reward_risk_from_cost_width(s.cost_to_width_ratio), abs=1e-6)


def test_expensive_spread_rejected_by_cost_to_width():
    long_leg = _leg(750.0, 0.60, 26.00, 26.40)
    short_leg = _leg(760.0, 0.33, 18.20, 18.60)
    res = _builder(max_cost_to_width=0.55).build(
        _chain([long_leg, short_leg]), Direction.BULLISH)
    assert not res.structures
    assert "OPT_COST_TO_WIDTH" in res.rejection_counts()


def test_tier3_admits_what_tier1_rejects():
    # limit 5.70 on a 10-wide spread: c/w 0.57, above Tier 1's 0.55 cap
    # and below Tier 3's 0.62
    long_leg = _leg(750.0, 0.60, 23.80, 24.20)
    short_leg = _leg(760.0, 0.33, 18.20, 18.60)
    chain = _chain([long_leg, short_leg])
    t1 = _builder(max_cost_to_width=0.55).build(chain, Direction.BULLISH)
    t3 = _builder(max_cost_to_width=0.62).build(chain, Direction.BULLISH)
    assert not t1.structures
    assert t3.structures


# ======================================================================
# structure validity
# ======================================================================

def test_bull_call_requires_long_strike_below_short():
    inverted = _builder().build(
        _chain([_leg(760.0, 0.60, 23.30, 23.70),
                _leg(750.0, 0.33, 18.20, 18.60)]),
        Direction.BULLISH)
    assert not inverted.structures


def test_bear_put_uses_puts_and_inverts_strike_order():
    long_put = _leg(760.0, -0.60, 21.60, 22.00, opt_type="PUT")
    short_put = _leg(750.0, -0.33, 16.50, 16.90, opt_type="PUT")
    res = _builder().build(_chain([long_put, short_put]), Direction.BEARISH)
    assert res.structures
    s = res.best
    assert s.strategy is StrategyType.BEAR_PUT_DEBIT
    assert s.long_leg.strike > s.short_leg.strike
    assert s.breakeven == pytest.approx(760.0 - s.initial_limit_debit)


def test_payoff_identity_holds():
    res = _builder().build(
        _chain([_leg(750.0, 0.60, 23.30, 23.70),
                _leg(760.0, 0.33, 18.20, 18.60)]), Direction.BULLISH)
    s = res.best
    d, w = s.initial_limit_debit, s.width
    assert s.max_loss_per_spread == pytest.approx(d * 100)
    assert s.max_profit_per_spread == pytest.approx((w - d) * 100)
    assert s.breakeven == pytest.approx(750.0 + d)


def test_limit_never_exceeds_natural_debit():
    res = _builder().build(
        _chain([_leg(750.0, 0.60, 23.30, 23.70),
                _leg(760.0, 0.33, 18.20, 18.60)]), Direction.BULLISH)
    s = res.best
    assert s.initial_limit_debit <= s.natural_debit


def test_risk_cap_clamps_the_limit():
    res = _builder().build(
        _chain([_leg(750.0, 0.60, 23.30, 23.70),
                _leg(760.0, 0.33, 18.20, 18.60)]),
        Direction.BULLISH, max_debit_allowed=5.00)
    assert res.best.initial_limit_debit <= 5.00


# ======================================================================
# delta bands
# ======================================================================

def test_legs_outside_both_delta_bands_are_rejected():
    res = _builder().build(
        _chain([_leg(700.0, 0.95, 70.00, 70.40),
                _leg(850.0, 0.05, 0.40, 0.44)]), Direction.BULLISH)
    assert not res.structures
    assert res.rejection_counts().get("OPT_DELTA_OUT_OF_BAND") == 2


def test_wider_tier3_bands_admit_more_legs():
    # 0.47 and 0.17 deltas fall outside both Tier 1 bands entirely
    legs = [_leg(750.0, 0.47, 20.00, 20.40), _leg(760.0, 0.17, 14.90, 15.30)]
    t1 = _builder().build(_chain(legs), Direction.BULLISH)
    t3 = _builder(long_delta_min=0.46, long_delta_max=0.80,
                  short_delta_min=0.17, short_delta_max=0.48,
                  max_cost_to_width=0.62).build(_chain(legs), Direction.BULLISH)
    assert not t1.structures
    assert t3.structures


# ======================================================================
# indicative staleness
# ======================================================================

def test_fresh_quotes_take_no_staleness_buffer():
    res = _builder().build(
        _chain([_leg(750.0, 0.60, 23.30, 23.70, lag=3.0),
                _leg(760.0, 0.33, 18.20, 18.60, lag=3.0)]), Direction.BULLISH)
    s = res.best
    assert s.staleness_buffer == 0.0
    assert not s.stale_adjusted


def test_stale_quotes_add_a_recorded_buffer():
    """Age pads the limit even when no delta adjustment was possible."""
    res = _builder().build(
        _chain([_leg(750.0, 0.60, 23.30, 23.70, lag=600.0),
                _leg(760.0, 0.33, 18.20, 18.60, lag=600.0)]), Direction.BULLISH)
    s = res.best
    assert s.staleness_buffer > 0
    assert s.max_quote_lag_seconds == 600.0
    # No underlying reference existed, so nothing was actually adjusted.
    assert not s.stale_adjusted


def test_stale_adjusted_only_when_prices_were_actually_adjusted():
    """The flag tracks whether chain.py delta-adjusted the mids, not age.

    chain.py adjusts only when a stored underlying bar exists at the quote's
    own timestamp. A stale quote with no reference is rejected there, so a
    structure can be padded for age without having been adjusted.
    """
    long_leg = _leg(750.0, 0.60, 23.30, 23.70, lag=600.0).model_copy(
        update={"underlying_price_at_quote": 768.10, "adjusted_mid": 23.86})
    short_leg = _leg(760.0, 0.33, 18.20, 18.60, lag=600.0).model_copy(
        update={"underlying_price_at_quote": 768.10, "adjusted_mid": 18.60})

    res = _builder().build(_chain([long_leg, short_leg], spot=769.30),
                           Direction.BULLISH)
    s = res.best
    assert s.stale_adjusted
    assert s.staleness_buffer > 0
    assert s.underlying_move == pytest.approx(769.30 - 768.10)


def test_learned_fill_bias_adds_to_the_limit():
    chain = _chain([_leg(750.0, 0.60, 23.30, 23.70),
                    _leg(760.0, 0.33, 18.20, 18.60)])
    base = _builder().build(chain, Direction.BULLISH).best
    biased = _builder().build(chain, Direction.BULLISH,
                              fill_bias_buffer=0.08).best
    assert biased.initial_limit_debit >= base.initial_limit_debit
    assert biased.initial_limit_debit <= biased.natural_debit


# ======================================================================
# ranking
# ======================================================================

def test_top_five_returned_with_distinct_strike_pairs():
    legs = [_leg(745.0, 0.66, 26.00, 26.40), _leg(750.0, 0.60, 23.30, 23.70),
            _leg(755.0, 0.54, 20.60, 21.00), _leg(760.0, 0.33, 18.20, 18.60),
            _leg(765.0, 0.28, 15.80, 16.20), _leg(770.0, 0.24, 13.60, 14.00)]
    res = _builder().build(_chain(legs), Direction.BULLISH)
    assert 1 <= len(res.structures) <= 5
    pairs = [(s.long_leg.strike, s.short_leg.strike) for s in res.structures]
    assert len(pairs) == len(set(pairs))
    assert [s.rank for s in res.structures] == list(
        range(1, len(res.structures) + 1))


def test_structures_sorted_by_score():
    legs = [_leg(745.0, 0.66, 26.00, 26.40), _leg(750.0, 0.60, 23.30, 23.70),
            _leg(760.0, 0.33, 18.20, 18.60), _leg(765.0, 0.28, 15.80, 16.20)]
    res = _builder().build(_chain(legs), Direction.BULLISH)
    scores = [s.structure_score for s in res.structures]
    assert scores == sorted(scores, reverse=True)


def test_thin_chain_yields_nothing_not_an_exception():
    res = _builder().build(_chain([_leg(750.0, 0.60, 23.30, 23.70)]),
                           Direction.BULLISH)
    assert res.structures == []
    assert "OPT_CHAIN_TOO_THIN" in res.rejection_counts()


# ======================================================================
# liquidity scoring
# ======================================================================

def test_tight_liquid_leg_scores_higher_than_wide_illiquid():
    good = leg_liquidity_score(_leg(750.0, 0.60, 23.30, 23.40, oi=8000,
                                    vol=1500), 0.15)
    poor = leg_liquidity_score(_leg(750.0, 0.60, 20.00, 22.60, oi=90,
                                    vol=6), 0.15)
    assert good > poor
    assert good > 80


def test_stale_quote_lowers_the_liquidity_score():
    fresh = leg_liquidity_score(_leg(750.0, 0.60, 23.30, 23.40, lag=5.0), 0.15)
    stale = leg_liquidity_score(_leg(750.0, 0.60, 23.30, 23.40, lag=1100.0), 0.15)
    assert fresh > stale


def test_round_to_tick():
    assert round_to_tick(5.4449) == 5.44
    assert round_to_tick(5.4451) == 5.45


# ======================================================================
# filter construction from config
# ======================================================================

def test_filters_read_tier_config():
    tier = {"dte": [5, 30], "min_open_interest": 100, "min_volume": 10,
            "max_leg_spread_pct": 0.20, "long_delta": [0.48, 0.78],
            "short_delta": [0.18, 0.46], "max_cost_to_width": 0.60}
    options = {"fresh_quote_seconds": 45, "max_quote_lag_seconds": 900,
               "structures_returned": 5}
    cf = ChainFilters.from_tier(tier, options)
    sf = SpreadFilters.from_config(tier, options)
    assert (cf.dte_min, cf.dte_max) == (5, 30)
    assert cf.min_open_interest == 100
    assert cf.fresh_quote_seconds == 45
    assert sf.max_cost_to_width == 0.60
    assert sf.long_delta_max == 0.78
