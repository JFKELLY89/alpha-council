"""
Alpha Council v2.5 - the Research Council.

Bull, Bear, Catalyst, Portfolio Manager, Red Team. Every output is a
validated Pydantic object; a refusal, a malformed response, or a
hallucinated field becomes NO TRADE plus a gate_rejections row.

Two boundaries the code enforces rather than trusting the prompts:

  THE PM CANNOT INVENT A STRUCTURE. Selection is by rank into a list the
  deterministic options engine produced. A rank outside that list is
  rejected here, not negotiated.

  A VETO IS FINAL. There is no code path from Verdict.VETO to an order.
  The revision step is only reachable on MODIFY.

v2.5 (§7): the Red Team now carries a mandatory trade-expression challenge
— assume the direction is right, then explain how the spread still loses.

Place at: alpha_council/agents/council.py
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from alpha_council.agents.evidence import AgentRole, EvidenceBuilder
from alpha_council.agents.llm import LLMClient, LLMResult
from alpha_council.evolution.scenarios import ScenarioGenerator, ScenarioResult
from alpha_council.models.candidate import AnalystAssessment, CandidateFeatures
from alpha_council.models.enums import CandidateTrack, Direction, Verdict
from alpha_council.models.trading import (
    OptionStructure,
    PortfolioProposal,
    RedTeamReview,
)
from alpha_council.settings import load_prompt
from alpha_council.utils.time import utc_now

# At least this many analysts must return valid output. Below it the PM
# would be reasoning from one perspective, which defeats the point of a
# council; the session degrades to NO TRADE.
MIN_ANALYSTS = 2


@dataclass(slots=True)
class CouncilOutcome:
    decision_id: str
    symbol: str
    traded: bool = False
    stopped_at: str = ""
    reason: str = ""
    gate_id: str | None = None

    assessments: list[AnalystAssessment] = field(default_factory=list)
    proposal: PortfolioProposal | None = None
    revision: PortfolioProposal | None = None
    selected_structure: OptionStructure | None = None
    original_structure: OptionStructure | None = None
    review: RedTeamReview | None = None
    scenarios: ScenarioResult | None = None

    cost_usd: float = 0.0
    calls: int = 0
    degraded: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=utc_now)

    @property
    def final_proposal(self) -> PortfolioProposal | None:
        return self.revision or self.proposal

    @property
    def verdict(self) -> Verdict | None:
        return self.review.verdict if self.review else None

    @property
    def structure_changed(self) -> bool:
        """Did the revision pick a different spread than the PM first chose?"""
        if not (self.original_structure and self.selected_structure):
            return False
        return (self.original_structure.structure_id
                != self.selected_structure.structure_id)

    def summary(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "traded": self.traded,
            "stopped_at": self.stopped_at, "reason": self.reason[:200],
            "verdict": str(self.verdict) if self.verdict else None,
            "structure_changed": self.structure_changed,
            "scenarios": bool(self.scenarios and self.scenarios.usable),
            "calls": self.calls, "cost_usd": round(self.cost_usd, 4),
            "degraded": self.degraded,
        }


class CouncilError(RuntimeError):
    pass


class Council:
    """Runs one full council session for one candidate."""

    def __init__(self, openai: LLMClient, anthropic: LLMClient,
                 config: dict[str, Any],
                 scenarios: ScenarioGenerator | None = None):
        self.openai = openai
        self.anthropic = anthropic
        self.config = config
        self.scenarios = scenarios
        self._prompts: dict[str, str] = {}

    def prompt(self, name: str) -> str:
        if name not in self._prompts:
            self._prompts[name] = load_prompt(name)
        return self._prompts[name]

    def _cap(self, purpose: str, default: int) -> int:
        return int(self.config.get("models", {}).get(purpose, {})
                   .get("evidence_cap_tokens", default))

    # ---- analysts ---------------------------------------------------

    async def _analyst(self, role: AgentRole, purpose: str, prompt_name: str,
                       builder: EvidenceBuilder, decision_id: str,
                       session_id: str) -> LLMResult:
        pkg = builder.build(role, cap_tokens=self._cap(purpose, 3500))
        return await self.openai.call(
            purpose, self.prompt(prompt_name), pkg, AnalystAssessment,
            decision_id=decision_id, session_id=session_id,
            estimated_cost=0.002)

    async def run_analysts(self, builder: EvidenceBuilder, decision_id: str,
                           session_id: str,
                           outcome: CouncilOutcome) -> list[AnalystAssessment]:
        """Bull, Bear and Catalyst run concurrently and independently.

        Independence is the point: they must not see each other's output,
        or the 'council' collapses into one argument with three signatures.
        """
        results = await asyncio.gather(
            self._analyst("BULL", "bull", "bull_system", builder,
                          decision_id, session_id),
            self._analyst("BEAR", "bear", "bear_system", builder,
                          decision_id, session_id),
            self._analyst("CATALYST", "catalyst", "catalyst_system", builder,
                          decision_id, session_id),
            return_exceptions=True,
        )

        assessments: list[AnalystAssessment] = []
        for name, result in zip(("BULL", "BEAR", "CATALYST"), results):
            if isinstance(result, BaseException):
                outcome.degraded.append(f"{name}: {type(result).__name__}")
                continue
            outcome.calls += 1
            outcome.cost_usd += result.cost_usd
            if result.failed or not isinstance(result.parsed, AnalystAssessment):
                outcome.degraded.append(f"{name}: {result.error}")
                continue
            assessments.append(result.parsed)
        return assessments

    # ---- portfolio manager ------------------------------------------

    async def propose(self, builder: EvidenceBuilder,
                      assessments: Sequence[AnalystAssessment],
                      decision_id: str, session_id: str,
                      outcome: CouncilOutcome,
                      payoffs: dict[str, Any] | None = None
                      ) -> PortfolioProposal | None:
        pkg = builder.build("PM", cap_tokens=self._cap("portfolio_manager",
                                                       6000),
                            analyst_outputs=assessments,
                            scenario_payoffs=payoffs)
        result = await self.openai.call(
            "portfolio_manager", self.prompt("pm_system"), pkg,
            PortfolioProposal, decision_id=decision_id,
            session_id=session_id, estimated_cost=0.05)
        outcome.calls += 1
        outcome.cost_usd += result.cost_usd
        if result.failed or not isinstance(result.parsed, PortfolioProposal):
            outcome.reason = result.error or "PM returned no valid proposal"
            return None
        return result.parsed

    async def select_structure(self, builder: EvidenceBuilder,
                               proposal: PortfolioProposal,
                               structures: Sequence[OptionStructure],
                               decision_id: str, session_id: str,
                               outcome: CouncilOutcome,
                               payoffs: dict[str, Any] | None = None
                               ) -> OptionStructure | None:
        pkg = builder.build("SELECTION",
                            cap_tokens=self._cap("structure_selection", 3000),
                            proposal=proposal, scenario_payoffs=payoffs)
        result = await self.openai.call(
            "structure_selection", self.prompt("pm_selection_system"), pkg,
            PortfolioProposal, decision_id=decision_id,
            session_id=session_id, estimated_cost=0.03)
        outcome.calls += 1
        outcome.cost_usd += result.cost_usd

        if result.failed or not isinstance(result.parsed, PortfolioProposal):
            outcome.reason = result.error or "structure selection failed"
            return None

        selection = result.parsed
        if not selection.trade:
            outcome.reason = selection.abstain_reason or "PM rejected all structures"
            return None
        return resolve_rank(selection.selected_structure_rank, structures,
                            outcome)

    async def revise_once(self, builder: EvidenceBuilder,
                          proposal: PortfolioProposal,
                          review: RedTeamReview,
                          structures: Sequence[OptionStructure],
                          decision_id: str, session_id: str,
                          outcome: CouncilOutcome) -> PortfolioProposal | None:
        pkg = builder.build("REVISION", cap_tokens=self._cap("pm_revision",
                                                             7000),
                            proposal=proposal,
                            red_team_summary=_review_summary(review))
        result = await self.openai.call(
            "pm_revision", self.prompt("pm_revision_system"), pkg,
            PortfolioProposal, decision_id=decision_id,
            session_id=session_id, estimated_cost=0.05)
        outcome.calls += 1
        outcome.cost_usd += result.cost_usd

        if result.failed or not isinstance(result.parsed, PortfolioProposal):
            outcome.reason = result.error or "revision failed"
            return None

        revised = result.parsed
        if revised.revision != 1:
            outcome.reason = f"revision returned revision={revised.revision}"
            return None
        # A revision may not increase requested risk. The Red Team is a
        # brake; a MODIFY that comes back larger is not a revision.
        if revised.desired_portfolio_risk_pct > proposal.desired_portfolio_risk_pct:
            outcome.reason = (
                f"revision raised risk {proposal.desired_portfolio_risk_pct} "
                f"-> {revised.desired_portfolio_risk_pct}")
            return None
        return revised

    # ---- red team ----------------------------------------------------

    async def red_team(self, builder: EvidenceBuilder,
                       proposal: PortfolioProposal, selected_rank: int,
                       assessments: Sequence[AnalystAssessment],
                       decision_id: str, session_id: str,
                       outcome: CouncilOutcome,
                       payoffs: dict[str, Any] | None = None
                       ) -> RedTeamReview | None:
        pkg = builder.build("RED_TEAM", cap_tokens=self._cap("red_team", 8000),
                            analyst_outputs=assessments, proposal=proposal,
                            selected_rank=selected_rank,
                            scenario_payoffs=payoffs)
        result = await self.anthropic.call(
            "red_team", self.prompt("red_team_system"), pkg, RedTeamReview,
            decision_id=decision_id, session_id=session_id,
            estimated_cost=0.04)
        outcome.calls += 1
        outcome.cost_usd += result.cost_usd

        if result.failed or not isinstance(result.parsed, RedTeamReview):
            # Anthropic being unavailable does not mean the trade is safe.
            outcome.reason = (result.error
                              or "Red Team returned no valid review")
            return None
        return result.parsed

    # ---- session -----------------------------------------------------

    async def run(self, candidate: CandidateFeatures,
                  structures: Sequence[OptionStructure],
                  builder: EvidenceBuilder, decision_id: str,
                  session_id: str) -> CouncilOutcome:
        outcome = CouncilOutcome(decision_id=decision_id,
                                 symbol=candidate.symbol)

        if not structures:
            outcome.stopped_at = "PRE_COUNCIL"
            outcome.reason = "no structures supplied"
            outcome.gate_id = "COUNCIL_NO_STRUCTURES"
            return outcome

        # 1. analysts and scenarios, concurrently
        #
        # The generator runs in parallel deliberately: it must not see the
        # Bull or Bear cases, or it would produce scenarios that agree with
        # whichever argument was stronger. It describes the underlying, not
        # the trade.
        spot = structures[0].underlying_price or 0.0
        if self.scenarios is not None and spot > 0:
            analysts_task = self.run_analysts(
                builder, decision_id, session_id, outcome)
            scenario_task = self.scenarios.generate(
                candidate, spot, structures, decision_id, session_id,
                intel_events=builder.events,
                market_summary=builder.market)
            assessments, scenario_result = await asyncio.gather(
                analysts_task, scenario_task, return_exceptions=True)

            if isinstance(assessments, BaseException):
                outcome.assessments = []
                outcome.degraded.append(
                    f"ANALYSTS: {type(assessments).__name__}")
            else:
                outcome.assessments = assessments

            if isinstance(scenario_result, BaseException):
                outcome.degraded.append(
                    f"SCENARIOS: {type(scenario_result).__name__}")
            else:
                outcome.scenarios = scenario_result
                outcome.calls += 1
                outcome.cost_usd += scenario_result.cost_usd
                if not scenario_result.usable:
                    # A rejected scenario set is not a reason to stop. The
                    # council proceeds without payoff tables, as it did
                    # before this feature existed.
                    outcome.degraded.append(
                        f"SCENARIOS: {scenario_result.error}")
        else:
            outcome.assessments = await self.run_analysts(
                builder, decision_id, session_id, outcome)

        if len(outcome.assessments) < MIN_ANALYSTS:
            outcome.stopped_at = "ANALYSTS"
            outcome.reason = (f"only {len(outcome.assessments)} of 3 analysts "
                              f"returned valid output")
            outcome.gate_id = "COUNCIL_ANALYSTS_FAILED"
            return outcome

        # 2. proposal
        payoffs = (outcome.scenarios.evidence(spot)
                   if outcome.scenarios and outcome.scenarios.usable else None)

        proposal = await self.propose(builder, outcome.assessments,
                                      decision_id, session_id, outcome,
                                      payoffs=payoffs)
        if proposal is None:
            outcome.stopped_at = "PM_PROPOSE"
            outcome.gate_id = "COUNCIL_PM_FAILED"
            return outcome

        outcome.proposal = proposal
        if not proposal.trade:
            outcome.stopped_at = "PM_ABSTAIN"
            outcome.reason = proposal.abstain_reason or "PM abstained"
            outcome.gate_id = "PM_ABSTAIN"
            return outcome

        if proposal.direction is not candidate.direction:
            outcome.stopped_at = "PM_PROPOSE"
            outcome.reason = (f"PM direction {proposal.direction} contradicts "
                              f"the scanned direction {candidate.direction}")
            outcome.gate_id = "COUNCIL_DIRECTION_MISMATCH"
            return outcome

        # 3. structure selection
        selected = await self.select_structure(
            builder, proposal, structures, decision_id, session_id, outcome,
            payoffs=payoffs)
        if selected is None:
            outcome.stopped_at = "STRUCTURE_SELECTION"
            outcome.gate_id = outcome.gate_id or "COUNCIL_NO_SELECTION"
            return outcome
        outcome.selected_structure = selected
        outcome.original_structure = selected

        # 4. red team
        review = await self.red_team(builder, proposal, selected.rank,
                                     outcome.assessments, decision_id,
                                     session_id, outcome, payoffs=payoffs)
        if review is None:
            outcome.stopped_at = "RED_TEAM"
            outcome.gate_id = "COUNCIL_RED_TEAM_FAILED"
            return outcome
        outcome.review = review

        if review.verdict is Verdict.VETO:
            outcome.stopped_at = "RED_TEAM"
            outcome.reason = review.summary
            outcome.gate_id = "RED_TEAM_VETO"
            return outcome

        # 5. one revision, only on MODIFY
        if review.verdict is Verdict.MODIFY:
            revised = await self.revise_once(builder, proposal, review,
                                             structures, decision_id,
                                             session_id, outcome)
            if revised is None:
                outcome.stopped_at = "REVISION"
                outcome.gate_id = "COUNCIL_REVISION_FAILED"
                return outcome
            outcome.revision = revised

            if not revised.trade:
                outcome.stopped_at = "REVISION"
                outcome.reason = revised.abstain_reason or "PM abstained on revision"
                outcome.gate_id = "PM_ABSTAIN"
                return outcome

            reselected = resolve_rank(revised.selected_structure_rank,
                                      structures, outcome)
            if reselected is None:
                outcome.stopped_at = "REVISION"
                outcome.gate_id = "COUNCIL_BAD_RANK"
                return outcome
            outcome.selected_structure = reselected

        outcome.traded = True
        outcome.stopped_at = "COMPLETE"
        return outcome


# ----------------------------------------------------------------------

def resolve_rank(rank: int | None, structures: Sequence[OptionStructure],
                 outcome: CouncilOutcome) -> OptionStructure | None:
    """Map a selected rank onto a real structure.

    The PM selects by rank into a list the options engine built. Anything
    outside that list is a hallucinated contract and is refused here.
    """
    if rank is None:
        outcome.reason = "no structure rank selected"
        outcome.gate_id = "COUNCIL_NO_RANK"
        return None
    match = next((s for s in structures if s.rank == rank), None)
    if match is None:
        outcome.reason = (f"rank {rank} is outside the "
                          f"{len(structures)} supplied structures")
        outcome.gate_id = "COUNCIL_BAD_RANK"
        return None
    return match


def _review_summary(review: RedTeamReview) -> dict[str, Any]:
    return {
        "verdict": str(review.verdict),
        "risk_score": review.risk_score,
        "fatal_flaw": review.fatal_flaw,
        "confidence_adjustment": review.confidence_adjustment,
        "recommended_max_risk_pct": review.recommended_max_risk_pct,
        "strongest_counterargument": review.strongest_counterargument,
        "summary": review.summary,
        "problems": [{"category": p.category, "severity": p.severity,
                      "description": p.description}
                     for p in review.problems],
        "information_to_reverse_verdict": review.information_to_reverse_verdict,
    }


def effective_risk_pct(outcome: CouncilOutcome) -> float:
    """Risk the Risk Constitution should size against.

    The Red Team's recommendation is a ceiling, never a floor: it can only
    reduce what the PM asked for.
    """
    proposal = outcome.final_proposal
    if proposal is None or not proposal.trade:
        return 0.0
    requested = proposal.desired_portfolio_risk_pct
    if outcome.review is None:
        return requested
    return min(requested, outcome.review.recommended_max_risk_pct)
