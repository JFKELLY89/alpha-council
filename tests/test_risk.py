"""
Alpha Council v2.4 - Risk Constitution tests.

Spec §22 requires these by name. An untested limit is an unenforced limit.

Place at: tests/test_risk.py

Run:
    uv run pytest tests/test_risk.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from alpha_council.models.enums import (
    DataConfidence,
    Direction,
    RiskDecision,
    Severity,
    StrategyType,
    Verdict,
)
from alpha_council.models.trading import OptionLeg, OptionStructure
from alpha_council.risk.constitution import (
    BlackoutWindow,
    PortfolioState,
    RiskConstitution,
    TradeRequest,
    load_blackouts,
    sector_of,
)
from alpha_council.risk.position_sizing import (
    max_qty_under_portfolio_limits,
    size_position,
)
from alpha_council.utils.time import ET

# Monday 2026-08-31, 10:30 ET - inside RTH, well before the cutoff
NOW = datetime(2026, 8, 31, 10, 30, tzinfo=ET)
EXP = date(2026, 9, 18)

RISK_CFG = {
    "paper_only": True,
    "hard": {
        "max_risk_per_trade_pct": 2.0,
        "max_total_open_option_risk_pct": 10.0,
        "max_sector_open_risk_pct": 4.0,
        "max_concurrent_positions": 5,
        "max_daily_drawdown_pct": 5.0,
        "max_competition_peak_drawdown_pct": 12.0,
        "min_dte": 3,
        "no_0dte": True,
        "new_trade_cutoff_et": "15:20",
    },
    "quality": {"target_risk_per_trade_pct": 1.25},
}

SCORING_CFG = {
    "tiers": {1: {"pm_confidence_floor": 0.60, "final_score_floor": 68.0,
                  "max_cost_to_width": 0.55}},
    "liquidity_floor": {"min_open_interest": 75, "min_volume": 5,
                        "max_leg_spread_pct": 0.22},
}


def _leg(strike: float, delta: float, bid: float, ask: float,
         side: str = "BUY", oi: int = 5000) -> OptionLeg:
    mid = (bid + ask) / 2
    return OptionLeg(
        symbol=f"SPY260918C{int(strike * 1000):08d}", underlying="SPY",
        expiration=EXP, option_type="CALL", strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=bid, ask=ask, raw_mid=mid, adjusted_mid=mid,
        quote_lag_seconds=3.0, delta=delta, open_interest=oi, volume=400,
    )


def _structure(limit: float = 5.20, width: float = 10.0,
               dte: int = 18, **kw) -> OptionStructure:
    long_leg = _leg(750.0, 0.60, 23.30, 23.70)
    short_leg = _leg(750.0 + width, 0.33, 18.20, 18.60, side="SELL")
    base = dict(
        structure_id="st_1", symbol="SPY",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=1, expiration=EXP,
        dte=dte, legs=[long_leg, short_leg], width=width, net_delta=0.27,
        raw_mid_debit=limit - 0.10, adjusted_mid_debit=limit - 0.10,
        natural_debit=limit + 0.30,
        staleness_buffer=0.0, initial_limit_debit=limit,
        # natural must sit at or above the limit; the model enforces it
        cost_to_width_ratio=limit / width,
        max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit,
        breakeven=750.0 + limit, max_quote_lag_seconds=3.0,
        liquidity_score=85.0, delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0,
    )
    return OptionStructure(**{**base, **kw})


def _request(**kw) -> TradeRequest:
    base = dict(
        decision_id="dec_1", symbol="SPY", sector="INDEX",
        direction=Direction.BULLISH, structure=_structure(),
        desired_risk_pct=1.25, pm_confidence=0.72,
        red_team_verdict=Verdict.PASS, red_team_max_risk_pct=1.5,
        equity_data_confidence=DataConfidence.HIGH,
        option_data_confidence=DataConfidence.HIGH,
        final_opportunity_score=74.0, market_open=True,
    )
    return TradeRequest(**{**base, **kw})


def _portfolio(**kw) -> PortfolioState:
    base = dict(equity=100_000.0, day_start_equity=100_000.0,
                peak_equity=100_000.0)
    return PortfolioState(**{**base, **kw})


def _rc(blackouts=()) -> RiskConstitution:
    return RiskConstitution(RISK_CFG, SCORING_CFG, blackouts)


def _rule_ids(ev) -> set[str]:
    return {v.rule_id for v in ev.violations}


# ======================================================================
# the happy path
# ======================================================================

def test_accept_valid_spread():
    ev = _rc().evaluate(_request(), _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.APPROVE
    assert ev.approved_qty == 2          # 1.25% of 100k = $1,250 / $520
    assert ev.approved_max_loss == pytest.approx(1040.0)
    assert not [v for v in ev.violations if v.severity is Severity.BLOCK]


# ======================================================================
# HALT conditions
# ======================================================================

def test_reject_live_mode():
    ev = _rc().evaluate(_request(), _portfolio(), paper_mode=False, now=NOW)
    assert ev.decision is RiskDecision.HALT
    assert "RISK_PAPER_MODE" in _rule_ids(ev)
    assert ev.approved_qty == 0


def test_halt_daily_drawdown():
    p = _portfolio(equity=94_500.0, day_start_equity=100_000.0)
    ev = _rc().evaluate(_request(), p, now=NOW)
    assert ev.decision is RiskDecision.HALT
    assert "RISK_DAILY_DRAWDOWN" in _rule_ids(ev)
    assert ev.daily_drawdown_pct == pytest.approx(5.5)


def test_halt_competition_drawdown():
    p = _portfolio(equity=87_000.0, day_start_equity=88_000.0,
                   peak_equity=100_000.0)
    ev = _rc().evaluate(_request(), p, now=NOW)
    assert ev.decision is RiskDecision.HALT
    assert "RISK_COMPETITION_DRAWDOWN" in _rule_ids(ev)


def test_drawdown_just_inside_the_limit_does_not_halt():
    p = _portfolio(equity=95_100.0, day_start_equity=100_000.0)
    ev = _rc().evaluate(_request(), p, now=NOW)
    assert ev.decision is not RiskDecision.HALT


# ======================================================================
# eligibility
# ======================================================================

def test_reject_claude_veto():
    ev = _rc().evaluate(
        _request(red_team_verdict=Verdict.VETO, red_team_max_risk_pct=0.0),
        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_RED_TEAM_VETO" in _rule_ids(ev)


def test_veto_cannot_be_overridden_by_high_confidence():
    ev = _rc().evaluate(
        _request(red_team_verdict=Verdict.VETO, red_team_max_risk_pct=0.0,
                 pm_confidence=0.99, final_opportunity_score=99.0),
        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT


def test_reject_stale_alpaca():
    ev = _rc().evaluate(
        _request(equity_data_confidence=DataConfidence.BLOCKED),
        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_DATA_BLOCKED" in _rule_ids(ev)


def test_reject_blocked_option_data():
    ev = _rc().evaluate(
        _request(option_data_confidence=DataConfidence.BLOCKED),
        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT


def test_reject_after_cutoff():
    late = datetime(2026, 8, 31, 15, 25, tzinfo=ET)
    ev = _rc().evaluate(_request(), _portfolio(), now=late)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_AFTER_CUTOFF" in _rule_ids(ev)


def test_reject_market_closed():
    ev = _rc().evaluate(_request(market_open=False), _portfolio(), now=NOW)
    assert "RISK_MARKET_CLOSED" in _rule_ids(ev)


def test_reject_low_candidate_score():
    ev = _rc().evaluate(_request(final_opportunity_score=61.0),
                        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_SCORE_FLOOR" in _rule_ids(ev)


def test_reject_low_pm_confidence():
    ev = _rc().evaluate(_request(pm_confidence=0.51), _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_PM_CONFIDENCE" in _rule_ids(ev)


# ======================================================================
# blackout windows
# ======================================================================

def test_reject_inside_event_blackout():
    window = BlackoutWindow(
        name="Employment Situation", source="BLS",
        timestamp_et=datetime(2026, 8, 31, 10, 30, tzinfo=ET),
        pre_block_minutes=15, post_block_minutes=5)
    ev = _rc([window]).evaluate(_request(), _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_EVENT_BLACKOUT" in _rule_ids(ev)


def test_outside_blackout_is_fine():
    window = BlackoutWindow(
        name="Employment Situation", source="BLS",
        timestamp_et=datetime(2026, 8, 31, 8, 30, tzinfo=ET))
    ev = _rc([window]).evaluate(_request(), _portfolio(), now=NOW)
    assert "RISK_EVENT_BLACKOUT" not in _rule_ids(ev)


def test_symbol_scoped_blackout_spares_other_symbols():
    window = BlackoutWindow(
        name="NVDA earnings", source="earnings",
        timestamp_et=datetime(2026, 8, 31, 10, 30, tzinfo=ET),
        symbols=["NVDA"])
    ev = _rc([window]).evaluate(_request(symbol="SPY"), _portfolio(), now=NOW)
    assert "RISK_EVENT_BLACKOUT" not in _rule_ids(ev)


def test_load_blackouts_from_config():
    windows = load_blackouts({"events": [
        {"name": "Trade Balance", "source": "BEA",
         "timestamp_et": "2026-09-03T08:30:00-04:00",
         "pre_block_minutes": 15, "post_block_minutes": 5},
        {"name": "broken", "source": "x"},
    ]})
    assert len(windows) == 1
    assert windows[0].name == "Trade Balance"


# ======================================================================
# structure checks
# ======================================================================

def test_reject_bad_reward_risk():
    """Enforced through cost/width, which is the same constraint."""
    ev = _rc().evaluate(_request(structure=_structure(limit=6.00)),
                        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_COST_TO_WIDTH" in _rule_ids(ev)


def test_reject_short_dte():
    ev = _rc().evaluate(_request(structure=_structure(dte=2)),
                        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_DTE_OUT_OF_BOUNDS" in _rule_ids(ev)


def test_reject_illiquid_leg():
    thin = _structure()
    thin.legs[1].open_interest = 40
    ev = _rc().evaluate(_request(structure=thin), _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_LEG_OPEN_INTEREST" in _rule_ids(ev)


def test_liquidity_floor_is_below_every_tier():
    """Tier 3 sets min OI 75; the absolute floor must not exceed it."""
    floor = SCORING_CFG["liquidity_floor"]["min_open_interest"]
    assert floor <= 75


# ======================================================================
# sizing and portfolio limits
# ======================================================================

def test_resize_oversized_trade():
    """Claude's cap resizes only when Claude asked for a change (MODIFY)."""
    ev = _rc().evaluate(_request(desired_risk_pct=2.0,
                                 red_team_verdict=Verdict.MODIFY,
                                 red_team_max_risk_pct=0.9),
                        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.RESIZE
    assert ev.approved_qty < ev.requested_qty
    assert ev.approved_qty >= 1


def test_calibration_trade_waives_quality_floors_only():
    """§1.5 lifecycle tests have no PM and no score by construction, so
    the PM-confidence and opportunity-score QUALITY floors do not apply.
    Nothing else is waived."""
    ev = _rc().evaluate(_request(pm_confidence=0.0,
                                 final_opportunity_score=0.0,
                                 is_calibration_trade=True),
                        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.APPROVE

    # The identical request WITHOUT the flag dies on both floors.
    ev = _rc().evaluate(_request(pm_confidence=0.0,
                                 final_opportunity_score=0.0),
                        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    rule_ids = {v.rule_id for v in ev.violations}
    assert {"RISK_PM_CONFIDENCE", "RISK_SCORE_FLOOR"} <= rule_ids


def test_calibration_trade_hard_gates_still_bind():
    """The waiver covers quality opinions, never safety: a VETO, the
    cutoff, and the drawdown halt all reject a calibration trade too."""
    veto = _rc().evaluate(_request(is_calibration_trade=True,
                                   pm_confidence=0.0,
                                   final_opportunity_score=0.0,
                                   red_team_verdict=Verdict.VETO,
                                   red_team_max_risk_pct=0.0),
                          _portfolio(), now=NOW)
    assert veto.decision is RiskDecision.REJECT
    assert any(v.rule_id == "RISK_RED_TEAM_VETO" for v in veto.violations)

    late = NOW.replace(hour=15, minute=30)
    after_cutoff = _rc().evaluate(_request(is_calibration_trade=True,
                                           pm_confidence=0.0,
                                           final_opportunity_score=0.0),
                                  _portfolio(), now=late)
    assert after_cutoff.decision is RiskDecision.REJECT
    assert any(v.rule_id == "RISK_AFTER_CUTOFF"
               for v in after_cutoff.violations)

    drawn_down = _portfolio(equity=94_000.0)
    halted = _rc().evaluate(_request(is_calibration_trade=True,
                                     pm_confidence=0.0,
                                     final_opportunity_score=0.0),
                            drawn_down, now=NOW)
    assert halted.decision is RiskDecision.HALT


def test_pass_verdict_cap_is_ignored():
    """On PASS the recommendation is context, not a constraint: no shadow
    variant exists for it, so honouring it would produce a size change the
    attribution decomposition cannot assign to anyone."""
    ev = _rc().evaluate(_request(desired_risk_pct=1.25,
                                 red_team_verdict=Verdict.PASS,
                                 red_team_max_risk_pct=0.5),
                        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.APPROVE
    assert ev.approved_qty == ev.requested_qty


def test_hard_cap_binds_above_two_percent():
    """The PM model caps desired risk at 2.0, so the hard cap is the last
    line rather than the usual one. It still has to hold."""
    sizing = size_position(equity=100_000.0, desired_risk_pct=5.0,
                           max_loss_per_spread=520.0)
    assert sizing.binding_cap == "hard_cap_2pct"
    assert sizing.approved_risk_dollars <= 2000.0


def test_red_team_can_shrink_but_not_grow():
    smaller = size_position(100_000.0, 1.25, 520.0, red_team_max_risk_pct=0.5)
    assert smaller.binding_cap == "red_team"
    assert smaller.approved_qty == 0    # $500 budget, $520 per spread

    larger = size_position(100_000.0, 1.25, 520.0, red_team_max_risk_pct=5.0)
    assert larger.binding_cap == "requested"
    assert larger.approved_qty == 2


def test_reject_sector_concentration():
    p = _portfolio(sector_risk_dollars={"INDEX": 3_900.0})
    ev = _rc().evaluate(_request(), p, now=NOW)
    assert ev.decision in (RiskDecision.REJECT, RiskDecision.RESIZE)
    assert ev.sector_risk_pct_after <= 4.0


def test_reject_total_open_risk():
    p = _portfolio(open_risk_dollars=9_900.0)
    ev = _rc().evaluate(_request(), p, now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_PORTFOLIO_FULL" in _rule_ids(ev) or ev.approved_qty == 0


def test_reject_max_positions():
    ev = _rc().evaluate(_request(), _portfolio(open_position_count=5), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_MAX_POSITIONS" in _rule_ids(ev)


def test_reject_duplicate_order():
    p = _portfolio(open_decision_ids={"dec_1"})
    ev = _rc().evaluate(_request(), p, now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_DUPLICATE_ORDER" in _rule_ids(ev)


def test_qty_zero_when_a_spread_costs_more_than_the_budget():
    expensive = _structure(limit=5.20, width=10.0)
    ev = _rc().evaluate(
        _request(structure=expensive, desired_risk_pct=0.4),
        _portfolio(), now=NOW)
    assert ev.decision is RiskDecision.REJECT
    assert "RISK_QTY_ZERO" in _rule_ids(ev)


def test_portfolio_room_calculation():
    assert max_qty_under_portfolio_limits(
        100_000.0, 520.0, 0.0, 10.0, 0.0, 4.0) == 7      # sector binds
    assert max_qty_under_portfolio_limits(
        100_000.0, 520.0, 9_800.0, 10.0, 0.0, 4.0) == 0  # total is full


# ======================================================================
# violation collection and reporting
# ======================================================================

def test_all_violations_collected_not_short_circuited():
    """Calibration needs the full list, not the first failure."""
    late = datetime(2026, 8, 31, 15, 30, tzinfo=ET)
    ev = _rc().evaluate(
        _request(red_team_verdict=Verdict.VETO, red_team_max_risk_pct=0.0,
                 pm_confidence=0.30, final_opportunity_score=40.0,
                 equity_data_confidence=DataConfidence.BLOCKED),
        _portfolio(open_position_count=5), now=late)
    ids = _rule_ids(ev)
    for expected in ("RISK_AFTER_CUTOFF", "RISK_DATA_BLOCKED",
                     "RISK_RED_TEAM_VETO", "RISK_PM_CONFIDENCE",
                     "RISK_SCORE_FLOOR", "RISK_MAX_POSITIONS"):
        assert expected in ids


def test_sector_mapping_defaults_to_unknown_bucket():
    sector_map = {"TECH": ["NVDA", "AMD"], "INDEX": ["SPY"]}
    assert sector_of("NVDA", sector_map) == "TECH"
    assert sector_of("SPY", sector_map) == "INDEX"
    assert sector_of("BTAI", sector_map) == "UNKNOWN"


# ======================================================================
# conviction vs. Red Team discount, and the event-track score bar
# (both measured live 2026-09-02)
# ======================================================================

def test_confidence_floor_tests_original_conviction():
    """A Red Team discount must not re-trip the floor that the original
    conviction already cleared: the discount prices its concerns through
    sizing (recommended_max_risk_pct). IREN died 0.01 below the floor on
    09-02 after every council stage had approved the trade."""
    ev = _rc().evaluate(
        _request(pm_confidence=0.51, pm_conviction=0.66,
                 red_team_verdict=Verdict.MODIFY, red_team_max_risk_pct=0.3),
        _portfolio(), now=NOW)
    assert "RISK_PM_CONFIDENCE" not in _rule_ids(ev)


def test_confidence_floor_still_blocks_weak_conviction():
    ev = _rc().evaluate(
        _request(pm_confidence=0.51, pm_conviction=0.55),
        _portfolio(), now=NOW)
    assert "RISK_PM_CONFIDENCE" in _rule_ids(ev)


def test_confidence_floor_falls_back_without_conviction():
    ev = _rc().evaluate(_request(pm_confidence=0.51), _portfolio(), now=NOW)
    assert "RISK_PM_CONFIDENCE" in _rule_ids(ev)


def test_event_track_uses_event_score_floor():
    rc = RiskConstitution(RISK_CFG, {
        "tiers": {1: {"pm_confidence_floor": 0.60,
                      "final_score_floor": 68.0,
                      "final_score_floor_event": 66.0,
                      "max_cost_to_width": 0.55}},
        "liquidity_floor": SCORING_CFG["liquidity_floor"],
    })
    ev = rc.evaluate(_request(final_opportunity_score=66.5,
                              candidate_track="EVENT"),
                     _portfolio(), now=NOW)
    assert "RISK_SCORE_FLOOR" not in _rule_ids(ev)
    ev = rc.evaluate(_request(final_opportunity_score=66.5),
                     _portfolio(), now=NOW)
    assert "RISK_SCORE_FLOOR" in _rule_ids(ev)
