"""
Alpha Council v2.3 - execution, shadow book, and attribution models.

AttributionSnapshot carries the four-way decomposition from Section 7.5.
The validator enforces that selection + sizing reconciles to the total
effect exactly, because that identity is the demo's central claim.

Place at: alpha_council/models/execution.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import MarkMethod, ShadowVariant
from alpha_council.models.trading import OptionLeg, OptionStructure


class ExecutionIntent(StrictModel):
    decision_id: str
    client_order_id: str
    structure_id: str
    qty: int = Field(ge=1)
    order_class: Literal["mleg"] = "mleg"
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["day"] = "day"
    # Always the magnitude. For an opening order this is the net debit paid;
    # for a closing order it is the net credit demanded, and the Alpaca
    # payload carries it with a negative sign (mleg convention: positive
    # limit = net debit, negative limit = net credit).
    limit_debit: float = Field(gt=0)
    limit_is_credit: bool = False
    attempt: int = Field(default=1, ge=1, le=3)
    legs: list[OptionLeg] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def _client_id_format(self) -> "ExecutionIntent":
        if not self.client_order_id.startswith("ac_"):
            raise ValueError("client_order_id must start with 'ac_'")
        if len(self.client_order_id) > 48:
            raise ValueError("client_order_id exceeds the 48 character limit")
        return self

    @model_validator(mode="after")
    def _opening_legs(self) -> "ExecutionIntent":
        intents = {leg.position_intent for leg in self.legs}
        opening = {"buy_to_open", "sell_to_open"}
        closing = {"buy_to_close", "sell_to_close"}
        if not (intents <= opening or intents <= closing):
            raise ValueError(f"legs mix opening and closing intents: {intents}")
        return self

    @model_validator(mode="after")
    def _credit_flag_matches_intents(self) -> "ExecutionIntent":
        """Closing a debit vertical is a net credit; opening is a net debit.

        Submitting a closing combo with a positive (debit) limit is a
        marketable order through the market — it can fill at any credit,
        including one near zero.
        """
        closing = all(leg.position_intent.endswith("close")
                      for leg in self.legs)
        if closing != self.limit_is_credit:
            raise ValueError(
                f"limit_is_credit={self.limit_is_credit} contradicts leg "
                f"intents {[leg.position_intent for leg in self.legs]}")
        return self

    def to_alpaca_payload(self) -> dict[str, Any]:
        """Positive mleg limit price = net debit; negative = net credit."""
        signed = -self.limit_debit if self.limit_is_credit else self.limit_debit
        return {
            "order_class": self.order_class,
            "qty": str(self.qty),
            "type": self.order_type,
            "limit_price": f"{signed:.2f}",
            "time_in_force": self.time_in_force,
            "client_order_id": self.client_order_id,
            "legs": [
                {
                    "symbol": leg.symbol,
                    "ratio_qty": str(leg.ratio_qty),
                    "side": leg.side.lower(),
                    "position_intent": leg.position_intent,
                }
                for leg in self.legs
            ],
        }


class OrderReceipt(StrictModel):
    decision_id: str
    client_order_id: str
    alpaca_order_id: str
    status: str
    submitted_at: datetime
    adopted: bool = False  # recovered by client-ID lookup rather than newly placed
    raw: dict[str, Any] = Field(default_factory=dict)


class ShadowTradeDefinition(StrictModel):
    shadow_id: str
    decision_id: str
    variant: ShadowVariant
    structure: OptionStructure
    qty: int = Field(ge=0)
    entry_timestamp: datetime
    entry_reference_debit: float = Field(gt=0)
    close_policy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _veto_is_flat(self) -> "ShadowTradeDefinition":
        """A vetoed trade has quantity zero. The value of not trading is
        still measurable against the GPT original."""
        if self.variant is ShadowVariant.EXECUTED and self.qty < 1:
            raise ValueError("EXECUTED variant requires a filled quantity")
        return self


class ShadowMark(StrictModel):
    shadow_mark_id: str
    shadow_id: str
    marked_at: datetime
    mark_debit: float = Field(ge=0)
    unrealized_pnl: float
    mark_method: MarkMethod = MarkMethod.ADJUSTED_MID
    quote_lag_seconds: float | None = Field(default=None, ge=0)
    source: str
    raw: dict[str, Any] = Field(default_factory=dict)


class AttributionSnapshot(StrictModel):
    """Spec Section 19.3.

        selection_effect(A->B) = (pnl_per_spread_B - pnl_per_spread_A) * qty_A
        sizing_effect(A->B)    = (qty_B - qty_A) * pnl_per_spread_B
        total_effect(A->B)     = selection + sizing
                               = pnl_B - pnl_A

    Answers the question a spec-compliant single number cannot: did the red
    team pick a worse trade, or just a smaller one?
    """

    decision_id: str
    as_of: datetime
    mark_method: MarkMethod = MarkMethod.ADJUSTED_MID

    gpt_original_pnl: float
    claude_modified_pnl: float
    executed_pnl: float

    gpt_original_pnl_per_spread: float
    claude_modified_pnl_per_spread: float
    executed_pnl_per_spread: float

    gpt_original_qty: int = Field(ge=0)
    claude_modified_qty: int = Field(ge=0)
    executed_qty: int = Field(ge=0)

    claude_selection_effect: float
    claude_sizing_effect: float
    risk_selection_effect: float
    risk_sizing_effect: float

    claude_value_added: float
    risk_constitution_value_added: float

    @staticmethod
    def decompose(pnl_per_spread_a: float, qty_a: int,
                  pnl_per_spread_b: float, qty_b: int) -> tuple[float, float]:
        selection = (pnl_per_spread_b - pnl_per_spread_a) * qty_a
        sizing = (qty_b - qty_a) * pnl_per_spread_b
        return selection, sizing

    @model_validator(mode="after")
    def _totals_reconcile(self) -> "AttributionSnapshot":
        tol = 0.01
        for label, per_spread, qty, total in (
            ("gpt_original", self.gpt_original_pnl_per_spread,
             self.gpt_original_qty, self.gpt_original_pnl),
            ("claude_modified", self.claude_modified_pnl_per_spread,
             self.claude_modified_qty, self.claude_modified_pnl),
            ("executed", self.executed_pnl_per_spread,
             self.executed_qty, self.executed_pnl),
        ):
            if abs(per_spread * qty - total) > tol:
                raise ValueError(
                    f"{label}: per_spread {per_spread} x qty {qty} "
                    f"!= total {total}"
                )

        claude_total = self.claude_modified_pnl - self.gpt_original_pnl
        if abs((self.claude_selection_effect + self.claude_sizing_effect)
               - claude_total) > tol:
            raise ValueError("claude selection + sizing does not equal the total effect")
        if abs(self.claude_value_added - claude_total) > tol:
            raise ValueError("claude_value_added does not equal the total effect")

        risk_total = self.executed_pnl - self.claude_modified_pnl
        if abs((self.risk_selection_effect + self.risk_sizing_effect)
               - risk_total) > tol:
            raise ValueError("risk selection + sizing does not equal the total effect")
        if abs(self.risk_constitution_value_added - risk_total) > tol:
            raise ValueError("risk_constitution_value_added does not equal the total")
        return self

    @property
    def total_governance_value_added(self) -> float:
        return self.executed_pnl - self.gpt_original_pnl
