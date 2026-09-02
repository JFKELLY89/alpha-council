"""
Alpha Council v2.4 - track-aware scoring tests.

The central property: EVENT and MOMENTUM are ranked separately, because a
merged list on two different scales systematically favors MOMENTUM.

Place at: tests/test_scoring.py

Run:
    uv run pytest tests/test_scoring.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alpha_council.models.enums import CandidateTrack, DiscoverySource, Direction
from alpha_council.models.intelligence import IntelligenceEvent
from alpha_council.quant.discovery import Stage0Result
from alpha_council.quant.indicators import IndicatorSet
from alpha_council.quant.scoring import (
    DEFAULT_OPP_WEIGHTS_EVENT,
    DEFAULT_OPP_WEIGHTS_MOMENTUM,
    DEFAULT_PRE_WEIGHTS_EVENT,
    DEFAULT_PRE_WEIGHTS_MOMENTUM,
    IntelSummary,
    apply_multipliers,
    build_candidate,
    classify_track,
    pre_score,
    rank_by_track,
    raw_opportunity_score,
    regime_factor,
    summarize_intel,
)

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)


def _stage0(symbol="NVDA", fast=70.0, direction=1, mom=75.0, rvol=70.0,
            rs=65.0, trend=75.0,
            source=DiscoverySource.CORE) -> Stage0Result:
    return Stage0Result(
        symbol=symbol, fast_score=fast, direction=direction,
        combined_direction=0.45 * direction, momentum=mom,
        relative_volume=rvol, relative_strength=rs, trend_regime=trend,
        discovery_boost=40.0, source=source,
        indicators=IndicatorSet(symbol=symbol, as_of=NOW, last_price=100.0,
                                bars_used=100),
        metrics={"rvol": 2.0, "rs15": 0.004, "rs60": 0.008, "data_gaps": 0},
    )


def _event(catalyst=80.0, direction=Direction.BULLISH, conf=0.8,
           fresh=90.0, event_id="e1") -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=event_id, item_id="i1", symbol="NVDA", event_type="8-K",
        direction=direction, direction_confidence=conf,
        source_reliability_score=100.0, freshness_score=fresh,
        novelty_score=85.0, corroboration_score=70.0, materiality_score=90.0,
        surprise_score=60.0, market_confirmation_score=75.0,
        catalyst_score=catalyst, created_at=NOW,
    )


# ======================================================================
# intelligence summary
# ======================================================================

def test_empty_intel_is_neutral():
    s = IntelSummary()
    assert not s.has_material_catalyst
    assert s.event_count == 0
    assert not s.contradicts(Direction.BULLISH)


def test_summary_takes_the_strongest_catalyst():
    s = summarize_intel([_event(catalyst=40.0, event_id="a"),
                         _event(catalyst=88.0, event_id="b")])
    assert s.catalyst_score == pytest.approx(88.0)
    assert s.event_count == 2


def test_weak_catalyst_is_not_material():
    assert not summarize_intel([_event(catalyst=40.0)]).has_material_catalyst


def test_stale_catalyst_is_not_material():
    assert not summarize_intel([_event(catalyst=85.0, fresh=20.0)
                                ]).has_material_catalyst


def test_contradiction_detected():
    bearish = summarize_intel([_event(direction=Direction.BEARISH, conf=0.8)])
    assert bearish.contradicts(Direction.BULLISH)
    assert not bearish.contradicts(Direction.BEARISH)


def test_weak_contrary_intel_does_not_contradict():
    weak = summarize_intel([_event(direction=Direction.BEARISH,
                                   catalyst=40.0, conf=0.3)])
    assert not weak.contradicts(Direction.BULLISH)


# ======================================================================
# track classification
# ======================================================================

def test_material_catalyst_makes_it_an_event():
    intel = summarize_intel([_event()])
    assert classify_track(intel, DiscoverySource.ALPACA_NEWS,
                          Direction.BULLISH) is CandidateTrack.EVENT


def test_no_catalyst_makes_it_momentum():
    assert classify_track(IntelSummary(), DiscoverySource.MOVER,
                          Direction.BULLISH) is CandidateTrack.MOMENTUM


def test_weak_news_falls_back_to_momentum():
    """A headline that fails the materiality bar should not discard the
    price action that also surfaced the symbol."""
    weak = summarize_intel([_event(catalyst=45.0)])
    assert classify_track(weak, DiscoverySource.ALPACA_NEWS,
                          Direction.BULLISH) is CandidateTrack.MOMENTUM


def test_sec_injection_stays_on_the_event_track():
    weak = summarize_intel([_event(catalyst=45.0)])
    assert classify_track(weak, DiscoverySource.SEC_EVENT,
                          Direction.BULLISH) is CandidateTrack.EVENT


# ======================================================================
# scoring
# ======================================================================

def test_event_prescore_uses_event_weights():
    s = _stage0(mom=80.0, rvol=70.0, trend=75.0, rs=65.0)
    intel = summarize_intel([_event(catalyst=80.0)])
    expected = (0.20 * 80 + 0.20 * 70 + 0.15 * 75 + 0.15 * 65
                + 0.20 * 80 + 0.05 * intel.corroboration_score
                + 0.05 * intel.novelty_score)
    assert pre_score(s, intel, CandidateTrack.EVENT) == pytest.approx(expected)


def test_momentum_prescore_reallocates_the_catalyst_weight():
    s = _stage0(mom=80.0, rvol=70.0, trend=75.0, rs=65.0)
    expected = 0.30 * 80 + 0.30 * 70 + 0.20 * 75 + 0.20 * 65
    assert pre_score(s, IntelSummary(),
                     CandidateTrack.MOMENTUM) == pytest.approx(expected)


def test_weight_sets_sum_to_one():
    for weights in (DEFAULT_PRE_WEIGHTS_EVENT, DEFAULT_PRE_WEIGHTS_MOMENTUM,
                    DEFAULT_OPP_WEIGHTS_EVENT, DEFAULT_OPP_WEIGHTS_MOMENTUM):
        assert sum(weights.values()) == pytest.approx(1.0)


def test_momentum_scores_higher_on_identical_technicals():
    """The reason tracks are ranked separately.

    Identical price action, but the EVENT candidate carries a mediocre
    catalyst through 30% of its weight while MOMENTUM does not.
    """
    s = _stage0(mom=85.0, rvol=80.0, trend=75.0, rs=75.0)
    mediocre = summarize_intel([_event(catalyst=56.0)])
    event = pre_score(s, mediocre, CandidateTrack.EVENT)
    momentum = pre_score(s, IntelSummary(), CandidateTrack.MOMENTUM)
    assert momentum > event


def test_final_score_applies_all_three_multipliers():
    assert apply_multipliers(80.0, 0.92, 0.90, 1.0) == pytest.approx(66.24)
    assert apply_multipliers(80.0, 1.0, 1.0, 0.0) == 0.0


def test_missing_component_raises_rather_than_scoring_zero():
    """A silently zeroed component is indistinguishable from a weak one."""
    s = _stage0()
    with pytest.raises(KeyError, match="missing score components"):
        pre_score(s, IntelSummary(), CandidateTrack.EVENT)


# ======================================================================
# regime factor
# ======================================================================

def test_regime_aligned_and_neutral():
    assert regime_factor(Direction.BULLISH, 0.004, False) == 1.00
    assert regime_factor(Direction.BULLISH, 0.0002, False) == 0.90


def test_regime_conflict_without_a_catalyst_is_fatal():
    assert regime_factor(Direction.BULLISH, -0.006, False) == 0.00


def test_strong_catalyst_survives_a_hostile_tape():
    assert regime_factor(Direction.BULLISH, -0.006, True) == 0.80


# ======================================================================
# candidate construction
# ======================================================================

def test_event_candidate_carries_intelligence_scores():
    c = build_candidate(_stage0(), summarize_intel([_event()]),
                        CandidateTrack.EVENT, NOW,
                        options_opportunity=76.0, options_liquidity=80.0)
    assert c.track is CandidateTrack.EVENT
    assert c.catalyst_score is not None
    assert c.final_opportunity_score == pytest.approx(c.raw_opportunity_score)


def test_momentum_candidate_leaves_intelligence_unset():
    c = build_candidate(_stage0(), IntelSummary(), CandidateTrack.MOMENTUM,
                        NOW, options_opportunity=76.0, options_liquidity=80.0)
    assert c.catalyst_score is None
    assert c.corroboration_score is None
    assert c.novelty_score is None


def test_multipliers_flow_into_the_final_score():
    c = build_candidate(_stage0(), IntelSummary(), CandidateTrack.MOMENTUM,
                        NOW, data_confidence_factor=0.92, regime=0.90,
                        options_opportunity=76.0, options_liquidity=80.0)
    assert c.final_opportunity_score == pytest.approx(
        c.raw_opportunity_score * 0.92 * 0.90, abs=0.01)


def test_blackout_zeroes_the_candidate():
    c = build_candidate(_stage0(), IntelSummary(), CandidateTrack.MOMENTUM,
                        NOW, event_risk=0.0)
    assert c.final_opportunity_score == 0.0
    assert c.blocked_by_event_risk


# ======================================================================
# quota ranking
# ======================================================================

def _cand(symbol: str, score: float, track: CandidateTrack) -> object:
    intel = (summarize_intel([_event()]) if track is CandidateTrack.EVENT
             else IntelSummary())
    s = _stage0(symbol=symbol)
    c = build_candidate(s, intel, track, NOW, options_opportunity=70.0,
                        options_liquidity=70.0)
    object.__setattr__(c, "final_opportunity_score", score)
    return c


def test_quota_reserves_slots_for_each_track():
    """Five MOMENTUM candidates score above every EVENT candidate. Without
    quotas the EVENT track would be shut out entirely."""
    candidates = (
        [_cand(f"M{i}", 90.0 - i, CandidateTrack.MOMENTUM) for i in range(5)]
        + [_cand(f"E{i}", 72.0 - i, CandidateTrack.EVENT) for i in range(5)]
    )
    ranked = rank_by_track(candidates, score_floor=68.0, total=5)
    assert ranked.event_count == 3
    assert ranked.momentum_count == 2
    assert len(ranked.selected) == 5


def test_below_floor_candidates_are_rejected():
    ranked = rank_by_track(
        [_cand("A", 90.0, CandidateTrack.MOMENTUM),
         _cand("B", 60.0, CandidateTrack.MOMENTUM)],
        score_floor=68.0, total=5)
    assert len(ranked.selected) == 1
    assert any(r[1] == "FINAL_SCORE_FLOOR" for r in ranked.rejected)


def test_unfilled_event_quota_backfills_from_momentum():
    """A session with no fresh news still produces a full candidate set."""
    candidates = [_cand(f"M{i}", 90.0 - i, CandidateTrack.MOMENTUM)
                  for i in range(6)]
    ranked = rank_by_track(candidates, score_floor=68.0, total=5)
    assert len(ranked.selected) == 5
    assert ranked.backfilled == 3
    assert all(c.track is CandidateTrack.MOMENTUM for c in ranked.selected)


def test_backfilled_candidates_are_not_left_as_rejections():
    candidates = [_cand(f"M{i}", 90.0 - i, CandidateTrack.MOMENTUM)
                  for i in range(6)]
    ranked = rank_by_track(candidates, score_floor=68.0, total=5)
    selected = {c.symbol for c in ranked.selected}
    quota_rejected = {r[0] for r in ranked.rejected
                      if r[1] == "TRACK_QUOTA_FULL"}
    assert not (selected & quota_rejected)


def test_selection_is_sorted_by_score():
    candidates = (
        [_cand(f"E{i}", 70.0 + i, CandidateTrack.EVENT) for i in range(3)]
        + [_cand(f"M{i}", 75.0 + i, CandidateTrack.MOMENTUM) for i in range(3)]
    )
    ranked = rank_by_track(candidates, score_floor=68.0, total=5)
    scores = [c.final_opportunity_score for c in ranked.selected]
    assert scores == sorted(scores, reverse=True)


def test_backfill_can_be_disabled():
    candidates = [_cand(f"M{i}", 90.0 - i, CandidateTrack.MOMENTUM)
                  for i in range(6)]
    ranked = rank_by_track(candidates, score_floor=68.0, total=5,
                           backfill=False)
    assert len(ranked.selected) == 2
    assert ranked.backfilled == 0


def test_event_floor_prices_each_track_separately():
    """A shared final floor structurally penalizes EVENT candidates,
    whose score carries the catalyst drag MOMENTUM scoring omits."""
    ranked = rank_by_track(
        [_cand("EV", 60.5, CandidateTrack.EVENT),
         _cand("MO", 60.5, CandidateTrack.MOMENTUM)],
        score_floor={"MOMENTUM": 62.0, "EVENT": 60.0}, total=5)
    assert [c.symbol for c in ranked.selected] == ["EV"]
    assert any(r[0] == "MO" and r[1] == "FINAL_SCORE_FLOOR"
               for r in ranked.rejected)
