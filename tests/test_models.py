"""
Alpha Council v2.3 - model contract tests.

Covers the invariants that would otherwise fail silently in production:
one-sided quotes, payoff math, verdict coherence, and the attribution
decomposition identity.

Place at: tests/test_models.py

Run:
    uv run pytest tests/test_models.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from alpha_council.models import (
    AttributionSnapshot,
    Bar,
    CandidateFeatures,
    DataConfidence,
    DataQualityResult,
    Direction,
    ExecutionIntent,
    GateRejection,
    GateStage,
    OptionLeg,
    OptionStructure,
    PortfolioProposal,
    QuoteObservation,
    RedTeamReview,
    RiskDecision,
    RiskEvaluation,
    RiskViolation,
    Severity,
    StrategyType,
    Verdict,
)
from alpha_council.models.trading import InvalidationRule, RedTeamProblem

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)


# ======================================================================
# QuoteObservation - the ask=0 bug observed on AAPL 2026-08-28
# ======================================================================

def _quote(**kw) -> QuoteObservation:
    base = dict(symbol="AAPL", source="ALPACA_IEX", observed_at=NOW)
    return QuoteObservation(**{**base, **kw})


def test_two_sided_quote_midpoint():
    q = _quote(bid=100.0, ask=100.10)
    assert q.is_two_sided
    assert q.midpoint() == pytest.approx(100.05)


def test_zero_ask_never_produces_a_midpoint():
    """AAPL returned bid=300.93 ask=0. A naive mid gives 150.47."""
    q = _quote(bid=300.93, ask=0.0)
    assert not q.is_two_sided
    assert q.midpoint() is None
    assert q.spread_pct() is None


def test_zero_ask_falls_back_to_last_not_bid():
    q = _quote(bid=300.93, ask=0.0, last=301.10)
    assert q.midpoint() == pytest.approx(301.10)


def test_crossed_quote_rejected():
    q = _quote(bid=101.0, ask=99.0)
    assert not q.is_two_sided
    assert q.midpoint() is None


def test_wide_spread_prefers_last_trade():
    """AAPL quoted 318.02/319.69 on 2026-08-28, a 0.52% spread."""
    q = _quote(bid=318.02, ask=319.69, last=318.90)
    assert q.spread_pct() == pytest.approx(0.005238, abs=1e-5)
        # 0.524% spread is below the 1.0% threshold, so the midpoint stands
    assert q.signal_price(prefer_last_above_spread_pct=0.010) == pytest.approx(318.855)
    assert q.signal_price(prefer_last_above_spread_pct=0.001) == pytest.approx(318.90)


def test_negative_price_rejected_by_field_constraint():
    with pytest.raises(ValidationError):
        _quote(bid=-1.0, ask=100.0)


# ======================================================================
# Bar
# ======================================================================

def _bar(**kw) -> Bar:
    base = dict(symbol="SPY", source="ALPACA_IEX", timeframe="5Min",
                timestamp=NOW, open=100.0, high=101.0, low=99.0,
                close=100.5, volume=1000.0)
    return Bar(**{**base, **kw})


def test_valid_bar():
    assert _bar().typical_price == pytest.approx(100.1667, abs=1e-3)


@pytest.mark.parametrize("bad", [
    {"high": 98.0},              # high below low
    {"open": 105.0},             # open outside range
    {"close": 95.0},             # close outside range
])
def test_incoherent_ohlc_rejected(bad):
    with pytest.raises(ValidationError):
        _bar(**bad)


# ======================================================================
# DataQualityResult
# ======================================================================

def test_blocked_must_not_carry_a_price():
    with pytest.raises(ValidationError, match="BLOCKED"):
        DataQualityResult(
            symbol="SPY", asset_type="EQUITY", evaluated_at=NOW,
            source="ALPACA_IEX", confidence=DataConfidence.BLOCKED,
            confidence_factor=0.0, signal_price=500.0, reason="stale",
        )


def test_blocked_requires_zero_factor():
    with pytest.raises(ValidationError):
        DataQualityResult(
            symbol="SPY", asset_type="EQUITY", evaluated_at=NOW,
            source="ALPACA_IEX", confidence=DataConfidence.BLOCKED,
            confidence_factor=0.9, reason="stale",
        )


def test_reason_is_mandatory():
    with pytest.raises(ValidationError):
        DataQualityResult(
            symbol="SPY", asset_type="EQUITY", evaluated_at=NOW,
            source="ALPACA_IEX", confidence=DataConfidence.HIGH,
            confidence_factor=1.0, reason="   ",
        )


# ======================================================================
# OptionLeg
# ======================================================================

def _leg(side="BUY", strike=750.0, delta=0.60, **kw) -> OptionLeg:
    base = dict(
        symbol=f"SPY260918C00{int(strike)}000", underlying="SPY",
        expiration=EXP, option_type="CALL", strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=10.0, ask=10.20, raw_mid=10.10, adjusted_mid=10.10,
        quote_lag_seconds=2.0, delta=delta, open_interest=5000, volume=400,
    )
    return OptionLeg(**{**base, **kw})


def test_leg_rejects_zero_ask():
    with pytest.raises(ValidationError, match="ask must be positive"):
        _leg(ask=0.0)


def test_leg_rejects_crossed_quote():
    with pytest.raises(ValidationError, match="crossed quote"):
        _leg(bid=11.0, ask=10.0)


def test_leg_rejects_side_intent_mismatch():
    with pytest.raises(ValidationError, match="contradicts intent"):
        _leg(side="SELL", position_intent="buy_to_open")


def test_leg_staleness_flag():
    assert not _leg(quote_lag_seconds=30.0).is_stale
    assert _leg(quote_lag_seconds=900.0).is_stale


# ======================================================================
# OptionStructure
# ======================================================================

def _structure(**kw) -> OptionStructure:
    long_leg = _leg(side="BUY", strike=750.0, delta=0.60,
                    bid=22.31, ask=24.65, raw_mid=23.48, adjusted_mid=23.48)
    short_leg = _leg(side="SELL", strike=760.0, delta=0.33,
                     bid=17.00, ask=18.00, raw_mid=17.50, adjusted_mid=17.50)
    debit = 5.50
    width = 10.0
    base = dict(
        structure_id="st_1", symbol="SPY",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=1, expiration=EXP, dte=18,
        legs=[long_leg, short_leg], width=width, net_delta=0.27,
        raw_mid_debit=5.98, adjusted_mid_debit=5.98, natural_debit=7.65,
        staleness_buffer=0.0, initial_limit_debit=debit,
        cost_to_width_ratio=debit / width,
        max_loss_per_spread=debit * 100,
        max_profit_per_spread=(width - debit) * 100,
        reward_risk_ratio=(width - debit) / debit,
        breakeven=750.0 + debit,
        max_quote_lag_seconds=2.0,
        liquidity_score=80.0, delta_fit_score=95.0, dte_fit_score=80.0,
        cost_efficiency_score=45.0, structure_score=76.0,
    )
    return OptionStructure(**{**base, **kw})


def test_valid_bull_call_structure():
    s = _structure()
    assert s.long_leg.strike == 750.0
    assert s.short_leg.strike == 760.0
    assert s.reward_risk_ratio == pytest.approx(0.818, abs=1e-3)
    assert s.cost_to_width_ratio == pytest.approx(0.55)


def test_reward_risk_follows_from_cost_to_width():
    """RR = (1 - c/w) / (c/w). Tier 1 caps c/w at 0.55, implying RR >= 0.82.
    This is why v2.2's hard RR >= 1.20 gate produced an empty set."""
    for cw, expected_rr in [(0.45, 1.2222), (0.50, 1.0), (0.55, 0.8182),
                            (0.60, 0.6667)]:
        assert (1 - cw) / cw == pytest.approx(expected_rr, abs=1e-3)


