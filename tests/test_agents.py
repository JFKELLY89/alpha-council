"""
Alpha Council v2.4 - agent infrastructure tests.

No network. Budget arithmetic, evidence scoping, and response parsing are
all deterministic and testable offline.

Place at: tests/test_agents.py

Run:
    uv run pytest tests/test_agents.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from alpha_council.agents.budget import (
    BudgetMode,
    RESERVE_ALLOWED,
    compute_cost,
)
from alpha_council.agents.evidence import (
    DATA_CAVEAT,
    EvidenceBuilder,
    estimate_tokens,
)
from alpha_council.agents.llm import parse_structured
from alpha_council.models.candidate import AnalystAssessment, CandidateFeatures
from alpha_council.models.enums import (
    CandidateTrack,
    Direction,
    DiscoverySource,
    StrategyType,
)
from alpha_council.models.intelligence import IntelligenceEvent
from alpha_council.models.trading import OptionLeg, OptionStructure

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)

PRICES = {
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20},
    "gpt-5.6-sol": {"input": 4.00, "output": 20.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
}


# ======================================================================
# cost arithmetic
# ======================================================================

def test_cost_matches_the_spec_projection():
    """PM propose: 6k in, 1.2k out on Sol = $0.048 (spec §14.3)."""
    assert compute_cost("gpt-5.6-sol", 6000, 1200, PRICES) == pytest.approx(
        0.048, abs=1e-4)


def test_red_team_cost_matches_projection():
    """Red Team: 8k in, 2k out on Sonnet 5 = $0.036."""
    assert compute_cost("claude-sonnet-5", 8000, 2000, PRICES) == pytest.approx(
        0.036, abs=1e-4)


def test_analyst_calls_are_nearly_free():
    assert compute_cost("gpt-5.6-luna", 3500, 700, PRICES) < 0.002


def test_full_session_lands_near_the_projection():
    openai = (3 * compute_cost("gpt-5.6-luna", 3500, 767, PRICES)
              + compute_cost("gpt-5.6-sol", 6000, 1200, PRICES)
              + compute_cost("gpt-5.6-sol", 3000, 500, PRICES)
              + 0.4 * compute_cost("gpt-5.6-sol", 7000, 1200, PRICES))
    assert 0.08 < openai < 0.12
    assert 60 * openai < 10.0        # 12 councils x 5 sessions, far under $50


def test_cached_tokens_are_cheaper():
    plain = compute_cost("gpt-5.6-sol", 6000, 1200, PRICES)
    cached = compute_cost("gpt-5.6-sol", 6000, 1200, PRICES,
                          cached_tokens=4000)
    assert cached < plain


def test_unknown_model_costs_nothing_rather_than_crashing():
    assert compute_cost("gpt-9-nonexistent", 1000, 1000, PRICES) == 0.0


# ======================================================================
# reserve mode
# ======================================================================

def test_reserve_keeps_the_pm_and_red_team():
    """Analysts are cut first; the PM decision and the Red Team objection
    are what the demo depends on."""
    assert "portfolio_manager" in RESERVE_ALLOWED["openai"]
    assert "pm_revision" in RESERVE_ALLOWED["openai"]
    assert "bull" not in RESERVE_ALLOWED["openai"]
    assert "catalyst" not in RESERVE_ALLOWED["openai"]
    assert RESERVE_ALLOWED["anthropic"] == {"red_team"}


def test_budget_modes_are_ordered():
    assert BudgetMode.NORMAL != BudgetMode.RESERVE != BudgetMode.BLOCKED


# ======================================================================
# evidence packages
# ======================================================================

def _candidate(track=CandidateTrack.EVENT) -> CandidateFeatures:
    is_event = track is CandidateTrack.EVENT
    return CandidateFeatures(
        symbol="NVDA", as_of=NOW, direction=Direction.BULLISH,
        combined_direction=0.45, track=track,
        discovery_source=DiscoverySource.ALPACA_NEWS if is_event
        else DiscoverySource.MOVER,
        momentum_score=78.0, relative_volume_score=72.0,
        trend_regime_score=75.0, relative_strength_score=68.0,
        options_opportunity_score=76.0, options_liquidity_score=82.0,
        catalyst_score=84.0 if is_event else None,
        corroboration_score=70.0 if is_event else None,
        novelty_score=88.0 if is_event else None,
        data_confidence_factor=1.0, regime_factor=1.0, event_risk_factor=1.0,
        pre_score=74.0,
    )


def _event(i: int = 0) -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=f"e{i}", item_id=f"i{i}", symbol="NVDA", event_type="8-K",
        direction=Direction.BULLISH, direction_confidence=0.8,
        source_reliability_score=100.0, freshness_score=90.0 - i,
        novelty_score=85.0, corroboration_score=70.0,
        materiality_score=90.0, surprise_score=60.0,
        market_confirmation_score=75.0, catalyst_score=84.0 - i,
        extracted_facts=[f"fact {i}"], evidence_urls=[f"https://x/{i}"],
        created_at=NOW,
    )


def _leg(strike: float, delta: float, side: str = "BUY") -> OptionLeg:
    return OptionLeg(
        symbol=f"NVDA260918C{int(strike * 1000):08d}", underlying="NVDA",
        expiration=EXP, option_type="CALL", strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=5.00, ask=5.20, raw_mid=5.10, adjusted_mid=5.10,
        quote_lag_seconds=4.0, delta=delta, open_interest=3000, volume=250,
        implied_volatility=0.31,
    )


def _structure(rank: int = 1) -> OptionStructure:
    limit, width = 5.20, 10.0
    return OptionStructure(
        structure_id=f"st_{rank}", symbol="NVDA",
        strategy=StrategyType.BULL_CALL_DEBIT, rank=rank, expiration=EXP,
        dte=18, legs=[_leg(200.0, 0.60), _leg(210.0, 0.33, side="SELL")],
        width=width, net_delta=0.27, raw_mid_debit=5.10,
        adjusted_mid_debit=5.10, natural_debit=5.60, staleness_buffer=0.0,
        initial_limit_debit=limit, cost_to_width_ratio=limit / width,
        max_loss_per_spread=limit * 100,
        max_profit_per_spread=(width - limit) * 100,
        reward_risk_ratio=(width - limit) / limit, breakeven=200.0 + limit,
        max_quote_lag_seconds=4.0, liquidity_score=82.0,
        delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0,
    )


def _builder(track=CandidateTrack.EVENT, n_events: int = 3) -> EvidenceBuilder:
    return EvidenceBuilder(
        candidate=_candidate(track),
        intel_events=[_event(i) for i in range(n_events)],
        structures=[_structure(r) for r in range(1, 6)],
        portfolio_state={"equity": 100000.0, "open_positions": 1},
        market_summary={"spy_r15": 0.003, "regime": "risk_on"},
        scheduled_events=[{"name": "Employment Situation", "et": "09-04 08:30"}],
    )


def test_every_package_carries_the_data_caveat():
    """Agents must know indicative quotes are estimates, not NBBO."""
    for role in ("BULL", "BEAR", "CATALYST", "PM", "SELECTION", "RED_TEAM"):
        pkg = _builder().build(role)          # type: ignore[arg-type]
        assert pkg.sections["context"]["data_caveat"] == DATA_CAVEAT


def test_analysts_do_not_receive_option_structures():
    """Bull and Bear reason about direction, not about strike selection."""
    for role in ("BULL", "BEAR", "CATALYST"):
        pkg = _builder().build(role)          # type: ignore[arg-type]
        assert "top_option_structures" not in pkg.sections


def test_selection_and_red_team_receive_structures():
    for role in ("SELECTION", "RED_TEAM"):
        pkg = _builder().build(role, cap_tokens=9000)  # type: ignore[arg-type]
        assert len(pkg.sections["top_option_structures"]) == 5


def test_structures_expose_staleness_fields():
    pkg = _builder().build("RED_TEAM", cap_tokens=9000)
    leg = pkg.sections["top_option_structures"][0]["long"]
    for key in ("raw_mid", "adjusted_mid", "quote_lag_seconds"):
        assert key in leg


def test_portfolio_state_only_reaches_pm_and_red_team():
    assert "portfolio_state" in _builder().build("PM", cap_tokens=9000).sections
    assert "portfolio_state" not in _builder().build("BULL").sections


def test_momentum_package_states_the_absence_of_a_catalyst():
    """Not an omitted key and not a neutral 50 — an explicit absence."""
    pkg = _builder(track=CandidateTrack.MOMENTUM).build("PM", cap_tokens=9000)
    features = pkg.sections["candidate_features"]
    assert features["catalyst"] is None
    assert "absence of evidence" in features["catalyst_note"]


def test_event_package_carries_intelligence_scores():
    features = _builder().build("PM", cap_tokens=9000).sections[
        "candidate_features"]
    assert features["catalyst"] == pytest.approx(84.0)
    assert "catalyst_note" not in features


def test_catalyst_analyst_gets_the_most_events():
    many = _builder(n_events=12)
    catalyst = many.build("CATALYST", cap_tokens=9000)
    bull = many.build("BULL", cap_tokens=9000)
    assert len(catalyst.sections["intelligence_events"]) > \
        len(bull.sections["intelligence_events"])


def test_events_are_ordered_by_catalyst_score():
    pkg = _builder(n_events=6).build("CATALYST", cap_tokens=9000)
    scores = [e["catalyst"] for e in pkg.sections["intelligence_events"]]
    assert scores == sorted(scores, reverse=True)


def test_oversized_package_is_shrunk():
    pkg = _builder(n_events=30).build("PM", cap_tokens=800)
    assert pkg.truncated
    assert pkg.token_estimate <= 1600


def test_shrinking_never_drops_the_decision_material():
    pkg = _builder(n_events=30).build("RED_TEAM", cap_tokens=600)
    assert "candidate_features" in pkg.sections
    assert "context" in pkg.sections
    assert "top_option_structures" in pkg.sections


def test_analyst_assessments_reach_pm_but_not_analysts():
    assessment = AnalystAssessment(
        symbol="NVDA", analyst="BULL", score=78.0, confidence=0.7,
        thesis="t", evidence_for=["a"], evidence_against=["b"],
        missing_information=[], invalidation_conditions=["c"])
    pm = _builder().build("PM", cap_tokens=9000,
                          analyst_outputs=[assessment])
    bull = _builder().build("BULL", cap_tokens=9000,
                            analyst_outputs=[assessment])
    assert "analyst_assessments" in pm.sections
    assert "analyst_assessments" not in bull.sections


def test_package_hash_is_stable_and_sensitive():
    a = _builder().build("PM", cap_tokens=9000)
    b = _builder().build("PM", cap_tokens=9000)
    c = _builder(track=CandidateTrack.MOMENTUM).build("PM", cap_tokens=9000)
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


def test_token_estimate_is_monotonic():
    small = _builder(n_events=1).build("PM", cap_tokens=9000)
    large = _builder(n_events=8).build("PM", cap_tokens=9000)
    assert large.token_estimate > small.token_estimate
    assert estimate_tokens("a" * 4000) == 1000


# ======================================================================
# response parsing
# ======================================================================

def test_parse_clean_json():
    a = parse_structured(
        '{"symbol":"NVDA","analyst":"BULL","score":78.0,"confidence":0.7,'
        '"thesis":"t","evidence_for":["a"],"evidence_against":["b"],'
        '"missing_information":[],"invalidation_conditions":["c"],'
        '"source_event_ids":[]}', AnalystAssessment)
    assert a.score == 78.0


def test_parse_strips_code_fences():
    raw = ('```json\n{"symbol":"NVDA","analyst":"BEAR","score":40.0,'
           '"confidence":0.6,"thesis":"t","evidence_for":["a"],'
           '"evidence_against":["b"],"missing_information":[],'
           '"invalidation_conditions":["c"],"source_event_ids":[]}\n```')
    assert parse_structured(raw, AnalystAssessment).analyst == "BEAR"


def test_parse_tolerates_leading_prose():
    raw = ('Here is my assessment:\n{"symbol":"NVDA","analyst":"BULL",'
           '"score":50.0,"confidence":0.5,"thesis":"t","evidence_for":["a"],'
           '"evidence_against":["b"],"missing_information":[],'
           '"invalidation_conditions":["c"],"source_event_ids":[]}')
    assert parse_structured(raw, AnalystAssessment).score == 50.0


def test_hallucinated_field_is_rejected():
    """extra='forbid' is how a bad response becomes NO TRADE."""
    raw = ('{"symbol":"NVDA","analyst":"BULL","score":78.0,"confidence":0.7,'
           '"thesis":"t","evidence_for":["a"],"evidence_against":["b"],'
           '"missing_information":[],"invalidation_conditions":["c"],'
           '"source_event_ids":[],"price_target":250.0}')
    with pytest.raises(ValidationError):
        parse_structured(raw, AnalystAssessment)


def test_out_of_range_value_is_rejected():
    raw = ('{"symbol":"NVDA","analyst":"BULL","score":180.0,"confidence":0.7,'
           '"thesis":"t","evidence_for":["a"],"evidence_against":["b"],'
           '"missing_information":[],"invalidation_conditions":["c"],'
           '"source_event_ids":[]}')
    with pytest.raises(ValidationError):
        parse_structured(raw, AnalystAssessment)


def test_empty_and_prose_only_responses_fail():
    with pytest.raises(ValueError):
        parse_structured("", AnalystAssessment)
    with pytest.raises(ValueError):
        parse_structured("I cannot answer that.", AnalystAssessment)
