"""
Alpha Council v2.3 - trading models.

OptionLeg and OptionStructure carry the stale-quote fields from Section 5.4.
The structure validator enforces the payoff identity, so a pricing bug
raises at construction rather than becoming a mis-sized position.

Place at: alpha_council/models/trading.py
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import Direction, StrategyType, Verdict

SCORE = Field(ge=0, le=100)


class InvalidationRule(StrictModel):
    """Must be evaluable from the UNDERLYING alone. Option marks may be
    delayed, so an option-price invalidation cannot be monitored in real
    time (spec Section 15, pm_system amendment)."""

    rule_type: Literal["PRICE", "VWAP", "TIME", "CATALYST", "COMPOSITE"]
    description: str
    threshold: float | None = None
    comparator: Literal["LT", "LTE", "GT", "GTE"] | None = None

    @model_validator(mode="after")
    def _threshold_needs_comparator(self) -> "InvalidationRule":
        if self.threshold is not None and self.comparator is None:
            raise ValueError("threshold requires a comparator")
        return self


# The system-wide holding ceiling (PortfolioProposal horizon le=15).
MAX_HORIZON_DAYS = 15

_DAY_WORDS = {
    "one": 1, "first": 1, "two": 2, "second": 2, "three": 3, "third": 3,
    "four": 4, "fourth": 4, "five": 5, "fifth": 5, "six": 6, "sixth": 6,
    "seven": 7, "seventh": 7, "eight": 8, "eighth": 8, "nine": 9,
    "ninth": 9, "ten": 10, "tenth": 10, "eleven": 11, "eleventh": 11,
    "twelve": 12, "twelfth": 12, "thirteen": 13, "thirteenth": 13,
    "fourteen": 14, "fourteenth": 14, "fifteen": 15, "fifteenth": 15,
}

_DAY_COUNT = re.compile(
    r"\b(\d{1,2}|" + "|".join(sorted(_DAY_WORDS, key=len, reverse=True))
    + r")\b(?:\s+(?:full|more|consecutive))*\s+(?:trading\s+)?"
    r"(?:day|session)s?\b", re.IGNORECASE)


def _parse_day_count(text: str) -> int | None:
    match = _DAY_COUNT.search(text or "")
    if not match:
        return None
    token = match.group(1).lower()
    value = _DAY_WORDS.get(token)
    if value is None:
        try:
            value = int(token)
        except ValueError:
            return None
    return value if 1 <= value <= MAX_HORIZON_DAYS else None


def normalize_invalidation_rules(
        rules: list[InvalidationRule],
        horizon_days: int) -> tuple[list[InvalidationRule], list[str]]:
    """Make TIME rules evaluable before they are persisted.

    The monitor measures a TIME rule as elapsed days against its
    threshold and skips any rule whose threshold is None, so a malformed
    threshold is a silently dead backstop. PMs occasionally put a price
    level in the slot (MSFT, 2026-11-04: threshold 558.0 on "exit at the
    end of the fifth trading day...", graded UNSUPPORTED by the audit as
    beyond the 15-day system maximum, and never evaluable). The day
    count is recovered from the rule's own wording, else from the PM's
    stated horizon, and the comparator is forced to GTE - a time stop
    that fires while elapsed time is BELOW its bound would close the
    position at the first poll. Every repair is reported so the operator
    sees it; other rule types pass through untouched.
    """
    fixed: list[InvalidationRule] = []
    notes: list[str] = []
    for index, rule in enumerate(rules or []):
        if rule.rule_type != "TIME":
            fixed.append(rule)
            continue
        threshold = rule.threshold
        sane = (threshold is not None
                and 1.0 <= threshold <= MAX_HORIZON_DAYS)
        if sane and rule.comparator in ("GT", "GTE"):
            fixed.append(rule)
            continue
        if sane:
            new_threshold = float(threshold)
            source = "kept the day count"
        else:
            parsed = _parse_day_count(rule.description)
            if parsed is not None:
                new_threshold = float(parsed)
                source = "parsed from the rule text"
            else:
                new_threshold = float(
                    min(max(int(horizon_days), 1), MAX_HORIZON_DAYS))
                source = "defaulted to the PM horizon"
        fixed.append(rule.model_copy(update={"threshold": new_threshold,
                                             "comparator": "GTE"}))
        notes.append(
            f"TIME rule {index}: {threshold} {rule.comparator or '-'} -> "
            f"{new_threshold:g} GTE ({source}): {rule.description[:60]}")
    return fixed, notes


class PortfolioProposal(StrictModel):
    decision_id: str
    revision: int = Field(ge=0, le=1)
    symbol: str
    trade: bool
    direction: Direction
    confidence: float = Field(ge=0, le=1)
    expected_horizon_days: int = Field(ge=1, le=15)
    desired_portfolio_risk_pct: float = Field(ge=0, le=2.0)
    thesis: str
    catalyst_summary: str
    key_supporting_evidence: list[str]
    key_contrary_evidence: list[str]
    invalidation: list[InvalidationRule]
    selected_structure_rank: int | None = Field(default=None, ge=1, le=5)
    abstain_reason: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "PortfolioProposal":
        if self.trade:
            if self.direction is Direction.NEUTRAL:
                raise ValueError("a trade cannot be NEUTRAL")
            if not self.invalidation:
                raise ValueError("a trade requires at least one invalidation rule")
            if self.desired_portfolio_risk_pct <= 0:
                raise ValueError("a trade requires positive requested risk")
        else:
            if not self.abstain_reason:
                raise ValueError("abstention requires abstain_reason")
        return self


class OptionLeg(StrictModel):
    symbol: str
    underlying: str
    expiration: date
    option_type: Literal["CALL", "PUT"]
    strike: float = Field(gt=0)
    side: Literal["BUY", "SELL"]
    position_intent: Literal["buy_to_open", "sell_to_open",
                             "buy_to_close", "sell_to_close"]
    ratio_qty: int = Field(default=1, ge=1, le=4)

    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    raw_mid: float = Field(ge=0)
    adjusted_mid: float = Field(ge=0)
    quote_lag_seconds: float = Field(ge=0)
    underlying_price_at_quote: float | None = Field(default=None, ge=0)

    delta: float = Field(ge=-1, le=1)
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    implied_volatility: float | None = Field(default=None, ge=0)

    open_interest: int | None = Field(default=None, ge=0)
    open_interest_date: date | None = None
    volume: int | None = Field(default=None, ge=0)
    quote_timestamp: datetime | None = None

    @model_validator(mode="after")
    def _quote_usable(self) -> "OptionLeg":
        if self.ask <= 0:
            raise ValueError(f"{self.symbol}: ask must be positive, got {self.ask}")
        if self.bid <= 0:
            raise ValueError(f"{self.symbol}: bid must be positive, got {self.bid}")
        if self.ask < self.bid:
            raise ValueError(f"{self.symbol}: crossed quote {self.bid}/{self.ask}")
        return self

    @model_validator(mode="after")
    def _intent_matches_side(self) -> "OptionLeg":
        buys = ("buy_to_open", "buy_to_close")
        if (self.side == "BUY") != (self.position_intent in buys):
            raise ValueError(
                f"side {self.side} contradicts intent {self.position_intent}"
            )
        return self

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / max(mid, 0.01)

    @property
    def is_stale(self) -> bool:
        return self.quote_lag_seconds > 60


class OptionStructure(StrictModel):
    structure_id: str
    symbol: str
    strategy: StrategyType
    rank: int = Field(ge=1, le=5)
    expiration: date
    dte: int = Field(ge=1)
    legs: list[OptionLeg] = Field(min_length=2, max_length=2)

    width: float = Field(gt=0)
    net_delta: float
    raw_mid_debit: float
    adjusted_mid_debit: float = Field(gt=0)
    natural_debit: float = Field(gt=0)
    staleness_buffer: float = Field(default=0.0, ge=0)
    initial_limit_debit: float = Field(gt=0)
    cost_to_width_ratio: float = Field(gt=0, le=1)

    max_loss_per_spread: float = Field(gt=0)
    max_profit_per_spread: float = Field(gt=0)
    reward_risk_ratio: float = Field(gt=0)
    breakeven: float = Field(gt=0)

    max_quote_lag_seconds: float = Field(ge=0)
    underlying_price: float | None = Field(default=None, ge=0)
    underlying_move: float | None = None
    stale_adjusted: bool = False

    liquidity_score: float = SCORE
    delta_fit_score: float = SCORE
    dte_fit_score: float = SCORE
    cost_efficiency_score: float = SCORE
    structure_score: float = SCORE

    @property
    def long_leg(self) -> OptionLeg:
        return next(leg for leg in self.legs if leg.side == "BUY")

    @property
    def short_leg(self) -> OptionLeg:
        return next(leg for leg in self.legs if leg.side == "SELL")

    @model_validator(mode="after")
    def _defined_risk_vertical(self) -> "OptionStructure":
        if len(self.legs) != 2:
            raise ValueError("a vertical has exactly two legs")

        sides = sorted(leg.side for leg in self.legs)
        if sides != ["BUY", "SELL"]:
            raise ValueError(f"expected one BUY and one SELL, got {sides}")

        underlyings = {leg.underlying for leg in self.legs}
        expirations = {leg.expiration for leg in self.legs}
        types = {leg.option_type for leg in self.legs}
        if len(underlyings) != 1:
            raise ValueError(f"legs span multiple underlyings: {underlyings}")
        if len(expirations) != 1:
            raise ValueError(f"legs span multiple expirations: {expirations}")
        if len(types) != 1:
            raise ValueError(f"legs mix option types: {types}")

        if types.pop() != self.strategy.option_type:
            raise ValueError(f"{self.strategy} requires {self.strategy.option_type} legs")

        long_leg, short_leg = self.long_leg, self.short_leg
        if self.strategy is StrategyType.BULL_CALL_DEBIT:
            if long_leg.strike >= short_leg.strike:
                raise ValueError("bull call: long strike must be below short strike")
        else:
            if long_leg.strike <= short_leg.strike:
                raise ValueError("bear put: long strike must be above short strike")
        return self

    @model_validator(mode="after")
    def _payoff_identity(self) -> "OptionStructure":
        """All risk math uses the LIMIT debit actually submitted, never the mid."""
        d = self.initial_limit_debit
        w = self.width

        if abs(abs(self.long_leg.strike - self.short_leg.strike) - w) > 0.001:
            raise ValueError("width does not match the strike distance")
        if d >= w:
            raise ValueError(f"debit {d} is not less than width {w}")
        if d > self.natural_debit + 1e-9:
            raise ValueError(f"limit debit {d} exceeds natural debit {self.natural_debit}")

        for label, expected, actual in (
            ("max_loss", d * 100, self.max_loss_per_spread),
            ("max_profit", (w - d) * 100, self.max_profit_per_spread),
            ("cost_to_width", d / w, self.cost_to_width_ratio),
        ):
            if abs(expected - actual) > 0.01:
                raise ValueError(f"{label}: expected {expected:.4f}, got {actual:.4f}")

        expected_rr = self.max_profit_per_spread / self.max_loss_per_spread
        if abs(expected_rr - self.reward_risk_ratio) > 0.001:
            raise ValueError(
                f"reward_risk_ratio {self.reward_risk_ratio:.4f} != {expected_rr:.4f}"
            )

        long_strike = self.long_leg.strike
        expected_be = (long_strike + d
                       if self.strategy is StrategyType.BULL_CALL_DEBIT
                       else long_strike - d)
        if abs(expected_be - self.breakeven) > 0.01:
            raise ValueError(f"breakeven {self.breakeven} != {expected_be}")
        return self

    @model_validator(mode="after")
    def _staleness_flag_consistent(self) -> "OptionStructure":
        if self.stale_adjusted and self.underlying_move is None:
            raise ValueError("stale_adjusted requires underlying_move")
        actual_lag = max(leg.quote_lag_seconds for leg in self.legs)
        if abs(actual_lag - self.max_quote_lag_seconds) > 0.5:
            raise ValueError(
                f"max_quote_lag_seconds {self.max_quote_lag_seconds} "
                f"!= worst leg lag {actual_lag}"
            )
        return self


class RedTeamProblem(StrictModel):
    category: Literal[
        "DATA", "SOURCE", "NOVELTY", "THESIS", "CATALYST", "VOLATILITY",
        "STRUCTURE", "LIQUIDITY", "CONCENTRATION", "CORRELATION",
        "INVALIDATION", "TIMING", "STALENESS", "EXPRESSION", "BREAKEVEN",
        "OTHER",
    ]
    severity: int = Field(ge=1, le=10)
    description: str
    evidence: list[str] = Field(default_factory=list)


class RedTeamReview(StrictModel):
    decision_id: str
    verdict: Verdict
    risk_score: int = Field(ge=1, le=10)
    fatal_flaw: bool
    confidence_adjustment: float = Field(ge=-0.5, le=0.2)
    recommended_max_risk_pct: float = Field(ge=0, le=2.0)
    problems: list[RedTeamProblem]
    strongest_counterargument: str
    information_to_reverse_verdict: list[str]
    summary: str

    @model_validator(mode="after")
    def _verdict_coherent(self) -> "RedTeamReview":
        if self.verdict is Verdict.VETO:
            if not self.fatal_flaw:
                raise ValueError("VETO requires fatal_flaw=True")
            if self.recommended_max_risk_pct != 0.0:
                raise ValueError("VETO requires recommended_max_risk_pct=0.0")
        if self.verdict is Verdict.MODIFY and not self.problems:
            raise ValueError("MODIFY requires at least one stated problem")
        if self.verdict is Verdict.PASS and self.fatal_flaw:
            raise ValueError("PASS cannot report a fatal flaw")
        return self