def test_wrong_payoff_math_rejected():
    with pytest.raises(ValidationError, match="max_profit"):
        _structure(max_profit_per_spread=999.0)


def test_debit_above_width_rejected():
    with pytest.raises(ValidationError):
        _structure(initial_limit_debit=12.0)


def test_limit_above_natural_rejected():
    with pytest.raises(ValidationError, match="exceeds natural"):
        _structure(initial_limit_debit=9.0, natural_debit=7.65,
                   cost_to_width_ratio=0.9, max_loss_per_spread=900.0,
                   max_profit_per_spread=100.0, reward_risk_ratio=1 / 9,
                   breakeven=759.0)


def test_inverted_bull_call_strikes_rejected():
    long_leg = _leg(side="BUY", strike=760.0)
    short_leg = _leg(side="SELL", strike=750.0)
    with pytest.raises(ValidationError, match="long strike must be below"):
        _structure(legs=[long_leg, short_leg])


def test_mixed_expirations_rejected():
    long_leg = _leg(side="BUY", strike=750.0)
    short_leg = _leg(side="SELL", strike=760.0, expiration=date(2026, 9, 11),
                     symbol="SPY260911C00760000")
    with pytest.raises(ValidationError, match="multiple expirations"):
        _structure(legs=[long_leg, short_leg])


