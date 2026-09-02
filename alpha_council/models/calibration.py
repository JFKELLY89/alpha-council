"""
Alpha Council v2.4 - execution fill calibration.

Measures the gap between what the Indicative feed said a spread was worth
and what Alpaca actually filled it at. This is measurement, not machine
learning: a rolling median over a handful of fills, used to nudge the
initial limit by a bounded amount.

The engine exists because indicative quotes are derived rather than OPRA
NBBO. Timestamp freshness does not make an indicative mid executable, so
the only honest way to know the bias is to measure it against real fills.

Place at: alpha_council/models/calibration.py
"""

from __future__ import annotations

import statistics
from datetime import datetime

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import CandidateTrack, Direction, OrderSide


class ExecutionCalibration(StrictModel):
    """One record per submitted opening or closing spread."""

    calibration_id: str
    decision_id: str
    symbol: str
    side: OrderSide
    candidate_track: CandidateTrack
    direction: Direction

    submitted_at: datetime
    filled_at: datetime | None = None

    indicative_raw_mid: float = Field(gt=0)
    indicative_adjusted_mid: float = Field(gt=0)
    natural_debit_estimate: float = Field(gt=0)
    initial_limit_debit: float = Field(gt=0)
    final_submitted_limit: float = Field(gt=0)
    actual_fill_debit: float | None = Field(default=None, gt=0)

    seconds_to_fill: float | None = Field(default=None, ge=0)
    limit_walk_steps: int = Field(default=0, ge=0, le=3)

    quote_lag_seconds: float = Field(ge=0)
    underlying_at_quote: float = Field(gt=0)
    underlying_at_submit: float = Field(gt=0)
    underlying_at_fill: float | None = Field(default=None, gt=0)

    fill_bias_vs_adjusted: float | None = None
    fill_bias_vs_limit: float | None = None
    fill_slippage_pct: float | None = None

    @model_validator(mode="after")
    def _fill_fields_are_all_or_nothing(self) -> "ExecutionCalibration":
        filled = self.actual_fill_debit is not None
        if filled and self.filled_at is None:
            raise ValueError("a fill requires filled_at")
        if not filled and self.filled_at is not None:
            raise ValueError("filled_at without a fill price")
        return self

    @model_validator(mode="after")
    def _limit_never_exceeds_natural(self) -> "ExecutionCalibration":
        """Opening debits walk UP toward natural and must stop there.

        Closing credits walk DOWN toward the conservative floor, so the
        inequality inverts and is enforced by the close-walk ladder itself;
        here the closing constraint is only that the final credit demanded
        never fell below that floor.
        """
        if self.side is OrderSide.OPEN:
            if self.final_submitted_limit > self.natural_debit_estimate + 1e-9:
                raise ValueError(
                    f"submitted limit {self.final_submitted_limit} exceeds "
                    f"natural debit {self.natural_debit_estimate}; the limit "
                    "walk must stop at natural")
        else:
            if self.final_submitted_limit < self.natural_debit_estimate - 1e-9:
                raise ValueError(
                    f"closing credit {self.final_submitted_limit} fell below "
                    f"the conservative floor {self.natural_debit_estimate}")
        return self

    @model_validator(mode="after")
    def _derived_metrics_consistent(self) -> "ExecutionCalibration":
        if self.actual_fill_debit is None:
            return self
        expected_adj = self.actual_fill_debit - self.indicative_adjusted_mid
        expected_lim = self.actual_fill_debit - self.initial_limit_debit
        expected_pct = expected_adj / max(self.indicative_adjusted_mid, 0.01)
        for label, expected, actual in (
            ("fill_bias_vs_adjusted", expected_adj, self.fill_bias_vs_adjusted),
            ("fill_bias_vs_limit", expected_lim, self.fill_bias_vs_limit),
            ("fill_slippage_pct", expected_pct, self.fill_slippage_pct),
        ):
            if actual is not None and abs(expected - actual) > 0.005:
                raise ValueError(f"{label}: expected {expected:.4f}, got {actual:.4f}")
        return self

    @classmethod
    def with_derived(cls, **kwargs) -> "ExecutionCalibration":
        """Construct with the three derived metrics computed for you."""
        fill = kwargs.get("actual_fill_debit")
        if fill is not None:
            adj = kwargs["indicative_adjusted_mid"]
            kwargs["fill_bias_vs_adjusted"] = fill - adj
            kwargs["fill_bias_vs_limit"] = fill - kwargs["initial_limit_debit"]
            kwargs["fill_slippage_pct"] = (fill - adj) / max(adj, 0.01)
        return cls(**kwargs)

    @property
    def is_usable_for_learning(self) -> bool:
        """Never learn from a stale quote or an unfilled order.

        Calibration-track fills count too — measuring the indicated-to-fill
        bias is exactly what they exist for. (The previous and/or chain
        reduced to this expression; it is now written as what it means.)
        """
        return (self.actual_fill_debit is not None
                and self.quote_lag_seconds <= 900)

    @property
    def underlying_drift_during_order(self) -> float:
        if self.underlying_at_fill is None:
            return 0.0
        return self.underlying_at_fill - self.underlying_at_submit


