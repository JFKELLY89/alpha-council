"""
Alpha Council v2.5 - scenario models.

A scenario is a named path the underlying might take, with a low/mid/high
band rather than a point estimate. Bands are honest about the fact that
nobody knows the number; a point forecast pretends otherwise.

No scenario carries a numeric probability. An LLM asked for "62% chance"
will produce one, and it will be meaningless. Qualitative likelihood is
what the evidence can actually support.

Place at: alpha_council/models/scenario.py
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import Direction


class ScenarioType(StrEnum):
    """The three paths that matter for a defined-risk directional spread."""

    CONTINUATION = "CONTINUATION"   # the thesis plays out
    STALL = "STALL"                 # direction right, magnitude or speed wrong
    REVERSAL = "REVERSAL"           # the thesis is wrong

    @property
    def is_failure(self) -> bool:
        return self is ScenarioType.REVERSAL

    @property
    def tests_expression(self) -> bool:
        """STALL is the scenario that separates a good idea from a good trade.

        Being directionally right and still losing is the single most
        common way a debit spread fails, and it is exactly what the Red
        Team's trade-expression challenge is looking for.
        """
        return self is ScenarioType.STALL


class Likelihood(StrEnum):
    UNLIKELY = "UNLIKELY"
    POSSIBLE = "POSSIBLE"
    LIKELY = "LIKELY"


class Scenario(StrictModel):
    scenario_type: ScenarioType
    narrative: str
    underlying_low: float = Field(gt=0)
    underlying_mid: float = Field(gt=0)
    underlying_high: float = Field(gt=0)
    horizon_days: int = Field(ge=1, le=15)
    likelihood: Likelihood
    key_drivers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _band_is_ordered(self) -> "Scenario":
        if not (self.underlying_low <= self.underlying_mid
                <= self.underlying_high):
            raise ValueError(
                f"band must be ordered: {self.underlying_low} <= "
                f"{self.underlying_mid} <= {self.underlying_high}")
        return self

    @model_validator(mode="after")
    def _band_has_width(self) -> "Scenario":
        """A zero-width band is a point estimate wearing a disguise."""
        if self.underlying_high == self.underlying_low:
            raise ValueError(
                "scenario band has zero width; state a range, not a point")
        return self

    def move_pct(self, spot: float) -> tuple[float, float, float]:
        return ((self.underlying_low - spot) / spot,
                (self.underlying_mid - spot) / spot,
                (self.underlying_high - spot) / spot)


class ScenarioSet(StrictModel):
    scenario_set_id: str
    decision_id: str
    symbol: str
    spot_at_generation: float = Field(gt=0)
    generated_at: datetime
    overall_uncertainty: Likelihood
    scenarios: list[Scenario] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def _covers_success_and_failure(self) -> "ScenarioSet":
        """A set without a failure case is not analysis, it is advocacy."""
        types = {s.scenario_type for s in self.scenarios}
        if ScenarioType.CONTINUATION not in types:
            raise ValueError("scenario set requires a CONTINUATION case")
        if ScenarioType.REVERSAL not in types:
            raise ValueError("scenario set requires a REVERSAL case")
        return self

    @model_validator(mode="after")
    def _no_duplicate_types(self) -> "ScenarioSet":
        types = [s.scenario_type for s in self.scenarios]
        if len(types) != len(set(types)):
            raise ValueError(f"duplicate scenario types: {types}")
        return self

    def by_type(self, scenario_type: ScenarioType) -> Scenario | None:
        return next((s for s in self.scenarios
                     if s.scenario_type is scenario_type), None)


class ScenarioPayoff(StrictModel):
    """Deterministic P&L of one structure under one scenario.

    Expiration values are exact. Horizon values are approximations and are
    labelled as such wherever they surface.
    """

    payoff_id: str
    decision_id: str
    structure_id: str
    scenario_type: ScenarioType

    underlying_low: float
    underlying_mid: float
    underlying_high: float

    pnl_low: float
    pnl_mid: float
    pnl_high: float

    at_expiration: bool = True
    approximate: bool = False
    qty: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def _horizon_values_are_flagged(self) -> "ScenarioPayoff":
        if not self.at_expiration and not self.approximate:
            raise ValueError(
                "a pre-expiration payoff is an estimate and must be marked "
                "approximate; only expiration values are exact")
        return self

    @property
    def worst(self) -> float:
        return min(self.pnl_low, self.pnl_mid, self.pnl_high)

    @property
    def best(self) -> float:
        return max(self.pnl_low, self.pnl_mid, self.pnl_high)

    @property
    def total_worst(self) -> float:
        return round(self.worst * self.qty, 2)

    @property
    def total_best(self) -> float:
        return round(self.best * self.qty, 2)


class PayoffSummary(StrictModel):
    """One structure judged across a whole scenario set."""

    structure_id: str
    rank: int
    decision_id: str
    payoffs: list[ScenarioPayoff]

    continuation_best: float
    stall_mid: float | None
    reversal_worst: float
    max_loss_per_spread: float

    breakeven_move_pct: float
    max_profit_move_pct: float
    stall_loses_money: bool

    @property
    def upside_to_downside(self) -> float | None:
        """Best continuation against worst reversal.

        Not a probability-weighted expectation. Weighting by invented
        probabilities would produce a confident number resting on nothing.
        """
        if self.reversal_worst >= 0:
            return None
        return round(abs(self.continuation_best / self.reversal_worst), 3)

    def payoff_for(self, scenario_type: ScenarioType) -> ScenarioPayoff | None:
        return next((p for p in self.payoffs
                     if p.scenario_type is scenario_type), None)
