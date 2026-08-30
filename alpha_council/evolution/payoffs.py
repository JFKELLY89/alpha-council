"""
Alpha Council v2.5 - deterministic scenario payoff engine.

Given a spread and a set of underlying scenarios, compute what the trade
makes or loses. No model, no LLM, no market data: this is arithmetic on
strikes, width, and the debit actually paid.

TWO KINDS OF NUMBER, AND THE DIFFERENCE MATTERS

  At expiration, a vertical's value is exact. A bull call spread is worth
  clip(S - long_strike, 0, width). There is no pricing model involved and
  no uncertainty beyond the underlying price itself.

  Before expiration, value depends on implied volatility and time, and any
  figure requires an option pricing model. Everything this module produces
  for a pre-expiration horizon is an approximation, is flagged
  approximate=True, and must never feed a hard gate.

That distinction is the point. Presenting a Black-Scholes estimate beside
an exact payoff as though both were equally solid is the kind of false
precision this system avoids everywhere else.

Place at: alpha_council/evolution/payoffs.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from alpha_council.db.engine import Database
from alpha_council.models.enums import StrategyType
from alpha_council.models.scenario import (
    PayoffSummary,
    Scenario,
    ScenarioPayoff,
    ScenarioSet,
    ScenarioType,
)
from alpha_council.models.trading import OptionStructure
from alpha_council.utils.ids import new_uuid
from alpha_council.utils.time import iso_utc

CONTRACT_MULTIPLIER = 100.0


# ======================================================================
# exact expiration math
# ======================================================================

def intrinsic_value(structure: OptionStructure, underlying: float) -> float:
    """Spread value at expiration, in dollars per share of width.

    Bull call: worth the underlying's excess over the long strike, capped
    at the width. Bear put: worth the shortfall below the long strike,
    likewise capped. Both floor at zero — a defined-risk debit spread
    cannot be worth less than nothing.
    """
    long_strike = structure.long_leg.strike
    width = structure.width

    if structure.strategy is StrategyType.BULL_CALL_DEBIT:
        raw = underlying - long_strike
    else:
        raw = long_strike - underlying
    return max(0.0, min(raw, width))


def expiration_pnl(structure: OptionStructure, underlying: float,
                   entry_debit: float | None = None) -> float:
    """Exact P&L per spread at expiration, in dollars."""
    debit = entry_debit if entry_debit is not None else structure.initial_limit_debit
    return round((intrinsic_value(structure, underlying) - debit)
                 * CONTRACT_MULTIPLIER, 2)


def breakeven_move_pct(structure: OptionStructure, spot: float,
                       entry_debit: float | None = None) -> float:
    """Underlying move required to break even, as a signed fraction.

    The single most useful number for judging whether a spread expresses a
    thesis. A trade needing a 4% move on a thesis that supports 2% is a bad
    expression of a possibly-correct idea.
    """
    debit = entry_debit if entry_debit is not None else structure.initial_limit_debit
    if structure.strategy is StrategyType.BULL_CALL_DEBIT:
        breakeven = structure.long_leg.strike + debit
    else:
        breakeven = structure.long_leg.strike - debit
    return round((breakeven - spot) / spot, 6)


def max_profit_move_pct(structure: OptionStructure, spot: float) -> float:
    """Move required to reach maximum profit, as a signed fraction."""
    target = structure.short_leg.strike
    return round((target - spot) / spot, 6)


def underlying_for_pnl(structure: OptionStructure, target_pnl: float,
                       entry_debit: float | None = None) -> float | None:
    """Underlying price that produces a given per-spread P&L at expiration.

    Returns None when the target is outside the payoff range, which is
    itself informative: a defined-risk spread simply cannot reach it.
    """
    debit = entry_debit if entry_debit is not None else structure.initial_limit_debit
    required_value = target_pnl / CONTRACT_MULTIPLIER + debit
    if required_value < 0 or required_value > structure.width:
        return None
    if structure.strategy is StrategyType.BULL_CALL_DEBIT:
        return round(structure.long_leg.strike + required_value, 4)
    return round(structure.long_leg.strike - required_value, 4)


# ======================================================================
# approximate pre-expiration value
# ======================================================================

def horizon_pnl_estimate(structure: OptionStructure, underlying: float,
                         days_held: int,
                         entry_debit: float | None = None) -> float:
    """APPROXIMATE P&L before expiration. Never exact, never a gate input.

    Linear extrinsic decay: the spread's current extrinsic value is assumed
    to bleed evenly to zero over its remaining life. That is wrong in
    detail — decay accelerates and volatility moves — but it is transparent,
    monotonic, and does not pretend to a precision the free Indicative feed
    could not support anyway.

    Used only to show the PM roughly what a stalled thesis costs. Anything
    that must be right uses expiration_pnl.
    """
    debit = entry_debit if entry_debit is not None else structure.initial_limit_debit
    intrinsic = intrinsic_value(structure, underlying)
    extrinsic_now = max(0.0, debit - intrinsic_value(
        structure, structure.underlying_price or underlying))

    remaining = max(0, structure.dte - days_held)
    decay_fraction = remaining / structure.dte if structure.dte > 0 else 0.0
    estimated_value = intrinsic + extrinsic_now * decay_fraction
    estimated_value = max(0.0, min(estimated_value, structure.width))
    return round((estimated_value - debit) * CONTRACT_MULTIPLIER, 2)


# ======================================================================
# engine
# ======================================================================

class PayoffEngine:
    """Computes scenario payoffs for structures. Deterministic throughout."""

    def __init__(self, db: Database | None = None):
        self.db = db

    def payoff(self, structure: OptionStructure, scenario: Scenario,
               decision_id: str, qty: int = 1,
               entry_debit: float | None = None,
               at_expiration: bool = True) -> ScenarioPayoff:
        if at_expiration:
            values = [expiration_pnl(structure, u, entry_debit)
                      for u in (scenario.underlying_low,
                                scenario.underlying_mid,
                                scenario.underlying_high)]
        else:
            values = [horizon_pnl_estimate(structure, u,
                                           scenario.horizon_days, entry_debit)
                      for u in (scenario.underlying_low,
                                scenario.underlying_mid,
                                scenario.underlying_high)]

        return ScenarioPayoff(
            payoff_id=f"po_{new_uuid()[:10]}",
            decision_id=decision_id,
            structure_id=structure.structure_id,
            scenario_type=scenario.scenario_type,
            underlying_low=scenario.underlying_low,
            underlying_mid=scenario.underlying_mid,
            underlying_high=scenario.underlying_high,
            pnl_low=values[0], pnl_mid=values[1], pnl_high=values[2],
            at_expiration=at_expiration,
            approximate=not at_expiration,
            qty=qty,
        )

    def summarize(self, structure: OptionStructure, scenarios: ScenarioSet,
                  decision_id: str, qty: int = 1,
                  entry_debit: float | None = None) -> PayoffSummary:
        """Judge one structure across a whole scenario set."""
        payoffs = [self.payoff(structure, s, decision_id, qty, entry_debit)
                   for s in scenarios.scenarios]
        spot = scenarios.spot_at_generation

        continuation = next((p for p in payoffs
                             if p.scenario_type is ScenarioType.CONTINUATION),
                            None)
        stall = next((p for p in payoffs
                      if p.scenario_type is ScenarioType.STALL), None)
        reversal = next((p for p in payoffs
                         if p.scenario_type is ScenarioType.REVERSAL), None)

        stall_mid = stall.pnl_mid if stall else None
        return PayoffSummary(
            structure_id=structure.structure_id, rank=structure.rank,
            decision_id=decision_id, payoffs=payoffs,
            continuation_best=continuation.best if continuation else 0.0,
            stall_mid=stall_mid,
            reversal_worst=reversal.worst if reversal else 0.0,
            max_loss_per_spread=structure.max_loss_per_spread,
            breakeven_move_pct=breakeven_move_pct(structure, spot, entry_debit),
            max_profit_move_pct=max_profit_move_pct(structure, spot),
            stall_loses_money=bool(stall_mid is not None and stall_mid < 0),
        )

    def rank_structures(self, structures: Sequence[OptionStructure],
                        scenarios: ScenarioSet, decision_id: str,
                        qty: int = 1) -> list[PayoffSummary]:
        """Summarize every structure. Deliberately returns no ranking score.

        Choosing among these is the PM's judgment call, informed by the
        numbers. Collapsing them into one composite would relocate the
        decision into a weighting the operator never chose.
        """
        return [self.summarize(s, scenarios, decision_id, qty)
                for s in structures]

    # ---- persistence -------------------------------------------------

    async def persist_set(self, scenarios: ScenarioSet) -> None:
        if self.db is None:
            return
        await self.db.execute(
            "INSERT OR REPLACE INTO scenario_sets(scenario_set_id, "
            "decision_id, symbol, generated_at, overall_uncertainty, "
            "scenarios_json) VALUES(?,?,?,?,?,?)",
            (scenarios.scenario_set_id, scenarios.decision_id,
             scenarios.symbol, iso_utc(scenarios.generated_at),
             str(scenarios.overall_uncertainty),
             json.dumps([s.model_dump(mode="json")
                         for s in scenarios.scenarios])))

    async def persist_payoffs(self, payoffs: Sequence[ScenarioPayoff]) -> int:
        if self.db is None or not payoffs:
            return 0
        await self.db.executemany(
            "INSERT OR REPLACE INTO scenario_payoffs(payoff_id, decision_id, "
            "structure_id, scenario_type, underlying_low, underlying_mid, "
            "underlying_high, pnl_low, pnl_mid, pnl_high) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(p.payoff_id, p.decision_id, p.structure_id,
              str(p.scenario_type), p.underlying_low, p.underlying_mid,
              p.underlying_high, p.pnl_low, p.pnl_mid, p.pnl_high)
             for p in payoffs])
        return len(payoffs)


# ======================================================================
# presentation
# ======================================================================

def format_summary(summary: PayoffSummary, spot: float) -> str:
    """Plain-language payoff table for the evidence pack and the dashboard."""
    lines = [
        f"Structure rank {summary.rank} ({summary.structure_id})",
        f"  breakeven requires a {summary.breakeven_move_pct:+.2%} move "
        f"from {spot:.2f}",
        f"  maximum profit requires {summary.max_profit_move_pct:+.2%}",
        f"  maximum loss per spread ${summary.max_loss_per_spread:,.2f}",
    ]
    for payoff in summary.payoffs:
        label = str(payoff.scenario_type).title()
        marker = " (estimate)" if payoff.approximate else ""
        lines.append(
            f"  {label:<13} {payoff.underlying_low:.2f}-"
            f"{payoff.underlying_high:.2f} -> "
            f"${payoff.pnl_low:,.0f} to ${payoff.pnl_high:,.0f}"
            f" per spread{marker}")

    if summary.stall_loses_money:
        lines.append(
            f"  WARNING: the stall case loses ${abs(summary.stall_mid):,.0f} "
            f"per spread. Being directionally right is not sufficient here.")
    ratio = summary.upside_to_downside
    if ratio is not None:
        lines.append(f"  continuation upside to reversal downside: {ratio:.2f}x")
    return "\n".join(lines)


def evidence_block(summaries: Sequence[PayoffSummary],
                   spot: float) -> dict[str, object]:
    """Scenario payoffs formatted for an agent evidence package.

    Carries an explicit instruction not to recompute: these are
    deterministic outputs, and a model re-deriving them would introduce
    arithmetic error into numbers that currently have none.
    """
    return {
        "spot": spot,
        "note": (
            "Expiration payoffs are exact given the underlying price. "
            "Any value marked approximate is a pre-expiration estimate. "
            "Do not recompute these numbers; they are deterministic."),
        "structures": [{
            "rank": s.rank,
            "structure_id": s.structure_id,
            "breakeven_move_pct": round(s.breakeven_move_pct, 5),
            "max_profit_move_pct": round(s.max_profit_move_pct, 5),
            "max_loss_per_spread": s.max_loss_per_spread,
            "stall_loses_money": s.stall_loses_money,
            "scenarios": [{
                "type": str(p.scenario_type),
                "underlying": [p.underlying_low, p.underlying_mid,
                               p.underlying_high],
                "pnl_per_spread": [p.pnl_low, p.pnl_mid, p.pnl_high],
                "approximate": p.approximate,
            } for p in s.payoffs],
        } for s in summaries],
    }
