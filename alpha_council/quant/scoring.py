"""
Alpha Council v2.4 - track-aware candidate scoring.

EVENT and MOMENTUM candidates are scored with different weight sets and
ranked SEPARATELY with quotas. That separation is not cosmetic: an EVENT
candidate with a weak catalyst carries a ~50-neutral drag through 20% of
its score, while a MOMENTUM candidate has no catalyst term at all. Merging
them into one list and applying one floor systematically favors MOMENTUM
for reasons that have nothing to do with trade quality.

A MOMENTUM candidate never receives a fabricated catalyst value. The
models reject one outright, because inventing a neutral 50 is inventing a
directional opinion the evidence does not support.

Place at: alpha_council/quant/scoring.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from alpha_council.models.candidate import CandidateFeatures
from alpha_council.models.enums import CandidateTrack, DiscoverySource, Direction
from alpha_council.models.intelligence import IntelligenceEvent
from alpha_council.quant.discovery import Stage0Result
from alpha_council.utils.math import clip, weighted_sum

# A catalyst must be at least this strong, and this fresh, to make a
# candidate EVENT rather than MOMENTUM. Below the bar the intelligence is
# background context, not a reason to trade.
EVENT_CATALYST_FLOOR = 55.0
EVENT_FRESHNESS_FLOOR = 40.0

DEFAULT_PRE_WEIGHTS_EVENT = {
    "momentum": 0.20, "relative_volume": 0.20, "trend_regime": 0.15,
    "relative_strength": 0.15, "catalyst": 0.20, "corroboration": 0.05,
    "novelty": 0.05,
}
DEFAULT_PRE_WEIGHTS_MOMENTUM = {
    "momentum": 0.30, "relative_volume": 0.30, "trend_regime": 0.20,
    "relative_strength": 0.20,
}
DEFAULT_OPP_WEIGHTS_EVENT = {
    "momentum": 0.15, "relative_volume": 0.15, "trend_regime": 0.10,
    "relative_strength": 0.10, "options_opportunity": 0.10,
    "options_liquidity": 0.10, "catalyst": 0.20, "corroboration": 0.05,
    "novelty": 0.05,
}
DEFAULT_OPP_WEIGHTS_MOMENTUM = {
    "momentum": 0.22, "relative_volume": 0.22, "trend_regime": 0.14,
    "relative_strength": 0.14, "options_opportunity": 0.14,
    "options_liquidity": 0.14,
}


@dataclass(slots=True)
class IntelSummary:
    """Aggregated intelligence for one symbol at one moment."""

    catalyst_score: float = 0.0
    corroboration_score: float = 0.0
    novelty_score: float = 0.0
    freshness_score: float = 0.0
    direction: Direction = Direction.NEUTRAL
    direction_confidence: float = 0.0
    event_count: int = 0
    event_ids: list[str] = field(default_factory=list)

    @property
    def has_material_catalyst(self) -> bool:
        return (self.event_count > 0
                and self.catalyst_score >= EVENT_CATALYST_FLOOR
                and self.freshness_score >= EVENT_FRESHNESS_FLOOR)

    @property
    def signed_direction(self) -> float:
        return self.direction.sign * self.direction_confidence

    def contradicts(self, direction: Direction) -> bool:
        """True when intelligence points the other way with conviction.

        A MOMENTUM candidate may have no catalyst, but it must not be
        traded straight into a fresh contrary filing.
        """
        if self.direction is Direction.NEUTRAL or self.event_count == 0:
            return False
        if self.direction is direction:
            return False
        return (self.catalyst_score >= EVENT_CATALYST_FLOOR
                and self.direction_confidence >= 0.5)


def summarize_intel(events: Sequence[IntelligenceEvent]) -> IntelSummary:
    """Collapse a symbol's events into one summary, weighted by catalyst score."""
    if not events:
        return IntelSummary()

    strongest = max(events, key=lambda e: e.catalyst_score)
    total_weight = sum(e.catalyst_score for e in events) or 1.0

    def wavg(attr: str) -> float:
        return sum(getattr(e, attr) * e.catalyst_score
                   for e in events) / total_weight

    return IntelSummary(
        catalyst_score=clip(strongest.catalyst_score),
        corroboration_score=clip(wavg("corroboration_score")),
        novelty_score=clip(wavg("novelty_score")),
        freshness_score=clip(wavg("freshness_score")),
        direction=strongest.direction,
        direction_confidence=strongest.direction_confidence,
        event_count=len(events),
        event_ids=[e.event_id for e in events],
    )


