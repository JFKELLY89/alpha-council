"""
Alpha Council v2.5 - exit rule tests.

The property that matters most: a position stays manageable when the
options feed is unusable and when both LLM providers are down. Every
primary trigger is computable from the underlying alone.

Place at: tests/test_position_monitor.py

Run:
    uv run pytest tests/test_position_monitor.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from alpha_council.execution.position_monitor import (
    ExitDecision,
    MonitoredPosition,
    check_invalidation,
    evaluate_exit,
)
from alpha_council.models.enums import (
    DataConfidence,
    Direction,
    ExitReason,
    StrategyType,
)
from alpha_council.models.trading import InvalidationRule, OptionLeg, OptionStructure
from alpha_council.utils.time import ET

# Tuesday 2026-09-01, 11:00 ET - mid-session, well before the flatten
NOW = datetime(2026, 9, 1, 11, 0, tzinfo=ET)
EXP = date(2026, 9, 18)

RISK_CFG = {
    "exits": {
        "primary": {
            "underlying_target_at_short_strike": True,
            "honor_pm_invalidation_rules": True,
            "time_stop_dte": 2,
        },
        "secondary": {
            "profit_target_pct_of_max": 0.55,
            "premium_stop_pct_of_entry": 0.45,
            "require_data_confidence": ["HIGH", "MEDIUM"],
        },
        "never_require_llm_to_exit": True,
    }
}


def _leg(strike: float, delta: float, option_type: str = "CALL",
         side: str = "BUY") -> OptionLeg:
    letter = "C" if option_type == "CALL" else "P"
    return OptionLeg(
        symbol=f"NVDA260918{letter}{int(strike * 1000):08d}",
        underlying="NVDA", expiration=EXP, option_type=option_type,
        strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=5.00, ask=5.20, raw_mid=5.10, adjusted_mid=5.10,
        quote_lag_seconds=4.0, delta=delta, open_interest=3000, volume=250)


def _structure(bullish: bool = True, expiration: date = EXP,
               dte: int = 17) -> OptionStructure:
    limit, width = 5.20, 10.0
    if bullish:
        legs = [_leg(200.0, 0.60), _leg(210.0, 0.33, side="SELL")]
        strategy = StrategyType.BULL_CALL_DEBIT
        breakeven = 200.0 + limit
    else:
        legs = [_leg(210.0, -0.60, "PUT"),
                _leg(200.0, -0.33, "PUT", side="SELL")]
        strategy = StrategyType.BEAR_PUT_DEBIT
        breakeven = 210.0 - limit

    return OptionStructure(
        structure_id="st_1", symbol="NVDA", strategy=strategy, rank=1,
        expiration=expiration, dte=dte, legs=legs, width=width,
        net_delta=0.27, raw_mid_debit=5.10, adjusted_mid_debit=5.10,
        natural_debit=5.60, staleness_buffer=0.0, initial_limit_debit=limit,
        cost_to_width_ratio=limit / width, max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit, breakeven=breakeven,
        max_quote_lag_seconds=4.0, liquidity_score=82.0,
        delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0)


def _position(bullish: bool = True, expiration: date = EXP,
              rules: list[InvalidationRule] | None = None,
              opened_at: datetime = NOW - timedelta(days=2)
              ) -> MonitoredPosition:
    return MonitoredPosition(
        decision_id="d1", symbol="NVDA",
        structure=_structure(bullish, expiration), qty=2, entry_debit=5.20,
        opened_at=opened_at, invalidation=rules or [])


def _evaluate(position: MonitoredPosition, underlying: float, **kw
              ) -> ExitDecision:
    return evaluate_exit(position, underlying, kw.pop("now", NOW), RISK_CFG,
                         **kw)


# ======================================================================
# no exit
# ======================================================================

def test_healthy_position_stays_open():
    decision = _evaluate(_position(), underlying=204.0)
    assert not decision.should_exit
    assert decision.reason is None


def test_all_primary_triggers_are_evaluated():
    decision = _evaluate(_position(), underlying=204.0)
    for trigger in ("COMPETITION_FLATTEN", "TIME_STOP", "UNDERLYING_TARGET",
                    "UNDERLYING_INVALIDATION"):
        assert trigger in decision.triggers_evaluated


# ======================================================================
# the position is never stranded
# ======================================================================

def test_exits_work_with_no_option_data_at_all():
    """The whole reason exits are underlying-driven.

    Option feed blocked, no mark available, and the position still exits
    correctly on its underlying target.
    """
    decision = _evaluate(_position(), underlying=211.0, spread_mark=None,
                         option_confidence=DataConfidence.BLOCKED)
    assert decision.should_exit
    assert decision.reason is ExitReason.UNDERLYING_TARGET
    assert not decision.advisory


def test_blocked_option_data_does_not_block_the_time_stop():
    decision = _evaluate(_position(expiration=NOW.date() + timedelta(days=1)),
                         underlying=204.0,
                         option_confidence=DataConfidence.BLOCKED)
    assert decision.should_exit
    assert decision.reason is ExitReason.TIME_STOP


def test_unusable_option_data_reports_itself():
    decision = _evaluate(_position(), underlying=204.0, spread_mark=None)
    assert not decision.should_exit
    assert "option data unusable" in decision.detail


# ======================================================================
# underlying target
# ======================================================================

def test_bullish_exits_at_the_short_strike():
    assert not _evaluate(_position(), underlying=209.99).should_exit
    hit = _evaluate(_position(), underlying=210.00)
    assert hit.should_exit and hit.reason is ExitReason.UNDERLYING_TARGET


def test_bearish_inverts_the_target():
    bear = _position(bullish=False)
    assert not _evaluate(bear, underlying=200.01).should_exit
    hit = _evaluate(bear, underlying=200.00)
    assert hit.should_exit and hit.reason is ExitReason.UNDERLYING_TARGET


def test_bearish_does_not_exit_on_an_adverse_rally():
    """A rally against a bear put is a loss, not a profit target."""
    decision = _evaluate(_position(bullish=False), underlying=225.0)
    assert not (decision.should_exit
                and decision.reason is ExitReason.UNDERLYING_TARGET)


# ======================================================================
# time stop
# ======================================================================

def test_time_stop_fires_at_two_dte():
    at_two = _position(expiration=NOW.date() + timedelta(days=2))
    at_three = _position(expiration=NOW.date() + timedelta(days=3))
    assert _evaluate(at_two, underlying=204.0).reason is ExitReason.TIME_STOP
    assert not _evaluate(at_three, underlying=204.0).should_exit


def test_time_stop_outranks_the_advisory_profit_target():
    """Expiration risk is not negotiable against an unrealized gain."""
    position = _position(expiration=NOW.date() + timedelta(days=1))
    decision = _evaluate(position, underlying=204.0, spread_mark=8.00,
                         option_confidence=DataConfidence.HIGH)
    assert decision.reason is ExitReason.TIME_STOP


# ======================================================================
# competition flatten
# ======================================================================

def test_flatten_outranks_everything():
    after = datetime(2026, 9, 3, 15, 46, tzinfo=ET)
    decision = _evaluate(_position(), underlying=204.0, now=after)
    assert decision.should_exit
    assert decision.reason is ExitReason.COMPETITION_FLATTEN


def test_before_flatten_normal_rules_apply():
    before = datetime(2026, 9, 3, 15, 44, tzinfo=ET)
    assert not _evaluate(_position(), underlying=204.0,
                         now=before).should_exit


# ======================================================================
# PM invalidation
# ======================================================================

def test_price_invalidation_fires():
    rules = [InvalidationRule(rule_type="PRICE",
                              description="below prior support",
                              threshold=198.0, comparator="LT")]
    fired = _evaluate(_position(rules=rules), underlying=197.5)
    assert fired.should_exit
    assert fired.reason is ExitReason.UNDERLYING_INVALIDATION
    assert not _evaluate(_position(rules=rules), underlying=198.5).should_exit


def test_vwap_invalidation_requires_a_vwap():
    rules = [InvalidationRule(rule_type="VWAP", description="lost vwap",
                              threshold=203.0, comparator="LT")]
    assert check_invalidation(rules, 204.0, None, NOW, NOW) is None
    assert check_invalidation(rules, 204.0, 202.0, NOW, NOW) is not None


def test_time_invalidation_measures_elapsed_days():
    rules = [InvalidationRule(rule_type="TIME",
                              description="thesis needs 3 days",
                              threshold=3.0, comparator="GTE")]
    opened = NOW - timedelta(days=4)
    assert check_invalidation(rules, 204.0, None, opened, NOW) is not None
    assert check_invalidation(rules, 204.0, None, NOW - timedelta(days=1),
                              NOW) is None


def test_unobservable_rule_types_are_skipped_not_guessed():
    """CATALYST and COMPOSITE need evidence the monitor does not carry."""
    for rule_type in ("CATALYST", "COMPOSITE"):
        rules = [InvalidationRule(rule_type=rule_type, description="x",
                                  threshold=1.0, comparator="LT")]
        assert check_invalidation(rules, 204.0, 204.0, NOW, NOW) is None


def test_rule_without_a_threshold_is_skipped():
    rules = [InvalidationRule(rule_type="PRICE", description="vague")]
    assert check_invalidation(rules, 204.0, None, NOW, NOW) is None


def test_first_firing_rule_wins():
    rules = [
        InvalidationRule(rule_type="PRICE", description="first",
                         threshold=198.0, comparator="LT"),
        InvalidationRule(rule_type="PRICE", description="second",
                         threshold=199.0, comparator="LT"),
    ]
    assert "first" in check_invalidation(rules, 197.0, None, NOW, NOW)


# ======================================================================
# advisory triggers
# ======================================================================

def test_profit_target_needs_good_data():
    position = _position()
    # 55% of $480 max profit = $264/spread -> mark of 7.84
    high = _evaluate(position, underlying=205.0, spread_mark=7.90,
                     option_confidence=DataConfidence.HIGH)
    assert high.should_exit
    assert high.reason is ExitReason.PROFIT_TARGET
    assert high.advisory

    degraded = _evaluate(position, underlying=205.0, spread_mark=7.90,
                         option_confidence=DataConfidence.DEGRADED)
    assert not degraded.should_exit


def test_profit_target_not_reached():
    decision = _evaluate(_position(), underlying=205.0, spread_mark=6.50,
                         option_confidence=DataConfidence.HIGH)
    assert not decision.should_exit


def test_premium_stop_fires_on_a_collapsing_spread():
    # 45% of the 5.20 entry = 2.34
    decision = _evaluate(_position(), underlying=201.0, spread_mark=2.30,
                         option_confidence=DataConfidence.HIGH)
    assert decision.should_exit
    assert decision.reason is ExitReason.PREMIUM_STOP
    assert decision.advisory


def test_premium_stop_not_reached():
    decision = _evaluate(_position(), underlying=203.0, spread_mark=3.00,
                         option_confidence=DataConfidence.HIGH)
    assert not decision.should_exit


def test_medium_confidence_permits_advisory_triggers():
    decision = _evaluate(_position(), underlying=205.0, spread_mark=7.90,
                         option_confidence=DataConfidence.MEDIUM)
    assert decision.should_exit


# ======================================================================
# position helpers
# ======================================================================

def test_unrealized_pnl_arithmetic():
    position = _position()
    assert position.unrealized(6.40) == pytest.approx(240.0)   # 1.20*100*2
    assert position.unrealized(4.20) == pytest.approx(-200.0)


def test_dte_counts_down():
    position = _position(expiration=NOW.date() + timedelta(days=7))
    assert position.dte(NOW) == 7
    assert position.dte(NOW + timedelta(days=5)) == 2


def test_direction_follows_the_strategy():
    assert _position().direction is Direction.BULLISH
    assert _position(bullish=False).direction is Direction.BEARISH
