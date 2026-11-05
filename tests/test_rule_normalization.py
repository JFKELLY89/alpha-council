"""
Alpha Council - F13 rule normalization tests.

A TIME invalidation rule with a malformed threshold is a silently dead
backstop: the monitor skips None thresholds, and a price in the slot
never matches elapsed days. Normalization recovers the day count from
the rule's own wording before the rule is persisted.

Run:
    uv run pytest tests/test_rule_normalization.py -v
"""

from __future__ import annotations

from alpha_council.models.trading import (
    InvalidationRule,
    normalize_invalidation_rules,
)


def time_rule(description: str, threshold: float | None = None,
              comparator: str | None = None) -> InvalidationRule:
    return InvalidationRule(rule_type="TIME", description=description,
                            threshold=threshold, comparator=comparator)


def test_price_valued_time_threshold_recovers_the_day_count():
    # MSFT, 2026-11-04: "TIME GTE 558.0" - the audit's UNSUPPORTED claim.
    rules, notes = normalize_invalidation_rules([time_rule(
        "Exit at the end of the fifth trading day after entry if MSFT "
        "has not recorded a close above 558.00",
        threshold=558.0, comparator="GTE")], horizon_days=3)
    assert rules[0].threshold == 5.0
    assert rules[0].comparator == "GTE"
    assert len(notes) == 1


def test_null_threshold_parses_word_counts():
    rules, notes = normalize_invalidation_rules([time_rule(
        "Exit after three trading days unless the move has held")],
        horizon_days=7)
    assert rules[0].threshold == 3.0
    assert notes


def test_digit_counts_and_session_wording_parse():
    rules, _ = normalize_invalidation_rules([time_rule(
        "Exit no later than 10 full trading sessions after entry")],
        horizon_days=5)
    assert rules[0].threshold == 10.0


def test_unparseable_text_falls_back_to_the_pm_horizon():
    rules, notes = normalize_invalidation_rules([time_rule(
        "Exit once the continuation thesis is exhausted")], horizon_days=7)
    assert rules[0].threshold == 7.0
    assert "horizon" in notes[0]


def test_minute_phrases_do_not_read_as_day_counts():
    rules, _ = normalize_invalidation_rules([time_rule(
        "Invalidate after two consecutive 15-minute closes below the "
        "reference")], horizon_days=4)
    assert rules[0].threshold == 4.0  # fell back to the horizon


def test_inverted_comparator_is_forced_to_gte():
    rules, notes = normalize_invalidation_rules([time_rule(
        "Exit after two trading days", threshold=2.0, comparator="LT")],
        horizon_days=5)
    assert rules[0].threshold == 2.0
    assert rules[0].comparator == "GTE"
    assert notes


def test_valid_time_rules_and_other_types_pass_through_untouched():
    price = InvalidationRule(rule_type="PRICE", description="below support",
                             threshold=100.0, comparator="LT")
    good = time_rule("Exit after 5 trading days", threshold=5.0,
                     comparator="GTE")
    rules, notes = normalize_invalidation_rules([price, good],
                                                horizon_days=9)
    assert rules == [price, good]
    assert notes == []