def classify_track(intel: IntelSummary, source: DiscoverySource,
                   direction: Direction) -> CandidateTrack:
    """EVENT when fresh material evidence exists, otherwise MOMENTUM.

    A symbol injected by SEC or news that then fails the materiality bar
    falls back to MOMENTUM rather than being dropped: the price action may
    still be worth trading even when the headline is not.
    """
    if intel.has_material_catalyst:
        return CandidateTrack.EVENT
    if source is DiscoverySource.SEC_EVENT and intel.event_count > 0:
        # A filing surfaced this symbol; discarding the filing and calling
        # it momentum would throw away the reason we are looking at it.
        return CandidateTrack.EVENT
    return CandidateTrack.MOMENTUM


# ----------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------

def _components(stage0: Stage0Result, intel: IntelSummary,
                track: CandidateTrack, options_opportunity: float = 0.0,
                options_liquidity: float = 0.0) -> dict[str, float]:
    base = {
        "momentum": stage0.momentum,
        "relative_volume": stage0.relative_volume,
        "trend_regime": stage0.trend_regime,
        "relative_strength": stage0.relative_strength,
        "options_opportunity": options_opportunity,
        "options_liquidity": options_liquidity,
    }
    if track is CandidateTrack.EVENT:
        if intel.event_count == 0:
            # Scoring EVENT weights against empty intelligence would post a
            # silent catalyst of 0.0, which reads as "we assessed the news
            # and it was worthless" rather than "there was no news". The
            # caller has used the wrong track.
            raise KeyError(
                "missing score components: EVENT track requires intelligence "
                "events, got none. Use CandidateTrack.MOMENTUM instead."
            )
        base.update({
            "catalyst": intel.catalyst_score,
            "corroboration": intel.corroboration_score,
            "novelty": intel.novelty_score,
        })
    return base


def pre_score(stage0: Stage0Result, intel: IntelSummary,
              track: CandidateTrack,
              weights: dict[str, dict[str, float]] | None = None) -> float:
    """§12.5. Weight set follows the track, never the other way around."""
    w = weights or {}
    chosen = (w.get("pre_score_weights_momentum", DEFAULT_PRE_WEIGHTS_MOMENTUM)
              if track is CandidateTrack.MOMENTUM
              else w.get("pre_score_weights_event", DEFAULT_PRE_WEIGHTS_EVENT))
    return weighted_sum(_components(stage0, intel, track), chosen)


def raw_opportunity_score(stage0: Stage0Result, intel: IntelSummary,
                          track: CandidateTrack, options_opportunity: float,
                          options_liquidity: float,
                          weights: dict[str, dict[str, float]] | None = None
                          ) -> float:
    """§12.6, before the confidence, regime, and event-risk multipliers."""
    w = weights or {}
    chosen = (w.get("opportunity_weights_momentum", DEFAULT_OPP_WEIGHTS_MOMENTUM)
              if track is CandidateTrack.MOMENTUM
              else w.get("opportunity_weights_event", DEFAULT_OPP_WEIGHTS_EVENT))
    return weighted_sum(
        _components(stage0, intel, track, options_opportunity,
                    options_liquidity), chosen)


def apply_multipliers(raw: float, data_confidence: float, regime: float,
                      event_risk: float) -> float:
    return clip(raw * data_confidence * regime * event_risk)


def regime_factor(direction: Direction, benchmark_return: float,
                  has_tier1_catalyst: bool,
                  factors: dict[str, float] | None = None) -> float:
    """§12.9. A strong idiosyncratic catalyst survives a hostile tape; a
    momentum candidate fighting the benchmark does not."""
    f = factors or {"aligned": 1.00, "neutral": 0.90,
                    "idiosyncratic_catalyst": 0.80, "conflict": 0.00}
    if abs(benchmark_return) < 0.001:
        return f["neutral"]
    bench_dir = Direction.BULLISH if benchmark_return > 0 else Direction.BEARISH
    if bench_dir is direction:
        return f["aligned"]
    return f["idiosyncratic_catalyst"] if has_tier1_catalyst else f["conflict"]


