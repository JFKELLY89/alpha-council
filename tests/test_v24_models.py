"""
Alpha Council v2.4 - tests for discovery, tracks, and fill calibration.

Supplements tests/test_models.py, which still passes unchanged because
CandidateFeatures defaults to the EVENT track.

Place at: tests/test_v24_models.py

Run:
    uv run pytest tests/test_v24_models.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from alpha_council.models import (
    CandidateFeatures,
    CandidateTrack,
    Direction,
    DiscoveryCandidate,
    DiscoveryDisableReason,
    DiscoverySource,
    DiscoverySourceStatus,
    ExecutionCalibration,
    FillBiasEstimate,
    FunnelSnapshot,
    GateStage,
    OrderSide,
)

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)


# ======================================================================
# DiscoverySource
# ======================================================================

def test_optional_sources_identified():
    assert DiscoverySource.MOST_ACTIVE.is_optional
    assert DiscoverySource.MOVER.is_optional
    assert DiscoverySource.SEC_EVENT.is_optional
    assert not DiscoverySource.CORE.is_optional
    assert not DiscoverySource.ALPACA_NEWS.is_optional


def test_event_bearing_sources():
    assert DiscoverySource.ALPACA_NEWS.is_event_bearing
    assert DiscoverySource.SEC_EVENT.is_event_bearing
    assert not DiscoverySource.MOVER.is_event_bearing
    assert not DiscoverySource.CORE.is_event_bearing


def test_only_core_is_permanent():
    assert not DiscoverySource.CORE.expires
    assert DiscoverySource.MOVER.expires


# ======================================================================
# DiscoveryCandidate
# ======================================================================

def _discovery(**kw) -> DiscoveryCandidate:
    base = dict(
        symbol="ABCD", discovered_at=NOW,
        expires_at=NOW + timedelta(minutes=90),
        source=DiscoverySource.ALPACA_NEWS, source_rank=3,
        discovery_reason="8-K filed 12 minutes ago, price +3.2% on 4x RVOL",
        is_core=False, asset_tradable=True, has_options=True,
        data_density_ok=True, fast_score=71.0, discovery_boost=100.0,
    )
    return DiscoveryCandidate(**{**base, **kw})


def test_valid_dynamic_candidate():
    d = _discovery()
    assert d.eligible
    assert not d.is_expired(NOW + timedelta(minutes=30))
    assert d.is_expired(NOW + timedelta(minutes=91))


def test_core_symbol_has_no_ttl():
    core = _discovery(symbol="SPY", source=DiscoverySource.CORE, is_core=True,
                      expires_at=None, discovery_reason="core universe")
    assert core.expires_at is None
    with pytest.raises(ValidationError, match="must not carry a TTL"):
        _discovery(symbol="SPY", source=DiscoverySource.CORE, is_core=True,
                   expires_at=NOW + timedelta(minutes=90),
                   discovery_reason="core universe")


def test_dynamic_symbol_requires_ttl():
    with pytest.raises(ValidationError, match="requires an expiry"):
        _discovery(expires_at=None)


def test_source_and_core_flag_must_agree():
    with pytest.raises(ValidationError, match="must agree"):
        _discovery(source=DiscoverySource.CORE, is_core=False)
    with pytest.raises(ValidationError, match="must agree"):
        _discovery(source=DiscoverySource.MOVER, is_core=True, expires_at=None)


def test_discovery_reason_is_mandatory():
    with pytest.raises(ValidationError, match="discovery_reason is required"):
        _discovery(discovery_reason="   ")


def test_ineligible_when_any_check_fails():
    assert not _discovery(has_options=False).eligible
    assert not _discovery(asset_tradable=False).eligible
    assert not _discovery(data_density_ok=False).eligible


def test_ttl_helper():
    assert DiscoveryCandidate.ttl_expiry(NOW, 90) == NOW + timedelta(minutes=90)


# ======================================================================
# DiscoverySourceStatus - 403 fails open
# ======================================================================

def test_forbidden_source_disabled_with_reason():
    st = DiscoverySourceStatus(
        source=DiscoverySource.MOST_ACTIVE, enabled=False, probed_at=NOW,
        disabled_at=NOW, disable_reason=DiscoveryDisableReason.FORBIDDEN_403,
    )
    assert not st.enabled
    assert st.disable_reason is DiscoveryDisableReason.FORBIDDEN_403


def test_disabled_source_must_state_why():
    with pytest.raises(ValidationError, match="must record why"):
        DiscoverySourceStatus(source=DiscoverySource.MOVER, enabled=False)


def test_core_cannot_be_disabled():
    with pytest.raises(ValidationError, match="not optional"):
        DiscoverySourceStatus(
            source=DiscoverySource.CORE, enabled=False,
            disable_reason=DiscoveryDisableReason.OPERATOR_DISABLED,
        )


def test_news_cannot_be_disabled_as_optional_screener():
    """Alpaca News is a primary intelligence source, not an optional screener."""
    with pytest.raises(ValidationError, match="not optional"):
        DiscoverySourceStatus(
            source=DiscoverySource.ALPACA_NEWS, enabled=False,
            disable_reason=DiscoveryDisableReason.FORBIDDEN_403,
        )


# ======================================================================
# FunnelSnapshot - the funnel must narrow
# ======================================================================

def _funnel(**kw) -> FunnelSnapshot:
    base = dict(scan_id="s1", as_of=NOW, discovery_count=243,
                stage0_survivors=30, prescore_survivors=12,
                options_prescreened=8, final_candidates=5,
                councils_started=3, event_track_count=3,
                momentum_track_count=2)
    return FunnelSnapshot(**{**base, **kw})


def test_valid_funnel():
    f = _funnel()
    assert f.survival_rate == pytest.approx(3 / 243, abs=1e-5)


@pytest.mark.parametrize("bad", [
    {"stage0_survivors": 300},        # wider than discovery
    {"prescore_survivors": 40},       # wider than stage0
    {"final_candidates": 20},         # wider than options prescreen
    {"councils_started": 9},          # more councils than candidates
])
def test_widening_funnel_rejected(bad):
    with pytest.raises(ValidationError, match="funnel widened"):
        _funnel(**bad)


def test_track_counts_cannot_exceed_final():
    with pytest.raises(ValidationError, match="exceed final candidates"):
        _funnel(event_track_count=4, momentum_track_count=4)


def test_stage0_stages_are_cheap():
    assert GateStage.STAGE0.is_cheap_stage
    assert GateStage.DISCOVERY.is_cheap_stage
    assert not GateStage.OPTIONS_CHAIN.is_cheap_stage
    assert not GateStage.RED_TEAM.is_cheap_stage


# ======================================================================
# CandidateTrack
# ======================================================================

def _event_candidate(**kw) -> CandidateFeatures:
    base = dict(
        symbol="NVDA", as_of=NOW, direction=Direction.BULLISH,
        combined_direction=0.45, track=CandidateTrack.EVENT,
        discovery_source=DiscoverySource.ALPACA_NEWS,
        momentum_score=70.0, relative_volume_score=65.0,
        trend_regime_score=75.0, relative_strength_score=60.0,
        catalyst_score=72.0, corroboration_score=70.0, novelty_score=80.0,
        data_confidence_factor=1.0, regime_factor=1.0, event_risk_factor=1.0,
        pre_score=69.5,
    )
    return CandidateFeatures(**{**base, **kw})


def _momentum_candidate(**kw) -> CandidateFeatures:
    base = dict(
        symbol="AMD", as_of=NOW, direction=Direction.BULLISH,
        combined_direction=0.52, track=CandidateTrack.MOMENTUM,
        discovery_source=DiscoverySource.MOVER,
        momentum_score=82.0, relative_volume_score=78.0,
        trend_regime_score=75.0, relative_strength_score=71.0,
        data_confidence_factor=1.0, regime_factor=1.0, event_risk_factor=1.0,
        pre_score=77.0,
    )
    return CandidateFeatures(**{**base, **kw})


def test_event_track_requires_a_catalyst():
    with pytest.raises(ValidationError, match="requires a catalyst_score"):
        _event_candidate(catalyst_score=None)


def test_event_track_requires_corroboration_and_novelty():
    with pytest.raises(ValidationError, match="corroboration and novelty"):
        _event_candidate(corroboration_score=None)


def test_momentum_track_must_not_fabricate_a_catalyst():
    """The whole point: never inject a neutral 50 the evidence doesn't support."""
    with pytest.raises(ValidationError, match="rather than fabricating"):
        _momentum_candidate(catalyst_score=50.0)
    with pytest.raises(ValidationError, match="rather than fabricating"):
        _momentum_candidate(novelty_score=0.0)


