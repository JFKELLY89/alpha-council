"""
Alpha Council v2.5 - scenario payoff tests.

Expiration payoffs are exact arithmetic, so these assert exact values
rather than approximations. The STALL case gets the most attention: being
directionally right and still losing is the most common way a debit spread
fails, and it is what the Red Team's trade-expression challenge hunts for.

Place at: tests/test_payoffs.py

Run:
    uv run pytest tests/test_payoffs.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from alpha_council.evolution.payoffs import (
    PayoffEngine,
    breakeven_move_pct,
    evidence_block,
    expiration_pnl,
    format_summary,
    horizon_pnl_estimate,
    intrinsic_value,
    max_profit_move_pct,
    underlying_for_pnl,
)
from alpha_council.models.enums import StrategyType
from alpha_council.models.scenario import (
    Likelihood,
    Scenario,
    ScenarioPayoff,
    ScenarioSet,
    ScenarioType,
)
from alpha_council.models.trading import OptionLeg, OptionStructure

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)
SPOT = 204.0


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


def _bull(limit: float = 5.20) -> OptionStructure:
    width = 10.0
    return OptionStructure(
        structure_id="st_1", symbol="NVDA",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=1, expiration=EXP,
        dte=18, legs=[_leg(200.0, 0.60), _leg(210.0, 0.33, side="SELL")],
        width=width, net_delta=0.27, raw_mid_debit=limit - 0.10,
        adjusted_mid_debit=limit - 0.10, natural_debit=limit + 0.40,
        staleness_buffer=0.0, initial_limit_debit=limit,
        cost_to_width_ratio=limit / width, max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit, breakeven=200.0 + limit,
        max_quote_lag_seconds=4.0, underlying_price=SPOT,
        liquidity_score=82.0, delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0)


def _bear(limit: float = 5.20) -> OptionStructure:
    width = 10.0
    return OptionStructure(
        structure_id="st_2", symbol="NVDA",
        strategy=StrategyType.BEAR_PUT_DEBIT, rank=2, expiration=EXP,
        dte=18, legs=[_leg(210.0, -0.60, "PUT"),
                      _leg(200.0, -0.33, "PUT", side="SELL")],
        width=width, net_delta=-0.27, raw_mid_debit=limit - 0.10,
        adjusted_mid_debit=limit - 0.10, natural_debit=limit + 0.40,
        staleness_buffer=0.0, initial_limit_debit=limit,
        cost_to_width_ratio=limit / width, max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit, breakeven=210.0 - limit,
        max_quote_lag_seconds=4.0, underlying_price=SPOT,
        liquidity_score=82.0, delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0)

def _bear_otm(limit: float = 5.20) -> OptionStructure:
    """A 200/190 put spread with spot at 204: fully out of the money."""
    width = 10.0
    return OptionStructure(
        structure_id="st_3", symbol="NVDA",
        strategy=StrategyType.BEAR_PUT_DEBIT, rank=3, expiration=EXP,
        dte=18, legs=[_leg(200.0, -0.40, "PUT"),
                      _leg(190.0, -0.20, "PUT", side="SELL")],
        width=width, net_delta=-0.20, raw_mid_debit=limit - 0.10,
        adjusted_mid_debit=limit - 0.10, natural_debit=limit + 0.40,
        staleness_buffer=0.0, initial_limit_debit=limit,
        cost_to_width_ratio=limit / width, max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit, breakeven=200.0 - limit,
        max_quote_lag_seconds=4.0, underlying_price=SPOT,
        liquidity_score=82.0, delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0)


def _scenario(kind: ScenarioType, low: float, mid: float, high: float,
              days: int = 5) -> Scenario:
    return Scenario(
        scenario_type=kind, narrative=f"{kind} case",
        underlying_low=low, underlying_mid=mid, underlying_high=high,
        horizon_days=days, likelihood=Likelihood.POSSIBLE,
        key_drivers=["driver"])


def _set(scenarios: list[Scenario] | None = None) -> ScenarioSet:
    return ScenarioSet(
        scenario_set_id="ss_1", decision_id="d1", symbol="NVDA",
        spot_at_generation=SPOT, generated_at=NOW,
        overall_uncertainty=Likelihood.POSSIBLE,
        scenarios=scenarios or [
            _scenario(ScenarioType.CONTINUATION, 210.0, 213.0, 218.0),
            _scenario(ScenarioType.STALL, 203.0, 205.0, 207.0),
            _scenario(ScenarioType.REVERSAL, 192.0, 196.0, 200.0),
        ])


# ======================================================================
# exact expiration math
# ======================================================================

@pytest.mark.parametrize("underlying,expected", [
    (195.0, 0.0),      # both legs worthless
    (200.0, 0.0),      # at the long strike
    (205.0, 5.0),      # halfway up the width
    (210.0, 10.0),     # at the short strike, full width
    (250.0, 10.0),     # capped: the short leg gives back the excess
])
def test_bull_call_intrinsic(underlying, expected):
    assert intrinsic_value(_bull(), underlying) == pytest.approx(expected)


@pytest.mark.parametrize("underlying,expected", [
    (215.0, 0.0),
    (210.0, 0.0),
    (205.0, 5.0),
    (200.0, 10.0),
    (150.0, 10.0),
])
def test_bear_put_intrinsic(underlying, expected):
    assert intrinsic_value(_bear(), underlying) == pytest.approx(expected)


def test_defined_risk_floor_and_ceiling():
    """A debit spread cannot lose more than the debit or make more than
    the width less the debit. Both bounds hold at any underlying price."""
    structure = _bull()
    for underlying in (1.0, 100.0, 204.0, 500.0, 10_000.0):
        pnl = expiration_pnl(structure, underlying)
        assert pnl >= -structure.max_loss_per_spread - 0.01
        assert pnl <= structure.max_profit_per_spread + 0.01


def test_max_loss_at_expiration_below_the_long_strike():
    assert expiration_pnl(_bull(), 190.0) == pytest.approx(-520.0)


def test_max_profit_at_expiration_above_the_short_strike():
    assert expiration_pnl(_bull(), 220.0) == pytest.approx(480.0)


def test_breakeven_produces_zero():
    structure = _bull()
    assert expiration_pnl(structure, structure.breakeven) == pytest.approx(0.0)


def test_bear_breakeven_produces_zero():
    structure = _bear()
    assert expiration_pnl(structure, structure.breakeven) == pytest.approx(0.0)


def test_entry_debit_override_uses_the_actual_fill():
    """Sizing and payoff must use what was paid, not what was quoted."""
    structure = _bull()
    quoted = expiration_pnl(structure, 220.0)
    filled = expiration_pnl(structure, 220.0, entry_debit=5.45)
    assert filled == pytest.approx(quoted - 25.0)


# ======================================================================
# move requirements
# ======================================================================

def test_breakeven_move_is_the_headline_number():
    """Breakeven 205.20 against a 204.00 spot: a 0.59% move.

    This is the fact that decides whether a spread expresses a thesis or
    fights it. A trade needing 4% on a thesis that supports 2% is a bad
    expression of a possibly-correct idea.
    """
    assert breakeven_move_pct(_bull(), SPOT) == pytest.approx(0.005882,
                                                              abs=1e-5)


def test_max_profit_move():
    assert max_profit_move_pct(_bull(), SPOT) == pytest.approx(0.029412,
                                                               abs=1e-5)


def test_bear_max_profit_requires_a_downward_move():
    """Breakeven is NOT necessarily below spot.

    This 210/200 put spread at a $5.20 debit breaks even at 204.80, just
    above the 204.00 spot, because it is already in the money. Only the
    max-profit target is unambiguously below spot.
    """
    assert max_profit_move_pct(_bear(), SPOT) < 0
    assert breakeven_move_pct(_bear(), SPOT) == pytest.approx(0.003922,
                                                              abs=1e-5)


def test_out_of_the_money_bear_needs_a_downward_move():
    """A 200/190 put spread breaks even at 194.80, a 4.5% fall."""
    assert breakeven_move_pct(_bear_otm(), SPOT) == pytest.approx(-0.045098,
                                                                  abs=1e-5)
    assert max_profit_move_pct(_bear_otm(), SPOT) < 0


def test_a_cheaper_spread_needs_a_smaller_move():
    assert breakeven_move_pct(_bull(4.00), SPOT) < breakeven_move_pct(
        _bull(6.00), SPOT)


def test_underlying_for_a_target_pnl():
    structure = _bull()
    assert underlying_for_pnl(structure, 0.0) == pytest.approx(205.20)
    assert underlying_for_pnl(structure, 240.0) == pytest.approx(207.60)


def test_unreachable_target_returns_none():
    """A defined-risk spread cannot reach every number, and saying so is
    more useful than extrapolating past the cap."""
    assert underlying_for_pnl(_bull(), 5000.0) is None
    assert underlying_for_pnl(_bull(), -5000.0) is None


# ======================================================================
# the stall case
# ======================================================================

def test_stall_loses_money_when_the_move_is_too_small():
    """Directionally right, magnitude wrong. The most common failure."""
    structure = _bull()
    stall = _scenario(ScenarioType.STALL, 203.0, 205.0, 207.0)
    payoff = PayoffEngine().payoff(structure, stall, "d1")
    assert payoff.pnl_mid < 0
    assert payoff.pnl_high > 0        # 207 clears the 205.20 breakeven
    assert payoff.worst == payoff.pnl_low


def test_stall_flag_is_set_in_the_summary():
    summary = PayoffEngine().summarize(_bull(), _set(), "d1")
    assert summary.stall_loses_money
    assert summary.stall_mid is not None and summary.stall_mid < 0


def test_a_cheaper_spread_survives_the_stall():
    """The whole reason to compute this: expression choice changes the
    outcome even when the thesis does not."""
    scenarios = _set()
    expensive = PayoffEngine().summarize(_bull(5.20), scenarios, "d1")
    cheap = PayoffEngine().summarize(_bull(3.50), scenarios, "d1")
    assert expensive.stall_loses_money
    assert not cheap.stall_loses_money


def test_scenario_type_flags():
    assert ScenarioType.STALL.tests_expression
    assert ScenarioType.REVERSAL.is_failure
    assert not ScenarioType.CONTINUATION.is_failure


# ======================================================================
# summaries
# ======================================================================

def test_summary_covers_every_scenario():
    summary = PayoffEngine().summarize(_bull(), _set(), "d1")
    assert len(summary.payoffs) == 3
    assert summary.continuation_best == pytest.approx(480.0)
    assert summary.reversal_worst == pytest.approx(-520.0)


def test_upside_to_downside_ratio():
    summary = PayoffEngine().summarize(_bull(), _set(), "d1")
    assert summary.upside_to_downside == pytest.approx(480.0 / 520.0, abs=1e-3)


def test_ratio_is_none_when_the_reversal_does_not_lose():
    scenarios = _set([
        _scenario(ScenarioType.CONTINUATION, 212.0, 215.0, 220.0),
        _scenario(ScenarioType.REVERSAL, 206.0, 208.0, 210.0),
    ])
    summary = PayoffEngine().summarize(_bull(), scenarios, "d1")
    assert summary.upside_to_downside is None


def test_quantity_scales_totals_not_per_spread():
    summary = PayoffEngine().summarize(_bull(), _set(), "d1", qty=3)
    continuation = summary.payoff_for(ScenarioType.CONTINUATION)
    assert continuation.best == pytest.approx(480.0)        # per spread
    assert continuation.total_best == pytest.approx(1440.0)  # x3


def test_rank_structures_returns_no_composite_score():
    """Choosing among structures is the PM's call, informed by numbers.

    A composite score would relocate the decision into a weighting the
    operator never chose.
    """
    summaries = PayoffEngine().rank_structures(
        [_bull(4.00), _bull(5.20), _bull(6.00)], _set(), "d1")
    assert len(summaries) == 3
    assert not any(hasattr(s, "composite_score") for s in summaries)


# ======================================================================
# approximation discipline
# ======================================================================

def test_expiration_payoffs_are_not_flagged_approximate():
    payoff = PayoffEngine().payoff(_bull(),
                                   _scenario(ScenarioType.CONTINUATION,
                                             210.0, 213.0, 218.0), "d1")
    assert payoff.at_expiration
    assert not payoff.approximate


def test_horizon_payoffs_are_flagged_approximate():
    payoff = PayoffEngine().payoff(
        _bull(), _scenario(ScenarioType.CONTINUATION, 210.0, 213.0, 218.0),
        "d1", at_expiration=False)
    assert not payoff.at_expiration
    assert payoff.approximate


def test_a_pre_expiration_payoff_cannot_claim_to_be_exact():
    """Presenting a horizon estimate as exact is the one thing this module
    exists to prevent, so the model refuses to construct that combination."""
    with pytest.raises(ValidationError, match="must be marked approximate"):
        ScenarioPayoff(
            payoff_id="po_1", decision_id="d1", structure_id="st_1",
            scenario_type=ScenarioType.CONTINUATION,
            underlying_low=210.0, underlying_mid=213.0, underlying_high=218.0,
            pnl_low=480.0, pnl_mid=480.0, pnl_high=480.0,
            at_expiration=False, approximate=False)


def test_an_approximate_horizon_payoff_is_valid():
    """The legitimate combination must still construct."""
    payoff = ScenarioPayoff(
        payoff_id="po_2", decision_id="d1", structure_id="st_1",
        scenario_type=ScenarioType.STALL,
        underlying_low=203.0, underlying_mid=205.0, underlying_high=207.0,
        pnl_low=-520.0, pnl_mid=-20.0, pnl_high=180.0,
        at_expiration=False, approximate=True)
    assert payoff.approximate
    assert not payoff.at_expiration


def test_horizon_estimate_decays_toward_expiration_value():
    structure = _bull()
    early = horizon_pnl_estimate(structure, 205.0, days_held=1)
    late = horizon_pnl_estimate(structure, 205.0, days_held=17)
    exact = expiration_pnl(structure, 205.0)
    assert abs(late - exact) <= abs(early - exact)


def test_horizon_estimate_respects_defined_risk_bounds():
    structure = _bull()
    for days in (0, 5, 18, 40):
        for underlying in (150.0, 205.0, 300.0):
            pnl = horizon_pnl_estimate(structure, underlying, days)
            assert -structure.max_loss_per_spread - 0.01 <= pnl
            assert pnl <= structure.max_profit_per_spread + 0.01


# ======================================================================
# scenario set validation
# ======================================================================

def test_set_requires_a_failure_case():
    """A scenario set without a reversal is advocacy, not analysis."""
    with pytest.raises(ValidationError, match="REVERSAL"):
        ScenarioSet(
            scenario_set_id="s", decision_id="d", symbol="NVDA",
            spot_at_generation=SPOT, generated_at=NOW,
            overall_uncertainty=Likelihood.POSSIBLE,
            scenarios=[_scenario(ScenarioType.CONTINUATION, 210.0, 213.0,
                                 218.0),
                       _scenario(ScenarioType.STALL, 203.0, 205.0, 207.0)])


def test_set_requires_a_continuation_case():
    with pytest.raises(ValidationError, match="CONTINUATION"):
        ScenarioSet(
            scenario_set_id="s", decision_id="d", symbol="NVDA",
            spot_at_generation=SPOT, generated_at=NOW,
            overall_uncertainty=Likelihood.POSSIBLE,
            scenarios=[_scenario(ScenarioType.STALL, 203.0, 205.0, 207.0),
                       _scenario(ScenarioType.REVERSAL, 192.0, 196.0, 200.0)])


def test_duplicate_scenario_types_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        ScenarioSet(
            scenario_set_id="s", decision_id="d", symbol="NVDA",
            spot_at_generation=SPOT, generated_at=NOW,
            overall_uncertainty=Likelihood.POSSIBLE,
            scenarios=[_scenario(ScenarioType.CONTINUATION, 210.0, 213.0,
                                 218.0),
                       _scenario(ScenarioType.CONTINUATION, 211.0, 214.0,
                                 219.0),
                       _scenario(ScenarioType.REVERSAL, 192.0, 196.0, 200.0)])


def test_unordered_band_rejected():
    with pytest.raises(ValidationError, match="band must be ordered"):
        _scenario(ScenarioType.CONTINUATION, 218.0, 213.0, 210.0)


def test_zero_width_band_rejected():
    """A point estimate wearing a disguise."""
    with pytest.raises(ValidationError, match="zero width"):
        _scenario(ScenarioType.CONTINUATION, 213.0, 213.0, 213.0)


def test_scenarios_carry_no_numeric_probability():
    fields = set(Scenario.model_fields)
    assert "probability" not in fields
    assert "likelihood" in fields


def test_move_pct_helper():
    scenario = _scenario(ScenarioType.CONTINUATION, 210.0, 213.0, 218.0)
    low, mid, high = scenario.move_pct(SPOT)
    assert low == pytest.approx(0.029412, abs=1e-5)
    assert high == pytest.approx(0.068627, abs=1e-5)


# ======================================================================
# presentation
# ======================================================================

def test_format_summary_warns_about_the_stall():
    text = format_summary(PayoffEngine().summarize(_bull(), _set(), "d1"),
                          SPOT)
    assert "breakeven requires a +0.59% move" in text
    assert "WARNING" in text
    assert "directionally right is not sufficient" in text


def test_evidence_block_tells_the_model_not_to_recompute():
    block = evidence_block(
        PayoffEngine().rank_structures([_bull(), _bull(4.00)], _set(), "d1"),
        SPOT)
    assert "Do not recompute" in block["note"]
    assert len(block["structures"]) == 2
    assert "breakeven_move_pct" in block["structures"][0]
