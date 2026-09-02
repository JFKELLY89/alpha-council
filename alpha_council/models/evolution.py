"""
Alpha Council v2.5 §14 - Alpha Evolution models.

PreMarketBrief, the Champion/Challenger contract, and the promotion
decision. Everything here is either produced by a model under schema
enforcement (brief, proposal) or computed deterministically in SQL
(performance, promotion) — the validators encode which is which.

The one rule every model here serves: **a Challenger may propose, shadow,
and accumulate evidence; it may never touch a constitutional parameter,
and during the competition it may never trade.**

Place at: alpha_council/models/evolution.py
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel


# ======================================================================
# pre-market brief (v2.5 §8)
# ======================================================================

class PreMarketBrief(StrictModel):
    """Session context. Never a scoring override, never an order."""

    session_date: str
    generated_at: datetime
    regime_summary: str = Field(min_length=20)
    session_bias: Literal["RISK_ON", "RISK_OFF", "MIXED", "NEUTRAL"]
    important_themes: list[str] = Field(default_factory=list, max_length=8)
    candidate_themes: list[str] = Field(default_factory=list, max_length=8)
    risk_windows: list[str] = Field(default_factory=list, max_length=8)
    portfolio_concerns: list[str] = Field(default_factory=list, max_length=6)
    prior_session_lessons: list[str] = Field(default_factory=list,
                                             max_length=6)
    confidence: float = Field(ge=0, le=1)

    def as_context(self) -> str:
        """Compact text for EvidenceBuilder(session_briefing=...)."""
        lines = [f"Session bias: {self.session_bias} "
                 f"(confidence {self.confidence:.2f})",
                 f"Regime: {self.regime_summary}"]
        if self.important_themes:
            lines.append("Themes: " + "; ".join(self.important_themes[:5]))
        if self.risk_windows:
            lines.append("Risk windows: " + "; ".join(self.risk_windows[:4]))
        if self.portfolio_concerns:
            lines.append("Portfolio concerns: "
                         + "; ".join(self.portfolio_concerns[:3]))
        if self.prior_session_lessons:
            lines.append("Prior lessons: "
                         + "; ".join(self.prior_session_lessons[:3]))
        lines.append("This brief is context only. It does not change "
                     "scores, gates, or risk limits.")
        return "\n".join(lines)


# ======================================================================
# challenger proposals (v2.5 §10)
# ======================================================================

class ChangeCategory(StrEnum):
    SCORING_WEIGHT = "SCORING_WEIGHT"
    QUALITY_THRESHOLD = "QUALITY_THRESHOLD"
    TRACK_QUOTA = "TRACK_QUOTA"
    DISCOVERY_PRIORITY = "DISCOVERY_PRIORITY"
    OPTIONS_PREFERENCE = "OPTIONS_PREFERENCE"
    PROMPT_EMPHASIS = "PROMPT_EMPHASIS"


class ParameterChange(StrictModel):
    category: ChangeCategory
    parameter_path: str = Field(min_length=3)
    champion_value: float | int | str
    challenger_value: float | int | str
    relative_change_pct: float | None = None
    rationale: str = Field(min_length=10)

    @model_validator(mode="after")
    def _must_actually_change(self) -> "ParameterChange":
        if self.champion_value == self.challenger_value:
            raise ValueError(
                f"{self.parameter_path}: challenger value equals the "
                "champion value; that is not a change")
        return self


class ChallengerProposal(StrictModel):
    challenger_id: str
    parent_champion_id: str
    created_at: datetime
    hypothesis: str = Field(min_length=20)
    evidence_summary: list[str] = Field(min_length=1, max_length=8)
    changes: list[ParameterChange] = Field(min_length=1, max_length=3)
    expected_benefit: str = Field(min_length=10)
    expected_failure_mode: str = Field(min_length=10)
    minimum_shadow_observations: int = Field(ge=5)
    confidence: Literal["LOW", "MEDIUM", "HIGH"]


class EvolutionDecision(StrictModel):
    """The Alpha Evolution agent's structured output.

    NO CHANGE is a first-class answer, not a failure: with a competition
    sample, it is usually the correct one (v2.5 §0.10).
    """

    propose: bool
    no_change_reason: str | None = None
    proposal: ChallengerProposal | None = None

    @model_validator(mode="after")
    def _coherent(self) -> "EvolutionDecision":
        if self.propose and self.proposal is None:
            raise ValueError("propose=true requires a proposal")
        if not self.propose and not (self.no_change_reason or "").strip():
            raise ValueError("declining to propose requires a stated reason")
        return self


# ======================================================================
# performance and promotion (v2.5 §12, §20)
# ======================================================================

class StrategyPerformance(StrictModel):
    strategy_id: str
    observations: int = Field(ge=0)
    closed_trades: int = Field(ge=0)
    total_pnl: float
    return_pct: float
    win_rate: float | None = Field(default=None, ge=0, le=1)
    expectancy: float | None = None
    max_drawdown_pct: float = Field(ge=0)
    average_win: float | None = None
    average_loss: float | None = None
    profit_factor: float | None = None
    event_pnl: float = 0.0
    momentum_pnl: float = 0.0
    execution_bias_mean: float | None = None
    execution_bias_median: float | None = None
    # How much of the challenger comparison could NOT be measured (e.g. it
    # would have traded where the champion did not, and no shadow mark
    # exists). Stated, never hidden: a comparison that quietly drops the
    # unmeasurable half is how fake edges get manufactured.
    unmeasured_observations: int = Field(default=0, ge=0)


class PromotionRecommendation(StrictModel):
    champion_id: str
    challenger_id: str
    generated_at: datetime
    champion_performance: StrategyPerformance
    challenger_performance: StrategyPerformance
    recommendation: Literal["KEEP_CHAMPION", "CONTINUE_SHADOW",
                            "PROMOTE_CHALLENGER"]
    evidence_strength: Literal["INSUFFICIENT", "LOW", "MEDIUM", "HIGH"]
    reasons: list[str] = Field(default_factory=list)
    failed_promotion_rules: list[str] = Field(default_factory=list)
    operator_approval_required: bool = True

    @model_validator(mode="after")
    def _promotion_needs_clean_rules(self) -> "PromotionRecommendation":
        """A failed hard rule cannot be argued past (v2.5 §17.7)."""
        if (self.recommendation == "PROMOTE_CHALLENGER"
                and self.failed_promotion_rules):
            raise ValueError(
                "PROMOTE_CHALLENGER with failed promotion rules: "
                f"{self.failed_promotion_rules}")
        return self
