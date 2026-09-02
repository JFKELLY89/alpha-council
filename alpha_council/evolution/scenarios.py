"""
Alpha Council v2.5 - scenario generation (Alpha Evolution Phase 1).

Produces three named paths the underlying might take — CONTINUATION,
STALL, REVERSAL — as price bands rather than point estimates. Those bands
feed the deterministic payoff engine, which computes exactly what each
candidate spread makes or loses under each path.

The point is to answer the objection every Portfolio Manager abstention
raised today: "no defensible underlying-price invalidation level." A
scenario set gives concrete price levels and a breakeven the PM can
reason against.

Three rules govern this:

  BANDS, NOT POINTS. A zero-width band is a point forecast wearing a
  disguise, and the model rejects it.

  NO NUMERIC PROBABILITIES. Asked for "62% likely" a model will produce
  one and it will mean nothing. Likelihood is a three-value enum.

  THE GENERATOR NEVER SEES THE TRADE. It is given the symbol, the price,
  the technical picture and the intelligence — not the proposed direction,
  not the structures, not the PM's thesis. A generator shown the trade
  would produce scenarios that agree with it.

Place at: alpha_council/evolution/scenarios.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from alpha_council.agents.evidence import DATA_CAVEAT, EvidencePackage
from alpha_council.agents.llm import LLMClient
from alpha_council.db.engine import Database
from alpha_council.evolution.payoffs import PayoffEngine, evidence_block
from alpha_council.models.candidate import CandidateFeatures
from alpha_council.models.enums import CandidateTrack, Direction
from alpha_council.models.intelligence import IntelligenceEvent
from alpha_council.models.scenario import (
    Likelihood,
    PayoffSummary,
    Scenario,
    ScenarioSet,
    ScenarioType,
)
from alpha_council.models.trading import OptionStructure
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import iso_utc, utc_now

# A band wider than this on a 1-15 day horizon is not a scenario, it is an
# admission that the model has no view.
MAX_BAND_WIDTH_PCT = 0.35
# Bands must move at least this far from spot to be distinguishable.
MIN_MOVE_PCT = 0.002


@dataclass(slots=True)
class ScenarioResult:
    ok: bool
    scenario_set: ScenarioSet | None = None
    summaries: list[PayoffSummary] = field(default_factory=list)
    cost_usd: float = 0.0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.ok and self.scenario_set is not None

    def evidence(self, spot: float) -> dict[str, Any] | None:
        if not self.usable:
            return None
        block = evidence_block(self.summaries, spot)
        block["scenarios"] = [{
            "type": str(s.scenario_type),
            "narrative": s.narrative,
            "band": [s.underlying_low, s.underlying_mid, s.underlying_high],
            "horizon_days": s.horizon_days,
            "likelihood": str(s.likelihood),
            "key_drivers": s.key_drivers[:3],
        } for s in self.scenario_set.scenarios]
        block["overall_uncertainty"] = str(
            self.scenario_set.overall_uncertainty)
        return block


def sanity_check(scenarios: ScenarioSet, spot: float,
                 direction: Direction) -> list[str]:
    """Reject scenario sets that are internally incoherent.

    The model can return a well-formed object that makes no sense — a
    continuation case below spot, or a reversal indistinguishable from it.
    Those would feed the payoff engine and produce confident nonsense.
    """
    problems: list[str] = []

    continuation = scenarios.by_type(ScenarioType.CONTINUATION)
    reversal = scenarios.by_type(ScenarioType.REVERSAL)
    stall = scenarios.by_type(ScenarioType.STALL)

    if continuation:
        move = (continuation.underlying_mid - spot) / spot
        if direction is Direction.BULLISH and move <= MIN_MOVE_PCT:
            problems.append(
                f"CONTINUATION mid {continuation.underlying_mid:.2f} is not "
                f"above spot {spot:.2f} for a bullish candidate")
        if direction is Direction.BEARISH and move >= -MIN_MOVE_PCT:
            problems.append(
                f"CONTINUATION mid {continuation.underlying_mid:.2f} is not "
                f"below spot {spot:.2f} for a bearish candidate")

    if reversal:
        move = (reversal.underlying_mid - spot) / spot
        if direction is Direction.BULLISH and move >= -MIN_MOVE_PCT:
            problems.append("REVERSAL is not below spot for a bullish candidate")
        if direction is Direction.BEARISH and move <= MIN_MOVE_PCT:
            problems.append("REVERSAL is not above spot for a bearish candidate")

    if continuation and reversal:
        # Directional cases that overlap describe the same outcome twice.
        if direction is Direction.BULLISH:
            if reversal.underlying_high >= continuation.underlying_low:
                problems.append(
                    "REVERSAL and CONTINUATION bands overlap; they do not "
                    "describe distinguishable outcomes")
        elif continuation.underlying_high >= reversal.underlying_low:
            problems.append("REVERSAL and CONTINUATION bands overlap")

    for scenario in scenarios.scenarios:
        width = (scenario.underlying_high - scenario.underlying_low) / spot
        if width > MAX_BAND_WIDTH_PCT:
            problems.append(
                f"{scenario.scenario_type} band spans {width:.1%} of spot, "
                "which is too wide to inform a decision")

    if stall:
        # A stall that sits far from spot is a directional case mislabelled.
        move = abs(stall.underlying_mid - spot) / spot
        if move > 0.05:
            problems.append(
                f"STALL mid is {move:.1%} from spot; that is a directional "
                "move, not a stall")

    return problems


class ScenarioGenerator:
    """One LLM call producing a validated, payoff-scored scenario set."""

    def __init__(self, client: LLMClient, payoffs: PayoffEngine,
                 db: Database, config: dict[str, Any]):
        self.client = client
        self.payoffs = payoffs
        self.db = db
        self.config = config
        self._prompt: str | None = None

    def prompt(self) -> str:
        if self._prompt is None:
            from alpha_council.settings import load_prompt

            self._prompt = load_prompt("scenario_system")
        return self._prompt

    def _package(self, candidate: CandidateFeatures, spot: float,
                 intel_events: Sequence[IntelligenceEvent],
                 market_summary: dict[str, Any] | None) -> EvidencePackage:
        """Build the generator's evidence.

        Deliberately excludes the proposed direction, the option
        structures, and any analyst output. The generator describes what
        the underlying might do; it is not asked to endorse a trade.
        """
        sections: dict[str, Any] = {
            "context": {
                "symbol": candidate.symbol,
                "spot": round(spot, 4),
                "timestamp": iso_utc(candidate.as_of),
                "track": str(candidate.track),
                "data_caveat": DATA_CAVEAT,
            },
            "technical_picture": {
                # The scanner's directional read is included because it is
                # computed from components this package does not carry. A
                # generator left to infer direction from the score
                # components alone reaches a different conclusion, and then
                # describes continuation of a move that is not happening.
                # This states which way the tape is leaning; it does not
                # say a trade is proposed, and CONTINUATION still means
                # "this persists", not "this is a good trade".
                "prevailing_direction": str(candidate.direction),
                "direction_strength": round(candidate.combined_direction, 3),
                "momentum": round(candidate.momentum_score, 1),
                "relative_volume": round(candidate.relative_volume_score, 1),
                "trend_regime": round(candidate.trend_regime_score, 1),
                "relative_strength": round(candidate.relative_strength_score, 1),
                "key_metrics": candidate.key_metrics,
            },
            "market_summary": market_summary or {},
            "intelligence_events": [{
                "type": e.event_type,
                "catalyst": round(e.catalyst_score, 1),
                "materiality": round(e.materiality_score, 1),
                "freshness": round(e.freshness_score, 1),
                "facts": e.extracted_facts[:3],
            } for e in sorted(intel_events,
                              key=lambda e: e.catalyst_score,
                              reverse=True)[:6]],
        }
        if candidate.track is CandidateTrack.MOMENTUM:
            sections["intelligence_note"] = (
                "No material catalyst was identified. Build scenarios from "
                "price behaviour and market context, not from imagined news.")

        package = EvidencePackage(symbol=candidate.symbol,
                                  as_of=candidate.as_of, role="PM",
                                  sections=sections)
        from alpha_council.agents.evidence import estimate_tokens

        package.token_estimate = estimate_tokens(package.to_json())
        return package

    async def generate(self, candidate: CandidateFeatures, spot: float,
                       structures: Sequence[OptionStructure],
                       decision_id: str, session_id: str,
                       intel_events: Sequence[IntelligenceEvent] = (),
                       market_summary: dict[str, Any] | None = None,
                       qty: int = 1) -> ScenarioResult:
        package = self._package(candidate, spot, intel_events, market_summary)

        result = await self.client.call(
            "scenario_generator", self.prompt(), package, ScenarioSet,
            decision_id=decision_id, session_id=session_id,
            estimated_cost=0.01)

        if result.failed or not isinstance(result.parsed, ScenarioSet):
            return ScenarioResult(
                ok=False, cost_usd=result.cost_usd,
                error=result.error or "no valid scenario set")

        # Identity and reference fields are OURS, not the model's. The
        # schema forces the model to emit them, but a model echoing the
        # same scenario_set_id twice would silently overwrite another
        # decision's row, and its idea of spot skews every breakeven
        # calculation downstream.
        scenarios = result.parsed.model_copy(update={
            "scenario_set_id": f"scn_{new_uuid()[:10]}",
            "decision_id": decision_id,
            "symbol": candidate.symbol,
            "spot_at_generation": spot,
            "generated_at": utc_now(),
        })
        problems = sanity_check(scenarios, spot, candidate.direction)
        if problems:
            # A malformed scenario set is worse than none: it would feed
            # the payoff engine and produce confident nonsense.
            await self.db.log_event(
                "WARN", "scenarios", "SCENARIO_SET_REJECTED",
                f"{candidate.symbol}: {problems[0]}",
                {"problems": problems, "decision_id": decision_id})
            return ScenarioResult(ok=False, cost_usd=result.cost_usd,
                                  error=problems[0], warnings=problems)

        summaries = self.payoffs.rank_structures(
            structures, scenarios, decision_id, qty)

        await self.payoffs.persist_set(scenarios)
        for summary in summaries:
            await self.payoffs.persist_payoffs(summary.payoffs)

        return ScenarioResult(ok=True, scenario_set=scenarios,
                              summaries=summaries, cost_usd=result.cost_usd)


def summarize_for_log(result: ScenarioResult, spot: float) -> dict[str, Any]:
    """Compact record of what the generator produced, for system_events."""
    if not result.usable:
        return {"ok": False, "error": result.error}

    scenarios = result.scenario_set
    losing_stalls = sum(1 for s in result.summaries if s.stall_loses_money)
    return {
        "ok": True,
        "uncertainty": str(scenarios.overall_uncertainty),
        "bands": {
            str(s.scenario_type): [round(s.underlying_low, 2),
                                   round(s.underlying_high, 2)]
            for s in scenarios.scenarios},
        "structures_scored": len(result.summaries),
        "structures_losing_on_stall": losing_stalls,
        "best_breakeven_move_pct": round(min(
            (s.breakeven_move_pct for s in result.summaries),
            key=abs, default=0.0), 5),
    }