class FillBiasEstimate(StrictModel):
    """Rolling summary used to nudge the initial limit debit.

    Bounded on purpose. With a handful of fills this is a sanity correction,
    not an optimizer, and it can never override natural debit or a risk limit.
    """

    side: OrderSide
    direction: Direction | None = None      # None means pooled across directions
    sample_size: int = Field(ge=0)
    median_bias: float = 0.0
    p80_bias: float = 0.0
    median_seconds_to_fill: float | None = None
    mean_limit_walk_steps: float = 0.0
    applied_buffer: float = Field(default=0.0, ge=0)
    computed_at: datetime

    @model_validator(mode="after")
    def _buffer_requires_a_sample(self) -> "FillBiasEstimate":
        if self.applied_buffer > 0 and self.sample_size < 3:
            raise ValueError(
                "a learned buffer requires at least 3 fills; "
                f"got {self.sample_size}"
            )
        return self

    @classmethod
    def from_records(
        cls,
        records: list[ExecutionCalibration],
        side: OrderSide,
        computed_at: datetime,
        direction: Direction | None = None,
        min_fills: int = 3,
        max_abs: float = 0.10,
        max_pct: float = 0.05,
    ) -> "FillBiasEstimate":
        usable = [
            r for r in records
            if r.side is side
            and r.fill_bias_vs_adjusted is not None
            and r.quote_lag_seconds <= 900
            and (direction is None or r.direction is direction)
        ]
        biases = [r.fill_bias_vs_adjusted for r in usable]  # type: ignore[misc]

        if len(biases) < min_fills:
            return cls(side=side, direction=direction, sample_size=len(biases),
                       computed_at=computed_at)

        biases_sorted = sorted(biases)
        idx80 = min(len(biases_sorted) - 1,
                    int(round(0.80 * (len(biases_sorted) - 1))))
        median = statistics.median(biases_sorted)
        p80 = biases_sorted[idx80]

        mids = [r.indicative_adjusted_mid for r in usable]
        pct_cap = max_pct * statistics.median(mids)
        buffer = max(0.0, min(median, max_abs, pct_cap))

        fill_times = [r.seconds_to_fill for r in usable if r.seconds_to_fill is not None]
        return cls(
            side=side,
            direction=direction,
            sample_size=len(biases),
            median_bias=round(median, 4),
            p80_bias=round(p80, 4),
            median_seconds_to_fill=(round(statistics.median(fill_times), 1)
                                    if fill_times else None),
            mean_limit_walk_steps=round(
                statistics.mean([r.limit_walk_steps for r in usable]), 2),
            applied_buffer=round(buffer, 4),
            computed_at=computed_at,
        )