def test_two_buys_rejected():
    with pytest.raises(ValidationError, match="one BUY and one SELL"):
        _structure(legs=[_leg(side="BUY", strike=750.0),
                         _leg(side="BUY", strike=760.0)])


def test_quote_lag_must_match_worst_leg():
    stale = _leg(side="SELL", strike=760.0, quote_lag_seconds=900.0,
                 bid=17.0, ask=18.0, raw_mid=17.5, adjusted_mid=17.5)
    with pytest.raises(ValidationError, match="worst leg lag"):
        _structure(legs=[_leg(side="BUY", strike=750.0, bid=22.31, ask=24.65,
                              raw_mid=23.48, adjusted_mid=23.48), stale],
                   max_quote_lag_seconds=2.0)


# ======================================================================
# PortfolioProposal
# ======================================================================

def _proposal(**kw) -> PortfolioProposal:
    base = dict(
        decision_id="d1", revision=0, symbol="SPY", trade=True,
        direction=Direction.BULLISH, confidence=0.7, expected_horizon_days=5,
        desired_portfolio_risk_pct=1.2, thesis="t", catalyst_summary="c",
        key_supporting_evidence=["a"], key_contrary_evidence=["b"],
        invalidation=[InvalidationRule(rule_type="PRICE", description="below VWAP",
                                       threshold=760.0, comparator="LT")],
        selected_structure_rank=1,
    )
    return PortfolioProposal(**{**base, **kw})


def test_trade_requires_invalidation():
    with pytest.raises(ValidationError, match="invalidation rule"):
        _proposal(invalidation=[])


def test_abstention_requires_a_reason():
    with pytest.raises(ValidationError, match="abstain_reason"):
        _proposal(trade=False, selected_structure_rank=None)


def test_neutral_trade_rejected():
    with pytest.raises(ValidationError, match="NEUTRAL"):
        _proposal(direction=Direction.NEUTRAL)


def test_structure_rank_allows_five():
    assert _proposal(selected_structure_rank=5).selected_structure_rank == 5
    with pytest.raises(ValidationError):
        _proposal(selected_structure_rank=6)


def test_threshold_requires_comparator():
    with pytest.raises(ValidationError, match="comparator"):
        InvalidationRule(rule_type="PRICE", description="x", threshold=100.0)


# ======================================================================
# RedTeamReview
# ======================================================================

def _review(**kw) -> RedTeamReview:
    base = dict(
        decision_id="d1", verdict=Verdict.PASS, risk_score=4, fatal_flaw=False,
        confidence_adjustment=0.0, recommended_max_risk_pct=1.2,
        problems=[], strongest_counterargument="x",
        information_to_reverse_verdict=[], summary="s",
    )
    return RedTeamReview(**{**base, **kw})


def test_veto_requires_fatal_flaw_and_zero_risk():
    with pytest.raises(ValidationError, match="fatal_flaw"):
        _review(verdict=Verdict.VETO, recommended_max_risk_pct=0.0)
    with pytest.raises(ValidationError, match="recommended_max_risk_pct"):
        _review(verdict=Verdict.VETO, fatal_flaw=True, recommended_max_risk_pct=1.0)
    assert _review(verdict=Verdict.VETO, fatal_flaw=True,
                   recommended_max_risk_pct=0.0).verdict is Verdict.VETO


