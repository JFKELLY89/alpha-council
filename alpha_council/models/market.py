"""
Alpha Council v2.3 - market data models.

The midpoint guard here exists because of a real observation: on 2026-08-28
after the close, Alpaca returned AAPL bid=300.93 ask=0. A naive midpoint
returns 150.47 from that. Every price path in this system routes through
QuoteObservation.midpoint(), which refuses one-sided and crossed quotes.

Place at: alpha_council/models/market.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import DataConfidence


class QuoteObservation(StrictModel):
    symbol: str
    source: Literal["ALPACA_IEX", "ALPACA_INDICATIVE"]
    observed_at: datetime
    source_timestamp: datetime | None = None
    quote_lag_seconds: float | None = Field(default=None, ge=0)
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    last: float | None = Field(default=None, ge=0)
    mark: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_two_sided(self) -> bool:
        """A quote is usable only with a positive bid AND a positive ask that
        is not below the bid. Zero or missing on either side is not a quote."""
        return (
            self.bid is not None and self.bid > 0
            and self.ask is not None and self.ask > 0
            and self.ask >= self.bid
        )

    def midpoint(self) -> float | None:
        """Quote midpoint, or the best available fallback.

        Order: two-sided mid -> mark -> last. Never returns a value derived
        from a one-sided or crossed quote.
        """
        if self.is_two_sided:
            return (self.bid + self.ask) / 2  # type: ignore[operator]
        if self.mark is not None and self.mark > 0:
            return self.mark
        if self.last is not None and self.last > 0:
            return self.last
        return None

    def spread_pct(self) -> float | None:
        if not self.is_two_sided:
            return None
        mid = (self.bid + self.ask) / 2  # type: ignore[operator]
        return (self.ask - self.bid) / mid if mid > 0 else None  # type: ignore[operator]

    def signal_price(self, prefer_last_above_spread_pct: float = 0.010) -> float | None:
        """Price used for signal calculations.

        When the quoted spread is wider than the threshold, the midpoint is
        not informative and the last trade is preferred. Observed on
        2026-08-28: AAPL quoted 318.02/319.69, a 0.52% spread, while SPY
        quoted 0.012%.
        """
        sp = self.spread_pct()
        if sp is not None and sp > prefer_last_above_spread_pct:
            if self.last is not None and self.last > 0:
                return self.last
        return self.midpoint()


class Bar(StrictModel):
    symbol: str
    source: str
    timeframe: str
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    vwap: float | None = Field(default=None, ge=0)
    trade_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ohlc_coherent(self) -> "Bar":
        if self.high < self.low:
            raise ValueError(f"high {self.high} < low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open {self.open} outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"close {self.close} outside [{self.low}, {self.high}]")
        return self

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3


class DataQualityResult(StrictModel):
    """Replaces v2.2 ConsensusResult. Single-source guard, spec Section 11."""

    symbol: str
    asset_type: Literal["EQUITY", "ETF", "OPTION"]
    evaluated_at: datetime
    source: Literal["ALPACA_IEX", "ALPACA_INDICATIVE"]
    quote_timestamp: datetime | None = None
    quote_lag_seconds: float | None = Field(default=None, ge=0)
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    raw_mid: float | None = Field(default=None, ge=0)
    adjusted_mid: float | None = Field(default=None, ge=0)
    underlying_move: float | None = None
    spread_pct: float | None = Field(default=None, ge=0)
    confidence: DataConfidence
    confidence_factor: float = Field(ge=0, le=1)
    signal_price: float | None = Field(default=None, ge=0)
    execution_reference_price: float | None = Field(default=None, ge=0)
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_present(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must explain the confidence assignment")
        return v

    @model_validator(mode="after")
    def _blocked_has_no_prices(self) -> "DataQualityResult":
        if self.confidence is DataConfidence.BLOCKED:
            if self.confidence_factor != 0.0:
                raise ValueError("BLOCKED requires confidence_factor 0.0")
            if self.signal_price is not None:
                raise ValueError("BLOCKED must not carry a signal_price")
        return self

    @property
    def tradable(self) -> bool:
        return self.confidence.tradable
