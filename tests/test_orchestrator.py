"""
Alpha Council v2.5 - tier ladder tests.

The ladder is the anti-zero-trade mechanism, and its ordering is the whole
point: search wider before lowering standards. These tests pin that
ordering, and pin the things the ladder must never do.

Place at: tests/test_orchestrator.py

Run:
    uv run pytest tests/test_orchestrator.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from alpha_council.orchestrator import DecisionOutcome, SessionSummary, TierManager
from alpha_council.utils.time import ET

CONFIG = {
    "breadth_expansion": {
        "first_expand_et": "11:00",
        "tier2_after_et": "12:30",
        "second_expand_et": "14:00",
        "tier3_after_et": "14:15",
        "pin_tier1_after_alpha_orders": 14,
    },
    "discovery": {
        "max_dynamic_symbols": 250,
        "stage0_top_n": 30,
        "options_prescreen_top_n": 12,
    },
    "tiers": {
        1: {"pre_score_floor": 62.0, "final_score_floor": 68.0,
            "min_volume": 25, "max_leg_spread_pct": 0.15},
        2: {"pre_score_floor": 56.0, "final_score_floor": 62.0,
            "min_volume": 10, "max_leg_spread_pct": 0.20},
        3: {"pre_score_floor": 52.0, "final_score_floor": 58.0,
            "min_volume": 5, "max_leg_spread_pct": 0.22},
    },
    "liquidity_floor": {"min_open_interest": 75, "min_volume": 5,
                        "max_leg_spread_pct": 0.22},
}


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 31, hour, minute, tzinfo=ET)


def _tm() -> TierManager:
    manager = TierManager(CONFIG, "v2.5")
    manager.start_session(_at(9, 0))
    return manager


# ======================================================================
# the session opens at Tier 1
# ======================================================================

def test_session_starts_at_tier_one():
    manager = _tm()
    assert manager.tier == 1
    assert manager.breadth_level == 0


def test_nothing_moves_before_eleven():
    manager = _tm()
    for hour, minute in [(9, 40), (10, 15), (10, 59)]:
        assert manager.evaluate(_at(hour, minute)) == []
    assert manager.tier == 1
    assert manager.breadth_level == 0


# ======================================================================
# breadth before looseness
# ======================================================================

def test_breadth_expands_before_quality_relaxes():
    """The central ordering claim. At 11:00 the search widens and the
    quality gates have not moved."""
    manager = _tm()
    assert manager.evaluate(_at(11, 0)) == ["BREADTH_EXPAND_1"]
    assert manager.breadth_level == 1
    assert manager.tier == 1


def test_tier_two_only_after_the_first_expansion():
    manager = _tm()
    manager.evaluate(_at(11, 0))
    assert manager.evaluate(_at(12, 30)) == ["TIER_2"]
    assert manager.tier == 2
    assert manager.breadth_level == 1


def test_second_expansion_precedes_tier_three():
    manager = _tm()
    manager.evaluate(_at(11, 0))
    manager.evaluate(_at(12, 30))
    assert manager.evaluate(_at(14, 0)) == ["BREADTH_EXPAND_2"]
    assert manager.tier == 2
    assert manager.evaluate(_at(14, 15)) == ["TIER_3"]
    assert manager.tier == 3


def test_full_ladder_in_order():
    manager = _tm()
    seen: list[str] = []
    for hour, minute in [(10, 0), (11, 0), (12, 0), (12, 30), (13, 30),
                         (14, 0), (14, 15), (15, 0)]:
        seen.extend(manager.evaluate(_at(hour, minute)))
    assert seen == ["BREADTH_EXPAND_1", "TIER_2", "BREADTH_EXPAND_2",
                    "TIER_3"]


def test_a_late_start_catches_up_in_one_call():
    """Starting the process at 14:30 must not skip the audit trail."""
    manager = _tm()
    transitions = manager.evaluate(_at(14, 30))
    assert transitions == ["BREADTH_EXPAND_1", "TIER_2", "BREADTH_EXPAND_2",
                           "TIER_3"]
    assert manager.tier == 3


# ======================================================================
# an order stops the ladder
# ======================================================================

def test_an_order_freezes_the_ladder():
    manager = _tm()
    manager.evaluate(_at(11, 0))
    manager.note_order()
    assert manager.evaluate(_at(12, 30)) == []
    assert manager.evaluate(_at(14, 15)) == []
    assert manager.tier == 1


def test_no_ratchet_back_down_within_a_session():
    """Once relaxed, the tier holds for the session. Oscillating thresholds
    make attribution meaningless."""
    manager = _tm()
    manager.evaluate(_at(14, 15))
    assert manager.tier == 3
    manager.note_order()
    assert manager.evaluate(_at(15, 0)) == []
    assert manager.tier == 3


def test_new_session_resets_quality_to_tier_one():
    """Yesterday's 2:15pm desperation is not today's opening posture."""
    manager = _tm()
    manager.evaluate(_at(14, 15))
    assert manager.tier == 3
    manager.start_session(datetime(2026, 9, 1, 9, 0, tzinfo=ET))
    assert manager.tier == 1
    assert manager.breadth_level == 0
    assert manager.state.orders_today == 0


# ======================================================================
# the pin
# ======================================================================

def test_pin_after_fourteen_alpha_orders():
    manager = _tm()
    for _ in range(14):
        manager.note_order(is_alpha=True)
    assert manager.state.pinned
    assert manager.tier == 1
    assert manager.evaluate(_at(14, 15)) == []
    assert manager.tier == 1


def test_calibration_trades_do_not_count_toward_the_pin():
    """Lifecycle demonstration trades are not alpha bets."""
    manager = _tm()
    for _ in range(14):
        manager.note_order(is_alpha=False)
    assert not manager.state.pinned
    assert manager.state.orders_today == 14
    assert manager.state.alpha_orders_total == 0


def test_calibration_order_still_freezes_todays_ladder():
    manager = _tm()
    manager.note_order(is_alpha=False)
    assert manager.evaluate(_at(14, 15)) == []


# ======================================================================
# breadth affects discovery, never quality
# ======================================================================

def test_expansion_widens_discovery_only():
    manager = _tm()
    base = manager.discovery_overrides()
    assert base["stage0_top_n"] == 30

    manager.evaluate(_at(11, 0))
    level1 = manager.discovery_overrides()
    assert level1["stage0_top_n"] == 35
    assert level1["options_prescreen_top_n"] == 12   # unchanged at level 1

    manager.evaluate(_at(14, 0))
    level2 = manager.discovery_overrides()
    assert level2["options_prescreen_top_n"] == 15


def test_tier_config_returns_the_active_tier():
    manager = _tm()
    assert manager.tier_config()["final_score_floor"] == 68.0
    manager.evaluate(_at(14, 15))
    assert manager.tier_config()["final_score_floor"] == 58.0


def test_tier_three_keeps_meaningful_liquidity():
    """Tier 3 relaxes score and confidence, never the liquidity floor.

    v2.3 allowed zero-volume legs and 28% spreads at Tier 3, which turned
    the anti-zero-trade mechanism into a licence to trade illiquid spreads.
    """
    tier3 = CONFIG["tiers"][3]
    floor = CONFIG["liquidity_floor"]
    assert tier3["min_volume"] >= floor["min_volume"] >= 5
    assert tier3["max_leg_spread_pct"] <= floor["max_leg_spread_pct"] <= 0.22


def test_quality_floors_are_monotonic_across_tiers():
    tiers = CONFIG["tiers"]
    assert (tiers[1]["final_score_floor"] > tiers[2]["final_score_floor"]
            > tiers[3]["final_score_floor"])
    assert (tiers[1]["pre_score_floor"] > tiers[2]["pre_score_floor"]
            > tiers[3]["pre_score_floor"])


# ======================================================================
# session summary
# ======================================================================

def test_session_report_counts_stop_reasons():
    summary = SessionSummary(session_id="s1", started_at=_at(9, 30))
    summary.outcomes = [
        DecisionOutcome("d1", "NVDA", traded=True, stage="FILLED"),
        DecisionOutcome("d2", "AMD", gate_id="PM_ABSTAIN"),
        DecisionOutcome("d3", "SPY", gate_id="PM_ABSTAIN"),
        DecisionOutcome("d4", "QQQ", gate_id="RED_TEAM_VETO"),
    ]
    summary.trades_opened = 1
    summary.councils_run = 4

    report = summary.report()
    assert report["trades_opened"] == 1
    assert report["stopped_by"]["PM_ABSTAIN"] == 2
    assert report["stopped_by"]["RED_TEAM_VETO"] == 1


def test_decision_outcome_summary_is_serializable():
    outcome = DecisionOutcome("d1", "NVDA", traded=True, stage="FILLED",
                              approved_qty=2, fill_debit=5.35, cost_usd=0.096)
    summary = outcome.summary()
    assert summary["traded"] is True
    assert summary["qty"] == 2
    assert summary["fill"] == 5.35