def test_modify_requires_a_stated_problem():
    with pytest.raises(ValidationError, match="MODIFY"):
        _review(verdict=Verdict.MODIFY)
    ok = _review(verdict=Verdict.MODIFY,
                 problems=[RedTeamProblem(category="STALENESS", severity=6,
                                          description="quotes 14 min old")])
    assert ok.problems[0].category == "STALENESS"


def test_pass_cannot_report_fatal_flaw():
    with pytest.raises(ValidationError, match="PASS"):
        _review(fatal_flaw=True)


# ======================================================================
# RiskEvaluation
# ======================================================================

def _risk(**kw) -> RiskEvaluation:
    base = dict(
        decision_id="d1", evaluated_at=NOW, decision=RiskDecision.APPROVE,
        account_equity=100000.0, requested_qty=2, approved_qty=2,
        requested_max_loss=1100.0, approved_max_loss=1100.0,
        total_open_risk_pct_after=1.1, sector_risk_pct_after=1.1,
        daily_drawdown_pct=0.0, competition_drawdown_pct=0.0,
    )
    return RiskEvaluation(**{**base, **kw})


def test_approve_must_grant_the_full_request():
    with pytest.raises(ValidationError, match="RESIZE otherwise"):
        _risk(approved_qty=1)


def test_resize_must_reduce():
    with pytest.raises(ValidationError, match="must reduce"):
        _risk(decision=RiskDecision.RESIZE, approved_qty=2)
    assert _risk(decision=RiskDecision.RESIZE, approved_qty=1,
                 approved_max_loss=550.0).approved_qty == 1


def test_reject_forces_zero_quantity():
    with pytest.raises(ValidationError, match="approved_qty=0"):
        _risk(decision=RiskDecision.REJECT)


def test_halt_requires_a_halt_violation():
    with pytest.raises(ValidationError, match="HALT-severity"):
        _risk(decision=RiskDecision.HALT, approved_qty=0, approved_max_loss=0.0)
    ok = _risk(decision=RiskDecision.HALT, approved_qty=0, approved_max_loss=0.0,
               daily_drawdown_pct=5.4,
               violations=[RiskViolation(rule_id="RISK_DAILY_DRAWDOWN",
                                         severity=Severity.HALT,
                                         message="daily drawdown 5.4%")])
    assert ok.blocking_violations


def test_approved_cannot_exceed_requested():
    with pytest.raises(ValidationError, match="cannot exceed"):
        _risk(requested_qty=1, approved_qty=2)


# ======================================================================
# GateRejection
# ======================================================================

def test_early_stage_cannot_be_shadow_marked():
    with pytest.raises(ValidationError, match="before a priced structure"):
        GateRejection(
            rejection_id="r1", occurred_at=NOW, config_version="v1",
            symbol="SPY", direction=Direction.BULLISH,
            stage=GateStage.PRESCORE, gate_id="PRESCORE_FLOOR",
            tier=1, hard_gate=False, shadow_eligible=True,
            shadow_structure_json="{}",
        )


def test_shadow_eligible_requires_the_structure():
    with pytest.raises(ValidationError, match="structure payload"):
        GateRejection(
            rejection_id="r1", occurred_at=NOW, config_version="v1",
            symbol="SPY", direction=Direction.BULLISH,
            stage=GateStage.RED_TEAM, gate_id="RED_TEAM_VETO",
            tier=1, hard_gate=False, shadow_eligible=True,
        )


def test_gate_stage_shadow_eligibility_map():
    assert GateStage.RISK.shadow_eligible
    assert GateStage.PM_ABSTAIN.shadow_eligible
    assert not GateStage.DATA_QUALITY.shadow_eligible
    assert not GateStage.UNIVERSE.shadow_eligible


# ======================================================================
# CandidateFeatures
# ======================================================================