def build_candidate(stage0: Stage0Result, intel: IntelSummary,
                    track: CandidateTrack, as_of: datetime,
                    data_confidence_factor: float = 1.0,
                    regime: float = 1.0, event_risk: float = 1.0,
                    options_opportunity: float = 0.0,
                    options_liquidity: float = 0.0,
                    weights: dict[str, Any] | None = None,
                    tier: int = 1,
                    config_version: str = "v2.4") -> CandidateFeatures:
    """Assemble a validated CandidateFeatures.

    The model enforces that EVENT carries intelligence scores and MOMENTUM
    does not, so a mismatch between track and weight set raises here rather
    than producing a quietly wrong ranking.
    """
    pre = pre_score(stage0, intel, track, weights)
    raw = raw_opportunity_score(stage0, intel, track, options_opportunity,
                                options_liquidity, weights)
    final = apply_multipliers(raw, data_confidence_factor, regime, event_risk)

    direction = (Direction.BULLISH if stage0.direction > 0
                 else Direction.BEARISH)
    is_event = track is CandidateTrack.EVENT

    return CandidateFeatures(
        symbol=stage0.symbol, as_of=as_of, direction=direction,
        combined_direction=stage0.combined_direction,
        track=track, discovery_source=stage0.source,
        momentum_score=stage0.momentum,
        relative_volume_score=stage0.relative_volume,
        trend_regime_score=stage0.trend_regime,
        relative_strength_score=stage0.relative_strength,
        options_opportunity_score=options_opportunity,
        options_liquidity_score=options_liquidity,
        catalyst_score=intel.catalyst_score if is_event else None,
        corroboration_score=intel.corroboration_score if is_event else None,
        novelty_score=intel.novelty_score if is_event else None,
        data_confidence_factor=data_confidence_factor,
        regime_factor=regime, event_risk_factor=event_risk,
        fast_score=stage0.fast_score,
        pre_score=pre, raw_opportunity_score=raw,
        final_opportunity_score=final,
        tier=tier, config_version=config_version,
        key_metrics={
            "rvol": stage0.metrics.get("rvol") or 0.0,
            "rs15": stage0.metrics.get("rs15") or 0.0,
            "rs60": stage0.metrics.get("rs60") or 0.0,
            "intel_events": intel.event_count,
            "data_gaps": stage0.metrics.get("data_gaps") or 0,
        },
    )


# ----------------------------------------------------------------------
# ranking
# ----------------------------------------------------------------------

@dataclass(slots=True)
class RankedSet:
    selected: list[CandidateFeatures] = field(default_factory=list)
    rejected: list[tuple[str, str, str]] = field(default_factory=list)
    event_count: int = 0
    momentum_count: int = 0
    backfilled: int = 0

    def counts(self) -> dict[str, int]:
        return {"EVENT": self.event_count, "MOMENTUM": self.momentum_count,
                "backfilled": self.backfilled}


def rank_by_track(candidates: Sequence[CandidateFeatures],
                  score_floor: float, total: int = 5,
                  quota: dict[str, int] | None = None,
                  backfill: bool = True,
                  score_attr: str = "final_opportunity_score") -> RankedSet:
    """Rank EVENT and MOMENTUM separately, then fill by quota.

    Default quota is 3 EVENT / 2 MOMENTUM. Unfilled slots in either track
    are backfilled from the other, so a session with no fresh news still
    produces a full candidate set.
    """
    q = quota or {"EVENT": 3, "MOMENTUM": 2}
    result = RankedSet()

    eligible: dict[CandidateTrack, list[CandidateFeatures]] = {
        CandidateTrack.EVENT: [], CandidateTrack.MOMENTUM: []}

    for c in candidates:
        score = getattr(c, score_attr)
        if score < score_floor:
            result.rejected.append(
                (c.symbol, "FINAL_SCORE_FLOOR",
                 f"{score:.1f} < {score_floor:.1f} ({c.track})"))
            continue
        if c.track in eligible:
            eligible[c.track].append(c)

    for track in eligible:
        eligible[track].sort(key=lambda c: getattr(c, score_attr), reverse=True)

    for track, limit in ((CandidateTrack.EVENT, q.get("EVENT", 3)),
                         (CandidateTrack.MOMENTUM, q.get("MOMENTUM", 2))):
        taken = eligible[track][:limit]
        result.selected.extend(taken)
        if track is CandidateTrack.EVENT:
            result.event_count = len(taken)
        else:
            result.momentum_count = len(taken)
        for c in eligible[track][limit:]:
            result.rejected.append(
                (c.symbol, "TRACK_QUOTA_FULL",
                 f"{track} quota {limit} filled"))

    if backfill and len(result.selected) < total:
        room = total - len(result.selected)
        chosen = {c.symbol for c in result.selected}
        spare = [c for t in eligible for c in eligible[t]
                 if c.symbol not in chosen]
        spare.sort(key=lambda c: getattr(c, score_attr), reverse=True)
        extra = spare[:room]
        result.selected.extend(extra)
        result.backfilled = len(extra)
        # A backfilled candidate is no longer quota-rejected.
        promoted = {c.symbol for c in extra}
        result.rejected = [r for r in result.rejected
                           if not (r[0] in promoted and r[1] == "TRACK_QUOTA_FULL")]

    result.selected.sort(key=lambda c: getattr(c, score_attr), reverse=True)
    return result


def rank_for_prescreen(candidates: Sequence[CandidateFeatures],
                       pre_floor: float, top_n: int = 12) -> RankedSet:
    """Stage-1 cut: which symbols earn an option-chain fetch.

    Ranked by PreScore with the same track separation, because the same
    scale problem applies before the options scores exist.
    """
    return rank_by_track(
        candidates, score_floor=pre_floor, total=top_n,
        quota={"EVENT": (top_n + 1) // 2, "MOMENTUM": top_n // 2},
        score_attr="pre_score")
