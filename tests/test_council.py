"""
Alpha Council v2.5 - council session tests.

A stub client returns scripted responses, so the whole session runs offline
and the control flow is testable without a single API call.

The properties under test are the ones that must never depend on a prompt
being obeyed: a VETO cannot reach an order, a rank outside the supplied
list is refused, and a revision cannot increase risk.

Place at: tests/test_council.py

Run:
    uv run pytest tests/test_council.py -v
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any, Type

import pytest
from pydantic import BaseModel

from alpha_council.agents.council import (
    Council,
    CouncilOutcome,
    effective_risk_pct,
    resolve_rank,
)
from alpha_council.agents.evidence import EvidenceBuilder
from alpha_council.agents.llm import LLMResult
from alpha_council.models.candidate import AnalystAssessment, CandidateFeatures
from alpha_council.models.enums import (
    CandidateTrack,
    Direction,
    DiscoverySource,
    StrategyType,
    Verdict,
)
from alpha_council.models.trading import (
    InvalidationRule,
    OptionLeg,
    OptionStructure,
    PortfolioProposal,
    RedTeamProblem,
    RedTeamReview,
)

NOW = datetime(2026, 8, 31, 14, 30, tzinfo=timezone.utc)
EXP = date(2026, 9, 18)


# ======================================================================
# fixtures
# ======================================================================

class StubClient:
    """Returns scripted parsed objects keyed by purpose."""

    def __init__(self, script: dict[str, Any]):
        self.script = script
        self.calls: list[str] = []

    async def call(self, purpose: str, system_prompt: str, evidence: Any,
                   schema: Type[BaseModel], decision_id: str | None = None,
                   session_id: str | None = None,
                   estimated_cost: float = 0.0) -> LLMResult:
        self.calls.append(purpose)
        entry = self.script.get(purpose)
        if entry is None:
            return LLMResult(ok=False, purpose=purpose, provider="stub",
                             model="stub", error="no scripted response")
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, str):
            return LLMResult(ok=False, purpose=purpose, provider="stub",
                             model="stub", error=entry)
        return LLMResult(ok=True, purpose=purpose, provider="stub",
                         model="stub", parsed=entry, cost_usd=0.01,
                         input_tokens=100, output_tokens=50)


def _assessment(analyst: str) -> AnalystAssessment:
    return AnalystAssessment(
        symbol="NVDA", analyst=analyst, score=70.0, confidence=0.7,
        thesis="t", evidence_for=["a"], evidence_against=["b"],
        missing_information=[], invalidation_conditions=["c"])


def _leg(strike: float, delta: float, side: str = "BUY") -> OptionLeg:
    return OptionLeg(
        symbol=f"NVDA260918C{int(strike * 1000):08d}", underlying="NVDA",
        expiration=EXP, option_type="CALL", strike=strike, side=side,
        position_intent="buy_to_open" if side == "BUY" else "sell_to_open",
        bid=5.00, ask=5.20, raw_mid=5.10, adjusted_mid=5.10,
        quote_lag_seconds=4.0, delta=delta, open_interest=3000, volume=250)


def _structure(rank: int) -> OptionStructure:
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
        reward_risk_ratio=(width - limit) / limit, breakeven=205.20,
        max_quote_lag_seconds=4.0, liquidity_score=82.0,
        delta_fit_score=100.0, dte_fit_score=80.0,
        cost_efficiency_score=48.0, structure_score=78.0)


STRUCTURES = [_structure(r) for r in range(1, 6)]


def _proposal(revision: int = 0, trade: bool = True, rank: int | None = 1,
              risk: float = 1.25, direction=Direction.BULLISH,
              reason: str | None = None) -> PortfolioProposal:
    return PortfolioProposal(
        decision_id="dec_1", revision=revision, symbol="NVDA", trade=trade,
        direction=direction, confidence=0.72, expected_horizon_days=5,
        desired_portfolio_risk_pct=risk if trade else 0.0,
        thesis="t", catalyst_summary="c", key_supporting_evidence=["a"],
        key_contrary_evidence=["b"],
        invalidation=[InvalidationRule(rule_type="PRICE", description="vwap",
                                       threshold=198.0, comparator="LT")]
        if trade else [],
        selected_structure_rank=rank if trade else None,
        abstain_reason=None if trade else (reason or "insufficient edge"))


def _review(verdict=Verdict.PASS, max_risk: float = 1.5) -> RedTeamReview:
    fatal = verdict is Verdict.VETO
    return RedTeamReview(
        decision_id="dec_1", verdict=verdict, risk_score=8 if fatal else 4,
        fatal_flaw=fatal, confidence_adjustment=-0.1 if fatal else 0.0,
        recommended_max_risk_pct=0.0 if fatal else max_risk,
        problems=[RedTeamProblem(category="EXPRESSION", severity=6,
                                 description="breakeven needs a 3.6% move")]
        if verdict is not Verdict.PASS else [],
        strongest_counterargument="x",
        information_to_reverse_verdict=[], summary="s")


def _candidate() -> CandidateFeatures:
    return CandidateFeatures(
        symbol="NVDA", as_of=NOW, direction=Direction.BULLISH,
        combined_direction=0.45, track=CandidateTrack.MOMENTUM,
        discovery_source=DiscoverySource.MOVER, momentum_score=78.0,
        relative_volume_score=72.0, trend_regime_score=75.0,
        relative_strength_score=68.0, options_opportunity_score=78.0,
        options_liquidity_score=82.0, data_confidence_factor=1.0,
        regime_factor=1.0, event_risk_factor=1.0, pre_score=74.0)


def _builder() -> EvidenceBuilder:
    return EvidenceBuilder(candidate=_candidate(), structures=STRUCTURES,
                           portfolio_state={"equity": 100000.0})


def _council(openai_script: dict, anthropic_script: dict) -> Council:
    c = Council(StubClient(openai_script), StubClient(anthropic_script),
                {"models": {}})
    c._prompts = {name: f"stub {name}" for name in
                  ("bull_system", "bear_system", "catalyst_system",
                   "pm_system", "pm_selection_system", "red_team_system",
                   "pm_revision_system")}
    return c


def _run(council: Council) -> CouncilOutcome:
    return asyncio.run(council.run(_candidate(), STRUCTURES, _builder(),
                                   "dec_1", "sess_1"))


def _full_script(**overrides) -> dict:
    base = {
        "bull": _assessment("BULL"), "bear": _assessment("BEAR"),
        "catalyst": _assessment("CATALYST"),
        "portfolio_manager": _proposal(),
        "structure_selection": _proposal(rank=2),
        "pm_revision": _proposal(revision=1, rank=3, risk=0.8),
    }
    base.update(overrides)
    return base


# ======================================================================
# the happy path
# ======================================================================

def test_pass_verdict_completes_without_revision():
    outcome = _run(_council(_full_script(), {"red_team": _review()}))
    assert outcome.traded
    assert outcome.stopped_at == "COMPLETE"
    assert outcome.selected_structure.rank == 2
    assert outcome.revision is None
    assert len(outcome.assessments) == 3
    assert outcome.calls == 6          # 3 analysts + PM + selection + red team
    assert outcome.verdict is Verdict.PASS


def test_modify_triggers_exactly_one_revision():
    outcome = _run(_council(_full_script(),
                            {"red_team": _review(Verdict.MODIFY)}))
    assert outcome.traded
    assert outcome.revision is not None
    assert outcome.revision.revision == 1
    assert outcome.selected_structure.rank == 3
    assert outcome.structure_changed


# ======================================================================
# the veto is absolute
# ======================================================================

def test_veto_stops_the_session():
    outcome = _run(_council(_full_script(),
                            {"red_team": _review(Verdict.VETO)}))
    assert not outcome.traded
    assert outcome.stopped_at == "RED_TEAM"
    assert outcome.gate_id == "RED_TEAM_VETO"
    assert outcome.revision is None


def test_veto_never_reaches_a_revision():
    """There is no code path from VETO to the revision step."""
    council = _council(_full_script(), {"red_team": _review(Verdict.VETO)})
    _run(council)
    assert "pm_revision" not in council.openai.calls


def test_red_team_failure_is_not_a_pass():
    """An unavailable Red Team does not mean the trade is safe."""
    outcome = _run(_council(_full_script(),
                            {"red_team": "provider timeout"}))
    assert not outcome.traded
    assert outcome.gate_id == "COUNCIL_RED_TEAM_FAILED"


# ======================================================================
# the PM cannot invent a structure
# ======================================================================

def test_rank_outside_the_supplied_list_is_refused():
    outcome = CouncilOutcome(decision_id="d", symbol="NVDA")
    assert resolve_rank(9, STRUCTURES, outcome) is None
    assert outcome.gate_id == "COUNCIL_BAD_RANK"


def test_missing_rank_is_refused():
    outcome = CouncilOutcome(decision_id="d", symbol="NVDA")
    assert resolve_rank(None, STRUCTURES, outcome) is None
    assert outcome.gate_id == "COUNCIL_NO_RANK"


def test_valid_rank_resolves_to_the_real_structure():
    outcome = CouncilOutcome(decision_id="d", symbol="NVDA")
    s = resolve_rank(4, STRUCTURES, outcome)
    assert s is not None and s.structure_id == "st_4"


def test_revision_with_a_bad_rank_stops_the_session():
    outcome = _run(_council(
        _full_script(pm_revision=_proposal(revision=1, rank=5, risk=0.8)),
        {"red_team": _review(Verdict.MODIFY)}))
    assert outcome.traded          # rank 5 exists
    outcome2 = _run(_council(
        _full_script(pm_revision=_proposal(revision=1, rank=None, risk=0.8)),
        {"red_team": _review(Verdict.MODIFY)}))
    assert not outcome2.traded


# ======================================================================
# the revision is a brake, not a lever
# ======================================================================

def test_revision_cannot_increase_risk():
    outcome = _run(_council(
        _full_script(pm_revision=_proposal(revision=1, rank=1, risk=2.0)),
        {"red_team": _review(Verdict.MODIFY)}))
    assert not outcome.traded
    assert outcome.gate_id == "COUNCIL_REVISION_FAILED"
    assert "raised risk" in outcome.reason


def test_revision_may_abstain():
    outcome = _run(_council(
        _full_script(pm_revision=_proposal(revision=1, trade=False,
                                           reason="red team was right")),
        {"red_team": _review(Verdict.MODIFY)}))
    assert not outcome.traded
    assert outcome.gate_id == "PM_ABSTAIN"


def test_revision_must_be_marked_revision_one():
    outcome = _run(_council(
        _full_script(pm_revision=_proposal(revision=0, rank=1, risk=0.8)),
        {"red_team": _review(Verdict.MODIFY)}))
    assert not outcome.traded


def test_red_team_cap_is_a_ceiling_not_a_floor():
    """The cap binds only on MODIFY. A PASS carries a recommendation the
    shadow book has no variant for; honouring it would shrink the executed
    size through a channel the attribution cannot decompose."""
    script = _full_script(pm_revision=_proposal(revision=1, rank=3,
                                                risk=1.25))
    outcome = _run(_council(script,
                            {"red_team": _review(Verdict.MODIFY,
                                                 max_risk=1.5)}))
    assert effective_risk_pct(outcome) == pytest.approx(1.25)  # PM asked less

    tight = _run(_council(script,
                          {"red_team": _review(Verdict.MODIFY,
                                               max_risk=0.6)}))
    assert effective_risk_pct(tight) == pytest.approx(0.6)


def test_pass_verdict_does_not_cap_risk():
    """On PASS nothing changed, so nothing may silently resize the trade."""
    outcome = _run(_council(_full_script(),
                            {"red_team": _review(Verdict.PASS,
                                                 max_risk=0.6)}))
    assert effective_risk_pct(outcome) == pytest.approx(1.25)


# ======================================================================
# abstention and degradation
# ======================================================================

def test_pm_abstention_stops_cleanly():
    outcome = _run(_council(
        _full_script(portfolio_manager=_proposal(trade=False,
                                                 reason="no edge")),
        {"red_team": _review()}))
    assert not outcome.traded
    assert outcome.stopped_at == "PM_ABSTAIN"
    assert outcome.gate_id == "PM_ABSTAIN"


def test_selection_abstention_stops_cleanly():
    outcome = _run(_council(
        _full_script(structure_selection=_proposal(
            trade=False, reason="none express the thesis")),
        {"red_team": _review()}))
    assert not outcome.traded
    assert outcome.stopped_at == "STRUCTURE_SELECTION"


def test_one_analyst_failure_degrades_but_continues():
    outcome = _run(_council(_full_script(bear="provider error"),
                            {"red_team": _review()}))
    assert outcome.traded
    assert len(outcome.assessments) == 2
    assert any("BEAR" in d for d in outcome.degraded)


def test_two_analyst_failures_stop_the_session():
    """One perspective is not a council."""
    outcome = _run(_council(
        _full_script(bear="error", catalyst="error"), {"red_team": _review()}))
    assert not outcome.traded
    assert outcome.gate_id == "COUNCIL_ANALYSTS_FAILED"


def test_pm_direction_must_match_the_scan():
    outcome = _run(_council(
        _full_script(portfolio_manager=_proposal(
            direction=Direction.BEARISH)),
        {"red_team": _review()}))
    assert not outcome.traded
    assert outcome.gate_id == "COUNCIL_DIRECTION_MISMATCH"


def test_no_structures_stops_before_any_call():
    council = _council(_full_script(), {"red_team": _review()})
    outcome = asyncio.run(council.run(_candidate(), [], _builder(),
                                      "dec_1", "sess_1"))
    assert not outcome.traded
    assert outcome.gate_id == "COUNCIL_NO_STRUCTURES"
    assert council.openai.calls == []


def test_analyst_exception_is_caught_not_propagated():
    outcome = _run(_council(_full_script(bull=RuntimeError("boom")),
                            {"red_team": _review()}))
    assert outcome.traded
    assert any("BULL" in d for d in outcome.degraded)


# ======================================================================
# accounting
# ======================================================================

def test_cost_accumulates_across_the_session():
    outcome = _run(_council(_full_script(),
                            {"red_team": _review(Verdict.MODIFY)}))
    assert outcome.calls == 7          # PASS path plus one revision
    assert outcome.cost_usd == pytest.approx(0.07)   # 7 calls at $0.01


def test_summary_is_serializable():
    summary = _run(_council(_full_script(), {"red_team": _review()})).summary()
    assert summary["traded"] is True
    assert summary["verdict"] == "PASS"
    assert "cost_usd" in summary