def test_momentum_candidate_valid_without_intelligence():
    c = _momentum_candidate()
    assert c.catalyst_score is None
    assert c.pre_score_weight_key() == "pre_score_weights_momentum"
    assert c.opportunity_weight_key() == "opportunity_weights_momentum"


def test_event_candidate_uses_event_weights():
    c = _event_candidate()
    assert c.pre_score_weight_key() == "pre_score_weights_event"
    assert c.opportunity_weight_key() == "opportunity_weights_event"


def test_sec_injected_symbol_cannot_run_momentum_track():
    with pytest.raises(ValidationError, match="discards the very evidence"):
        _momentum_candidate(discovery_source=DiscoverySource.SEC_EVENT)


def test_calibration_track_is_not_alpha():
    assert not CandidateTrack.CALIBRATION.is_alpha
    assert CandidateTrack.EVENT.is_alpha
    assert CandidateTrack.MOMENTUM.is_alpha
    assert CandidateTrack.EVENT.requires_catalyst
    assert not CandidateTrack.MOMENTUM.requires_catalyst


def test_final_score_identity_still_holds_on_both_tracks():
    for maker in (_event_candidate, _momentum_candidate):
        c = maker(raw_opportunity_score=80.0, data_confidence_factor=0.92,
                  final_opportunity_score=73.6)
        assert c.final_opportunity_score == pytest.approx(73.6)
    with pytest.raises(ValidationError, match="raw \\* factors"):
        _momentum_candidate(raw_opportunity_score=80.0,
                            data_confidence_factor=0.92,
                            final_opportunity_score=80.0)


