"""
Alpha Council v2.3 - intelligence models.

Place at: alpha_council/models/intelligence.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import Direction, SourceTier

SCORE = Field(ge=0, le=100)


class IntelligenceItem(StrictModel):
    """One retrieved document. Deduplication is scoped per source, so two
    independent outlets publishing identical text both persist and can be
    counted as separate corroboration clusters (spec Section 8.1)."""

    item_id: str
    source_id: str
    source_native_id: str | None = None
    source_tier: SourceTier
    retrieved_at: datetime
    published_at: datetime | None = None
    updated_at: datetime | None = None
    url: str | None = None
    canonical_url: str | None = None
    title: str
    summary: str | None = None
    content_text: str | None = None
    content_hash: str
    duplicate_cluster_id: str | None = None
    symbols: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s.strip()]

    @property
    def effective_timestamp(self) -> datetime:
        """Freshness uses the earliest trustworthy publication time, never
        retrieval time (spec Section 10.2)."""
        return self.published_at or self.retrieved_at


class IntelligenceEvent(StrictModel):
    """A scored, symbol-attributed event derived from an item."""

    event_id: str
    item_id: str
    symbol: str
    event_type: str
    direction: Direction
    direction_confidence: float = Field(ge=0, le=1)

    source_reliability_score: float = SCORE
    freshness_score: float = SCORE
    novelty_score: float = SCORE
    corroboration_score: float = SCORE
    materiality_score: float = SCORE
    surprise_score: float = SCORE
    market_confirmation_score: float = SCORE
    catalyst_score: float = SCORE

    provisional: bool = False  # event younger than 5 minutes
    extracted_facts: list[str] = Field(default_factory=list)
    evidence_urls: list[str] = Field(default_factory=list)
    created_at: datetime

    @property
    def signed_direction(self) -> float:
        """Direction with confidence applied, for Section 12.2."""
        return self.direction.sign * self.direction_confidence

    def recompute_catalyst_score(self, weights: dict[str, float]) -> float:
        """Deterministic. Novelty and corroboration are deliberately excluded
        here because they carry separate weights in the Opportunity Score."""
        return (
            weights["materiality"] * self.materiality_score
            + weights["freshness"] * self.freshness_score
            + weights["source_reliability"] * self.source_reliability_score
            + weights["market_confirmation"] * self.market_confirmation_score
            + weights["surprise"] * self.surprise_score
        )
