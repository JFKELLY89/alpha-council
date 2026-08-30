"""
Alpha Council v2.4 - execution tests.

A duplicate spread is worse than a missed one: it doubles risk silently and
corrupts the attribution ledger. These tests exist to make that impossible.

Place at: tests/test_execution.py

Run:
    uv run pytest tests/test_execution.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from alpha_council.execution.order_manager import (
    _extract_fill_debit,
    build_intent,
    walk_prices,
)
from alpha_council.models.enums import StrategyType
from alpha_council.models.trading import OptionLeg, OptionStructure
from alpha_council.utils.ids import (
    client_order_id,
    decision_fragment,
    is_valid_client_order_id,
)

EXP = date(2026, 9, 18)


def _leg(strike: float, delta: float, bid: float, ask: float,
         side: str = "BUY") -> OptionLeg:
    mid = (bid + ask) / 2
    return OptionLeg(
        symbol=f"SPY260918C{int(strike * 1000):08d}", underlying="SPY",
        expiration=EXP, option_type="CALL", strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=bid, ask=ask, raw_mid=mid, adjusted_mid=mid,
        quote_lag_seconds=3.0, delta=delta, open_interest=5000, volume=400,
    )


def _structure(limit: float = 5.20, width: float = 10.0) -> OptionStructure:
    return OptionStructure(
        structure_id="st_1", symbol="SPY",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=1, expiration=EXP, dte=18,
        legs=[_leg(750.0, 0.60, 23.30, 23.70),
              _leg(750.0 + width, 0.33, 18.20, 18.60, side="SELL")],
        width=width, net_delta=0.27,
        raw_mid_debit=limit - 0.10, adjusted_mid_debit=limit - 0.10,
        natural_debit=limit + 0.40, staleness_buffer=0.0,
        initial_limit_debit=limit, cost_to_width_ratio=limit / width,
        max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit,
        breakeven=750.0 + limit, max_quote_lag_seconds=3.0,
        liquidity_score=85.0, delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0,
    )


# ======================================================================
# client order IDs
# ======================================================================

def test_client_order_id_is_unique_per_submission():
    ids = {client_order_id("dec_abc", 0) for _ in range(200)}
    assert len(ids) == 200


def test_decision_fragment_is_stable_across_submissions():
    """Unique per order, but traceable back to one decision."""
    a, b = client_order_id("dec_abc", 0), client_order_id("dec_abc", 0)
    assert a != b
    assert decision_fragment(a) == decision_fragment(b)


def test_revision_is_encoded():
    assert "_r0_" in client_order_id("dec_abc", 0)
    assert "_r1_" in client_order_id("dec_abc", 1)


def test_client_order_id_fits_alpaca_limit():
    cid = client_order_id("dec_" + "x" * 200, 1)
    assert len(cid) <= 48
    assert is_valid_client_order_id(cid)


# ======================================================================
# intent construction
# ======================================================================

def test_opening_intent_payload():
    intent = build_intent(_structure(), "dec_1", qty=2, limit_debit=5.35)
    payload = intent.to_alpaca_payload()
    assert payload["order_class"] == "mleg"
    assert payload["type"] == "limit"
    assert payload["time_in_force"] == "day"
    assert payload["qty"] == "2"
    assert payload["limit_price"] == "5.35"
    assert len(payload["legs"]) == 2
    intents = {leg["position_intent"] for leg in payload["legs"]}
    assert intents == {"buy_to_open", "sell_to_open"}


def test_closing_intent_reverses_sides_and_intents():
    intent = build_intent(_structure(), "dec_1", qty=1, limit_debit=6.10,
                          closing=True)
    payload = intent.to_alpaca_payload()
    intents = {leg["position_intent"] for leg in payload["legs"]}
    assert intents == {"sell_to_close", "buy_to_close"}
    long_leg = next(leg for leg in payload["legs"]
                    if leg["position_intent"] == "sell_to_close")
    assert long_leg["side"] == "sell"


def test_positive_limit_price_is_a_debit():
    intent = build_intent(_structure(), "dec_1", qty=1, limit_debit=5.35)
    assert float(intent.to_alpaca_payload()["limit_price"]) > 0


# ======================================================================
# the limit walk
# ======================================================================

def test_walk_is_monotonic_and_bounded():
    prices = walk_prices(adjusted_mid=5.00, natural=6.00, buffer=0.0,
                         max_allowed=6.00)
    assert prices == sorted(prices)
    assert len(prices) == 3
    assert prices[-1] <= 6.00


def test_walk_never_exceeds_natural():
    prices = walk_prices(5.00, 6.00, buffer=2.00, max_allowed=99.0)
    assert max(prices) <= 6.00


def test_risk_ceiling_binds_below_natural():
    prices = walk_prices(5.00, 6.00, buffer=0.0, max_allowed=5.40)
    assert max(prices) <= 5.40


def test_staleness_buffer_lifts_the_first_attempt():
    plain = walk_prices(5.00, 6.00, buffer=0.0, max_allowed=6.00)
    padded = walk_prices(5.00, 6.00, buffer=0.15, max_allowed=6.00)
    assert padded[0] > plain[0]


def test_walk_collapses_when_the_ceiling_is_tight():
    """A ceiling at or below the mid still yields one submittable price."""
    prices = walk_prices(5.00, 6.00, buffer=0.0, max_allowed=4.80)
    assert len(prices) >= 1
    assert all(p <= 4.80 for p in prices)


def test_walk_has_no_duplicate_prices():
    prices = walk_prices(5.00, 5.02, buffer=0.0, max_allowed=5.02)
    assert len(prices) == len(set(prices))


# ======================================================================
# fill extraction
# ======================================================================

def test_fill_debit_from_order_average():
    assert _extract_fill_debit({"filled_avg_price": "5.42"}, 5.35) == 5.42


def test_fill_debit_computed_net_from_legs():
    order = {"legs": [
        {"side": "buy", "filled_avg_price": "23.55", "ratio_qty": "1"},
        {"side": "sell", "filled_avg_price": "18.20", "ratio_qty": "1"},
    ]}
    assert _extract_fill_debit(order, 5.35) == pytest.approx(5.35, abs=0.01)


def test_fill_debit_falls_back_to_the_limit():
    assert _extract_fill_debit({}, 5.35) == 5.35
    assert _extract_fill_debit({"filled_avg_price": None}, 5.35) == 5.35


def test_negative_net_from_legs_falls_back():
    """A credit on a debit spread means the legs were misread; do not trust it."""
    order = {"legs": [
        {"side": "buy", "filled_avg_price": "18.20", "ratio_qty": "1"},
        {"side": "sell", "filled_avg_price": "23.55", "ratio_qty": "1"},
    ]}
    assert _extract_fill_debit(order, 5.35) == 5.35