# ======================================================================
# ExecutionCalibration
# ======================================================================

def _calibration(**kw) -> ExecutionCalibration:
    base = dict(
        calibration_id="cal1", decision_id="d1", symbol="SPY",
        side=OrderSide.OPEN, candidate_track=CandidateTrack.EVENT,
        direction=Direction.BULLISH, submitted_at=NOW,
        filled_at=NOW + timedelta(seconds=22),
        indicative_raw_mid=5.40, indicative_adjusted_mid=5.48,
        natural_debit_estimate=6.10, initial_limit_debit=5.65,
        final_submitted_limit=5.65, actual_fill_debit=5.72,
        seconds_to_fill=22.0, limit_walk_steps=1, quote_lag_seconds=3.1,
        underlying_at_quote=769.20, underlying_at_submit=769.35,
        underlying_at_fill=769.41,
    )
    return ExecutionCalibration.with_derived(**{**base, **kw})


def test_derived_metrics_computed():
    c = _calibration()
    assert c.fill_bias_vs_adjusted == pytest.approx(0.24)
    assert c.fill_bias_vs_limit == pytest.approx(0.07)
    assert c.fill_slippage_pct == pytest.approx(0.24 / 5.48, abs=1e-4)


def test_limit_may_never_exceed_natural_debit():
    with pytest.raises(ValidationError, match="exceeds natural"):
        _calibration(final_submitted_limit=6.50)


def test_fill_price_and_timestamp_are_all_or_nothing():
    with pytest.raises(ValidationError, match="requires filled_at"):
        ExecutionCalibration(
            calibration_id="c", decision_id="d", symbol="SPY",
            side=OrderSide.OPEN, candidate_track=CandidateTrack.EVENT,
            direction=Direction.BULLISH, submitted_at=NOW, filled_at=None,
            indicative_raw_mid=5.4, indicative_adjusted_mid=5.48,
            natural_debit_estimate=6.1, initial_limit_debit=5.65,
            final_submitted_limit=5.65, actual_fill_debit=5.72,
            quote_lag_seconds=3.1, underlying_at_quote=769.2,
            underlying_at_submit=769.35,
        )


def test_inconsistent_derived_metric_rejected():
    with pytest.raises(ValidationError, match="fill_bias_vs_adjusted"):
        ExecutionCalibration(
            calibration_id="c", decision_id="d", symbol="SPY",
            side=OrderSide.OPEN, candidate_track=CandidateTrack.EVENT,
            direction=Direction.BULLISH, submitted_at=NOW,
            filled_at=NOW + timedelta(seconds=10),
            indicative_raw_mid=5.4, indicative_adjusted_mid=5.48,
            natural_debit_estimate=6.1, initial_limit_debit=5.65,
            final_submitted_limit=5.65, actual_fill_debit=5.72,
            quote_lag_seconds=3.1, underlying_at_quote=769.2,
            underlying_at_submit=769.35, fill_bias_vs_adjusted=99.0,
        )


