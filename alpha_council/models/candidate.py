"""
Alpha Council v2.4 - candidate scoring and analyst output models.

v2.4: candidates carry a track (EVENT / MOMENTUM / CALIBRATION) and a
discovery source. The MOMENTUM track has no catalyst by definition, so
catalyst/corroboration/novelty are optional and MUST NOT be filled with a
fabricated neutral 50 — that would invent a directional opinion the
evidence does not support.

Place at: alpha_council/models/candidate.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import CandidateTrack, DiscoverySource, Direction

SCORE = Field(ge=0, le=100)
FACTOR = Field(ge=0, le=1)


class CandidateFeatures(StrictModel):
    symbol: str
    as_of: datetime
    direction: Direction
    combined_direction: float = Field(ge=-1, le=1)

    track: CandidateTrack = CandidateTrack.EVENT
    discovery_source: DiscoverySource = DiscoverySource.CORE

    # technical components - always present
    momentum_score: float = SCORE
    relative_volume_score: float = SCORE
    trend_regime_score: float = SCORE
    relative_strength_score: float = SCORE

    # options components - zero until the options pre-screen runs
    options_opportunity_score: float = Field(default=0.0, ge=0, le=100)
    options_liquidity_score: float = Field(default=0.0, ge=0, le=100)

    # intelligence components - None on the MOMENTUM track, never faked
    catalyst_score: float | None = Field(default=None, ge=0, le=100)
    corroboration_score: float | None = Field(default=None, ge=0, le=100)
    novelty_score: float | None = Field(default=None, ge=0, le=100)

    data_confidence_factor: float = FACTOR
    regime_factor: float = FACTOR
    event_risk_factor: float = FACTOR

    fast_score: float = Field(default=0.0, ge=0, le=100)
    pre_score: float = SCORE
    raw_opportunity_score: float = Field(default=0.0, ge=0, le=100)
    final_opportunity_score: float = Field(default=0.0, ge=0, le=100)

    config_version: str = "v2.4"
    tier: int = Field(default=1, ge=1, le=3)
    key_metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _direction_matches_signal(self) -> "CandidateFeatures":
        if self.direction is Direction.BULLISH and self.combined_direction < 0:
            raise ValueError("BULLISH candidate with negative combined_direction")
        if self.direction is Direction.BEARISH and self.combined_direction > 0:
            raise ValueError("BEARISH candidate with positive combined_direction")
        return self

    @model_validator(mode="after")
    def _track_requires_matching_evidence(self) -> "CandidateFeatures":
        """EVENT needs a catalyst. MOMENTUM must not carry one.

        The second half matters: reusing an EVENT scorer on a MOMENTUM
        candidate silently reintroduces the ~50-neutral catalyst drag that
        makes the two tracks incomparable.
        """
        if self.track is CandidateTrack.EVENT:
            if self.catalyst_score is None:
                raise ValueError("EVENT track requires a catalyst_score")
            if self.corroboration_score is None or self.novelty_score is None:
                raise ValueError(
                    "EVENT track requires corroboration and novelty scores"
                )
        elif self.track is CandidateTrack.MOMENTUM:
            if any(s is not None for s in (self.catalyst_score,
                                           self.corroboration_score,
                                           self.novelty_score)):
                raise ValueError(
                    "MOMENTUM track must leave catalyst/corroboration/novelty "
                    "unset rather than fabricating a neutral value"
                )
        return self

    @model_validator(mode="after")
    def _final_is_product_of_raw(self) -> "CandidateFeatures":
        """FinalOpportunityScore = Raw * DataConfidence * Regime * EventRisk."""
        if self.raw_opportunity_score == 0.0:
            return self
        expected = (self.raw_opportunity_score * self.data_confidence_factor
                    * self.regime_factor * self.event_risk_factor)
        if abs(expected - self.final_opportunity_score) > 0.01:
            raise ValueError(
                f"final_opportunity_score {self.final_opportunity_score:.4f} "
                f"!= raw * factors {expected:.4f}"
            )
        return self

    @model_validator(mode="after")
    def _dynamic_source_is_not_core(self) -> "CandidateFeatures":
        if (self.discovery_source is DiscoverySource.SEC_EVENT
                and self.track is CandidateTrack.MOMENTUM):
            raise ValueError(
                "a SEC-injected symbol on the MOMENTUM track discards the very "
                "evidence that surfaced it; use the EVENT track"
            )
        return self

    @property
    def blocked_by_event_risk(self) -> bool:
        return self.event_risk_factor == 0.0

    @property
    def is_alpha_candidate(self) -> bool:
        return self.track.is_alpha

    def opportunity_weight_key(self) -> str:
        """Which weight set in scoring.yaml applies to this candidate."""
        return ("opportunity_weights_momentum"
                if self.track is CandidateTrack.MOMENTUM
                else "opportunity_weights_event")

    def pre_score_weight_key(self) -> str:
        return ("pre_score_weights_momentum"
                if self.track is CandidateTrack.MOMENTUM
                else "pre_score_weights_event")


class AnalystAssessment(StrictModel):
    """Structured output contract for Bull, Bear, and Catalyst agents."""

    symbol: str
    analyst: Literal["BULL", "BEAR", "CATALYST"]
    score: float = SCORE
    confidence: float = Field(ge=0, le=1)
    thesis: str
    evidence_for: list[str]
    evidence_against: list[str]
    missing_information: list[str]
    invalidation_conditions: list[str]
    source_event_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_one_sided(self) -> "AnalystAssessment":
        if not self.evidence_for and not self.evidence_against:
            raise ValueError("assessment contains no evidence at all")
        return self
