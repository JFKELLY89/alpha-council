"""
Alpha Council v2.4 - discovery models.

The funnel is 250 -> 30 -> 12 -> 5 -> <=3 councils. FunnelSnapshot
validates that it actually narrows, so a bug that widens the funnel and
multiplies LLM cost fails loudly instead of quietly.

Place at: alpha_council/models/discovery.py
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field, model_validator

from alpha_council.models.base import StrictModel
from alpha_council.models.enums import (
    DiscoveryDisableReason,
    DiscoverySource,
)


class DiscoveryCandidate(StrictModel):
    """A symbol admitted to the Dynamic Discovery Universe for one scan."""

    symbol: str
    discovered_at: datetime
    expires_at: datetime | None = None
    source: DiscoverySource
    source_rank: int | None = Field(default=None, ge=1)
    discovery_reason: str
    is_core: bool

    asset_tradable: bool
    has_options: bool
    data_density_ok: bool

    fast_score: float = Field(default=0.0, ge=0, le=100)
    discovery_boost: float = Field(default=0.0, ge=0, le=100)

    @model_validator(mode="after")
    def _core_never_expires(self) -> "DiscoveryCandidate":
        if self.is_core and self.expires_at is not None:
            raise ValueError("Core symbols are permanent and must not carry a TTL")
        if not self.is_core and self.source is not DiscoverySource.CORE:
            if self.expires_at is None:
                raise ValueError(f"{self.source} membership requires an expiry")
        return self

    @model_validator(mode="after")
    def _source_matches_core_flag(self) -> "DiscoveryCandidate":
        if (self.source is DiscoverySource.CORE) != self.is_core:
            raise ValueError("source CORE and is_core must agree")
        return self

    @model_validator(mode="after")
    def _reason_present(self) -> "DiscoveryCandidate":
        if not self.discovery_reason.strip():
            raise ValueError(
                "discovery_reason is required; the dashboard must be able to "
                "answer 'why did Alpha Council notice this symbol?'"
            )
        return self

    @property
    def eligible(self) -> bool:
        """All three checks must pass before any option-chain work."""
        return self.asset_tradable and self.has_options and self.data_density_ok

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    @staticmethod
    def ttl_expiry(discovered_at: datetime, ttl_minutes: int) -> datetime:
        return discovered_at + timedelta(minutes=ttl_minutes)


class DiscoverySourceStatus(StrictModel):
    """Session state for an optional discovery source.

    A 403 disables the source for the session and is logged once. It never
    fails a scan, and it is never retried every cycle.
    """

    source: DiscoverySource
    enabled: bool
    probed_at: datetime | None = None
    disabled_at: datetime | None = None
    disable_reason: DiscoveryDisableReason | None = None
    symbols_contributed: int = Field(default=0, ge=0)
    consecutive_errors: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _disabled_has_a_reason(self) -> "DiscoverySourceStatus":
        if not self.enabled and self.disable_reason is None:
            raise ValueError("a disabled source must record why")
        if self.disable_reason is not None and self.enabled:
            raise ValueError("a source with a disable reason must not be enabled")
        return self

    @model_validator(mode="after")
    def _required_sources_cannot_be_disabled(self) -> "DiscoverySourceStatus":
        if not self.source.is_optional and not self.enabled:
            raise ValueError(f"{self.source} is not optional and cannot be disabled")
        return self


class FunnelSnapshot(StrictModel):
    """One row per scan. The narrowing invariant is enforced here because a
    funnel that widens multiplies option-chain fetches and LLM spend."""

    scan_id: str
    as_of: datetime
    discovery_count: int = Field(ge=0)
    stage0_survivors: int = Field(ge=0)
    prescore_survivors: int = Field(ge=0)
    options_prescreened: int = Field(ge=0)
    final_candidates: int = Field(ge=0)
    councils_started: int = Field(ge=0)

    event_track_count: int = Field(default=0, ge=0)
    momentum_track_count: int = Field(default=0, ge=0)
    source_counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _monotonically_narrows(self) -> "FunnelSnapshot":
        stages = [
            ("discovery", self.discovery_count),
            ("stage0", self.stage0_survivors),
            ("prescore", self.prescore_survivors),
            ("options_prescreen", self.options_prescreened),
            ("final", self.final_candidates),
            ("councils", self.councils_started),
        ]
        for (prev_name, prev), (name, current) in zip(stages, stages[1:]):
            if current > prev:
                raise ValueError(
                    f"funnel widened at {name}: {current} > {prev_name} {prev}"
                )
        return self

    @model_validator(mode="after")
    def _track_counts_reconcile(self) -> "FunnelSnapshot":
        tracked = self.event_track_count + self.momentum_track_count
        if tracked > self.final_candidates:
            raise ValueError(
                f"track counts {tracked} exceed final candidates {self.final_candidates}"
            )
        return self

    @property
    def survival_rate(self) -> float:
        if self.discovery_count == 0:
            return 0.0
        return self.councils_started / self.discovery_count