def test_unfilled_order_is_valid():
    c = ExecutionCalibration(
        calibration_id="c", decision_id="d", symbol="SPY",
        side=OrderSide.OPEN, candidate_track=CandidateTrack.EVENT,
        direction=Direction.BULLISH, submitted_at=NOW,
        indicative_raw_mid=5.4, indicative_adjusted_mid=5.48,
        natural_debit_estimate=6.1, initial_limit_debit=5.65,
        final_submitted_limit=6.10, limit_walk_steps=3,
        quote_lag_seconds=3.1, underlying_at_quote=769.2,
        underlying_at_submit=769.35,
    )
    assert c.actual_fill_debit is None
    assert not c.is_usable_for_learning


# ======================================================================
# FillBiasEstimate
# ======================================================================

def test_no_buffer_below_minimum_sample():
    est = FillBiasEstimate.from_records(
        [_calibration(calibration_id=f"c{i}") for i in range(2)],
        side=OrderSide.OPEN, computed_at=NOW,
    )
    assert est.sample_size == 2
    assert est.applied_buffer == 0.0


def test_buffer_computed_from_three_fills():
    """Median bias binds: 0.07 < max_abs 0.10 < pct cap 0.274."""
    records = [_calibration(calibration_id=f"c{i}", actual_fill_debit=5.55)
               for i in range(4)]
    est = FillBiasEstimate.from_records(records, side=OrderSide.OPEN,
                                        computed_at=NOW)
    assert est.sample_size == 4
    assert est.median_bias == pytest.approx(0.07)
    assert est.applied_buffer == pytest.approx(0.07)
    assert est.median_seconds_to_fill == pytest.approx(22.0)


def test_buffer_capped_at_absolute_maximum():
    records = [_calibration(calibration_id=f"c{i}", actual_fill_debit=6.05)
               for i in range(4)]
    est = FillBiasEstimate.from_records(records, side=OrderSide.OPEN,
                                        computed_at=NOW)
    assert est.median_bias == pytest.approx(0.57)
    assert est.applied_buffer == pytest.approx(0.10)   # max_abs floor binds

def test_buffer_capped_at_percentage_of_mid_on_cheap_spreads():
    """On a $1.20 spread, 5% is $0.06 and binds before the $0.10 absolute cap."""
    records = [_calibration(calibration_id=f"p{i}",
                            indicative_raw_mid=1.15,
                            indicative_adjusted_mid=1.20,
                            natural_debit_estimate=1.60,
                            initial_limit_debit=1.30,
                            final_submitted_limit=1.30,
                            actual_fill_debit=1.28)
               for i in range(4)]
    est = FillBiasEstimate.from_records(records, side=OrderSide.OPEN,
                                        computed_at=NOW)
    assert est.median_bias == pytest.approx(0.08)
    assert est.applied_buffer == pytest.approx(0.06)

def test_negative_bias_never_becomes_a_negative_buffer():
    """Filling better than the reference must not make limits more aggressive."""
    records = [_calibration(calibration_id=f"c{i}", actual_fill_debit=5.20)
               for i in range(4)]
    est = FillBiasEstimate.from_records(records, side=OrderSide.OPEN,
                                        computed_at=NOW)
    assert est.median_bias < 0
    assert est.applied_buffer == 0.0


def test_stale_quotes_excluded_from_learning():
    records = [_calibration(calibration_id=f"c{i}", quote_lag_seconds=1500.0)
               for i in range(5)]
    est = FillBiasEstimate.from_records(records, side=OrderSide.OPEN,
                                        computed_at=NOW)
    assert est.sample_size == 0
    assert est.applied_buffer == 0.0


def test_open_and_close_kept_separate():
    opens = [_calibration(calibration_id=f"o{i}") for i in range(4)]
    # A CLOSE record's natural_debit_estimate is the conservative CREDIT
    # floor, which the demanded credit must not fall below.
    closes = [_calibration(calibration_id=f"c{i}", side=OrderSide.CLOSE,
                           natural_debit_estimate=5.20)
              for i in range(4)]
    est = FillBiasEstimate.from_records(opens + closes, side=OrderSide.CLOSE,
                                        computed_at=NOW)
    assert est.sample_size == 4


def test_close_credit_may_not_fall_below_conservative_floor():
    with pytest.raises(ValidationError, match="fell below"):
        _calibration(side=OrderSide.CLOSE, natural_debit_estimate=5.90,
                     final_submitted_limit=5.65)


def test_buffer_requires_sample_validator():
    with pytest.raises(ValidationError, match="requires at least 3 fills"):
        FillBiasEstimate(side=OrderSide.OPEN, sample_size=1,
                         applied_buffer=0.05, computed_at=NOW)