def _candidate(**kw) -> CandidateFeatures:
    base = dict(
        symbol="SPY", as_of=NOW, direction=Direction.BULLISH,
        combined_direction=0.45, momentum_score=70.0,
        relative_volume_score=65.0, trend_regime_score=75.0,
        relative_strength_score=60.0, options_opportunity_score=76.0,
        options_liquidity_score=80.0, catalyst_score=72.0,
        corroboration_score=70.0, novelty_score=80.0,
        data_confidence_factor=1.0, regime_factor=1.0, event_risk_factor=1.0,
        pre_score=69.5, raw_opportunity_score=71.6,
        final_opportunity_score=71.6,
    )
    return CandidateFeatures(**{**base, **kw})


def test_final_score_is_the_product_of_raw_and_factors():
    c = _candidate(data_confidence_factor=0.92, final_opportunity_score=71.6 * 0.92)
    assert c.final_opportunity_score == pytest.approx(65.872)


def test_inconsistent_final_score_rejected():
    with pytest.raises(ValidationError, match="raw \\* factors"):
        _candidate(data_confidence_factor=0.8, final_opportunity_score=71.6)


def test_direction_must_match_the_signal_sign():
    with pytest.raises(ValidationError, match="BULLISH"):
        _candidate(combined_direction=-0.4)


def test_event_blackout_zeroes_the_score():
    c = _candidate(event_risk_factor=0.0, final_opportunity_score=0.0)
    assert c.blocked_by_event_risk


# ======================================================================
# ExecutionIntent
# ======================================================================

def test_alpaca_payload_shape():
    s = _structure()
    intent = ExecutionIntent(
        decision_id="d1", client_order_id="ac_a1b2c3d4_r0_e5f6a7b8",
        structure_id=s.structure_id, qty=2, limit_debit=5.50, legs=s.legs,
    )
    payload = intent.to_alpaca_payload()
    assert payload["order_class"] == "mleg"
    assert payload["limit_price"] == "5.50"
    assert len(payload["legs"]) == 2
    assert {leg["side"] for leg in payload["legs"]} == {"buy", "sell"}
    assert payload["legs"][0]["position_intent"] == "buy_to_open"


def test_client_order_id_prefix_enforced():
    s = _structure()
    with pytest.raises(ValidationError, match="must start with"):
        ExecutionIntent(decision_id="d1", client_order_id="xx_1",
                        structure_id="s1", qty=1, limit_debit=5.5, legs=s.legs)


def test_mixed_open_close_intents_rejected():
    s = _structure()
    closing = s.legs[1].model_copy(update={"position_intent": "buy_to_close",
                                           "side": "BUY"})
    with pytest.raises(ValidationError, match="mix opening and closing"):
        ExecutionIntent(decision_id="d1", client_order_id="ac_1_r0_2",
                        structure_id="s1", qty=1, limit_debit=5.5,
                        legs=[s.legs[0], closing])


# ======================================================================
# AttributionSnapshot - the demo's central arithmetic
# ======================================================================

def test_decomposition_is_exact():
    """Claude picked a worse structure AND cut the size. The two effects
    must separate cleanly and sum to the total."""
    gpt_ps, gpt_qty = 120.0, 3
    claude_ps, claude_qty = 90.0, 2
    sel, siz = AttributionSnapshot.decompose(gpt_ps, gpt_qty, claude_ps, claude_qty)
    assert sel == pytest.approx((90.0 - 120.0) * 3)     # -90 selection
    assert siz == pytest.approx((2 - 3) * 90.0)         # -90 sizing
    assert sel + siz == pytest.approx(claude_ps * claude_qty - gpt_ps * gpt_qty)


def test_full_snapshot_reconciles():
    gpt_ps, gpt_qty = 120.0, 3
    cl_ps, cl_qty = 90.0, 2
    ex_ps, ex_qty = 90.0, 1
    c_sel, c_siz = AttributionSnapshot.decompose(gpt_ps, gpt_qty, cl_ps, cl_qty)
    r_sel, r_siz = AttributionSnapshot.decompose(cl_ps, cl_qty, ex_ps, ex_qty)

    snap = AttributionSnapshot(
        decision_id="d1", as_of=NOW,
        gpt_original_pnl=gpt_ps * gpt_qty,
        claude_modified_pnl=cl_ps * cl_qty,
        executed_pnl=ex_ps * ex_qty,
        gpt_original_pnl_per_spread=gpt_ps,
        claude_modified_pnl_per_spread=cl_ps,
        executed_pnl_per_spread=ex_ps,
        gpt_original_qty=gpt_qty, claude_modified_qty=cl_qty,
        executed_qty=ex_qty,
        claude_selection_effect=c_sel, claude_sizing_effect=c_siz,
        risk_selection_effect=r_sel, risk_sizing_effect=r_siz,
        claude_value_added=c_sel + c_siz,
        risk_constitution_value_added=r_sel + r_siz,
    )
    assert snap.total_governance_value_added == pytest.approx(90.0 - 360.0)
    assert snap.risk_selection_effect == 0.0      # same structure
    assert snap.risk_sizing_effect == pytest.approx(-90.0)


def test_veto_case_measures_the_value_of_not_trading():
    snap = AttributionSnapshot(
        decision_id="d1", as_of=NOW,
        gpt_original_pnl=-450.0, claude_modified_pnl=0.0, executed_pnl=0.0,
        gpt_original_pnl_per_spread=-150.0,
        claude_modified_pnl_per_spread=0.0, executed_pnl_per_spread=0.0,
        gpt_original_qty=3, claude_modified_qty=0, executed_qty=0,
        claude_selection_effect=(0.0 - -150.0) * 3,
        claude_sizing_effect=(0 - 3) * 0.0,
        risk_selection_effect=0.0, risk_sizing_effect=0.0,
        claude_value_added=450.0, risk_constitution_value_added=0.0,
    )
    assert snap.claude_value_added == pytest.approx(450.0)


def test_broken_reconciliation_rejected():
    with pytest.raises(ValidationError, match="selection \\+ sizing"):
        AttributionSnapshot(
            decision_id="d1", as_of=NOW,
            gpt_original_pnl=360.0, claude_modified_pnl=180.0, executed_pnl=180.0,
            gpt_original_pnl_per_spread=120.0,
            claude_modified_pnl_per_spread=90.0,
            executed_pnl_per_spread=90.0,
            gpt_original_qty=3, claude_modified_qty=2, executed_qty=2,
            claude_selection_effect=0.0, claude_sizing_effect=0.0,
            risk_selection_effect=0.0, risk_sizing_effect=0.0,
            claude_value_added=-180.0, risk_constitution_value_added=0.0,
        )


def test_per_spread_times_qty_must_equal_total():
    with pytest.raises(ValidationError, match="!= total"):
        AttributionSnapshot(
            decision_id="d1", as_of=NOW,
            gpt_original_pnl=999.0, claude_modified_pnl=0.0, executed_pnl=0.0,
            gpt_original_pnl_per_spread=120.0,
            claude_modified_pnl_per_spread=0.0, executed_pnl_per_spread=0.0,
            gpt_original_qty=3, claude_modified_qty=0, executed_qty=0,
            claude_selection_effect=0.0, claude_sizing_effect=0.0,
            risk_selection_effect=0.0, risk_sizing_effect=0.0,
            claude_value_added=0.0, risk_constitution_value_added=0.0,
        )


# ======================================================================
# extra="forbid" - how a bad LLM response becomes NO TRADE
# ======================================================================

def test_unknown_field_rejected_everywhere():
    with pytest.raises(ValidationError):
        _proposal(hallucinated_field="surprise")
    with pytest.raises(ValidationError):
        _review(extra_commentary="unrequested")
    with pytest.raises(ValidationError):
        _quote(nonsense=1)


def test_enum_helpers():
    assert Direction.BULLISH.sign == 1
    assert Direction.BEARISH.sign == -1
    assert StrategyType.BEAR_PUT_DEBIT.option_type == "PUT"
    assert StrategyType.BULL_CALL_DEBIT.direction is Direction.BULLISH
    assert RiskDecision.HALT.blocks_trade
    assert not DataConfidence.BLOCKED.tradable
